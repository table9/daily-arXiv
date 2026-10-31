#!/usr/bin/env python3
"""Run dated announcement harvesting without replacing existing results."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'daily_arxiv'))
from daily_arxiv.check_stats import perform_deduplication


def selected_dates(single='', start='', end='', today=None):
    today = today or datetime.now(timezone.utc).date()
    if single and (start or end):
        raise ValueError('--date cannot be combined with a date range')
    if end and not start:
        raise ValueError('--end-date requires --start-date')
    def parse(value):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Dates must use YYYY-MM-DD')
        return date.fromisoformat(value)
    first = parse(single or start) if (single or start) else today
    last = parse(end) if end else first
    if first > last:
        raise ValueError('Start date must not be after end date')
    if first < today - timedelta(days=90) or last > today:
        raise ValueError('arXiv catchup dates must be within the last 90 days')
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def read_records(path, enhanced=False):
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    for item in records:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key].strip()
                                                for key in ('id', 'title', 'summary')):
            raise ValueError(f'Incomplete paper in {path}')
        if enhanced:
            ai = item.get('AI', {})
            if not all(isinstance(ai.get(key), str) and ai[key].strip() and ai[key].strip() != 'Error'
                       for key in ('tldr', 'motivation', 'method', 'result', 'conclusion')):
                raise ValueError(f'Incomplete AI result for {item["id"]} in {path}')
    return records


def validate_enhanced(raw, enhanced):
    items = read_records(enhanced, enhanced=True)
    ids = [item['id'] for item in items]
    if len(ids) != len(set(ids)) or set(ids) != {item['id'] for item in raw}:
        raise ValueError(f'Raw/AI paper IDs differ: {enhanced}')


def write_once(path, content):
    """Atomically install a file, refusing to overwrite a different result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f'Existing result differs; preserved for review: {path}')
        return False
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
        try:
            os.link(temporary, path)  # Fails if another writer already created path.
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f'Existing result differs; preserved for review: {path}')
            return False
        return True
    finally:
        Path(temporary).unlink()


def publish_day(outputs):
    """Publish a fully validated day, rolling back only files created by this call."""
    # Materialize and check the whole set before exposing any part of the day.
    outputs = list(outputs)
    for destination, content in outputs:
        if destination.exists() and destination.read_bytes() != content:
            raise ValueError(f'Existing result differs; preserved for review: {destination}')
    created = []
    try:
        for destination, content in outputs:
            if write_once(destination, content):
                created.append(destination)
    except Exception:
        for destination in reversed(created):
            destination.unlink()
        raise


def paths_for(root, day, language):
    return (root / 'data' / f'{day}.jsonl',
            root / 'data' / f'{day}_AI_enhanced_{language}.jsonl',
            root / 'md' / f'{day}.md',
            root / 'assets/harvest-status' / f'{day}.json')


