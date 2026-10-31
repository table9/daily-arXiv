# Define here the models for your spider middleware
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/spider-middleware.html

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
from types import SimpleNamespace

from scrapy import Request, signals
from scrapy.downloadermiddlewares.retry import RetryMiddleware, get_retry_request

from daily_arxiv.pipelines import log_arxiv_http_error


logger = logging.getLogger(__name__)


class ArxivRetryMiddleware(RetryMiddleware):
    """Cool down transient download failures without blocking the Scrapy reactor."""

    def _retry_delay(self, request, response):
        delay = 60 * 2 ** request.meta.get('retry_times', 0)
        value = response.headers.get('Retry-After')
        if value:
            value = value.decode('ascii', errors='replace')
            try:
                retry_after = float(value)
            except ValueError:
                try:
                    retry_after = (parsedate_to_datetime(value)
                                   - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    retry_after = 0
            if math.isfinite(retry_after):
                delay = max(delay, retry_after)
            elif retry_after > 0:
                delay = math.inf
        return delay

    def _record_failure(self, response):
        if response.status < 400:
            return
        try:
            record = SimpleNamespace(
                status_code=response.status, url=response.url,
                headers=response.headers.to_unicode_dict(),
                text=getattr(response, 'text', None) or response.body.decode('utf-8', errors='replace'),
            )
            log_arxiv_http_error(record, event='arxiv_html_http_error')
        except Exception:
            # Failure to record diagnostics cannot replace the original error.
            logger.exception('Could not record arXiv HTML HTTP diagnostics')

    async def process_exception(self, request, exception, spider):
        # Retain Scrapy's retry eligibility, counters, and exhausted-error path.
        # Network failures have no response/Retry-After, but still need the same
        # nonblocking cooldown as transient HTTP responses before rescheduling.
        result = super().process_exception(request, exception, spider)
        if isinstance(result, Request):
            delay = 60 * 2 ** request.meta.get('retry_times', 0)
            logger.warning('Retrying arXiv HTML %s after %s in %ss (attempt %s/5)',
                           request.url, type(exception).__name__, delay,
                           result.meta['retry_times'] + 1)
            await asyncio.sleep(delay)
        return result

    async def process_response(self, request, response, spider):
        self._record_failure(response)
        retryable = (not request.meta.get('dont_retry', False)
                     and response.status in self.retry_http_codes)
        delayed = retryable and (response.status in {408, 429} or response.status >= 500)
        if delayed:
            max_retries = request.meta.get('max_retry_times', self.max_retry_times)
            if request.meta.get('retry_times', 0) >= max_retries:
                return super().process_response(request, response, spider)
            delay = self._retry_delay(request, response)
            if delay > 900:
                # Use Scrapy's normal exhausted-retry error/statistics path.
                get_retry_request(
                    request, spider=spider, max_retry_times=0,
                    reason=f'HTTP {response.status}; server cooldown exceeds 900 seconds',
                )
                return response
        result = super().process_response(request, response, spider)
        if delayed and isinstance(result, Request):
            logger.warning('Retrying arXiv HTML %s in %ss (attempt %s/5)',
                           request.url, delay, result.meta['retry_times'] + 1)
            await asyncio.sleep(delay)
        return result

# useful for handling different item types with a single interface


class DailyArxivSpiderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the spider middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_spider_input(self, response, spider):
        # Called for each response that goes through the spider
        # middleware and into the spider.

        # Should return None or raise an exception.
        return None

    def process_spider_output(self, response, result, spider):
        # Called with the results returned from the Spider, after
        # it has processed the response.

        # Must return an iterable of Request, or item objects.
        for i in result:
            yield i

    def process_spider_exception(self, response, exception, spider):
        # Called when a spider or process_spider_input() method
        # (from other spider middleware) raises an exception.

        # Should return either None or an iterable of Request or item objects.
        pass

    def process_start_requests(self, start_requests, spider):
        # Called with the start requests of the spider, and works
        # similarly to the process_spider_output() method, except
        # that it doesn’t have a response associated.

        # Must return only requests (not items).
        for r in start_requests:
            yield r

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)


class DailyArxivDownloaderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the downloader middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request, spider):
        # Called for each request that goes through the downloader
        # middleware.

        # Must either:
        # - return None: continue processing this request
        # - or return a Response object
        # - or return a Request object
        # - or raise IgnoreRequest: process_exception() methods of
        #   installed downloader middleware will be called
        return None

    def process_response(self, request, response, spider):
        # Called with the response returned from the downloader.

        # Must either;
        # - return a Response object
        # - return a Request object
        # - or raise IgnoreRequest
        return response

    def process_exception(self, request, exception, spider):
        # Called when a download handler or a process_request()
        # (from other downloader middleware) raises an exception.

        # Must either:
        # - return None: continue processing this exception
        # - return a Response object: stops process_exception() chain
        # - return a Request object: stops process_exception() chain
        pass

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)
