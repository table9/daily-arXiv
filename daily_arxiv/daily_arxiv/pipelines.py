# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
import arxiv
import json
import logging
import tempfile
from pathlib import Path
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from requests.exceptions import ConnectionError, Timeout
from twisted.internet.defer import DeferredSemaphore
from twisted.internet.threads import deferToThread


logger = logging.getLogger(__name__)
_DIAGNOSTIC_HEADERS = {
    "retry-after", "date", "content-type", "content-length", "server", "via",
    "x-request-id", "request-id", "x-correlation-id", "correlation-id",
    "x-cloud-trace-context", "traceparent", "x-amzn-trace-id", "x-amz-cf-id",
    "cf-ray", "x-served-by", "x-cache", "x-cache-hits", "x-timer",
}


def log_arxiv_http_error(response, *args, event='arxiv_api_http_error', **kwargs):
    """Capture each failed attempt before arxiv.py discards the response."""
    if response.status_code < 400:
        return response
    try:
        headers = {key.lower(): value for key, value in response.headers.items()
                   if key.lower() in _DIAGNOSTIC_HEADERS}
        body = response.text
        record = {
            "event": event,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "status": response.status_code,
            "url": response.url,
            "retry_after": headers.get("retry-after"),
            "response_headers": headers,
            "body": body,
            "body_empty": not body,
        }
        preview = dict(record, body=body[:4096], body_truncated=len(body) > 4096)
        logger.warning("arXiv HTTP diagnostics: %s",
                       json.dumps(preview, ensure_ascii=False))
        path = Path(os.environ.get(
            "ARXIV_HTTP_DIAGNOSTICS_FILE",
            str(Path(tempfile.gettempdir()) / "arxiv-http-errors.jsonl"),
        ))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Diagnostics must not replace the original HTTP failure or retry logic.
        logger.exception("Could not persist arXiv HTTP diagnostics")
    return response


class DailyArxivPipeline:
    def __init__(self):
        self.page_size = 100
        # Own the bounded retry policy: arxiv.py otherwise retries 429 every 3s.
        self.client = arxiv.Client(self.page_size, num_retries=0)
        self.client._session.hooks['response'].append(log_arxiv_http_error)
        self.client._session.hooks['response'].append(self.record_retry_after)
        self._retry_after = None
        self._papers = {}
        self._metadata_queue = DeferredSemaphore(1)

    def record_retry_after(self, response, *args, **kwargs):
        self._retry_after = response.headers.get('Retry-After')
        return response

    def fetch_paper(self, paper_id):
        if paper_id in self._papers:
            return self._papers[paper_id]
        search = arxiv.Search(id_list=[paper_id])
        for attempt in range(5):
            self._retry_after = None
            try:
                paper = next(self.client.results(search))
                self._papers[paper_id] = paper
                return paper
            except (arxiv.HTTPError, ConnectionError, Timeout) as error:
                if isinstance(error, arxiv.HTTPError) and error.status not in {
                    408, 429, 500, 502, 503, 504
                }:
                    raise
                if attempt == 4:
                    raise
                delay = 30 * 2 ** attempt
                if self._retry_after:
                    try:
                        retry_after = float(self._retry_after)
                    except ValueError:
                        try:
                            retry_after = (parsedate_to_datetime(self._retry_after)
                                           - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            retry_after = 0
                    delay = max(delay, retry_after)
                # Do not retry before a long server deadline or wait forever.
                if delay > 900:
                    raise
                logger.warning('Retrying arXiv metadata %s in %ss (attempt %s/5): %s',
                               paper_id, delay, attempt + 2, error)
                time.sleep(delay)

    def process_item(self, item: dict, spider):
        # Keep the requests client/cache serialized without blocking Scrapy's
        # reactor while metadata HTTP requests and retry sleeps are in flight.
        return self._metadata_queue.run(deferToThread, self.enrich_item, item)

    def enrich_item(self, item: dict):
        item["pdf"] = f"https://arxiv.org/pdf/{item['id']}"
        item["abs"] = f"https://arxiv.org/abs/{item['id']}"
        paper = self.fetch_paper(item['id'])
        item["authors"] = [a.name for a in paper.authors]
        item["title"] = paper.title
        item["categories"] = paper.categories
        item["comment"] = paper.comment
        item["summary"] = paper.summary
        print(item)
        return item