def existing_state(root, day, language, categories):
    raw, enhanced, markdown, marker = paths_for(root, day, language)
    if marker.exists():
        status = json.loads(marker.read_text(encoding='utf-8'))
        if status['categories'] != sorted(categories):
            raise ValueError(f'Category configuration changed for {day}; review its checkpoint')
        for path in (raw, enhanced, markdown):
            expected = status['files'].get(path.name)
            if expected and (not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                raise ValueError(f'Checkpoint mismatch; existing file preserved: {path}')
        if status['status'] == 'no_new_content':
            if raw.exists() or enhanced.exists() or markdown.exists():
                raise ValueError(f'No-content checkpoint conflicts with existing files for {day}')
            return 'skip_no_new_content'
    if not raw.exists() and (enhanced.exists() or markdown.exists()):
        raise ValueError(f'Missing raw data for existing results on {day}; manual review required')
    if raw.exists():
        items = read_records(raw)
        if not items:
            raise ValueError(f'Existing raw file is empty; preserved for review: {raw}')
        if enhanced.exists():
            validate_enhanced(items, enhanced)
            if markdown.exists():
                if not markdown.read_text(encoding='utf-8').strip():
                    raise ValueError(f'Existing Markdown is empty: {markdown}')
                return 'skip_complete'
        if markdown.exists():
            raise ValueError(f'Markdown exists without validated AI data for {day}')
        return 'resume'
    return 'crawl'


def scheduled_dates(root, language, categories, today=None, max_days=7):
    """Continue a contiguous checkpoint history; fail closed on large/unsafe gaps."""
    today = today or datetime.now(timezone.utc).date()
    checkpoints = sorted(date.fromisoformat(path.stem)
                         for path in (root / 'assets/harvest-status').glob('*.json')
                         if re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.stem))
    if not checkpoints:
        raise ValueError('No harvest checkpoints; bootstrap with an explicit reviewed date range')
    if checkpoints[-1] > today:
        raise ValueError('Future checkpoint found; preserve it for review')
    if any(later != earlier + timedelta(days=1)
           for earlier, later in zip(checkpoints, checkpoints[1:])):
        raise ValueError('Checkpoint history has an internal gap; review an explicit date range')
    latest = checkpoints[-1].isoformat()
    raw, enhanced, markdown, marker = paths_for(root, latest, language)
    status = json.loads(marker.read_text(encoding='utf-8'))
    if (not isinstance(status, dict) or status.get('date') != latest
            or status.get('date_basis') not in ('arxiv_catchup_announcement_date', 'existing_raw_archive_date')
            or status.get('status') not in ('complete', 'no_new_content')):
        raise ValueError(f'Invalid latest checkpoint; preserved for review: {marker}')
    if not isinstance(status.get('files'), dict) or type(status.get('papers')) is not int:
        raise ValueError(f'Invalid latest checkpoint fields; preserved for review: {marker}')
    if status['status'] == 'complete':
        if (set(status['files']) != {raw.name, enhanced.name, markdown.name}
                or not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                           for value in status['files'].values())
                or status['papers'] <= 0
                or status['papers'] != len({item['id'] for item in read_records(raw)})):
            raise ValueError(f'Incomplete latest checkpoint; preserved for review: {marker}')
    elif status.get('papers') != 0 or status.get('files') != {}:
        raise ValueError(f'Invalid empty checkpoint; preserved for review: {marker}')
    expected = 'skip_complete' if status['status'] == 'complete' else 'skip_no_new_content'
    if existing_state(root, latest, language, categories) != expected:
        raise ValueError(f'Latest checkpoint is not complete; preserved for review: {marker}')
    gap = (today - checkpoints[-1]).days
    if gap > max_days:
        raise ValueError(f'Checkpoint gap exceeds {max_days} days; use an explicit reviewed date range')
    # Including an already completed today produces a harmless, auditable skip.
    first = checkpoints[-1] + timedelta(days=1) if gap else today
    return selected_dates(start=first.isoformat(), end=today.isoformat(), today=today)


