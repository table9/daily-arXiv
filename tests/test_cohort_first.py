"""A complete announcement cohort must precede every metadata request."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_backfill import AI, ROOT, DAY as RECENT_DAY, cohort_items, cohort_stats, runner, write_jsonl


DAY = '2026-11-04'
CATEGORIES = {'cond-mat', 'cond-mat.str-el', 'cond-mat.mes-hall', 'cond-mat.mtrl-sci',
              'cond-mat.supr-con', 'cond-mat.stat-mech', 'physics.chem-ph', 'physics.comp-ph'}
CLOCK = runner.harvest_clock(datetime(2026, 11, 5, 6, tzinfo=timezone.utc))
PAPERS = [{'id': '2611.00001', 'categories': ['cond-mat.mtrl-sci'], 'title': 'First',
           'summary': 'First abstract', 'authors': ['A'], 'comment': None,
           'pdf': 'https://arxiv.org/pdf/2611.00001', 'abs': 'https://arxiv.org/abs/2611.00001'},
          {'id': '2611.00002', 'categories': ['physics.comp-ph'], 'title': 'Second',
           'summary': 'Second abstract', 'authors': ['B'], 'comment': '4 pages',
           'pdf': 'https://arxiv.org/pdf/2611.00002', 'abs': 'https://arxiv.org/abs/2611.00002'}]


class CohortFirstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'work').mkdir()
        self.items = cohort_items(PAPERS)
        self.stats = cohort_stats(DAY, self.items, CATEGORIES)
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        if 'scrapy' in command:
            self.assertIn('ITEM_PIPELINES={}', command)
            write_jsonl(Path(command[-1]), self.items)
            stats_path = Path(next(arg.split('=', 1)[1] for arg in command if arg.startswith('stats_file=')))
            stats_path.write_text(json.dumps(self.stats), encoding='utf-8')
        elif 'enhance.py' in command:
            raw = Path(command[command.index('--data') + 1])
            write_jsonl(raw.with_name(raw.stem + '_AI_enhanced_Chinese.jsonl'),
                        [dict(item, AI=AI) for item in runner.read_records(raw)])
        else:
            output = Path(command[command.index('--out') + 1])
            output.mkdir()
            (output / f'{DAY}.md').write_text('# Complete fixture\n', encoding='utf-8')

    def execute(self):
        return runner.run_day(self.root, DAY, 'Chinese', CATEGORIES, self.root / 'work',
                              run=self.fake_run, clock=CLOCK)

    def assert_unpublished(self):
        self.assertTrue(all(not path.exists() for path in runner.paths_for(self.root, DAY, 'Chinese')))

    def test_missing_two_html_categories_prevents_all_metadata_ai_and_publication(self):
        self.items = [{'id': f'2611.{number:05}', 'categories': ['cond-mat.mtrl-sci']}
                      for number in range(1, 93)]
        self.stats = cohort_stats(DAY, self.items, CATEGORIES)
        self.stats['missing_pages'] = [page for page in self.stats['expected_pages']
                                      if '/cond-mat.mes-hall/' in page or '/cond-mat.str-el/' in page]
        self.stats['completed_pages'] = [page for page in self.stats['expected_pages']
                                        if page not in self.stats['missing_pages']]
        self.stats['log_count/ERROR'] = 2
        self.stats['downloader/response_status_count/429'] = 12
        with patch.object(runner, 'enrich_cohort') as metadata:
            with self.assertRaisesRegex(ValueError, 'Incomplete crawl'):
                self.execute()
        metadata.assert_not_called()
        self.assertEqual(len(self.calls), 1)
        self.assert_unpublished()

    def test_positive_page_inventory_and_original_error_count_gates_are_required(self):
        mutations = [lambda s: s.update(categories=sorted(CATEGORIES - {'cond-mat'})),
                     lambda s: s.update(expected_pages=s['expected_pages'][1:]),
                     lambda s: s.update(completed_pages=s['completed_pages'][1:]),
                     lambda s: s.update(expected_items=3),
                     lambda s: s.update(item_scraped_count=1),
                     lambda s: s.update({'log_count/ERROR': 1}),
                     lambda s: s.update({'spider_exceptions/count': 1}),
                     lambda s: s.update(close_reason='cancelled')]
        for mutate in mutations:
            stats = deepcopy(self.stats)
            mutate(stats)
            with self.subTest(stats=stats), self.assertRaisesRegex(ValueError, 'Incomplete crawl'):
                runner.validate_cohort(stats, self.items, DAY, CATEGORIES, 'fixture-stats')

    def test_invalid_or_duplicate_ids_fail_before_metadata(self):
        for items in ([self.items[0], self.items[0]], [{'id': '2611.00001', 'categories': []}]):
            self.items = items
            self.stats = cohort_stats(DAY, self.items, CATEGORIES)
            with self.subTest(items=items), patch.object(runner, 'enrich_cohort') as metadata:
                with self.assertRaises(ValueError):
                    self.execute()
                metadata.assert_not_called()
                self.assert_unpublished()

    def test_valid_cohort_enrichment_preserves_original_metadata_one_for_one(self):
        from daily_arxiv import pipelines
        pipeline = pipelines.DailyArxivPipeline()
        by_id = {item['id']: SimpleNamespace(
            authors=[SimpleNamespace(name=name) for name in item['authors']],
            title=item['title'], categories=item['categories'], comment=item['comment'], summary=item['summary'])
                 for item in PAPERS}
        before = deepcopy(self.items)
        with patch.object(pipelines, 'DailyArxivPipeline', return_value=pipeline), \
                patch.object(pipeline, 'fetch_paper', side_effect=lambda paper_id: by_id[paper_id]) as fetch, \
                patch.object(runner.time, 'sleep') as cooldown:
            enriched = runner.enrich_cohort(self.items)
        self.assertEqual(enriched, PAPERS)
        self.assertEqual(self.items, before)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [item['id'] for item in PAPERS])
        cooldown.assert_called_once_with(3)

    def test_empty_cohort_has_no_pipeline_or_cooldown(self):
        from daily_arxiv import pipelines
        with patch.object(pipelines, 'DailyArxivPipeline') as pipeline, \
                patch.object(runner.time, 'sleep') as cooldown:
            self.assertEqual(runner.enrich_cohort([]), [])
        pipeline.assert_not_called()
        cooldown.assert_not_called()

    def test_metadata_failure_stops_the_date_before_later_ids_ai_or_publication(self):
        from daily_arxiv import pipelines
        pipeline = pipelines.DailyArxivPipeline()
        with patch.object(pipelines, 'DailyArxivPipeline', return_value=pipeline), \
                patch.object(pipeline, 'fetch_paper', side_effect=RuntimeError('metadata unavailable')) as fetch, \
                patch.object(runner.time, 'sleep') as cooldown:
            with self.assertRaisesRegex(RuntimeError, 'metadata unavailable'):
                self.execute()
        fetch.assert_called_once_with(PAPERS[0]['id'])
        cooldown.assert_called_once_with(3)
        self.assertEqual(len(self.calls), 1)
        self.assert_unpublished()

    def test_metadata_id_mismatch_fails_before_ai_or_publication(self):
        with patch.object(runner, 'enrich_cohort', return_value=[PAPERS[0]]):
            with self.assertRaisesRegex(ValueError, 'cohort/metadata IDs differ'):
                self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assert_unpublished()

    def test_complete_cohort_publishes_original_raw_and_retains_default_pipeline(self):
        from daily_arxiv import settings
        with patch.object(runner, 'enrich_cohort', return_value=deepcopy(PAPERS)):
            self.assertEqual(self.execute()['status'], 'complete')
        raw = runner.paths_for(self.root, DAY, 'Chinese')[0]
        self.assertEqual(runner.read_records(raw), PAPERS)
        self.assertEqual(settings.ITEM_PIPELINES['daily_arxiv.pipelines.DailyArxivPipeline'], 300)


HTML_ONLY_SMOKE = r'''
import json,os,sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
root,feed,stats,day=sys.argv[1:]
sys.path.insert(0,str(Path(root)/'daily_arxiv'))
os.environ['SCRAPY_SETTINGS_MODULE']='daily_arxiv.settings'
os.environ['CATEGORIES']='cond-mat.mes-hall,cond-mat.str-el'
from scrapy.http import HtmlResponse
from daily_arxiv.pipelines import DailyArxivPipeline
def forbidden(cls,crawler):
    raise AssertionError('Metadata pipeline was instantiated during HTML-only crawl')
DailyArxivPipeline.from_crawler=classmethod(forbidden)
class LocalResponses:
    def process_request(self,request,spider):
        category=urlsplit(request.url).path.split('/')[2]
        heading=datetime.strptime(day,'%Y-%m-%d').strftime('%a, %d %b %Y')
        body=('<div id="dlpage"><h1>Catchup results on '+heading+'</h1>Total of 1 entries'
              '<dl><h3>New submissions</h3><dt><a title="Abstract" href="/abs/2611.00001"></a></dt>'
              '<dd><span class="primary-subject">Physics ('+category+')</span></dd></dl></div>')
        return HtmlResponse(request.url,body=body.encode(),encoding='utf-8',request=request)
from scrapy.cmdline import execute
execute(['scrapy','crawl','arxiv','-a','date='+day,'-a','stats_file='+stats,
         '-s','ITEM_PIPELINES={}','-s','ROBOTSTXT_OBEY=False','-s','TELNETCONSOLE_ENABLED=False',
         '-s','LOG_LEVEL=ERROR','-s','DOWNLOADER_MIDDLEWARES={"__main__.LocalResponses":1}',
         '-O',feed])
'''


class HtmlOnlyScrapySmokeTests(unittest.TestCase):
    def test_real_scrapy_cli_override_exports_ids_without_constructing_metadata_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            feed, stats = Path(directory) / 'cohort.jsonl', Path(directory) / 'stats.json'
            result = subprocess.run([sys.executable, '-c', HTML_ONLY_SMOKE, str(ROOT), str(feed), str(stats), RECENT_DAY],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            items = runner.read_cohort(feed)
            self.assertEqual(len(items), 1)
            self.assertEqual(set(items[0]), {'id', 'categories'})
            status = json.loads(stats.read_text(encoding='utf-8'))
            runner.validate_cohort(status, items, RECENT_DAY, {'cond-mat.mes-hall', 'cond-mat.str-el'}, stats)
            self.assertEqual(len(status['completed_pages']), 2)


if __name__ == '__main__':
    unittest.main()
