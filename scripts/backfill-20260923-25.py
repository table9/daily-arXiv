"""Recover exact daily selections from failed-run ID manifests."""
import json, os, re, sys, time, subprocess
from datetime import date, timedelta
from pathlib import Path
import arxiv, requests

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "scripts/backfill-20260923-25.json").read_text())
REPORT = {}
FIELDS = ("tldr", "motivation", "method", "result", "conclusion")
LANG = os.environ.get("LANGUAGE") or "Chinese"
MODEL = os.environ.get("MODEL_NAME") or "deepseek-chat"

def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []

def write(path, items):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items), encoding="utf-8")
    tmp.replace(path)

class TimedSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", (10, 60))
        response = super().request(*args, **kwargs)
        if response.status_code != 200:
            print(json.dumps({"api_status": response.status_code, "retry_after": response.headers.get("Retry-After"), "body": response.text[:1000]}), flush=True)
        return response

client = arxiv.Client(page_size=20, delay_seconds=6, num_retries=0)
client._session = TimedSession()

def fetch(ids):
    out = {}
    for offset in range(0, len(ids), 20):
        batch = ids[offset:offset+20]
        for attempt in range(3):
            try:
                papers = list(client.results(arxiv.Search(id_list=batch, max_results=len(batch))))
                got = {}
                for p in papers:
                    pid = re.sub(r"v\d+$", "", p.get_short_id())
                    got[pid] = {"id": pid, "pdf": f"https://arxiv.org/pdf/{pid}", "abs": f"https://arxiv.org/abs/{pid}",
                                "authors": [a.name for a in p.authors], "title": p.title,
                                "categories": p.categories, "comment": p.comment, "summary": p.summary}
                if set(got) != set(batch):
                    raise RuntimeError(f"Incomplete API batch: missing={set(batch)-set(got)}, extra={set(got)-set(batch)}")
                out.update(got)
                print(f"Metadata: {len(out)}/{len(ids)}", flush=True)
                break
            except (arxiv.HTTPError, requests.RequestException) as exc:
                print(f"Metadata attempt {attempt+1}: {exc}", flush=True)
                if attempt == 2:
                    raise
                time.sleep(60 * (attempt + 1))
    return [out[pid] for pid in ids]

def valid(item):
    ai = item.get("AI", {})
    return isinstance(ai, dict) and all(isinstance(ai.get(k), str) and ai[k].strip() and ai[k].strip().lower() != "error" for k in FIELDS)

os.chdir(ROOT / "ai")
sys.path.insert(0, str(ROOT / "ai"))
from enhance import process_all_items

for day, source in MANIFEST["days"].items():
    history = set()
    for delta in range(1, 8):
        previous = (date.fromisoformat(day) - timedelta(days=delta)).isoformat()
        history.update(x["id"] for x in read(ROOT / "data" / f"{previous}.jsonl"))
    ids = [pid for pid in source["ids"] if pid not in history]
    raw_path = ROOT / "data" / f"{day}.jsonl"
    enhanced_path = ROOT / "data" / f"{day}_AI_enhanced_{LANG}.jsonl"
    raw = read(raw_path)
    if not raw:
        raw = fetch(ids)
        write(raw_path, raw)
    if [x["id"] for x in raw] != ids:
        raise RuntimeError(f"{day}: existing data differs from original selection")
    cached = {x["id"]: x for x in read(enhanced_path) if valid(x)}
    for offset in range(0, len(raw), 10):
        batch = raw[offset:offset+10]
        pending = [x for x in batch if x["id"] not in cached]
        for attempt in range(3):
            if not pending:
                break
            results = process_all_items(pending, MODEL, LANG, 4)
            for x in results:
                if valid(x):
                    cached[x["id"]] = x
            write(enhanced_path, [cached[x["id"]] for x in raw if x["id"] in cached])
            pending = [x for x in batch if x["id"] not in cached]
            if pending and attempt < 2:
                time.sleep(15)
        if pending:
            raise RuntimeError(f"Invalid AI summaries for {[x['id'] for x in pending]}")
        print(f"{day}: valid summaries {len(cached)}/{len(raw)}", flush=True)
    enhanced = read(enhanced_path)
    if [x["id"] for x in enhanced] != ids or not all(valid(x) for x in enhanced):
        raise RuntimeError(f"{day}: completeness validation failed")
    subprocess.run([sys.executable, str(ROOT / "to_md/convert.py"), "--data", str(enhanced_path)], check=True)
    REPORT[day] = {"source_run": source["run_url"], "original_unique_candidates": len(source["ids"]),
                   "excluded_history_ids": [pid for pid in source["ids"] if pid in history],
                   "papers": len(raw), "valid_summaries": len(enhanced), "ids": ids}
    (ROOT / "assets/backfill-20260923-25-report.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "assets/file-list.txt").write_text("".join(p.name + "\n" for p in sorted((ROOT / "data").glob("*.jsonl"))), encoding="utf-8")
    print(f"COMPLETE {day}: {len(raw)} papers", flush=True)