def run_day(root, day, language, categories, work_dir, run=subprocess.run):
    state = existing_state(root, day, language, categories)
    if state.startswith('skip_'):
        return {'date': day, 'status': state}
    raw, enhanced, markdown, marker = paths_for(root, day, language)
    reused_raw = raw.exists()
    stage = Path(tempfile.mkdtemp(prefix=day + '-', dir=work_dir))
    staged_raw = stage / 'data' / raw.name
    staged_raw.parent.mkdir()
    if raw.exists():
        shutil.copyfile(raw, staged_raw)
        items = read_records(staged_raw)
        crawl_count = None  # Existing raw data is authoritative; do not re-deduplicate it.
    else:
        stats_path = stage / 'crawl-stats.json'
        run([sys.executable, '-m', 'scrapy', 'crawl', 'arxiv', '-a', f'date={day}',
             '-a', f'stats_file={stats_path}', '-s', 'FEED_STORE_EMPTY=True',
             '-s', 'CONCURRENT_REQUESTS=1',
             '-s', 'CONCURRENT_REQUESTS_PER_DOMAIN=1', '-s', 'DOWNLOAD_DELAY=3',
             '-s', 'RANDOMIZE_DOWNLOAD_DELAY=False', '-O', str(staged_raw)],
            cwd=root / 'daily_arxiv', check=True)
        stats = json.loads(stats_path.read_text(encoding='utf-8'))
        items = read_records(staged_raw)
        if (stats.get('date') != day or stats.get('close_reason') != 'finished'
                or stats.get('missing_pages') != [] or stats.get('expected_items') != len(items)
                or stats.get('log_count/ERROR', 0) or stats.get('spider_exceptions/count', 0)
                or stats.get('item_scraped_count', 0) != len(items)):
            raise ValueError(f'Incomplete crawl for {day}; inspect {stats_path}')
        crawl_count = len(items)
        for offset in range(1, 8):
            previous = (date.fromisoformat(day) - timedelta(days=offset)).isoformat() + '.jsonl'
            source = root / 'data' / previous
            if source.exists():
                shutil.copyfile(source, staged_raw.parent / previous)
        status = perform_deduplication(day, staged_raw.parent)
        if status == 'error':
            raise ValueError(f'Deduplication failed for {day}')
        items = read_records(staged_raw) if staged_raw.exists() else []
        if status in ('no_new_content', 'no_data'):
            items = []
    result = {'date': day, 'status': 'complete' if items else 'no_new_content',
              'date_basis': 'existing_raw_archive_date' if reused_raw else 'arxiv_catchup_announcement_date',
              'categories': sorted(categories),
              'crawled': crawl_count, 'papers': len({item['id'] for item in items}), 'files': {}}
    outputs = []
    if items:
        staged_ai = staged_raw.with_name(enhanced.name)
        if enhanced.exists():
            shutil.copyfile(enhanced, staged_ai)
        else:
            run([sys.executable, 'enhance.py', '--data', str(staged_raw)], cwd=root / 'ai', check=True)
        validate_enhanced(items, staged_ai)  # enhance.py may exit 0 even after per-paper errors.
        run([sys.executable, 'convert.py', '--data', str(staged_ai), '--out', str(stage / 'md')],
            cwd=root / 'to_md', check=True)
        staged_md = stage / 'md' / markdown.name
        if not staged_md.read_text(encoding='utf-8').strip():
            raise ValueError(f'Empty Markdown for {day}')
        for source, destination in ((staged_raw, raw), (staged_ai, enhanced), (staged_md, markdown)):
            content = source.read_bytes()
            outputs.append((destination, content))
            result['files'][destination.name] = hashlib.sha256(content).hexdigest()
    outputs.append((marker, (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode()))
    publish_day(outputs)
    # Keep staged outputs for the Actions artifact, including manual preview runs.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default='')
    parser.add_argument('--start-date', default='')
    parser.add_argument('--end-date', default='')
    parser.add_argument('--catch-up', action='store_true',
                        help='Continue after the latest checkpoint through UTC today (at most 7 new days)')
    parser.add_argument('--dry-run', action='store_true', help='Plan only; no network, AI, or result writes')
    parser.add_argument('--work-dir', type=Path, default=REPO / '.arxiv-work')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        language = os.environ.get('LANGUAGE') or 'Chinese'
        if not re.fullmatch(r'[A-Za-z0-9_-]+', language):
            raise ValueError('LANGUAGE must be a simple filename-safe name')
        categories = {s.strip() for s in os.environ.get('CATEGORIES', '').split(',') if s.strip()}
        if not categories or not all(re.fullmatch(r'[A-Za-z0-9.-]+', s) for s in categories):
            raise ValueError('Set CATEGORIES to the same nonempty list used by the daily workflow')
        if args.catch_up and (args.date or args.start_date or args.end_date):
            raise ValueError('--catch-up cannot be combined with explicit dates')
        days = (scheduled_dates(REPO, language, categories) if args.catch_up
                else selected_dates(args.date, args.start_date, args.end_date))
        # Validate every selected checkpoint before any network call or mutation.
        states = {day: existing_state(REPO, day, language, categories) for day in days}
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    results = []
    if not args.dry_run:
        args.work_dir.mkdir(parents=True, exist_ok=True)
    for index, day in enumerate(days):  # Keep the original 7-day deduplication chronological.
        if args.dry_run:
            result = {'date': day, 'status': 'would_' + states[day],
                      'categories': sorted(categories), 'language': language,
                      'date_basis': 'arxiv_catchup_announcement_date'}
        else:
            try:
                result = run_day(REPO, day, language, categories, args.work_dir)
            except Exception as error:
                result = {'date': day, 'status': 'failed', 'error': str(error)}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result['status'] == 'failed':
            results.extend({'date': later, 'status': 'not_attempted'} for later in days[index + 1:])
            break  # Do not deduplicate later days against an incomplete recovery window.
    if not args.dry_run:
        names = sorted(path.name for path in (REPO / 'data').glob('*.jsonl'))
        (REPO / 'assets/file-list.txt').write_text(''.join(name + '\n' for name in names), encoding='utf-8')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return int(any(item['status'] == 'failed' for item in results))


if __name__ == '__main__':
    sys.exit(main())
