#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
to_md/convert.py  — Robust converter for AI-enhanced arXiv JSONL → Markdown

用法：
    python convert.py --data path/to/2025-08-15_AI_enhanced_Chinese.jsonl

主要特性：
- 对缺失的 "AI" 字段自动兜底，避免 KeyError
- 兼容 highlights 为 list 或 str
- 自动选择输出目录（_posts/ 或 docs/；都没有则创建 md/）
- 生成 Jekyll 友好的 Front Matter
"""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple


# ----------------------------
# Helpers
# ----------------------------
def safe_json_lines(path: Path) -> Iterable[Dict[str, Any]]:
    """Yield JSON objects from a JSONL file, skipping malformed lines but not crashing."""
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception as e:
                print(f"[WARN] Bad JSON at line {i}: {e}")
                continue


def normalize_ai_block(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a robust AI block; gracefully handle missing fields.
    优先取 item["AI"] 或 item["ai"]，没有则用原始字段兜底。
    """
    ai = item.get("AI") or item.get("ai") or {}

    # 兜底：如果确实没有 AI，就从原始字段构造一个最小可用块
    if not ai or not isinstance(ai, dict):
        ai = {}

    # 容错填充
    # 优先中文标题/摘要；若没有则用英文原文
    ai.setdefault("title_zh", item.get("title_zh") or item.get("title") or "")
    ai.setdefault("summary_zh", item.get("summary_zh") or item.get("abstract") or item.get("summary") or "")
    ai.setdefault("tldr", item.get("tldr") or (item.get("abstract") or "")[:140])

    # highlights 既可能是 list 也可能是 str
    highlights = ai.get("highlights")
    if isinstance(highlights, list):
        hl = [str(x).strip() for x in highlights if str(x).strip()]
    elif isinstance(highlights, str):
        # 按行或分号切分
        parts = [p.strip(" •- \t") for p in highlights.replace("；", ";").splitlines() if p.strip()]
        if len(parts) == 1 and ";" in parts[0]:
            parts = [p.strip() for p in parts[0].split(";") if p.strip()]
        hl = parts
    else:
        hl = []
    ai["highlights"] = hl

    return ai


