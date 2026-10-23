"""Catchup integrity checks with real Scrapy and entirely local responses."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scrapy.http import HtmlResponse, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daily_arxiv'))
from daily_arxiv.spiders.arxiv import ArxivSpider

DAY = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
CATEGORY = 'cond-mat.mtrl-sci'


def page_html(day, total, count, section='Replacement submissions'):
    heading = datetime.strptime(day, '%Y-%m-%d').strftime('%a, %d %b %Y')
    papers = ''.join(
        f'<dt><a title="Abstract" href="/abs/2610.{index:05d}"></a></dt>'
        f'<dd><span class="primary-subject">Materials Science ({CATEGORY})</span></dd>'
        for index in range(count)
    )
    return (f'<div id="dlpage"><h1>Catchup results for Materials Science on {heading}</h1>'
            f'Total of {total} entries<dl><h3>{section}</h3>{papers}</dl></div>')


class CatchupCountTests(unittest.TestCase):
    def spider(self):
        with patch.dict(os.environ, {'CATEGORIES': CATEGORY}):
            return ArxivSpider(date=DAY)

    def response(self, total, count, page=1, category=CATEGORY):
        return HtmlResponse(
            url=f'https://arxiv.org/catchup/{category}/{DAY}?abs=False&page={page}',
            body=page_html(DAY, total, count).encode(), encoding='utf-8',
        )

    def test_total_cannot_shrink_between_otherwise_valid_pages(self):
        spider = self.spider()
        first = list(spider.parse(self.response(2001, 2000)))
        self.assertEqual(len(first), 1)
        self.assertIsInstance(first[0], Request)
        # Previously both pages passed their individual dt-count checks and
        # selected_ids/item_scraped_count still agreed despite the missing item.
        second = self.response(2000, 0, page=2)
        with self.assertRaisesRegex(ValueError, 'total changed between pages'):
            list(spider.parse(second))
        self.assertNotIn(second.url, spider.completed_pages)

    def test_total_cannot_grow_between_pages(self):
        spider = self.spider()
        list(spider.parse(self.response(2001, 2000)))
        with self.assertRaisesRegex(ValueError, 'total changed between pages'):
            list(spider.parse(self.response(2002, 2, page=2)))

    def test_distinct_categories_have_independent_totals(self):
        spider = self.spider()
        self.assertEqual(list(spider.parse(self.response(0, 0))), [])
        self.assertEqual(list(spider.parse(self.response(1, 1, category='physics.comp-ph'))), [])
        self.assertEqual(set(spider.catchup_totals.values()), {0, 1})


SMOKE = r'''
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

root, mode, feed_path, stats_path = sys.argv[1:]
sys.path.insert(0, str(Path(root) / 'daily_arxiv'))
sys.path.insert(0, str(Path(root) / 'tests'))
from test_catchup_integrity import page_html, DAY, CATEGORY
from daily_arxiv.spiders.arxiv import ArxivSpider
from scrapy import Request, signals
from scrapy.crawler import CrawlerProcess
from scrapy.http import HtmlResponse

os.environ['CATEGORIES'] = CATEGORY

class LocalResponses:
    def process_request(self, request, spider):
        if request.url.startswith('data:'):
            return None  # Exercise the real built-in data URI downloader.
        if not request.url.startswith('https://arxiv.org/catchup/'):
            raise RuntimeError('Test forbids network: ' + request.url)
        page = int(parse_qs(urlsplit(request.url).query)['page'][0])
        if mode == 'metadata_error':
            body = page_html(DAY, 1, 1, 'New submissions')
        elif mode == 'empty':
            body = page_html(DAY, 0, 0)
        elif page == 1:
            body = page_html(DAY, 2001, 2000)
        else:
            body = page_html(DAY, 2000, 0) if mode == 'count_changed' else page_html(DAY, 2001, 1)
        return HtmlResponse(request.url, body=body.encode(), encoding='utf-8', request=request)

class FixturePipeline:
    def process_item(self, item, spider):
        if mode == 'metadata_error':
            raise RuntimeError('fixture metadata lookup failed')
        return item

class FixtureSpider(ArxivSpider):
    def start_requests(self):
        yield Request('data:text/plain,local-catchup-fixture', callback=self.begin, dont_filter=True)
    def begin(self, response):
        # A list avoids Scrapy inspecting source for a generator callback
        # defined via python -c, where inspect.getsource is unavailable.
        return [Request(url, callback=self.parse, dont_filter=True) for url in self.start_urls]

settings = {
    'ROBOTSTXT_OBEY': False, 'TELNETCONSOLE_ENABLED': False, 'LOG_LEVEL': 'ERROR',
    'TWISTED_REACTOR': 'twisted.internet.asyncioreactor.AsyncioSelectorReactor',
    'DOWNLOADER_MIDDLEWARES': {'__main__.LocalResponses': 1},
    'ITEM_PIPELINES': {'__main__.FixturePipeline': 300},
    'FEEDS': {Path(feed_path).as_uri(): {'format': 'jsonlines'}},
    'FEED_STORE_EMPTY': True,
}
process = CrawlerProcess(settings)
crawler = process.create_crawler(FixtureSpider)
errors = []
def failed(failure, **kwargs):
    errors.append(str(failure.value))
crawler.signals.connect(failed, signal=signals.spider_error, weak=False)
crawler.signals.connect(failed, signal=signals.item_error, weak=False)
process.crawl(crawler, date=DAY, stats_file=stats_path)
process.start()
print(json.dumps({'errors': errors, 'stats': json.loads(Path(stats_path).read_text())}))
'''


class CatchupScrapySmokeTests(unittest.TestCase):
    def run_fixture(self, mode):
        with tempfile.TemporaryDirectory() as temporary:
            feed = Path(temporary) / 'feed.jsonl'
            stats = Path(temporary) / 'stats.json'
            result = subprocess.run(
                [sys.executable, '-c', SMOKE, str(ROOT), mode, str(feed), str(stats)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(stats.exists(), result.stdout + result.stderr)
            self.assertTrue(feed.exists(), result.stdout + result.stderr)
            return json.loads(result.stdout.strip().splitlines()[-1]), feed.read_text(encoding='utf-8')

    def test_changed_count_leaves_missing_page_and_signals_callback_failure(self):
        report, feed = self.run_fixture('count_changed')
        self.assertEqual(report['stats']['close_reason'], 'finished')
        self.assertEqual(len(report['stats']['missing_pages']), 1)
        self.assertTrue(any('total changed between pages' in error for error in report['errors']))

    def test_stable_pages_finish_and_export_without_following_the_next_day(self):
        report, feed = self.run_fixture('stable')
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['stats']['missing_pages'], [])
        self.assertEqual(report['stats']['response_received_count'], 3)  # data URI + 2 pages
        self.assertEqual(report['stats']['expected_items'], 0)  # replacements excluded
        self.assertEqual(feed, '')

    def test_pipeline_failure_is_visible_after_all_pages_complete(self):
        report, feed = self.run_fixture('metadata_error')
        self.assertEqual(report['stats']['missing_pages'], [])
        self.assertEqual(report['stats']['expected_items'], 1)
        self.assertEqual(report['stats'].get('item_scraped_count', 0), 0)
        self.assertTrue(any('fixture metadata lookup failed' in error for error in report['errors']))

    def test_no_announcement_is_a_valid_completed_empty_page(self):
        report, feed = self.run_fixture('empty')
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['stats']['missing_pages'], [])
        self.assertEqual(report['stats']['expected_items'], 0)
        self.assertEqual(feed, '')


if __name__ == '__main__':
    unittest.main()
