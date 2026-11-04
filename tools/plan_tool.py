#!/usr/bin/env python3
"""每日计划生成器（离线、不联网、确定性；仅依赖 Python ≥3.11 标准库）。

读取 config/profile.toml 与 config/sources.toml，按调度规则算出「今天该查哪些类别、用哪些关键词」，
写出给 Routine 看的 out/brief.md 与给其他脚本读的 out/plan.json：

    python3 tools/plan_tool.py brief    [--today D] [--out DIR]   # 写 plan.json + brief.md，并打印 brief
    python3 tools/plan_tool.py brief --quiet                     # 同上，但只打印「已写入 …」一行（CI 日志用）
    python3 tools/plan_tool.py plan     [--today D] [--out DIR]   # 只写 plan.json
    python3 tools/plan_tool.py schedule [--days 14] [--today D]   # 打印两周日历（每天 due 类别与关键词数）

确定性：同一天重复运行输出完全一致；唯一的「外部输入」是今天的日期（可用 --today 覆盖）、
jobs.csv 的最大「状态更新日期」（用于推断上次运行日 → 补跑）与 out/fetched.md 是否存在。

调度规则（DESIGN.md §6）：
- day = (today − epoch).days；every_eff = season.every（当月在 season.months）否则 7（当月不在 active_months）否则 every；
- 类别 due ⇔ enabled 且 roles 有交集 且 (day + offset) % every_eff == 0 且（weekdays 省略或 isoweekday ∈ weekdays）；
- 周六日 queries_per_turn × weekend_factor 向上取整（至少 1）；
- 关键词展开：turn = day // every_eff，模板起点 (turn × q) % len(templates)，取 q 个模板；每个模板的每个槽位取候选列表
  （按 weight 降序）的第 (day + 模板序号) % len(候选) 个（对角轮询，不做笛卡尔积）；{unit} 按窗口滚动 [(turn × q) % n, +q)；
  pinned 永远在最前、不计入 queries_per_turn、不会被砍；
- 总数（含 pinned）超过 max_queries_per_day 时按 weight 从低到高逐个减 1（每类至少保留 1 组非固定检索）；
- 补跑：today − 上次运行日 L ≥ 2 时，把 (L, today] 每天 due 的类别并集都纳入，lookback 延长到 today − L + 1。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg  # noqa: E402
import profile_tool as pt  # noqa: E402

ROOT = cfg.ROOT
OUT_DIR = os.path.join(ROOT, "out")
JOBS_CSV = os.path.join(ROOT, "jobs.csv")
LEARNED_RULES = os.path.join(ROOT, "learned_rules.md")
OFFSEASON_EVERY = 7
NEXT_DUE_HORIZON = 400          # 查找下次 due 日期的最远天数
RULE_COUNT_RE = re.compile(r"依据\s*(\d+)\s*条")
UPDATE_COL = "状态更新日期"


# --------------------------------------------------------------------------- 调度

def day_index(today: dt.date, epoch: dt.date) -> int:
    return (today - epoch).days


def every_eff(cat: dict, today: dt.date) -> int:
    """当天生效的轮换周期：旺季 season.every → 淡季 7 → every。"""
    season = cat.get("season")
    if isinstance(season, dict) and today.month in (season.get("months") or []):
        return max(1, int(season.get("every") or 1))
    am = cat.get("active_months")
    if am and today.month not in am:
        return OFFSEASON_EVERY
    return max(1, int(cat.get("every") or 1))


def is_due(cat: dict, today: dt.date, epoch: dt.date, profile: dict | None) -> bool:
    if not cfg.category_active(cat, profile):
        return False
    ev = every_eff(cat, today)
    if (day_index(today, epoch) + int(cat.get("offset") or 0)) % ev != 0:
        return False
    wd = cat.get("weekdays")
    if wd and today.isoweekday() not in wd:
        return False
    return True


def next_due(cat: dict, today: dt.date, epoch: dt.date, profile: dict | None) -> dt.date | None:
    """today 之后（不含）第一个 due 日期；一年内都不轮到返回 None。"""
    for i in range(1, NEXT_DUE_HORIZON + 1):
        d = today + dt.timedelta(days=i)
        if is_due(cat, d, epoch, profile):
            return d
    return None


def queries_for_day(cat: dict, schedule: dict, today: dt.date) -> int:
    q = max(1, int(cat.get("queries_per_turn") or 1))
    if today.isoweekday() >= 6:
        q = max(1, math.ceil(q * float(schedule.get("weekend_factor") or 1)))
    return q


# --------------------------------------------------------------------------- 槽位候选

def _weighted_terms(items: list, lang: str) -> list[str]:
    """按 weight 降序（稳定）把 topics/domains 的 terms 摊平成候选词列表（去重保序）。"""
    ordered = sorted([t for t in items if isinstance(t, dict)], key=lambda t: -int(t.get("weight") or 1))
    out: list[str] = []
    for t in ordered:
        for w in cfg.terms_for(t, lang):
            if w not in out:
                out.append(w)
    return out


def slot_candidates(cat: dict, profile: dict, today: dt.date, units_window: list[str]) -> dict[str, list[str]]:
    """每个槽位的候选列表（空列表 = 该槽位不可用，含它的模板整条跳过）。"""
    lang = cat.get("lang") or "en"
    research = profile.get("research") or {}
    target = profile.get("target") or {}
    loc = profile.get("locations") or {}
    status = profile.get("status") or {}

    cities = [c for c in (loc.get("cities") or []) if isinstance(c, str)]
    top = loc.get("top_cities")
    if isinstance(top, int) and top >= 0:
        cities = cities[:top] if top else []
    countries = sorted([c for c in (loc.get("countries") or []) if isinstance(c, dict)], key=lambda c: int(c.get("tier") or 1))
    country_names = [cfg.text_for(c.get("name"), lang) for c in countries]
    country_names = [n for n in country_names if n]

    zones = [z for z in (loc.get("special_zones") or []) if isinstance(z, dict)]
    if cat.get("special_zone"):
        zones = [z for z in zones if z.get("id") == cat.get("special_zone")]
    zone_names = [str(z.get("name")) for z in zones if z.get("name")]

    cohort: list[str] = []
    if status.get("cohort_system", "none") == "cn":
        ci = pt.cohort_info(profile, today)
        if ci:
            cohort = [str(ci["cohort_year"])]

    edu = cfg.highest_education(profile) or {}
    level = str(edu.get("level", "")).lower()
    degree = pt.LEVEL_NAMES["en" if lang == "en" else "zh"].get(level, "")

    return {
        "topic": _weighted_terms(research.get("topics") or [], lang),
        "domain": _weighted_terms(target.get("domains") or [], lang),
        "role": cfg.terms_for(target.get("role_terms") or {}, lang),
        "city": cities,
        "province": [p for p in (loc.get("provinces") or []) if isinstance(p, str)],
        "country": country_names,
        "unit": list(units_window),
        "zone": zone_names,
        "year": [str(today.year)],
        "year_next": [str(today.year + 1)],
        "cohort": cohort,
        "degree": [degree] if degree else [],
    }


def fill_template(template: str, candidates: dict[str, list[str]], pick: int) -> str | None:
    """用候选列表第 pick 个（对角轮询）填满模板；任一槽位无候选 → None（整条跳过）。"""
    slots = cfg.template_slots(template)
    out = template
    for s in slots:
        cand = candidates.get(s) or []
        if not cand:
            return None
        out = out.replace("{" + s + "}", cand[pick % len(cand)])
    return re.sub(r"\s+", " ", out).strip()


def units_window(units: list[str], turn: int, q: int) -> list[str]:
    n = len(units)
    if n == 0:
        return []
    if q >= n:
        return list(units)
    start = (turn * q) % n
    return [units[(start + i) % n] for i in range(q)]


def expand_category(cat: dict, profile: dict, sources: dict, today: dt.date) -> dict:
    """展开某类别在 today 的关键词：返回 {queries, pinned, units, every_eff, queries_per_turn, turn, skipped}。"""
    schedule = sources.get("schedule") or {}
    epoch = schedule["epoch"]
    day = day_index(today, epoch)
    ev = every_eff(cat, today)
    turn = day // ev
    q = queries_for_day(cat, schedule, today)
    units = [u for u in (cat.get("units") or []) if isinstance(u, str)]
    window = units_window(units, turn, q)
    candidates = slot_candidates(cat, profile, today, window)
    templates = [t for t in (cat.get("query_templates") or []) if isinstance(t, str) and t.strip()]
    queries: list[str] = []
    skipped: list[str] = []
    if templates:
        start = (turn * q) % len(templates)
        for k in range(len(templates)):
            if len(queries) >= q:
                break
            idx = (start + k) % len(templates)
            filled = fill_template(templates[idx], candidates, day + idx)
            if filled is None:
                skipped.append(templates[idx])
            elif filled not in queries:
                queries.append(filled)
    pinned = []
    for p in cat.get("pinned") or []:
        if isinstance(p, str) and p.strip():
            filled = fill_template(p, candidates, day)
            if filled is None:
                skipped.append(p)          # 固定检索词含无候选的槽位（如 {cohort}）时也记入 skipped，便于 brief/plan 可见
            elif filled not in pinned:
                pinned.append(filled)
    return {"queries": queries, "pinned": pinned, "units": window, "every_eff": ev,
            "queries_per_turn": q, "turn": turn, "skipped_templates": skipped}


# --------------------------------------------------------------------------- 补跑 / 负面清单 / 来源

def last_run_date(jobs_csv: str) -> dt.date | None:
    """jobs.csv 中最大的「状态更新日期」（容忍 16/17 列与个别坏行）；文件缺失或无日期返回 None。"""
    if not jobs_csv or not os.path.exists(jobs_csv):
        return None
    best: dt.date | None = None
    try:
        with open(jobs_csv, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header or UPDATE_COL not in header:
                return None
            col = header.index(UPDATE_COL)
            for row in reader:
                if len(row) <= col:
                    continue
                m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", row[col] or "")
                if not m:
                    continue
                try:
                    d = dt.date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if best is None or d > best:
                    best = d
    except OSError:
        return None
    return best


def parse_negative_rules(path: str, min_count: int = 3) -> list[str]:
    """learned_rules.md 顶部条目中「依据 N 条」且 N ≥ min_count 的规律描述。

    描述 = 列表项正文中「依据 N 条」之前的文字，再去掉尾部的破折号/括号/标点/「各」；
    接受 "- " / "* " / "+ " 三种列表符，「——依据 3 条」「 — 依据3条」「（依据 5 条）」「。依据4条」等写法都能正确截断。
    """
    if not path or not os.path.exists(path):
        return []
    out: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s.startswith("## "):
                break  # 之后是标记语义表
            lm = re.match(r"[-*+]\s+", s)
            if not lm:
                continue
            body = s[lm.end():]
            m = RULE_COUNT_RE.search(body)
            if not m or int(m.group(1)) < min_count:
                continue
            desc = body[: m.start()]
            desc = re.sub(r"[\s—–\-（(。；;，,：:]*(?:各)?$", "", desc).strip()
            if desc and desc not in out:
                out.append(desc)
    return out


def negative_list(profile: dict, rules_path: str) -> list[str]:
    lang = (profile.get("identity") or {}).get("language", "zh")
    items = parse_negative_rules(rules_path)
    for a in cfg.terms_for((profile.get("target") or {}).get("avoid") or {}, lang):
        if a not in items:
            items.append(a)
    return items


def sources_of(cat_id: str, sources: dict) -> list[dict]:
    return [s for s in (sources.get("source") or []) if isinstance(s, dict) and s.get("category") == cat_id]


def browse_urls(cat: dict, sources: dict) -> list[str]:
    """直接访问页 = category.browse ∪ 该类别下 fetch = browse 的 source 的 url/pages（去重保序）。"""
    out: list[str] = []
    for u in cat.get("browse") or []:
        if isinstance(u, str) and u not in out:
            out.append(u)
    for s in sources_of(cat.get("id", ""), sources):
        if s.get("fetch") == "browse":
            for u in [s.get("url")] + list(s.get("pages") or []):
                if isinstance(u, str) and u and u not in out:
                    out.append(u)
    return out


def browse_notes(cat: dict, sources: dict) -> dict[str, str]:
    """直接访问页的说明：fetch = browse 来源的 access_note（如「JS 页面，脚本打不开」），brief 中标注「需人工打开」。"""
    notes: dict[str, str] = {}
    for s in sources_of(cat.get("id", ""), sources):
        if s.get("fetch") == "browse" and s.get("access_note"):
            for u in [s.get("url")] + list(s.get("pages") or []):
                if isinstance(u, str) and u:
                    notes[u] = str(s["access_note"])
    return notes


# --------------------------------------------------------------------------- 计划

def apply_budget(entries: list[dict], max_total: int) -> list[str]:
    """总数（含 pinned）超过 max_total 时逐个减 1：每次砍 weight 最低、其次当前组数最多（再次配置顺序靠后）的类别；
    pinned 永不砍，每类至少保留 1 组非固定检索。返回裁剪说明。"""
    notes: list[str] = []
    total = sum(len(e["queries"]) + len(e["pinned"]) for e in entries)
    if total <= max_total:
        return notes
    while total > max_total:
        cuttable = [i for i, e in enumerate(entries) if len(e["queries"]) > 1]
        if not cuttable:
            notes.append(f"已无法再砍：每类至少保留 1 组，合计 {total} 组仍超过上限 {max_total}")
            break
        i = min(cuttable, key=lambda k: (int(entries[k]["weight"]), -(len(entries[k]["queries"]) + len(entries[k]["pinned"])), -k))
        e = entries[i]
        e.setdefault("cut", []).append(e["queries"].pop())
        total -= 1
    for e in entries:
        if e.get("cut"):
            notes.append(f"{e['name']}（weight {e['weight']}）砍去 {len(e['cut'])} 组：" + "；".join(e["cut"]))
    return notes


def build_plan(profile: dict, sources: dict, today: dt.date, *, jobs_csv: str | None = JOBS_CSV,
               rules_path: str | None = LEARNED_RULES, fetched_md: str | None = None,
               catch_up: bool = True) -> dict:
    schedule = sources["schedule"]
    epoch = schedule["epoch"]
    day = day_index(today, epoch)
    max_q = int(schedule.get("max_queries_per_day") or 15)
    min_q = int(schedule.get("min_queries_per_day") or 8)

    # 补跑：从 jobs.csv 推断上次运行日
    last = last_run_date(jobs_csv) if (catch_up and jobs_csv) else None
    catch_days: list[dt.date] = []
    if last and (today - last).days >= 2:
        catch_days = [last + dt.timedelta(days=i) for i in range(1, (today - last).days)]
    lookback_extra = (today - last).days + 1 if catch_days else 0

    entries: list[dict] = []
    not_due: list[dict] = []
    for cat in sources.get("category") or []:
        if not isinstance(cat, dict):
            continue
        active = cfg.category_active(cat, profile)
        due_today = is_due(cat, today, epoch, profile)
        caught = [d for d in catch_days if is_due(cat, d, epoch, profile)] if active else []
        nd = next_due(cat, today, epoch, profile) if active else None
        base = {"id": cat.get("id"), "name": cat.get("name"), "lang": cat.get("lang", "en"),
                "weight": int(cat.get("weight") or 1), "next_due": pt.fmt(nd) or None}
        if not (due_today or caught):
            not_due.append({**base, "active": active,
                            "reason": "今天未轮到" if active else "已关闭（enabled=false、roles 无交集或 special_zone 未定义）"})
            continue
        # 补跑的类别按它最后一次漏掉的 due 日展开关键词，使轮换序列与原计划一致
        expand_day = today if due_today else caught[-1]
        exp = expand_category(cat, profile, sources, expand_day)
        browse = browse_urls(cat, sources)
        keywords_note = ""
        if not exp["queries"] and not exp["pinned"]:
            # 展开后一组关键词都没有：没有点名单位/直接访问页的类别本轮等于无事可做，移入 not_due 并说明原因；
            # 有 units/browse 的保留，但明确标注只做直接访问，避免 Routine 把它当成检索任务
            if not exp["units"] and not browse:
                not_due.append({**base, "active": active,
                                "reason": "本轮无可展开关键词（模板槽位无候选或 cohort_system = none）"})
                continue
            keywords_note = "本轮无关键词，仅直接访问"
        lookback = int(cat.get("lookback_days") or (int(cat.get("every") or 1) + 1))
        entries.append({
            **base,
            "due": True,
            "due_today": due_today,
            "catch_up_from": pt.fmt(caught[0]) if caught else None,
            "every_eff": exp["every_eff"],
            "turn": exp["turn"],
            "queries_per_turn": exp["queries_per_turn"],
            "lookback_days": max(lookback, lookback_extra),
            "queries": exp["queries"],
            "pinned": exp["pinned"],
            "units": exp["units"],
            "browse": browse,
            "browse_notes": browse_notes(cat, sources),
            "keywords_note": keywords_note,
            "sources": [s.get("id") for s in sources_of(cat.get("id", ""), sources)],
            "sources_detail": [{"id": s.get("id"), "name": s.get("name"), "fetch": s.get("fetch"), "url": s.get("url"),
                                "every": s.get("every", 1), "status": s.get("status", "stable")}
                               for s in sources_of(cat.get("id", ""), sources)],
            "level_hint": cat.get("level_hint", ""),
            "extra_instructions": cat.get("extra_instructions", ""),
            "skipped_templates": exp["skipped_templates"],
        })

    budget_notes = apply_budget(entries, max_q)
    planned = sum(len(e["queries"]) + len(e["pinned"]) for e in entries)
    lang = (profile.get("identity") or {}).get("language", "zh")
    target = profile.get("target") or {}
    matching = profile.get("matching") or {}
    fetched_path = fetched_md if fetched_md is not None else os.path.join(OUT_DIR, "fetched.md")
    fetched_exists = bool(fetched_path) and os.path.exists(fetched_path)

    return {
        "schema": 1,
        "today": pt.fmt(today),
        "weekday": today.isoweekday(),
        "day": day,
        "epoch": pt.fmt(epoch),
        "generated_at_note": f"由 tools/plan_tool.py 按 {pt.fmt(today)} 的配置离线生成；同一天重复运行结果一致，不含时间戳",
        "language": lang,
        "last_run": pt.fmt(last) or None,
        "catch_up_days": len(catch_days),
        "catch_up_dates": [pt.fmt(d) for d in catch_days],
        "budget": {"planned": planned, "min": min_q, "max": max_q, "notes": budget_notes,
                   "weekend_factor_applied": today.isoweekday() >= 6},
        "categories": entries,
        "not_due": not_due,
        "negative_list": negative_list(profile, rules_path or ""),
        "hard_requirements": list(target.get("hard_requirements") or []),
        "matching": {k: matching.get(k, "") for k in ("high", "consider", "reference") if matching.get(k)},
        "orientation": target.get("orientation", ""),
        "overseas_qs_max": target.get("overseas_qs_max"),
        "fetched_md": fetched_path if fetched_exists else None,
        "candidate_status_md": pt.status_markdown(profile, today),
    }


# --------------------------------------------------------------------------- brief.md

def _count_lines(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def render_brief(plan: dict) -> str:
    today = dt.date.fromisoformat(plan["today"])
    L: list[str] = []
    L.append(f"# 今日计划简报 {plan['today']}（{pt.weekday_zh(today)}，day={plan['day']}）")
    L.append("")
    L.append(f"> {plan['generated_at_note']}。先读完本文再开始检索；只检索「今日计划」列出的类别与关键词。")
    L.append("")
    L.append(plan["candidate_status_md"].rstrip())
    L.append("")

    L.append("## 今日计划")
    L.append("")
    cats = plan["categories"]
    if not cats:
        L.append("今天没有轮到任何类别。")
    for e in cats:
        tag = "" if e["due_today"] else f"（补跑：{e['catch_up_from']} 起漏查）"
        L.append(f"### {e['name']}（{e['id']}）{tag}")
        L.append("")
        L.append(f"- 关键词（{len(e['pinned']) + len(e['queries'])} 组，语言 {e['lang']}）：")
        for n, q in enumerate(e["pinned"] + e["queries"], 1):
            L.append(f"  {n}. {q}" + ("［固定］" if n <= len(e["pinned"]) else ""))
        if not e["pinned"] and not e["queries"]:
            L.append(f"  - （{e.get('keywords_note') or '本轮无关键词，仅直接访问'}：只核对点名单位与直接访问页，不占检索次数）")
        if e["units"]:
            L.append("- 点名单位（本轮滚动到）：" + "、".join(e["units"]))
        if e["browse"]:
            L.append("- 直接访问页：")
            for u in e["browse"]:
                note = (e.get("browse_notes") or {}).get(u)
                L.append(f"  - {u}（需人工打开：{note}）" if note else f"  - {u}")
        if e["sources"]:
            L.append("- 脚本抓取来源：" + "、".join(
                f"{s['id']}（{s['fetch']}）" for s in e["sources_detail"]))
        L.append(f"- 回看天数：只纳入最近 {e['lookback_days']} 天内发布/更新的岗位（更早的只在仍可报名时补录）")
        if e["level_hint"]:
            L.append(f"- 分级提示：{e['level_hint']}")
        if e["extra_instructions"]:
            L.append(f"- 特殊规则：{e['extra_instructions']}")
        if e["skipped_templates"]:
            # 不回显模板原文（含花括号，Routine 可能误当成检索词）；原文保留在 plan.json 的 skipped_templates 供调试
            L.append(f"- （{len(e['skipped_templates'])} 条模板因槽位无候选（如 city/domain/country）或 cohort_system = none 而跳过，详见 out/plan.json）")
        L.append("")

    L.append("## 直接抓取结果")
    L.append("")
    if plan.get("fetched_md"):
        L.append(f"见 {os.path.relpath(plan['fetched_md'], ROOT)}（{_count_lines(plan['fetched_md'])} 行）；其中候选必须打开原文核实后才能 add。")
    else:
        L.append("本次未运行抓取（out/fetched.md 不存在）。")
    L.append("")

    L.append("## 未轮到的类别与下次日期")
    L.append("")
    if not plan["not_due"]:
        L.append("（全部类别今天都已轮到）")
    for n in plan["not_due"]:
        nd = n.get("next_due") or "一年内不轮到"
        L.append(f"- {n['name']}（{n['id']}）：{n['reason']}，下次 {nd}")
    L.append("")

    L.append("## 负面清单")
    L.append("")
    L.append("不要为这些类型花检索次数（命中时不推送，在运行附注注明排除数）：")
    for x in plan["negative_list"]:
        L.append(f"- {x}")
    if not plan["negative_list"]:
        L.append("- （无）")
    L.append("")

    L.append("## 硬门槛清单")
    L.append("")
    for x in plan["hard_requirements"]:
        L.append(f"- {x}")
    if not plan["hard_requirements"]:
        L.append("- （profile 未填写 target.hard_requirements）")
    L.append("")

    L.append("## 分级口径")
    L.append("")
    names = {"high": "⭐高匹配", "consider": "可考虑", "reference": "仅供参考"}
    for k, v in plan["matching"].items():
        L.append(f"- {names.get(k, k)}：{v}")
    if plan.get("orientation"):
        L.append(f"- 偏好取向：{plan['orientation']}")
    if plan.get("overseas_qs_max"):
        L.append(f"- 海外院校 QS 排名上限：{plan['overseas_qs_max']}")
    if not plan["matching"]:
        L.append("- （profile 未填写 matching）")
    L.append("")

    b = plan["budget"]
    L.append("## 预算")
    L.append("")
    L.append(f"- 计划检索总数：{b['planned']} 组（含固定检索）；合计不少于 {b['min']} 组，软上限 {b['max']} 组"
             + ("；周末已按 weekend_factor 减量" if b.get("weekend_factor_applied") else ""))
    for n in b.get("notes") or []:
        L.append(f"- 预算裁剪：{n}")
    if b["planned"] < b["min"] and cats:
        L.append(f"- 计划数低于下限：可在计划类别内增加相近关键词补足，并在 runlog 的 note 中说明")
    L.append("")

    L.append("## 补跑说明")
    L.append("")
    if plan["catch_up_days"]:
        L.append(f"- 上次运行 {plan['last_run']}，补跑 {plan['catch_up_days']} 天（{plan['catch_up_dates'][0]} 至 {plan['catch_up_dates'][-1]}）："
                 f"漏查日轮到的类别已并入今日计划，回看天数相应延长。")
    elif plan.get("last_run"):
        L.append(f"- 上次运行 {plan['last_run']}，无需补跑。")
    else:
        L.append("- 无法从 jobs.csv 推断上次运行日（文件缺失或无日期），不补跑。")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- schedule

def schedule_lines(profile: dict, sources: dict, today: dt.date, days: int) -> list[str]:
    epoch = sources["schedule"]["epoch"]
    lines = [f"两周日历（自 {pt.fmt(today)} 起 {days} 天；epoch {pt.fmt(epoch)}；格式：类别id×关键词数，含固定检索）"]
    for i in range(days):
        d = today + dt.timedelta(days=i)
        plan = build_plan(profile, sources, d, jobs_csv=None, rules_path=None, fetched_md="", catch_up=False)
        parts = [f"{e['id']}×{len(e['queries']) + len(e['pinned'])}" for e in plan["categories"]]
        lines.append(f"{pt.fmt(d)} {pt.weekday_zh(d)} day={day_index(d, epoch):>3} | 合计 {plan['budget']['planned']:>2} | "
                     + (", ".join(parts) if parts else "（无）"))
    return lines


# --------------------------------------------------------------------------- CLI

def _load(args) -> tuple[dict, dict]:
    try:
        profile = cfg.load_profile(args.profile)
        sources = cfg.load_sources(args.sources)
    except (FileNotFoundError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)
    errs, _ = cfg.validate_sources(sources, profile)
    perrs, _ = cfg.validate_profile(profile)
    for e in perrs + errs:
        print(f"错误：配置不合法：{e}", file=sys.stderr)
    if perrs or errs:
        sys.exit(2)
    return profile, sources


def _today(args, profile: dict) -> dt.date:
    if args.today:
        try:
            return pt.parse_day(args.today)
        except ValueError as e:
            print(f"错误：--today {e}", file=sys.stderr)
            sys.exit(2)
    return pt.today_for(profile)


def _write(out_dir: str, plan: dict, with_brief: bool) -> tuple[str, str | None]:
    os.makedirs(out_dir, exist_ok=True)
    plan_path = os.path.join(out_dir, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
        f.write("\n")
    brief_path = None
    if with_brief:
        brief_path = os.path.join(out_dir, "brief.md")
        with open(brief_path, "w", encoding="utf-8") as f:
            f.write(render_brief(plan))
    return plan_path, brief_path


def _build(args, profile: dict, sources: dict, today: dt.date) -> dict:
    out_dir = args.out or OUT_DIR
    return build_plan(profile, sources, today, jobs_csv=args.jobs, rules_path=args.rules,
                      fetched_md=os.path.join(out_dir, "fetched.md"), catch_up=not args.no_catch_up)


def cmd_brief(args) -> int:
    profile, sources = _load(args)
    plan = _build(args, profile, sources, _today(args, profile))
    plan_path, brief_path = _write(args.out or OUT_DIR, plan, with_brief=True)
    written = f"已写入 {os.path.abspath(brief_path)} 与 {os.path.abspath(plan_path)}"
    if getattr(args, "quiet", False):
        # CI / 定时任务：stdout 只有这一行，brief 正文（含候选人状态）不进日志
        print(written)
        return 0
    print(render_brief(plan))
    print(written, file=sys.stderr)
    return 0


def cmd_plan(args) -> int:
    profile, sources = _load(args)
    plan = _build(args, profile, sources, _today(args, profile))
    plan_path, _ = _write(args.out or OUT_DIR, plan, with_brief=False)
    print(f"已写入 {os.path.abspath(plan_path)}：{len(plan['categories'])} 个类别，计划 {plan['budget']['planned']} 组检索")
    return 0


def cmd_schedule(args) -> int:
    profile, sources = _load(args)
    for line in schedule_lines(profile, sources, _today(args, profile), args.days):
        print(line)
    return 0


def _common_options(p: argparse.ArgumentParser, suppress: bool) -> None:
    """公共选项既可写在子命令前也可写在子命令后（子命令上的版本用 SUPPRESS 以免覆盖）。"""
    S = argparse.SUPPRESS
    p.add_argument("--profile", default=S if suppress else None, help="profile.toml 路径（默认 config/profile.toml）")
    p.add_argument("--sources", default=S if suppress else None, help="sources.toml 路径（默认 config/sources.toml）")
    p.add_argument("--today", default=S if suppress else None, help="覆盖「今天」（YYYY-MM-DD；默认 profile 时区的当天）")
    p.add_argument("--out", default=S if suppress else None, help="输出目录（默认 out/）")
    p.add_argument("--jobs", default=S if suppress else JOBS_CSV, help="用于推断上次运行日的 jobs.csv 路径")
    p.add_argument("--rules", default=S if suppress else LEARNED_RULES, help="learned_rules.md 路径（负面清单来源）")
    p.add_argument("--no-catch-up", action="store_true", default=S if suppress else False, help="不做补跑推断")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="每日计划生成器：brief.md / plan.json / 两周日历（离线、确定性）")
    _common_options(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("brief", help="写出 out/plan.json 与 out/brief.md 并打印 brief")
    _common_options(b, suppress=True)
    b.add_argument("--quiet", action="store_true", help="只打印「已写入 …」一行，不回显 brief 正文（CI 日志用）")
    b.set_defaults(func=cmd_brief)
    pl = sub.add_parser("plan", help="只写出 out/plan.json")
    _common_options(pl, suppress=True)
    pl.set_defaults(func=cmd_plan)
    s = sub.add_parser("schedule", help="打印未来 N 天日历：每天轮到的类别与关键词数")
    _common_options(s, suppress=True)
    s.add_argument("--days", type=int, default=14, help="天数（默认 14）")
    s.set_defaults(func=cmd_schedule)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
