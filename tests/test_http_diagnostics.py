import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "daily_arxiv/daily_arxiv/pipelines.py"

class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "errors.jsonl"
        self.env = patch.dict(os.environ, {"ARXIV_HTTP_DIAGNOSTICS_FILE": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        session = SimpleNamespace(hooks={"response": []})
        fake_arxiv = SimpleNamespace(Client=lambda *a, **kw: SimpleNamespace(_session=session))
        with patch.dict(sys.modules, {"arxiv": fake_arxiv}):
            spec = importlib.util.spec_from_file_location("pipeline_under_test", MODULE_PATH)
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)

    def response(self, status, body="", headers=None):
        return SimpleNamespace(status_code=status, text=body,
                               headers=headers or {}, url="https://export.arxiv.org/api/query?id_list=2609.25075")

    def test_hook_registered_on_real_request_path(self):
        pipeline = self.module.DailyArxivPipeline()
        self.assertIn(self.module.log_arxiv_http_error, pipeline.client._session.hooks["response"])

    def test_empty_406_and_full_429_body_append_separately(self):
        body = "temporarily unavailable\n" + "中" * 5000
        with self.assertLogs(self.module.logger, level=logging.WARNING) as logs:
            first = self.response(406, headers={"X-Cloud-Trace-Context": "trace-406"})
            second = self.response(429, body, {"Retry-After": "120", "X-Request-Id": "req-429",
                                             "Set-Cookie": "must-not-save", "Authorization": "must-not-save"})
            self.assertIs(self.module.log_arxiv_http_error(first), first)
            self.assertIs(self.module.log_arxiv_http_error(second), second)
        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 2)
        self.assertTrue(records[0]["body_empty"])
        self.assertIsNone(records[0]["retry_after"])
        self.assertEqual(records[0]["response_headers"]["x-cloud-trace-context"], "trace-406")
        self.assertEqual(records[1]["body"], body)
        self.assertEqual(records[1]["retry_after"], "120")
        self.assertEqual(records[1]["response_headers"]["x-request-id"], "req-429")
        self.assertNotIn("must-not-save", self.path.read_text(encoding="utf-8"))
        self.assertIn('"body_truncated": true', logs.output[-1])

    def test_success_creates_no_diagnostic_file(self):
        response = self.response(200, "valid feed")
        self.assertIs(self.module.log_arxiv_http_error(response), response)
        self.assertFalse(self.path.exists())

    def test_write_failure_does_not_replace_http_response(self):
        self.path.mkdir()
        response = self.response(406)
        with self.assertLogs(self.module.logger, level=logging.WARNING):
            self.assertIs(self.module.log_arxiv_http_error(response), response)

