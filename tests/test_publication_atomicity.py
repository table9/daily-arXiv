"""Failure-injection coverage for publishing a complete announcement date."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_backfill import AI, CATEGORIES, DAY, PAPER, ROOT, runner, write_jsonl


class PublicationAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.publication-test-', dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'work').mkdir()
        self.missing_markdown = False
        self.conflicting_markdown = False
        self.calls = []

    def fake_run(self, command, cwd, check):
        self.calls.append(command)
        if 'scrapy' in command:
            raw = Path(command[command.index('-O') + 1])
            write_jsonl(raw, [PAPER])
            stats = Path(next(arg.split('=', 1)[1] for arg in command if arg.startswith('stats_file=')))
            stats.write_text(json.dumps({'date': DAY, 'close_reason': 'finished', 'missing_pages': [],
                                         'item_scraped_count': 1, 'expected_items': 1}), encoding='utf-8')
        elif 'enhance.py' in command:
            raw = Path(command[command.index('--data') + 1])
            items = deepcopy(runner.read_records(raw))
            for item in items:
                item['AI'] = dict(AI)
            write_jsonl(raw.with_name(raw.stem + '_AI_enhanced_Chinese.jsonl'), items)
        elif not self.missing_markdown:
            # Real Markdown generation, with all crawling and paid AI calls stubbed.
            env = dict(os.environ, PYTHONIOENCODING='utf-8')
            subprocess.run(command, cwd=ROOT / 'to_md', check=check, capture_output=True, env=env)
            if self.conflicting_markdown:
                markdown = self.root / 'md' / f'{DAY}.md'
                markdown.parent.mkdir(parents=True, exist_ok=True)
                markdown.write_bytes(b'Concurrent reviewed result\n')

    def execute(self):
        return runner.run_day(self.root, DAY, 'Chinese', CATEGORIES, self.root / 'work', run=self.fake_run)

    def snapshot(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in ('data', 'md', 'assets')
                for path in (self.root / folder).rglob('*') if path.is_file()}

    def test_missing_markdown_output_fails_before_any_publication(self):
        self.missing_markdown = True
        with self.assertRaises((OSError, ValueError)):
            self.execute()
        self.assertEqual(self.snapshot(), {})

    def test_second_link_failure_rolls_back_new_raw_and_all_other_new_files(self):
        original_link = os.link
        count = 0

        def fail_second(source, target, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('injected second publication link failure')
            return original_link(source, target, *args, **kwargs)

        with patch.object(runner.os, 'link', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'injected second publication link failure'):
                self.execute()
        self.assertEqual(count, 2)
        self.assertEqual(self.snapshot(), {})

    def test_second_link_failure_preserves_preexisting_raw_bytes(self):
        raw = self.root / 'data' / f'{DAY}.jsonl'
        write_jsonl(raw, [PAPER])
        before = self.snapshot()
        original_link = os.link
        count = 0

        def fail_second(source, target, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('injected resume publication failure')
            return original_link(source, target, *args, **kwargs)

        with patch.object(runner.os, 'link', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'injected resume publication failure'):
                self.execute()
        self.assertEqual(self.snapshot(), before)

    def test_marker_write_failure_rolls_back_raw_ai_and_markdown(self):
        marker = runner.paths_for(self.root, DAY, 'Chinese')[3]
        original_link = os.link
        installed_before_marker = []

        def fail_marker(source, target, *args, **kwargs):
            if Path(target) == marker:
                raise OSError('injected checkpoint publication failure')
            result = original_link(source, target, *args, **kwargs)
            installed_before_marker.append(Path(target))
            return result

        with patch.object(runner.os, 'link', side_effect=fail_marker):
            with self.assertRaisesRegex(OSError, 'injected checkpoint publication failure'):
                self.execute()
        self.assertEqual(len(installed_before_marker), 3)
        self.assertEqual(self.snapshot(), {})

    def test_concurrent_conflicting_result_is_preserved_without_partial_new_files(self):
        self.conflicting_markdown = True
        with self.assertRaises((OSError, ValueError)):
            self.execute()
        markdown = self.root / 'md' / f'{DAY}.md'
        self.assertEqual(markdown.read_bytes(), b'Concurrent reviewed result\n')
        self.assertEqual(set(self.snapshot()), {str(markdown.relative_to(self.root))})

    def test_complete_success_then_repeat_preserves_every_file_hash(self):
        self.assertEqual(self.execute()['status'], 'complete')
        first = self.snapshot()
        self.assertEqual(len(first), 4)
        call_count = len(self.calls)
        self.assertEqual(self.execute()['status'], 'skip_complete')
        self.assertEqual(self.snapshot(), first)
        self.assertEqual(len(self.calls), call_count)


if __name__ == '__main__':
    unittest.main()
