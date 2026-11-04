#!/usr/bin/env python3
"""sources_found.md（待人工核实的新来源收件箱）的维护工具。

    python3 tools/sources_tool.py check URL                      # 是否已在 sources.toml 或 sources_found.md 中；0 新 / 2 已有
    python3 tools/sources_tool.py add URL --note 说明 [--category 类别id] [--today YYYY-MM-DD]
    python3 tools/sources_tool.py lint                           # 格式、重复、复查流水检查（CI 运行）

sources_found.md 每行固定格式：
    - URL ｜ 一句说明 ｜ 发现日期 ｜ 建议类别id
类别 id 必须是 config/sources.toml 中的 [[category]].id，未定时写「待定」。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ROOT, SOURCES_TOML, load_sources, normalize_url  # noqa: E402

SOURCES_FOUND_MD = os.path.join(ROOT, "sources_found.md")
UNDECIDED = "待定"
# 「复查流水」关键词：这类文字不该出现在收件箱里
FLOW_RE = re.compile(r"复查|无新|本次无|本次未|本次见|仍无|暂无新")
URL_RE = re.compile(r"^https?://\S+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEP_RE = re.compile(r"\s*[｜|]\s*")

HEADER = """# 待人工核实的新来源（收件箱）

由 Routine 通过 `python3 tools/sources_tool.py add URL --note "一句说明" [--category 类别id]` 追加；
不要手写「某日复查无新岗位」之类的流水。定期人工核实：有效且结构稳定的并入 `config/sources.toml`
（`[[source]]`，或某类别的 `browse` / `units`），无效的删除。

每行固定格式：`- URL ｜ 一句说明 ｜ 发现日期 ｜ 建议类别id`（`python3 tools/sources_tool.py lint` 检查）。