class MetadataRetryTests(unittest.TestCase):
    FEED = b'''<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <opensearch:totalResults>1</opensearch:totalResults>
      <entry><id>http://arxiv.org/abs/2610.14048v1</id>
        <updated>2026-10-13T12:00:00Z</updated><published>2026-10-13T12:00:00Z</published>
        <title>Complete metadata</title><summary>Original abstract.</summary>
        <author><name>Example Author</name></author>
        <arxiv:comment>5 pages</arxiv:comment>
        <arxiv:primary_category term="cond-mat.mes-hall"/>
        <category term="cond-mat.mes-hall"/>
        <link href="https://arxiv.org/abs/2610.14048v1" rel="alternate"/>
      </entry>
    </feed>'''

    def setUp(self):
        # Exercise the installed arxiv.Client and requests response hooks offline.
        import arxiv
        import requests
        from requests.adapters import BaseAdapter

        self.arxiv = arxiv
        self.requests = requests
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'ARXIV_HTTP_DIAGNOSTICS_FILE': str(Path(self.tmp.name) / 'errors.jsonl')})
        self.env.start()
        self.addCleanup(self.env.stop)
        spec = importlib.util.spec_from_file_location('pipeline_retry_test', MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.pipeline = self.module.DailyArxivPipeline()
        self.assertEqual(self.pipeline.client.num_retries, 0)
        self.assertEqual(self.pipeline.client.delay_seconds, 3)
        self.pipeline.client.delay_seconds = 0
        self.sleeper = patch.object(self.module.time, 'sleep')
        self.sleep = self.sleeper.start()
        self.addCleanup(self.sleeper.stop)
        self.send = Mock()

        class Adapter(BaseAdapter):
            def send(adapter, request, **kwargs):
                result = self.send(request)
                if isinstance(result, Exception):
                    raise result
                status, headers = result
                response = requests.Response()
                response.status_code = status
                response.headers.update(headers)
                response.url = request.url
                response.request = request
                response._content = self.FEED if status == 200 else b'Rate exceeded.'
                return response

            def close(adapter):
                pass

        self.pipeline.client._session.mount('https://', Adapter())

    def test_transient_http_recovers_all_metadata_and_caches_duplicate(self):
        self.send.side_effect = [(429, {}), (503, {'Retry-After': '0'}), (200, {})]
        with self.assertLogs(self.module.logger, level='WARNING'):
            item = self.pipeline.enrich_item({'id': '2610.14048'})
        self.assertEqual(self.sleep.call_args_list, [unittest.mock.call(30), unittest.mock.call(60)])
        self.assertEqual(item['title'], 'Complete metadata')
        self.assertEqual(item['authors'], ['Example Author'])
        self.assertEqual(item['summary'], 'Original abstract.')
        self.assertEqual(item['comment'], '5 pages')
        self.assertEqual(item['categories'], ['cond-mat.mes-hall'])
        self.assertEqual(item['abs'], 'https://arxiv.org/abs/2610.14048')
        self.assertEqual(item['pdf'], 'https://arxiv.org/pdf/2610.14048')
        duplicate = self.pipeline.enrich_item({'id': '2610.14048'})
        self.assertEqual(item, duplicate)
        self.assertEqual(self.send.call_count, 3)
        records = [json.loads(line) for line in (Path(self.tmp.name) / 'errors.jsonl').read_text().splitlines()]
        self.assertEqual([r['status'] for r in records], [429, 503])

    def test_exhausted_transient_error_still_fails_without_cache(self):
        self.send.side_effect = [(503, {})] * 5
        with self.assertLogs(self.module.logger, level='WARNING'):
            with self.assertRaises(self.arxiv.HTTPError):
                self.pipeline.fetch_paper('2610.14048')
        self.assertEqual(self.send.call_count, 5)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [30, 60, 120, 240])
        self.assertFalse(self.pipeline._papers)

    def test_permanent_errors_do_not_retry(self):
        for status in (403, 404, 406):
            with self.subTest(status=status):
                self.send.reset_mock(side_effect=True)
                self.send.side_effect = [(status, {})]
                with self.assertLogs(self.module.logger, level='WARNING'):
                    with self.assertRaises(self.arxiv.HTTPError):
                        self.pipeline.fetch_paper('2610.14048')
                self.assertEqual(self.send.call_count, 1)
                self.sleep.assert_not_called()

    def test_network_timeout_then_success(self):
        self.send.side_effect = [self.requests.exceptions.Timeout('timed out'), (200, {})]
        with self.assertLogs(self.module.logger, level='WARNING'):
            self.pipeline.fetch_paper('2610.14048')
        self.sleep.assert_called_once_with(30)

    def test_retry_after_seconds_are_respected(self):
        self.send.side_effect = [(429, {'Retry-After': '120'}), (200, {})]
        with self.assertLogs(self.module.logger, level='WARNING'):
            self.pipeline.fetch_paper('2610.14048')
        self.sleep.assert_called_once_with(120)

    def test_retry_after_http_date_is_respected(self):
        from datetime import datetime, timezone
        with patch.object(self.module, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 24, tzinfo=timezone.utc)
            self.send.side_effect = [(503, {'Retry-After': 'Sat, 24 Oct 2026 00:02:00 GMT'}), (200, {})]
            with self.assertLogs(self.module.logger, level='WARNING'):
                self.pipeline.fetch_paper('2610.14048')
        self.sleep.assert_called_once_with(120)

    def test_long_server_deadline_fails_instead_of_retrying_early(self):
        self.send.side_effect = [(429, {'Retry-After': '3600'})]
        with self.assertLogs(self.module.logger, level='WARNING'):
            with self.assertRaises(self.arxiv.HTTPError):
                self.pipeline.fetch_paper('2610.14048')
        self.assertEqual(self.send.call_count, 1)
        self.sleep.assert_not_called()

    def test_invalid_retry_after_uses_backoff(self):
        self.send.side_effect = [(503, {'Retry-After': 'invalid'}), (200, {})]
        with self.assertLogs(self.module.logger, level='WARNING'):
            self.pipeline.fetch_paper('2610.14048')
        self.sleep.assert_called_once_with(30)


if __name__ == "__main__":
    unittest.main()
