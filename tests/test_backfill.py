"""Offline regression tests: no arXiv requests, AI billing, or repository writes."""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scrapy.http import HtmlResponse, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daily_arxiv'))
from daily_arxiv.check_stats import perform_deduplication
from daily_arxiv.spiders.arxiv import ArxivSpider

spec = importlib.util.spec_from_file_location('run_dates', ROOT / 'scripts/run_dates.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

TODAY = datetime.now(timezone.utc).date()
DAY = (TODAY - timedelta(days=2)).isoformat()
CATEGORIES = {'cond-mat.mtrl-sci'}
PAPER = {'id': '2610.12345', 'title': 'Example paper', 'summary': 'Original abstract',
         'authors': ['A'], 'categories': ['cond-mat.mtrl-sci']}
AI = dict.fromkeys(('tldr', 'motivation', 'method', 'result', 'conclusion'), 'Valid summary')


def write_jsonl(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(item) + '\n' for item in items), encoding='utf-8')


class DateTests(unittest.TestCase):
    def test_range_is_inclusive_and_chronological(self):
        days = runner.selected_dates(start='2026-10-14', end='2026-10-21', today=date(2026, 10, 22))
        self.assertEqual(len(days), 8)
        self.assertEqual(days[0], '2026-10-14')
        self.assertEqual(days[-1], '2026-10-21')

    def test_default_and_single_day(self):
        self.assertEqual(runner.selected_dates(today=date(2026, 10, 22)), ['2026-10-22'])
        self.assertEqual(runner.selected_dates(start='2026-10-14', today=date(2026, 10, 22)), ['2026-10-14'])

    def test_invalid_inputs_fail_before_processing(self):
        cases = [dict(single='2026-10-14', start='2026-10-15'), dict(end='2026-10-21'),
                 dict(single='20261014'), dict(single='2026-02-30'), dict(single='2026-10-23'),
                 dict(start='2026-10-21', end='2026-10-14'), dict(single='2026-01-01')]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                runner.selected_dates(today=date(2026, 10, 22), **case)


class DedupTests(unittest.TestCase):
    def test_target_date_history_and_within_day_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = dict(PAPER, id='old')
            write_jsonl(root / '2026-10-14.jsonl', [old])
            write_jsonl(root / '2026-10-15.jsonl', [old, PAPER, PAPER])
            write_jsonl(root / '2026-10-16.jsonl', [PAPER])  # Future files must not suppress recovery.
            self.assertEqual(perform_deduplication('2026-10-15', root), 'has_new_content')
            self.assertEqual(runner.read_records(root / '2026-10-15.jsonl'), [PAPER])

    def test_corrupt_history_is_failure_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_jsonl(root / '2026-10-15.jsonl', [PAPER])
            before = (root / '2026-10-15.jsonl').read_bytes()
            (root / '2026-10-14.jsonl').write_text('broken JSON')
            self.assertEqual(perform_deduplication('2026-10-15', root), 'error')
            self.assertEqual((root / '2026-10-15.jsonl').read_bytes(), before)


class SpiderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'CATEGORIES': 'cond-mat.mtrl-sci,physics.comp-ph'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.spider = ArxivSpider(date=DAY)

    def response(self, sections, total, page=1, heading=None):
        heading = heading or datetime.strptime(DAY, '%Y-%m-%d').strftime('%a, %d %b %Y')
        body = f'<div id="dlpage"><h1>Catchup results for Material Science on {heading}</h1>Total of {total} entries'
        for section, items in sections:
            body += '<dl><h3>' + section + '</h3>'
            for paper_id, primary in items:
                body += (f'<dt><a title="Abstract" href="/abs/{paper_id}"></a></dt>'
                         f'<dd><span class="primary-subject">Material Science ({primary})</span></dd>')
            body += '</dl>'
        body += '</div>'
        url = f'https://arxiv.org/catchup/cond-mat.mtrl-sci/{DAY}?abs=False&page={page}'
        return HtmlResponse(url=url, body=body.encode(), encoding='utf-8')

    def test_uses_dated_source_and_keeps_primary_filter_excluding_replacements(self):
        self.assertTrue(all('/catchup/' in url and DAY in url for url in self.spider.start_urls))
        response = self.response([
            ('New submissions', [('new', 'cond-mat.mtrl-sci'), ('other', 'cs.CV')]),
            ('Cross submissions', [('cross', 'physics.comp-ph'), ('new', 'cond-mat.mtrl-sci')]),
            ('Replacement submissions', [('replaced', 'cond-mat.mtrl-sci')])], 5)
        self.assertEqual([item['id'] for item in self.spider.parse(response)], ['new', 'cross'])

    def test_zero_entries_is_valid_but_wrong_date_or_truncation_fails(self):
        self.assertEqual(list(self.spider.parse(self.response([], 0))), [])
        with self.assertRaises(ValueError):
            list(self.spider.parse(self.response([], 1)))
        with self.assertRaises(ValueError):
            list(self.spider.parse(self.response([], 0, heading='Wrong date')))

    def test_pagination_does_not_follow_next_day(self):
        papers = [('id' + str(i), 'cond-mat.mtrl-sci') for i in range(2000)]
        output = list(self.spider.parse(self.response([('New submissions', papers)], 2001)))
        requests = [item for item in output if isinstance(item, Request)]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url, self.spider.start_urls[0].replace('page=1', 'page=2'))
        self.assertIn(requests[0].url, self.spider.expected_pages)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'work').mkdir()
        self.items = [PAPER]
        self.invalid_ai = False
        self.crawl_error = False
        self.calls = []

    def fake_run(self, command, cwd, check):
        self.calls.append(command)
        if 'scrapy' in command:
            raw = Path(command[command.index('-O') + 1])
            write_jsonl(raw, self.items)
            stats = Path(next(arg.split('=', 1)[1] for arg in command if arg.startswith('stats_file=')))
            stats.write_text(json.dumps({'date': DAY, 'close_reason': 'finished', 'missing_pages': [],
                'item_scraped_count': len(self.items), 'expected_items': len(self.items),
                'log_count/ERROR': int(self.crawl_error)}))
        elif 'enhance.py' in command:
            raw = Path(command[command.index('--data') + 1])
            data = deepcopy(runner.read_records(raw))
            for item in data:
                item['AI'] = AI if not self.invalid_ai else dict(AI, result='Error')
            write_jsonl(raw.with_name(raw.stem + '_AI_enhanced_Chinese.jsonl'), data)
        else:
            # Exercise the real Markdown CLI against the staged data, without network or AI.
            subprocess.run(command, cwd=ROOT / 'to_md', check=check, capture_output=True)

    def execute(self):
        return runner.run_day(self.root, DAY, 'Chinese', CATEGORIES, self.root / 'work', run=self.fake_run)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for folder in ('data', 'md', 'assets')
                for p in (self.root / folder).rglob('*') if p.is_file()}

    def test_first_run_and_repeat_are_byte_identical(self):
        self.assertEqual(self.execute()['status'], 'complete')
        snapshot, calls = self.snapshot(), len(self.calls)
        self.assertEqual(self.execute()['status'], 'skip_complete')
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(len(self.calls), calls)
        self.assertIn('Example paper', (self.root / 'md' / f'{DAY}.md').read_text())

    def test_existing_raw_resumes_without_crawl_or_raw_rewrite(self):
        raw = self.root / 'data' / f'{DAY}.jsonl'
        write_jsonl(raw, [PAPER])
        before = raw.read_bytes()
        result = self.execute()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['date_basis'], 'existing_raw_archive_date')
        self.assertEqual(raw.read_bytes(), before)
        self.assertFalse(any('scrapy' in command for command in self.calls))

    def test_existing_ai_is_reused_to_regenerate_only_missing_markdown(self):
        raw, enhanced, _, _ = runner.paths_for(self.root, DAY, 'Chinese')
        write_jsonl(raw, [PAPER])
        write_jsonl(enhanced, [dict(PAPER, AI=AI)])
        before = enhanced.read_bytes()
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(enhanced.read_bytes(), before)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('convert.py', self.calls[0])

    def test_ai_failure_never_publishes_new_results_or_changes_existing_raw(self):
        raw = self.root / 'data' / f'{DAY}.jsonl'
        write_jsonl(raw, [PAPER])
        before = self.snapshot()
        self.invalid_ai = True
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.snapshot(), before)

    def test_scrapy_success_exit_with_error_stats_is_a_failure(self):
        self.crawl_error = True
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.snapshot(), {})

    def test_empty_announcement_checkpoint_is_idempotent(self):
        self.items = []
        self.assertEqual(self.execute()['status'], 'no_new_content')
        snapshot, calls = self.snapshot(), len(self.calls)
        self.assertEqual(self.execute()['status'], 'skip_no_new_content')
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(len(self.calls), calls)
        self.assertFalse((self.root / 'data' / f'{DAY}.jsonl').exists())

    def test_modified_results_and_bad_existing_ai_are_preserved(self):
        self.execute()
        raw, enhanced, _, _ = runner.paths_for(self.root, DAY, 'Chinese')
        enhanced.write_text('broken JSON')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.snapshot(), before)

    def test_atomic_install_refuses_to_replace_existing_content(self):
        target = self.root / 'result'
        runner.write_once(target, b'original')
        runner.write_once(target, b'original')
        with self.assertRaises(ValueError):
            runner.write_once(target, b'replacement')
        self.assertEqual(target.read_bytes(), b'original')

    def test_cli_dry_run_has_no_network_ai_or_result_writes(self):
        before = {str(path) for path in self.root.rglob('*')}
        output = io.StringIO()
        with patch.object(runner, 'REPO', self.root), patch.object(runner, 'run_day') as run_day, \
             patch.dict('os.environ', {'CATEGORIES': 'cond-mat.mtrl-sci', 'LANGUAGE': 'Chinese'}), \
             patch.object(sys, 'argv', ['run_dates.py', '--date', DAY, '--dry-run']), redirect_stdout(output):
            self.assertEqual(runner.main(), 0)
            run_day.assert_not_called()
        self.assertEqual({str(path) for path in self.root.rglob('*')}, before)
        self.assertEqual(json.loads(output.getvalue())['status'], 'would_crawl')

    def test_cli_range_failure_reports_unattempted_dates_and_returns_failure(self):
        (self.root / 'assets').mkdir()
        days = [(TODAY - timedelta(days=i)).isoformat() for i in (3, 2, 1)]
        report = self.root / 'report.json'
        with patch.object(runner, 'REPO', self.root), \
             patch.object(runner, 'run_day', side_effect=[{'date': days[0], 'status': 'complete'}, RuntimeError('source unavailable')]) as run_day, \
             patch.dict('os.environ', {'CATEGORIES': 'cond-mat.mtrl-sci', 'LANGUAGE': 'Chinese'}), \
             patch.object(sys, 'argv', ['run_dates.py', '--start-date', days[0], '--end-date', days[-1], '--report', str(report)]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 1)
            self.assertEqual([call.args[1] for call in run_day.call_args_list], days[:2])
        self.assertEqual([item['status'] for item in json.loads(report.read_text())],
                         ['complete', 'failed', 'not_attempted'])


if __name__ == '__main__':
    unittest.main()
