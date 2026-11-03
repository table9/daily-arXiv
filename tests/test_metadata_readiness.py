"""Deterministic readiness dates and pre-network rejection; entirely offline."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_schedule_catchup as catchup_tests


runner = catchup_tests.runner
CATEGORIES = catchup_tests.CATEGORIES


def clock_at(value):
    return runner.harvest_clock(datetime.fromisoformat(value))


class MetadataReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def checkpoint(self, day, complete=False):
        return catchup_tests.ScheduledCatchupTests.checkpoint(self, day, complete)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def cli(self, argv, clock, run_day=None):
        output, errors = io.StringIO(), io.StringIO()
        run_day = run_day or Mock(side_effect=AssertionError('No harvest allowed'))
        with patch.object(runner, 'REPO', self.root), \
                patch.dict('os.environ', {'CATEGORIES': ','.join(CATEGORIES), 'LANGUAGE': 'Chinese'}), \
                patch.object(sys, 'argv', ['run_dates.py', *argv]), \
                patch.object(runner, 'harvest_clock', return_value=clock) as capture, \
                patch.object(runner, 'run_day', run_day), redirect_stdout(output), redirect_stderr(errors):
            try:
                code = runner.main()
            except SystemExit as error:
                code = error.code
        self.assertEqual(capture.call_count, 1)
        return code, output.getvalue(), errors.getvalue(), run_day

    def test_clock_rejects_naive_time_and_normalizes_one_aware_instant(self):
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            runner.harvest_clock(datetime(2026, 11, 3, 2, 23))
        first = clock_at('2026-11-03T02:23:00+00:00')
        second = clock_at('2026-11-02T21:23:00-05:00')
        self.assertEqual(first, second)
        self.assertEqual(first.utc_today.isoformat(), '2026-11-03')
        self.assertEqual(first.ready_through.isoformat(), '2026-11-02')
        self.assertEqual(first.now_utc.tzinfo, timezone.utc)

    def test_winter_and_summer_midnight_boundaries(self):
        for instant, expected in [
            ('2026-11-03T04:59:59+00:00', '2026-11-02'),
            ('2026-11-03T05:00:00+00:00', '2026-11-03'),
            ('2026-07-03T03:59:59+00:00', '2026-07-02'),
            ('2026-07-03T04:00:00+00:00', '2026-07-03'),
        ]:
            with self.subTest(instant=instant):
                self.assertEqual(clock_at(instant).ready_through.isoformat(), expected)
        self.assertEqual(runner.metadata_ready_at('2026-11-03').isoformat(), '2026-11-03T05:00:00+00:00')
        self.assertEqual(runner.metadata_ready_at('2026-07-03').isoformat(), '2026-07-03T04:00:00+00:00')

    def test_spring_dst_transition_does_not_move_the_date_boundary_early(self):
        for instant in ('2026-03-08T05:00:00+00:00', '2026-03-08T06:59:59+00:00',
                        '2026-03-08T07:00:00+00:00'):
            self.assertEqual(clock_at(instant).ready_through.isoformat(), '2026-03-08')
        self.assertEqual(runner.metadata_ready_at('2026-03-08').hour, 5)
        self.assertEqual(runner.metadata_ready_at('2026-03-09').hour, 4)

    def test_fall_repeated_hour_does_not_move_the_date_backwards(self):
        for instant in ('2026-11-01T04:00:00+00:00', '2026-11-01T05:59:59+00:00',
                        '2026-11-01T06:00:00+00:00'):
            self.assertEqual(clock_at(instant).ready_through.isoformat(), '2026-11-01')
        self.assertEqual(runner.metadata_ready_at('2026-11-01').hour, 4)
        self.assertEqual(runner.metadata_ready_at('2026-11-02').hour, 5)

    def test_schedule_delayed_across_utc_day_stops_at_eastern_ready_date(self):
        self.checkpoint('2026-11-01')
        clock = clock_at('2026-11-03T02:23:00+00:00')
        self.assertEqual(runner.scheduled_dates(self.root, 'Chinese', CATEGORIES, clock=clock), ['2026-11-02'])
        code, output, _, run = self.cli(['--catch-up', '--dry-run'], clock)
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)['date'] for line in output.splitlines()], ['2026-11-02'])
        self.assertFalse((self.root / 'assets/harvest-status/2026-11-03.json').exists())
        run.assert_not_called()

    def test_valid_checkpoint_after_eastern_date_is_not_future_and_is_untouched(self):
        self.checkpoint('2026-11-03', complete=True)
        before = self.snapshot()
        clock = clock_at('2026-11-03T02:23:00+00:00')
        self.assertEqual(runner.scheduled_dates(self.root, 'Chinese', CATEGORIES, clock=clock), [])
        code, output, _, run = self.cli(['--catch-up'], clock)
        self.assertEqual((code, output), (0, ''))
        run.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / '.arxiv-work').exists())

    def test_checkpoint_after_utc_date_still_fails_closed(self):
        self.checkpoint('2026-11-04')
        with self.assertRaisesRegex(ValueError, 'Future checkpoint'):
            runner.scheduled_dates(self.root, 'Chinese', CATEGORIES,
                                   clock=clock_at('2026-11-03T02:23:00+00:00'))

    def test_seven_day_limit_counts_only_ready_candidates(self):
        self.checkpoint('2026-10-26')
        before_midnight = clock_at('2026-11-03T04:59:59+00:00')
        self.assertEqual(len(runner.scheduled_dates(self.root, 'Chinese', CATEGORIES, clock=before_midnight)), 7)
        with self.assertRaisesRegex(ValueError, 'exceeds 7 days'):
            runner.scheduled_dates(self.root, 'Chinese', CATEGORIES,
                                   clock=clock_at('2026-11-03T05:00:00+00:00'))

    def test_explicit_unready_dry_run_reports_wait_without_writing(self):
        before = self.snapshot()
        code, output, _, run = self.cli(['--date', '2026-11-03', '--dry-run'],
                                        clock_at('2026-11-03T02:23:00+00:00'))
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result['date'], '2026-11-03')
        self.assertEqual(result['status'], 'would_wait_metadata')
        self.assertEqual(result['metadata_ready_at_utc'], '2026-11-03T05:00:00+00:00')
        run.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_formal_unready_explicit_date_rejects_before_any_writes_or_calls(self):
        report = self.root / 'report.json'
        code, _, errors, run = self.cli(['--date', '2026-11-03', '--report', str(report)],
                                        clock_at('2026-11-03T02:23:00+00:00'))
        self.assertEqual(code, 2)
        self.assertIn('2026-11-03T05:00:00+00:00', errors)
        self.assertIn('No harvesting started', errors)
        run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_range_with_unready_last_day_is_preflighted_before_first_day(self):
        code, _, _, run = self.cli(['--start-date', '2026-11-02', '--end-date', '2026-11-03'],
                                   clock_at('2026-11-03T02:23:00+00:00'))
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_completed_unready_date_can_skip_without_changes(self):
        self.checkpoint('2026-11-03', complete=True)
        before = self.snapshot()
        run = Mock(wraps=runner.run_day)
        code, output, _, _ = self.cli(['--date', '2026-11-03'],
                                      clock_at('2026-11-03T02:23:00+00:00'), run)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'skip_complete')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / '.arxiv-work').exists())

    def test_unready_resume_is_rejected_directly_before_stage_creation(self):
        raw, _, _, _ = runner.paths_for(self.root, '2026-11-03', 'Chinese')
        raw.parent.mkdir()
        raw.write_text(json.dumps({'id': '2611.00001', 'title': 'Paper', 'summary': 'Abstract'}) + '\n')
        before = self.snapshot()
        run = Mock(side_effect=AssertionError('No HTTP or AI'))
        with self.assertRaisesRegex(ValueError, 'earliest readiness boundary'):
            runner.run_day(self.root, '2026-11-03', 'Chinese', CATEGORIES, self.root / 'work', run=run,
                           clock=clock_at('2026-11-03T02:23:00+00:00'))
        run.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / 'work').exists())

    def test_checkpoint_hash_validation_is_not_bypassed_by_readiness(self):
        self.checkpoint('2026-11-03', complete=True)
        raw, _, _, _ = runner.paths_for(self.root, '2026-11-03', 'Chinese')
        raw.write_text('corrupted')
        code, _, errors, run = self.cli(['--date', '2026-11-03', '--dry-run'],
                                        clock_at('2026-11-03T02:23:00+00:00'))
        self.assertEqual(code, 2)
        self.assertIn('Checkpoint mismatch', errors)
        run.assert_not_called()

    def test_main_passes_the_same_frozen_clock_into_execution(self):
        clock = clock_at('2026-11-03T05:00:00+00:00')
        run = Mock(return_value={'date': '2026-11-03', 'status': 'failed', 'error': 'offline fixture'})
        code, _, _, _ = self.cli(['--date', '2026-11-03'], clock, run)
        self.assertEqual(code, 1)
        self.assertIs(run.call_args.kwargs['clock'], clock)


if __name__ == '__main__':
    unittest.main()
