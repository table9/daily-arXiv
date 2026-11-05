"""Empty API results, using the real arxiv.Client and an offline HTTP adapter."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import test_http_diagnostics as existing_tests


ROOT = Path(__file__).resolve().parents[1]


class EmptyMetadataTests(unittest.TestCase):
    FEED = existing_tests.MetadataRetryTests.FEED
    VALID_FEED = FEED
    EMPTY_FEED = b'''<?xml version="1.0" encoding="UTF-8"?>
      <feed xmlns="http://www.w3.org/2005/Atom"
            xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
        <opensearch:totalResults>0</opensearch:totalResults>
      </feed>'''

    def setUp(self):
        existing_tests.MetadataRetryTests.setUp(self)
        blocker = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def responses(self, *outcomes):
        outcomes = iter(outcomes)

        def respond(request):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                return outcome
            self.FEED = self.EMPTY_FEED if outcome == 'empty' else self.VALID_FEED
            return (200 if isinstance(outcome, str) else outcome, {})

        self.send.side_effect = respond

    def assert_delays(self, expected):
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], expected)

    def test_empty_then_success_returns_real_metadata_and_caches(self):
        self.responses('empty', 'valid')
        with self.assertLogs(self.module.logger, level='WARNING'):
            item = self.pipeline.enrich_item({'id': '2610.14048'})
        self.assertEqual(item['title'], 'Complete metadata')
        self.assertEqual(item['summary'], 'Original abstract.')
        self.assert_delays([30])
        self.assertIsNone(self.pipeline._metadata_unavailable)
        self.pipeline.fetch_paper('2610.14048')
        self.assertEqual(self.send.call_count, 2)

    def test_persistent_empty_exhausts_once_and_stops_other_ids_without_caching(self):
        self.responses(*(['empty'] * 5))
        with self.assertLogs(self.module.logger, level='WARNING'):
            with self.assertRaisesRegex(self.module.MetadataUnavailable, 'no result'):
                self.pipeline.fetch_paper('2611.00001')
        self.assertEqual(self.send.call_count, 5)
        self.assert_delays([30, 60, 120, 240])
        self.assertFalse(self.pipeline._papers)
        for paper_id in ('2611.00001', '2611.00002', '2611.00003'):
            with self.assertRaisesRegex(self.module.MetadataUnavailable, 'remains pending'):
                self.pipeline.fetch_paper(paper_id)
        self.assertEqual(self.send.call_count, 5)

    def test_http_empty_and_timeout_share_five_attempts_and_preserve_last_error(self):
        final = self.requests.exceptions.Timeout('last request timed out')
        self.responses(503, 'empty', self.requests.exceptions.Timeout('timeout'), 'empty', final)
        with self.assertLogs(self.module.logger, level='WARNING'):
            with self.assertRaises(self.requests.exceptions.Timeout) as caught:
                self.pipeline.fetch_paper('2611.00001')
        self.assertIs(caught.exception, final)
        self.assertEqual(self.send.call_count, 5)
        self.assert_delays([30, 60, 120, 240])
        with self.assertRaises(self.module.MetadataUnavailable):
            self.pipeline.fetch_paper('2611.00002')
        self.assertEqual(self.send.call_count, 5)

    def test_mixed_budget_can_recover_on_fifth_attempt_without_stopping_next_query(self):
        self.responses('empty', 503, 'empty', self.requests.exceptions.Timeout('timeout'), 'valid', 'valid')
        with self.assertLogs(self.module.logger, level='WARNING'):
            self.pipeline.fetch_paper('2610.14048')
        self.assertEqual(self.send.call_count, 5)
        self.assertIsNone(self.pipeline._metadata_unavailable)
        self.pipeline.fetch_paper('2610.14049')
        self.assertEqual(self.send.call_count, 6)
        self.assert_delays([30, 60, 120, 240])

    def test_successful_cache_is_still_available_after_stop(self):
        self.responses('valid', *(['empty'] * 5))
        cached = self.pipeline.fetch_paper('2610.14048')
        with self.assertLogs(self.module.logger, level='WARNING'):
            with self.assertRaises(self.module.MetadataUnavailable):
                self.pipeline.fetch_paper('2611.00001')
        self.assertIs(self.pipeline.fetch_paper('2610.14048'), cached)
        self.assertEqual(self.send.call_count, 6)

    def test_new_date_pipeline_does_not_inherit_stop(self):
        self.responses(*(['empty'] * 5))
        with self.assertLogs(self.module.logger, level='WARNING'):
            with self.assertRaises(self.module.MetadataUnavailable):
                self.pipeline.fetch_paper('2611.00001')
        fresh = self.module.DailyArxivPipeline()
        self.assertIsNone(fresh._metadata_unavailable)
        fresh.client = self.pipeline.client  # Reuse the offline adapter only.
        self.responses('valid')
        self.assertEqual(fresh.fetch_paper('2611.00001').title, 'Complete metadata')
        self.assertEqual(self.send.call_count, 6)

    def test_pure_http_exhaustion_keeps_existing_behavior(self):
        self.responses(*([503] * 5), 'valid')
        with self.assertLogs(self.module.logger, level='WARNING'):
            with self.assertRaises(self.arxiv.HTTPError):
                self.pipeline.fetch_paper('2610.14048')
        self.assertIsNone(self.pipeline._metadata_unavailable)
        self.pipeline.fetch_paper('2610.14049')
        self.assertEqual(self.send.call_count, 6)

    def test_unavailable_metadata_still_fails_date_before_ai_or_publication(self):
        spec = importlib.util.spec_from_file_location('empty_metadata_runner', ROOT / 'scripts/run_dates.py')
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        root = Path(self.tmp.name) / 'date-run'
        root.mkdir()
        work = root / 'work'
        work.mkdir()
        commands = []
        self.responses(*(['empty'] * 5))

        def crawl(command, **kwargs):
            commands.append(command)
            self.assertIn('scrapy', command)
            from test_backfill import cohort_stats
            cohort = [{'id': paper_id, 'categories': ['cond-mat.mes-hall']}
                      for paper_id in ('2611.00001', '2611.00002')]
            Path(command[-1]).write_text(''.join(json.dumps(item) + '\n' for item in cohort), encoding='utf-8')
            stats_path = Path(next(arg.split('=', 1)[1] for arg in command if arg.startswith('stats_file=')))
            stats_path.write_text(json.dumps(cohort_stats('2026-11-03', cohort, {'cond-mat.mes-hall'})),
                                  encoding='utf-8')

        with self.assertLogs(self.module.logger, level='WARNING'), \
                patch.object(runner, 'enrich_cohort', side_effect=lambda cohort:
                             [self.pipeline.enrich_item(dict(item)) for item in cohort]):
            with self.assertRaises(self.module.MetadataUnavailable):
                runner.run_day(root, '2026-11-03', 'Chinese', {'cond-mat.mes-hall'}, work, run=crawl,
                               clock=runner.harvest_clock(datetime(2026, 11, 3, 12, tzinfo=timezone.utc)))
        self.assertEqual(len(commands), 1)  # No AI or Markdown subprocess.
        self.assertEqual(self.send.call_count, 5)
        self.assertTrue(all(not path.exists() for path in
                            runner.paths_for(root, '2026-11-03', 'Chinese')))


if __name__ == '__main__':
    unittest.main()