"""


# --------------------------------------------------------------------------- 解析

def parse_sources_found(path: str = SOURCES_FOUND_MD) -> tuple[list[dict], list[str]]:
    """解析 sources_found.md；返回 (条目列表, 格式错误列表)。

    条目字段：line_no, url, note, date, category, raw。非「- 」开头的行视为说明文字，忽略。
    """
    entries: list[dict] = []
    errors: list[str] = []
    if not os.path.exists(path):
        return entries, errors
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.rstrip("\r\n")
            if not line.startswith("- "):
                continue
            parts = SEP_RE.split(line[2:].strip())
            if len(parts) != 4:
                errors.append(f"第 {n} 行：应为 4 段「URL ｜ 说明 ｜ 发现日期 ｜ 建议类别id」，实际 {len(parts)} 段")
                entries.append({"line_no": n, "url": parts[0].strip(), "note": "", "date": "", "category": "", "raw": line, "bad": True})
                continue
            url, note, date, category = (p.strip() for p in parts)
            entries.append({"line_no": n, "url": url, "note": note, "date": date, "category": category, "raw": line, "bad": False})
    return entries, errors


def known_urls(sources_path: str = SOURCES_TOML) -> dict[str, str]:
    """sources.toml 中已有的 URL（source.url / source.pages / category.browse）→ 位置说明。"""
    out: dict[str, str] = {}
    try:
        sources = load_sources(sources_path)
    except (FileNotFoundError, ValueError):
        return out
    for cat in sources.get("category", []):
        if not isinstance(cat, dict):
            continue
        for u in cat.get("browse", []) or []:
            out.setdefault(normalize_url(u), f"sources.toml 类别 {cat.get('id')} 的 browse")
    for src in sources.get("source", []):
        if not isinstance(src, dict):
            continue
        for u in [src.get("url", "")] + list(src.get("pages", []) or []):
            if u:
                out.setdefault(normalize_url(u), f"sources.toml 来源 {src.get('id')}")
    return out


def category_ids(sources_path: str = SOURCES_TOML) -> set[str]:
    try:
        sources = load_sources(sources_path)
    except (FileNotFoundError, ValueError):
        return set()
    return {c.get("id") for c in sources.get("category", []) if isinstance(c, dict) and c.get("id")}


def find_existing(url: str, sources_path: str = SOURCES_TOML, found_path: str = SOURCES_FOUND_MD) -> str | None:
    """URL 已存在时返回位置说明，否则 None。"""
    key = normalize_url(url)
    if not key:
        return None
    hit = known_urls(sources_path).get(key)
    if hit:
        return hit
    entries, _ = parse_sources_found(found_path)
    for e in entries:
        if normalize_url(e["url"]) == key:
            return f"sources_found.md 第 {e['line_no']} 行"
    return None


def default_today() -> dt.date:
    """与其他脚本一致：按 profile identity.timezone 取「今天」；读不到 profile 时退回本地日期。"""
    try:
        import config as cfg
        from profile_tool import today_for
        return today_for(cfg.load_profile())
    except Exception:
        return dt.date.today()


def format_line(url: str, note: str, date: dt.date, category: str) -> str:
    note = SEP_RE.sub(" / ", note.strip()).replace("\n", " ")
    return f"- {url.strip()} ｜ {note} ｜ {date.isoformat()} ｜ {category or UNDECIDED}"


def append_entry(url: str, note: str, date: dt.date, category: str, found_path: str = SOURCES_FOUND_MD) -> str:
    line = format_line(url, note, date, category)
    existing = ""
    if os.path.exists(found_path):
        with open(found_path, encoding="utf-8") as f:
            existing = f.read()
    with open(found_path, "a", encoding="utf-8") as f:
        if not existing:
            f.write(HEADER)
        elif not existing.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
    return line


def lint(found_path: str = SOURCES_FOUND_MD, sources_path: str = SOURCES_TOML) -> list[str]:
    """返回问题列表（空列表 = 通过）。"""
    entries, problems = parse_sources_found(found_path)
    cats = category_ids(sources_path)
    known = known_urls(sources_path)
    seen: dict[str, int] = {}
    for e in entries:
        n = e["line_no"]
        if e.get("bad"):
            continue
        if not URL_RE.match(e["url"]):
            problems.append(f"第 {n} 行：URL 不合法（须以 http:// 或 https:// 开头且无空格）：{e['url']!r}")
        if not e["note"]:
            problems.append(f"第 {n} 行：缺少说明")
        elif FLOW_RE.search(e["note"]):
            problems.append(f"第 {n} 行：说明含复查流水关键词（复查/无新/本次无…），收件箱只记录来源本身：{e['note']!r}")
        if not DATE_RE.match(e["date"]):
            problems.append(f"第 {n} 行：发现日期须为 YYYY-MM-DD：{e['date']!r}")
        else:
            try:
                dt.date.fromisoformat(e["date"])
            except ValueError:
                problems.append(f"第 {n} 行：发现日期无效：{e['date']!r}")
        if cats and e["category"] not in cats and e["category"] != UNDECIDED:
            problems.append(f"第 {n} 行：建议类别 {e['category']!r} 不是 sources.toml 中的类别 id（或「{UNDECIDED}」）")
        key = normalize_url(e["url"])
        if key in seen:
            problems.append(f"第 {n} 行：URL 与第 {seen[key]} 行重复")
        else:
            seen[key] = n
        if key in known:
            problems.append(f"第 {n} 行：URL 已在 {known[key]} 中，应从收件箱删除")
    return problems


# --------------------------------------------------------------------------- CLI

def cmd_check(args) -> int:
    if not URL_RE.match(args.url.strip()):
        print(f"错误：URL 不合法：{args.url!r}")
        return 1
    hit = find_existing(args.url, args.sources, args.file)
    if hit:
        print(f"已有：{hit}")
        return 2
    print("新来源：未在 sources.toml 与 sources_found.md 中出现")
    return 0


def cmd_add(args) -> int:
    url = args.url.strip()
    if not URL_RE.match(url):
        print(f"错误：URL 不合法：{url!r}")
        return 1
    note = (args.note or "").strip()
    if not note:
        print("错误：--note 不能为空，请写一句这个来源是什么")
        return 1
    if FLOW_RE.search(note):
        print("错误：说明含复查流水关键词（复查/无新/本次无…）。收件箱只记录来源本身，不记录某日复查结果")
        return 1
    cats = category_ids(args.sources)
    category = (args.category or "").strip() or UNDECIDED
    if cats and category != UNDECIDED and category not in cats:
        print(f"错误：类别 {category!r} 不是 sources.toml 中的类别 id；可选：{', '.join(sorted(cats))}，或省略 --category")
        return 1
    hit = find_existing(url, args.sources, args.file)
    if hit:
        print(f"已有：{hit}；未追加")
        return 2
    try:
        today = dt.date.fromisoformat(args.today) if args.today else default_today()
    except ValueError:
        print(f"错误：--today 须为 YYYY-MM-DD：{args.today!r}")
        return 1
    line = append_entry(url, note, today, category, args.file)
    print(f"已追加：{line}")
    return 0


def cmd_lint(args) -> int:
    problems = lint(args.file, args.sources)
    for p in problems:
        print(f"错误：{p}")
    if problems:
        print(f"sources_found.md 检查未通过：{len(problems)} 个问题")
        return 1
    entries, _ = parse_sources_found(args.file)
    print(f"sources_found.md 检查通过（{len(entries)} 条）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="维护 sources_found.md（待人工核实的新来源收件箱）")
    p.add_argument("--file", default=SOURCES_FOUND_MD, help="sources_found.md 路径")
    p.add_argument("--sources", default=SOURCES_TOML, help="config/sources.toml 路径")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="URL 是否已在 sources.toml / sources_found.md 中（0 新，2 已有）")
    c.add_argument("url")
    c.set_defaults(func=cmd_check)
    a = sub.add_parser("add", help="check 后按固定格式追加一行")
    a.add_argument("url")
    a.add_argument("--note", required=True, help="一句说明（不要写复查流水）")
    a.add_argument("--category", default="", help="建议类别 id（sources.toml 中的 [[category]].id），省略写「待定」")
    a.add_argument("--today", default="", help="发现日期 YYYY-MM-DD，默认 profile 时区当天")
    a.set_defaults(func=cmd_add)
    lnt = sub.add_parser("lint", help="格式、重复、复查流水检查；有问题返回 1")
    lnt.set_defaults(func=cmd_lint)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
