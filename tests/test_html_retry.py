"""Offline HTTP retry behavior using real Scrapy requests, responses and stats."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scrapy import Request, Spider
from scrapy.http import HtmlResponse
from scrapy.exceptions import IgnoreRequest
from scrapy.settings import Settings
from scrapy.statscollectors import MemoryStatsCollector
from twisted.internet.error import DNSLookupError, TimeoutError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daily_arxiv'))
from daily_arxiv import middlewares, settings as project_settings


class HtmlRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.errors = Path(self.tmp.name) / 'errors.jsonl'
        env = patch.dict(os.environ, {'ARXIV_HTTP_DIAGNOSTICS_FILE': str(self.errors)})
        env.start()
        self.addCleanup(env.stop)
        settings = Settings({'RETRY_TIMES': project_settings.RETRY_TIMES})
        crawler = SimpleNamespace(settings=settings)
        crawler.stats = MemoryStatsCollector(crawler)
        self.spider = Spider('fixture')
        self.spider.crawler = crawler
        self.stats = crawler.stats
        self.middleware = middlewares.ArxivRetryMiddleware(settings)
        sleeper = patch.object(middlewares.asyncio, 'sleep', new_callable=AsyncMock)
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.request = Request('https://arxiv.org/catchup/cond-mat/2026-10-15?abs=False&page=1')

    def response(self, request, status, headers=None):
        return HtmlResponse(request.url, request=request, status=status,
                            headers=headers or {}, body=b'Rate exceeded.' if status != 200 else b'Complete page')

    async def receive(self, request, status, headers=None):
        return await self.middleware.process_response(
            request, self.response(request, status, headers), self.spider)

    async def test_transient_responses_recover_with_nonblocking_backoff(self):
        first = await self.receive(self.request, 429)
        second = await self.receive(first, 503, {'Retry-After': '0'})
        success = self.response(second, 200)
        result = await self.middleware.process_response(second, success, self.spider)
        self.assertIs(result, success)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [60, 120])
        self.assertEqual(second.meta['retry_times'], 2)
        self.assertTrue(second.dont_filter)
        self.assertEqual(self.stats.get_value('retry/count'), 2)
        records = [json.loads(line) for line in self.errors.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([record['event'] for record in records], ['arxiv_html_http_error'] * 2)
        self.assertEqual([record['status'] for record in records], [429, 503])
        self.assertEqual(records[0]['body'], 'Rate exceeded.')

    async def test_five_attempts_exhaust_and_preserve_failure_response(self):
        request = self.request
        for _ in range(4):
            request = await self.receive(request, 429)
            self.assertIsInstance(request, Request)
        response = self.response(request, 429)
        with self.assertLogs('scrapy.downloadermiddlewares.retry', level='ERROR'):
            result = await self.middleware.process_response(request, response, self.spider)
        self.assertIs(result, response)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [60, 120, 240, 480])
        self.assertEqual(self.stats.get_value('retry/count'), 4)
        self.assertEqual(self.stats.get_value('retry/max_reached'), 1)
        self.assertEqual(len(self.errors.read_text(encoding='utf-8').splitlines()), 5)

    async def test_permanent_errors_do_not_retry_or_sleep(self):
        for status in (403, 404, 406):
            with self.subTest(status=status):
                response = self.response(self.request, status)
                self.assertIs(await self.middleware.process_response(
                    self.request, response, self.spider), response)
        self.sleep.assert_not_awaited()
        self.assertIsNone(self.stats.get_value('retry/count'))

    async def test_retry_after_seconds_and_http_date_are_respected(self):
        await self.receive(self.request, 429, {'Retry-After': '300'})
        with patch.object(middlewares, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 24, tzinfo=timezone.utc)
            await self.receive(self.request, 503, {'Retry-After': 'Sat, 24 Oct 2026 00:05:00 GMT'})
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [300, 300])

    async def test_long_retry_after_fails_without_retrying_early(self):
        response = self.response(self.request, 429, {'Retry-After': '3600'})
        with self.assertLogs('scrapy.downloadermiddlewares.retry', level='ERROR'):
            result = await self.middleware.process_response(self.request, response, self.spider)
        self.assertIs(result, response)
        self.sleep.assert_not_awaited()
        self.assertIsNone(self.stats.get_value('retry/count'))
        self.assertEqual(self.stats.get_value('retry/max_reached'), 1)

    async def test_invalid_and_past_retry_after_use_backoff(self):
        for value in ('invalid', '-1', 'Thu, 01 Jan 1970 00:00:00 GMT', 'NaN'):
            with self.subTest(value=value):
                await self.receive(self.request, 503, {'Retry-After': value})
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [60] * 4)

    async def test_dont_retry_remains_respected(self):
        request = self.request.replace(meta={'dont_retry': True})
        response = self.response(request, 429, {'Retry-After': '3600'})
        self.assertIs(await self.middleware.process_response(request, response, self.spider), response)
        self.sleep.assert_not_awaited()
        self.assertIsNone(self.stats.get_value('retry/max_reached'))

    async def test_network_exception_retains_retry_logic_and_cools_down(self):
        result = await self.middleware.process_exception(self.request, TimeoutError(), self.spider)
        self.assertIsInstance(result, Request)
        self.assertEqual(result.meta['retry_times'], 1)
        self.assertTrue(result.dont_filter)
        self.assertEqual(self.stats.get_value('retry/count'), 1)
        self.sleep.assert_awaited_once_with(60)

    async def test_five_network_attempts_keep_retry_limit_and_error_counters(self):
        request = self.request
        for _ in range(4):
            request = await self.middleware.process_exception(request, TimeoutError(), self.spider)
            self.assertIsInstance(request, Request)
        with self.assertLogs('scrapy.downloadermiddlewares.retry', level='ERROR'):
            result = await self.middleware.process_exception(request, TimeoutError(), self.spider)
        self.assertIsNone(result)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [60, 120, 240, 480])
        self.assertEqual(self.stats.get_value('retry/count'), 4)
        self.assertEqual(self.stats.get_value('retry/max_reached'), 1)
        self.assertEqual(self.stats.get_value(
            'retry/reason_count/twisted.internet.error.TimeoutError'), 4)

    async def test_mixed_http_and_network_errors_share_one_retry_budget(self):
        first = await self.receive(self.request, 429)
        second = await self.middleware.process_exception(first, TimeoutError(), self.spider)
        third = await self.receive(second, 503)
        fourth = await self.middleware.process_exception(third, DNSLookupError(), self.spider)
        with self.assertLogs('scrapy.downloadermiddlewares.retry', level='ERROR'):
            self.assertIsNone(await self.middleware.process_exception(
                fourth, TimeoutError(), self.spider))
        self.assertEqual(fourth.meta['retry_times'], 4)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [60, 120, 240, 480])
        self.assertEqual(self.stats.get_value('retry/count'), 4)
        self.assertEqual(self.stats.get_value('retry/max_reached'), 1)

    async def test_exception_dont_retry_and_unhandled_errors_do_not_sleep(self):
        disabled = self.request.replace(meta={'dont_retry': True})
        self.assertIsNone(await self.middleware.process_exception(
            disabled, TimeoutError(), self.spider))
        for error in (ValueError('fixture'), IgnoreRequest(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(await self.middleware.process_exception(
                    self.request, error, self.spider))
        self.sleep.assert_not_awaited()
        self.assertIsNone(self.stats.get_value('retry/count'))
        self.assertIsNone(self.stats.get_value('retry/max_reached'))

    async def test_exception_cooldown_keeps_event_loop_responsive(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def cooldown(delay):
            entered.set()
            await release.wait()

        self.sleep.side_effect = cooldown
        retry = asyncio.create_task(self.middleware.process_exception(
            self.request, TimeoutError(), self.spider))
        await entered.wait()
        asyncio.get_running_loop().call_soon(release.set)
        self.assertIsInstance(await retry, Request)
        self.sleep.assert_awaited_once_with(60)

    async def test_cancellation_during_network_cooldown_propagates(self):
        entered = asyncio.Event()

        async def cooldown(delay):
            entered.set()
            await asyncio.Event().wait()

        self.sleep.side_effect = cooldown
        retry = asyncio.create_task(self.middleware.process_exception(
            self.request, TimeoutError(), self.spider))
        await entered.wait()
        retry.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await retry
        self.sleep.assert_awaited_once_with(60)

    async def test_diagnostics_failure_does_not_replace_retry(self):
        with patch.object(middlewares, 'log_arxiv_http_error', side_effect=OSError('fixture write failure')):
            with self.assertLogs(middlewares.logger, level='ERROR'):
                result = await self.receive(self.request, 429)
        self.assertIsInstance(result, Request)
        self.sleep.assert_awaited_once_with(60)

    async def test_project_replaces_builtin_retry_middleware(self):
        configured = project_settings.DOWNLOADER_MIDDLEWARES
        self.assertIsNone(configured['scrapy.downloadermiddlewares.retry.RetryMiddleware'])
        self.assertEqual(configured['daily_arxiv.middlewares.ArxivRetryMiddleware'], 550)
        self.assertEqual(project_settings.RETRY_TIMES, 4)

    async def test_cooldown_allows_other_event_loop_callbacks(self):
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def cooldown(delay):
            waiting.set()
            await release.wait()

        self.sleep.side_effect = cooldown
        retry = asyncio.create_task(self.receive(self.request, 429))
        await waiting.wait()
        asyncio.get_running_loop().call_soon(release.set)
        self.assertIsInstance(await retry, Request)
        self.sleep.assert_awaited_once_with(60)


if __name__ == '__main__':
    unittest.main()