def norm_list(val: Any) -> List[str]:
    """Normalize authors/categories to a list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        # 兼容以逗号或分号分隔的字符串
        parts = [p.strip() for p in val.replace("；", ";").replace(",", ";").split(";") if p.strip()]
        return parts
    if isinstance(val, list):
        out = []
        for x in val:
            if isinstance(x, dict):
                # authors 可能是 [{'name': 'A'}, {'name': 'B'}]
                name = x.get("name") or x.get("author") or x.get("text")
                if name:
                    out.append(str(name).strip())
            else:
                out.append(str(x).strip())
        return [p for p in out if p]
    return [str(val).strip()]


def pick_id_and_urls(item: Dict[str, Any]) -> Tuple[str, str, str]:
    """Return (arxiv_id, abs_url, pdf_url) with fallbacks."""
    arxiv_id = (
        item.get("arxiv_id")
        or item.get("id")
        or item.get("arxivId")
        or item.get("identifier")
        or ""
    )
    arxiv_id = str(arxiv_id).strip()

    abs_url = item.get("url") or item.get("link") or ""
    pdf_url = item.get("pdf_url") or ""

    if arxiv_id:
        if not abs_url:
            abs_url = f"https://arxiv.org/abs/{arxiv_id}"
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    return arxiv_id, abs_url, pdf_url


def detect_output_dir(repo_root: Path) -> Path:
    """
    选择输出目录：
    1) _posts/（若存在）
    2) docs/（若存在）
    3) md/（新建）
    """
    if (repo_root / "_posts").is_dir():
        return repo_root / "_posts"
    if (repo_root / "docs").is_dir():
        return repo_root / "docs"
    out = repo_root / "md"
    out.mkdir(parents=True, exist_ok=True)
    return out


def derive_date_from_filename(data_path: Path) -> str:
    """
    从文件名提取 YYYY-MM-DD，如果失败则用今天日期（UTC）。
    期望类似：2025-08-15_AI_enhanced_Chinese.jsonl
    """
    stem = data_path.stem  # 去掉 .jsonl
    # 先尝试前 10 位
    candidate = stem[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
        return candidate
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def md_escape(text: str) -> str:
    """简单转义 Markdown 中可能有问题的符号。"""
    if not text:
        return ""
    # 轻量处理：避免标题/列表符号引发歧义
    repl = (
        ("<", "&lt;"),
        (">", "&gt;"),
    )
    for a, b in repl:
        text = text.replace(a, b)
    return text


# ----------------------------
# Markdown rendering
# ----------------------------
def render_item_md(idx: int, item: Dict[str, Any]) -> str:
    ai = normalize_ai_block(item)

    title_en = str(item.get("title") or "").strip()
    title_zh = str(ai.get("title_zh") or "").strip()
    tldr = str(ai.get("tldr") or "").strip()
    summary_zh = str(ai.get("summary_zh") or "").strip()
    highlights = ai.get("highlights") or []

    authors = norm_list(item.get("authors") or item.get("author"))
    categories = norm_list(item.get("categories") or item.get("category"))

    arxiv_id, abs_url, pdf_url = pick_id_and_urls(item)

    # 标题优先中文，其次英文
    display_title = title_zh or title_en or arxiv_id or f"Item #{idx}"

    lines = []
    lines.append(f"### {md_escape(display_title)}")
    if abs_url:
        lines.append(f"- **arXiv**: [{arxiv_id or 'link'}]({abs_url})" + (f"  ·  [PDF]({pdf_url})" if pdf_url else ""))
    if title_en and title_zh:
        lines.append(f"- **Title (EN)**: {md_escape(title_en)}")
    if authors:
        lines.append(f"- **Authors**: {', '.join(md_escape(a) for a in authors)}")
    if categories:
        lines.append(f"- **Categories**: {', '.join(md_escape(c) for c in categories)}")
    if tldr:
        lines.append(f"- **TL;DR**: {md_escape(tldr)}")

    if highlights:
        lines.append("- **Highlights:**")
        for h in highlights:
            h = str(h).strip()
            if not h:
                continue
            # 确保不是裸的 Markdown 列表符等
            lines.append(f"  - {md_escape(h)}")

    if summary_zh:
        lines.append("")
        lines.append(md_escape(summary_zh))

    lines.append("")  # 末尾空行
    return "\n".join(lines)


def render_day_md(date_str: str, items: List[Dict[str, Any]]) -> str:
    count = len(items)
    title = f"arXiv Daily · {date_str} · {count} papers"

    # Jekyll front matter（若落在 _posts/ 将按博客文章处理）
    fm = [
        "---",
        f'title: "{title}"',
        f"date: {date_str}",
        "layout: post",
        "tags: [arxiv, daily]",
        "---",
        "",
        f"# {title}",
        "",
    ]

    body = []
    for i, item in enumerate(items, 1):
        body.append(render_item_md(i, item))

    return "\n".join(fm + body)


# ----------------------------
# Main
# ----------------------------
def main():
    p = argparse.ArgumentParser(description="Convert AI-enhanced arXiv JSONL to Markdown.")
    p.add_argument("--data", required=True, help="Path to *_AI_enhanced_*.jsonl")
    p.add_argument("--out", default="", help="Optional output directory; default auto-detect (_posts/docs/md)")
    args = p.parse_args()

    data_path = Path(args.data).resolve()
    if not data_path.exists():
        raise FileNotFoundError(f"Data file not found: {data_path}")

    repo_root = Path(__file__).resolve().parents[1]  # 仓库根（to_md 的上级）
    out_dir = Path(args.out).resolve() if args.out else detect_output_dir(repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    date_str = derive_date_from_filename(data_path)
    items = list(safe_json_lines(data_path))

    # 允许空文件但给出提示
    if not items:
        print(f"[WARN] No items found in {data_path}. Writing an empty stub MD.")
    md_text = render_day_md(date_str, items)

    # 输出文件名：_posts/YYYY-MM-DD-arxiv-daily.md；否则 docs/YYYY-MM-DD.md
    if out_dir.name == "_posts":
        out_file = out_dir / f"{date_str}-arxiv-daily.md"
    else:
        out_file = out_dir / f"{date_str}.md"

    with out_file.open("w", encoding="utf-8") as f:
        f.write(md_text)

    print(f"[OK] Wrote Markdown → {out_file}")


if __name__ == "__main__":
    main()
