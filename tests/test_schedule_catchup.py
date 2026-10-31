"""Offline evidence that the daily workflow resumes missing dates safely."""
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('catchup_runner', ROOT / 'scripts/run_dates.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
CATEGORIES = {'cond-mat.mtrl-sci'}
TODAY = datetime.now(timezone.utc).date()


class ScheduledCatchupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def checkpoint(self, day, complete=False):
        raw, enhanced, markdown, marker = runner.paths_for(self.root, day, 'Chinese')
        files = {}
        if complete:
            paper = {'id': '2610.12345', 'title': 'Paper', 'summary': 'Abstract'}
            ai = dict(paper, AI=dict.fromkeys(('tldr', 'motivation', 'method', 'result', 'conclusion'), 'Valid'))
            for path, value in ((raw, json.dumps(paper) + '\n'),
                                (enhanced, json.dumps(ai) + '\n'), (markdown, '# Paper\n')):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding='utf-8')
                files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({'date': day, 'status': 'complete' if complete else 'no_new_content',
            'date_basis': 'arxiv_catchup_announcement_date', 'categories': sorted(CATEGORIES),
            'papers': int(complete), 'crawled': int(complete), 'files': files}), encoding='utf-8')
        return marker

    def plan(self, today=date(2026, 10, 31)):
        return runner.scheduled_dates(self.root, 'Chinese', CATEGORIES, today=today)

    def test_failed_friday_is_selected_before_the_next_daily_date(self):
        self.checkpoint('2026-10-29')
        self.assertEqual(self.plan(), ['2026-10-30', '2026-10-31'])
        self.checkpoint('2026-10-30', complete=True)
        self.assertEqual(self.plan(), ['2026-10-31'])

    def test_already_completed_today_skips_without_any_subprocess_or_changes(self):
        self.checkpoint('2026-10-31', complete=True)
        self.assertEqual(self.plan(), ['2026-10-31'])
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        run = Mock(side_effect=AssertionError('No network or AI allowed'))
        self.assertEqual(runner.run_day(self.root, '2026-10-31', 'Chinese', CATEGORIES,
                                       self.root/'work', run=run)['status'], 'skip_complete')
        run.assert_not_called()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_empty_checkpoint_after_dedup_is_a_valid_anchor(self):
        path = self.checkpoint('2026-10-30')
        content = json.loads(path.read_text())
        content['crawled'] = 3  # All candidates were already present in the previous week.
        path.write_text(json.dumps(content))
        self.assertEqual(self.plan(), ['2026-10-31'])

    def test_no_anchor_and_more_than_seven_days_require_explicit_review(self):
        with self.assertRaisesRegex(ValueError, 'No harvest checkpoints'):
            self.plan()
        self.checkpoint('2026-10-23')
        with self.assertRaisesRegex(ValueError, 'exceeds 7 days'):
            self.plan()

    def test_internal_history_gap_and_future_checkpoint_are_not_silently_skipped(self):
        self.checkpoint('2026-10-28')
        self.checkpoint('2026-10-30')
        with self.assertRaisesRegex(ValueError, 'internal gap'):
            self.plan()
        self.checkpoint('2026-11-01')
        with self.assertRaisesRegex(ValueError, 'Future checkpoint'):
            self.plan()

    def test_modified_or_wrong_configuration_anchor_fails_closed(self):
        self.checkpoint('2026-10-30', complete=True)
        raw, enhanced, _, _ = runner.paths_for(self.root, '2026-10-30', 'Chinese')
        enhanced.write_text('corrupted', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Checkpoint mismatch'):
            self.plan()
        with self.assertRaisesRegex(ValueError, 'Category configuration changed'):
            runner.scheduled_dates(self.root, 'Chinese', {'physics.comp-ph'}, today=date(2026, 10, 31))

    def test_wrong_checkpoint_date_or_state_never_advances_the_window(self):
        marker = self.checkpoint('2026-10-30')
        content = json.loads(marker.read_text())
        for field, bad in (('date', '2026-10-29'), ('status', 'failed'), ('date_basis', 'submittedDate')):
            with self.subTest(field=field):
                marker.write_text(json.dumps(dict(content, **{field: bad})))
                with self.assertRaisesRegex(ValueError, 'Invalid latest checkpoint'):
                    self.plan()

    def test_missing_or_malformed_sha_values_never_authorize_catchup(self):
        marker = self.checkpoint('2026-10-30', complete=True)
        original = json.loads(marker.read_text())
        for value in ('', None, False, 'not-a-sha256'):
            with self.subTest(value=value):
                content = dict(original, files=dict.fromkeys(original['files'], value))
                marker.write_text(json.dumps(content))
                with self.assertRaisesRegex(ValueError, 'Incomplete latest checkpoint'):
                    self.plan()

    def test_non_object_checkpoint_or_files_are_rejected_without_mutation(self):
        marker = self.checkpoint('2026-10-30')
        original = json.loads(marker.read_text())
        for content in ([], dict(original, files=[]), dict(original, papers=False)):
            with self.subTest(content=content):
                marker.write_text(json.dumps(content))
                before = marker.read_bytes()
                with self.assertRaises(ValueError):
                    self.plan()
                self.assertEqual(marker.read_bytes(), before)

    def cli(self, argv):
        return (patch.object(runner, 'REPO', self.root),
                patch.dict('os.environ', {'CATEGORIES': ','.join(CATEGORIES), 'LANGUAGE': 'Chinese'}),
                patch.object(sys, 'argv', ['run_dates.py', *argv]))

    def test_cli_catchup_dry_run_plans_the_gap_without_network_or_result_writes(self):
        anchor = (TODAY - timedelta(days=2)).isoformat()
        self.checkpoint(anchor)
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        output = io.StringIO()
        repo, env, argv = self.cli(['--catch-up', '--dry-run'])
        with repo, env, argv, patch.object(runner, 'run_day') as run, redirect_stdout(output):
            self.assertEqual(runner.main(), 0)
            run.assert_not_called()
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([x['date'] for x in rows], [(TODAY-timedelta(days=1)).isoformat(), TODAY.isoformat()])
        self.assertTrue(all(x['status'] == 'would_crawl' for x in rows))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_explicit_dates_cannot_be_mixed_with_automatic_catchup(self):
        repo, env, argv = self.cli(['--catch-up', '--date', TODAY.isoformat(), '--dry-run'])
        with repo, env, argv, patch.object(runner, 'run_day') as run, redirect_stdout(io.StringIO()), \
                patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            runner.main()
        run.assert_not_called()

    def test_first_failed_catchup_day_stops_later_days_without_advancing_anchor(self):
        anchor = (TODAY-timedelta(days=2)).isoformat()
        self.checkpoint(anchor)
        report = self.root/'run-report.json'
        repo, env, argv = self.cli(['--catch-up', '--report', str(report)])
        with repo, env, argv, patch.object(runner, 'run_day', side_effect=RuntimeError('source unavailable')) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 1)
            self.assertEqual(run.call_count, 1)
        self.assertEqual([x['status'] for x in json.loads(report.read_text())], ['failed', 'not_attempted'])
        self.assertEqual(self.plan(today=TODAY), [(TODAY-timedelta(days=1)).isoformat(), TODAY.isoformat()])


if __name__ == '__main__':
    unittest.main()
