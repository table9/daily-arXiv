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
from datetime import datetime, timedelta, timezone


logger = logging.getLogger(__name__)
_DIAGNOSTIC_HEADERS = {
    "retry-after", "date", "content-type", "content-length", "server", "via",
    "x-request-id", "request-id", "x-correlation-id", "correlation-id",
    "x-cloud-trace-context", "traceparent", "x-amzn-trace-id", "x-amz-cf-id",
    "cf-ray", "x-served-by", "x-cache", "x-cache-hits", "x-timer",
}


def log_arxiv_http_error(response, *args, **kwargs):
    """Capture each failed attempt before arxiv.py discards the response."""
    if response.status_code < 400:
        return response
    try:
        headers = {key.lower(): value for key, value in response.headers.items()
                   if key.lower() in _DIAGNOSTIC_HEADERS}
        body = response.text
        record = {
            "event": "arxiv_api_http_error",
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
        self.client = arxiv.Client(self.page_size)
        self.client._session.hooks['response'].append(log_arxiv_http_error)

    def process_item(self, item: dict, spider):
        item["pdf"] = f"https://arxiv.org/pdf/{item['id']}"
        item["abs"] = f"https://arxiv.org/abs/{item['id']}"
        search = arxiv.Search(
            id_list=[item["id"]],
        )
        paper = next(self.client.results(search))
        item["authors"] = [a.name for a in paper.authors]
        item["title"] = paper.title
        item["categories"] = paper.categories
        item["comment"] = paper.comment
        item["summary"] = paper.summary
        print(item)
        return item
