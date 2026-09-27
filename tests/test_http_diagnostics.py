import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
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
        fake_arxiv = SimpleNamespace(Client=lambda *a: SimpleNamespace(_session=session))
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

if __name__ == "__main__":
    unittest.main()
