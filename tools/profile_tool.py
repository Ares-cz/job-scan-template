#!/usr/bin/env python3
"""候选人状态工具（离线、确定性；仅依赖 Python ≥3.11 标准库）。

把「年龄门槛、学位阶段、届别、应届身份有效期」这些容易算错的个人事实交给脚本：

    python3 tools/profile_tool.py status  [--today D]              # 输出 Markdown 块「候选人当前状态」
    python3 tools/profile_tool.py age-ok --born-after YYYY-MM-DD [--today D]   # 公告「须 … 后出生」是否满足，退出码 0/1/2
                                                                               #（0 满足 / 1 不满足 / 2 无法判断：未填 birth_month、日期格式错，
                                                                               #  或划线日落在出生当月且不是 1 日——profile 只记录到月，需人工核实）
    python3 tools/profile_tool.py keywords [--today D] [--category ID]        # 预览当天各类别展开的关键词

所有个人事实都来自 config/profile.toml（可用 --profile 指定别的文件）；本文件不含任何个人信息。
「今天」默认取 profile identity.timezone 的当天日期，可用 --today YYYY-MM-DD 覆盖（测试与补跑用）。

年龄口径说明（公告写法不一，两种都给出，Routine 以公告原文为准）：
- 宽口径「N 周岁以下 / 不超过 N 周岁 = 未满 N+1 周岁」：最后合格日 = 出生日 + (N+1) 年 − 1 天；
- 严口径「不超过 N 周岁 = 满 N 周岁当日之前」：最后合格日 = 出生日 + N 年 − 1 天；
- 出生日按 birth_month 当月 1 日保守计算（实际生日只会更晚，不会更早）。
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg  # noqa: E402

LEVEL_NAMES = {
    "zh": {"phd": "博士", "master": "硕士", "bachelor": "学士"},
    "en": {"phd": "PhD", "master": "master's", "bachelor": "bachelor's"},
}
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

# 学位阶段四段（PROMPT 要求 Routine 原样使用其中的「措辞」）
STAGE_NOT_DEFENDED = "未答辩"
STAGE_DEFENDED = "已答辩待授位"
STAGE_CONFERRED = "已授位证书办理中"
STAGE_DIPLOMA = "已获学位"
STAGE_UNKNOWN = "阶段不详"
STAGE_NAMES_EN = {STAGE_NOT_DEFENDED: "not yet defended", STAGE_DEFENDED: "defended, awaiting conferral",
                  STAGE_CONFERRED: "conferred, diploma pending", STAGE_DIPLOMA: "degree in hand", STAGE_UNKNOWN: "unknown"}


# --------------------------------------------------------------------------- 日期工具

def add_months(d: dt.date, months: int) -> dt.date:
    """加 N 个月；日号超出目标月天数时取该月最后一天。"""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return dt.date(y, m, day)


def add_years(d: dt.date, years: int) -> dt.date:
    return add_months(d, 12 * years)


def parse_day(text: str) -> dt.date:
    """解析 YYYY-MM-DD 或 YYYY-MM（按 1 日）；不合法抛 ValueError（中文说明）。"""
    s = (text or "").strip().replace("/", "-").replace(".", "-")
    try:
        if len(s) == 7:
            return dt.date.fromisoformat(s + "-01")
        return dt.date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"日期格式应为 YYYY-MM-DD 或 YYYY-MM：{text!r}") from e


def today_for(profile: dict | None) -> dt.date:
    """按 profile 时区取「今天」；缺 tzdata 时退回本地日期。"""
    tz = ((profile or {}).get("identity") or {}).get("timezone") or cfg.DEFAULT_TIMEZONE
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return dt.date.today()


def fmt(d: dt.date | None) -> str:
    return d.isoformat() if isinstance(d, dt.date) else ""


def weekday_zh(d: dt.date) -> str:
    return "星期" + WEEKDAY_ZH[d.weekday()]


# --------------------------------------------------------------------------- 年龄

def birth_date(profile: dict) -> dt.date | None:
    """保守出生日：birth_month 当月 1 日。未填 birth_month 返回 None。"""
    b = (profile.get("identity") or {}).get("birth_month")
    if isinstance(b, dt.datetime):
        b = b.date()
    if not isinstance(b, dt.date):
        return None
    return b.replace(day=1)


def age_on(birth: dt.date, today: dt.date) -> int:
    """周岁（已满几岁）。"""
    years = today.year - birth.year
    if (today.month, today.day) < (birth.month, birth.day):
        years -= 1
    return years


def age_threshold(birth: dt.date, n: int, today: dt.date) -> dict:
    """某个年龄上限 N 的两种口径：最后合格日与今天是否满足。"""
    loose_last = add_years(birth, n + 1) - dt.timedelta(days=1)   # 未满 N+1 周岁
    strict_last = add_years(birth, n) - dt.timedelta(days=1)      # 未满 N 周岁
    return {
        "threshold": n,
        "loose_last_day": loose_last,
        "loose_ok": today <= loose_last,
        "strict_last_day": strict_last,
        "strict_ok": today <= strict_last,
    }


def born_after_ok(birth: dt.date, born_after: dt.date) -> bool:
    """公告「YYYY-MM-DD 以后出生」（含当日）是否满足；birth 为保守出生日。"""
    return birth >= born_after


# --------------------------------------------------------------------------- 学位阶段 / 届别 / 应届身份

def degree_stage(edu: dict, today: dt.date) -> dict:
    """最高学位的阶段：未答辩 / 已答辩待授位 / 已授位证书办理中 / 已获学位。

    defense_date 缺失时用 conferral_date 代替（反之亦然）；两者都缺且未拿证 → 阶段不详。
    brief 在早晨生成，所以答辩日当天仍按「未答辩」保守处理（note 提示今天是答辩日）；
    授位日当天即视为「已授位」。
    """
    defense = edu.get("defense_date") or edu.get("conferral_date")
    conferral = edu.get("conferral_date") or edu.get("defense_date")
    diploma = bool(edu.get("diploma_received"))
    note = ""
    if diploma:
        stage = STAGE_DIPLOMA
    elif not isinstance(defense, dt.date):
        stage = STAGE_UNKNOWN
    elif today <= defense:
        stage = STAGE_NOT_DEFENDED
    elif today < conferral:
        stage = STAGE_DEFENDED
    else:
        stage = STAGE_CONFERRED
    if isinstance(defense, dt.date) and today == defense:
        note = "今天是答辩日"
    return {"stage": stage, "defense_date": defense if isinstance(defense, dt.date) else None,
            "conferral_date": conferral if isinstance(conferral, dt.date) else None,
            "diploma_received": diploma, "note": note}


def stage_wording(stage_info: dict, level: str, lang: str) -> str:
    """PROMPT 要求 Routine 原样使用的一句话措辞。"""
    names = LEVEL_NAMES["en" if lang == "en" else "zh"]
    lv = names.get(level, level)
    d, c = fmt(stage_info["defense_date"]), fmt(stage_info["conferral_date"])
    s = stage_info["stage"]
    if lang == "en":
        return {
            STAGE_NOT_DEFENDED: f"{lv} candidate; defence scheduled {d}, degree expected {c} (graduating student)",
            STAGE_DEFENDED: f"{lv} defended on {d}; degree to be conferred on {c}",
            STAGE_CONFERRED: f"{lv} conferred on {c}; diploma being issued",
            STAGE_DIPLOMA: f"{lv} conferred on {c} (diploma in hand)",
        }.get(s, f"{lv}: degree stage unknown (no defense/conferral date in profile)")
    return {
        STAGE_NOT_DEFENDED: f"{lv}在读，预计 {d} 答辩、{c} 授予学位（应届毕业生）",
        STAGE_DEFENDED: f"已于 {d} 通过{lv}答辩，预计 {c} 正式授予学位",
        STAGE_CONFERRED: f"已于 {c} 获{lv}学位，学位证书办理中",
        STAGE_DIPLOMA: f"已于 {c} 获{lv}学位（证书已取得）",
    }.get(s, f"{lv}学位阶段不详（profile 未填写 defense_date / conferral_date）")


def season_cohort_year(today: dt.date) -> int:
    """当前校招季届别：8 月起为次年届。"""
    return today.year + 1 if today.month >= 8 else today.year


def cohort_info(profile: dict, today: dt.date) -> dict | None:
    status = profile.get("status") or {}
    if status.get("cohort_system", "none") != "cn":
        return None
    cohort = status.get("cohort_year")
    if not cohort:
        top = cfg.highest_education(profile) or {}
        cohort = cfg.derive_cohort_year(top.get("conferral_date") or top.get("defense_date"))
    if not cohort:
        return None
    season = season_cohort_year(today)
    return {"cohort_year": int(cohort), "season_year": season, "same": int(cohort) == season}


def fresh_grad_until(edu: dict, profile: dict) -> dt.date | None:
    conferral = edu.get("conferral_date") or edu.get("defense_date")
    grace = (profile.get("status") or {}).get("fresh_grad_grace_months", 24)
    if not isinstance(conferral, dt.date) or not isinstance(grace, int):
        return None
    return add_months(conferral, grace)


def degree_years_until(edu: dict, n: int) -> dt.date | None:
    conferral = edu.get("conferral_date") or edu.get("defense_date")
    if not isinstance(conferral, dt.date):
        return None
    return add_years(conferral, n)


# --------------------------------------------------------------------------- 状态块

def status_lines(profile: dict, today: dt.date) -> list[str]:
    """「候选人当前状态」的正文行（不含标题）。省略的 profile 字段 → 对应句子整段省略。"""
    lang = (profile.get("identity") or {}).get("language", "zh")
    en = lang == "en"
    lines: list[str] = []
    lines.append(f"- 今天：{fmt(today)}（{weekday_zh(today)}）" if not en else f"- Today: {fmt(today)} ({today.strftime('%A')})")
    ident = profile.get("identity") or {}
    if ident.get("email"):
        lines.append(f"- 日报唯一收件地址：{ident['email']}（输出语言：{lang}）" if not en
                     else f"- Sole report recipient: {ident['email']} (output language: {lang})")

    # 年龄
    birth = birth_date(profile)
    thresholds = (profile.get("status") or {}).get("age_thresholds") or []
    if birth:
        age = age_on(birth, today)
        if en:
            lines.append(f"- Age: {age} (conservatively counted from the 1st of the birth month, {birth.strftime('%Y-%m')})")
        else:
            lines.append(f"- 年龄：{age} 周岁（按出生月 {birth.strftime('%Y-%m')} 当月 1 日保守计算）")
        for n in thresholds:
            t = age_threshold(birth, n, today)
            if en:
                lines.append(
                    f"  - Limit {n}: loose reading (under {n + 1}) eligible until {fmt(t['loose_last_day'])}"
                    f" → {'OK' if t['loose_ok'] else 'NOT OK'} today; strict reading (not yet {n}) eligible until"
                    f" {fmt(t['strict_last_day'])} → {'OK' if t['strict_ok'] else 'NOT OK'} today. Follow the announcement's own wording.")
            else:
                lines.append(
                    f"  - 年龄上限 {n} 周岁：宽口径「{n} 周岁以下 = 未满 {n + 1} 周岁」合格至 {fmt(t['loose_last_day'])}"
                    f"（今天{'满足' if t['loose_ok'] else '不满足'}）；严口径「不超过 {n} 周岁 = 满 {n} 周岁当日前」合格至"
                    f" {fmt(t['strict_last_day'])}（今天{'满足' if t['strict_ok'] else '不满足'}）。以公告原文为准；"
                    f"公告按出生日期划线（如「某年某月某日以后出生」）时直接与出生月比较，可运行 `profile_tool.py age-ok --born-after 日期`。")

    # 学位阶段
    top = cfg.highest_education(profile)
    if top:
        stage = degree_stage(top, today)
        level = str(top.get("level", "")).lower()
        wording = stage_wording(stage, level, lang)
        inst = top.get("institution_en") if en and top.get("institution_en") else top.get("institution", "")
        if en:
            lines.append(f"- Degree stage: {STAGE_NAMES_EN.get(stage['stage'], stage['stage'])} — use this exact wording: \"{wording}\" ({inst})")
        else:
            lines.append(f"- 学位阶段：{stage['stage']}。Routine 原样使用的措辞：「{wording}」（{inst}）")
        if stage["note"]:
            lines.append(f"  - {stage['note']}" if not en else "  - Today is the defence day")
        if stage["stage"] == STAGE_NOT_DEFENDED and stage["defense_date"]:
            days = (stage["defense_date"] - today).days
            lines.append(f"  - 距答辩还有 {days} 天；公告要求「已获博士学位」的岗位须确认是否接受应届在读博士" if not en
                         else f"  - {days} days to the defence; check whether posts requiring a completed PhD accept graduating candidates")

    # 届别
    ci = cohort_info(profile, today)
    if ci:
        if ci["same"]:
            lines.append(f"- 届别：{ci['cohort_year']} 届（与当前校招季一致）" if not en
                         else f"- Cohort: class of {ci['cohort_year']} (matches the current recruiting season)")
        else:
            if ci["cohort_year"] > ci["season_year"]:
                hint = (f"候选人晚于当前校招季：{ci['season_year']} 届公告一般不接受，{ci['cohort_year']} 届校招通常自 8 月起陆续发布；"
                        f"校招类公告以其写明的届别口径为准")
            else:
                hint = (f"候选人早于当前校招季：{ci['season_year']} 届公告须核实是否允许 {ci['cohort_year']} 届/留学归国人员报考，"
                        f"否则只能按「应届身份有效期内」或社招口径投递")
            if en:
                lines.append(f"- Cohort: class of {ci['cohort_year']} vs. current recruiting season class of {ci['season_year']} — check each campus-recruitment notice's cohort rule")
            else:
                lines.append(f"- 届别：候选人为 {ci['cohort_year']} 届，当前校招季为 {ci['season_year']} 届。{hint}")

    # 应届 / 留学归国身份有效期
    if top:
        until = fresh_grad_until(top, profile)
        status = profile.get("status") or {}
        if until:
            grace = status.get("fresh_grad_grace_months", 24)
            conferral = fmt(top.get("conferral_date") or top.get("defense_date"))
            who = "留学归国应届身份" if status.get("overseas_returnee") else "应届身份"
            state = "有效" if today <= until else "已过期"
            if en:
                lines.append(f"- Fresh-graduate{' (returnee)' if status.get('overseas_returnee') else ''} status: {grace} months from {conferral} → until {fmt(until)} ({'valid' if today <= until else 'expired'} today; local rules vary)")
            else:
                lines.append(f"- {who}：按学位授予日 {conferral} 起 {grace} 个月计算，至 {fmt(until)}（今天{state}；各地认定口径不同，以公告为准）")
        for n in status.get("degree_years_thresholds") or []:
            d = degree_years_until(top, n)
            if d:
                base = fmt(top.get("conferral_date") or top.get("defense_date"))
                if en:
                    lines.append(f"- \"Within {n} years of PhD\": until {fmt(d)} (counted from {base})")
                else:
                    lines.append(f"- 「获博士学位 {n} 年内」：至 {fmt(d)}（自 {base} 起算{'，尚未授位' if top.get('conferral_date') and today < top['conferral_date'] else ''}）")

    # 其他静态事实（简短）
    research = profile.get("research") or {}
    disciplines = research.get("disciplines") or []
    if disciplines:
        lines.append(("- 可对口学科：" if not en else "- Matching disciplines: ") + "、".join(disciplines))
    ok = (profile.get("target") or {}).get("degree_requirements_ok") or []
    if ok:
        lines.append(("- 可接受的学历要求写法：" if not en else "- Acceptable degree requirements: ") + "、".join(ok))
    return lines


def status_markdown(profile: dict, today: dt.date) -> str:
    return "## 候选人当前状态\n\n" + "\n".join(status_lines(profile, today)) + "\n"


# --------------------------------------------------------------------------- CLI

def _load_profile(path: str | None) -> dict:
    try:
        return cfg.load_profile(path)
    except (FileNotFoundError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)


def _today(args, profile: dict) -> dt.date:
    if args.today:
        try:
            return parse_day(args.today)
        except ValueError as e:
            print(f"错误：--today {e}", file=sys.stderr)
            sys.exit(2)
    return today_for(profile)


def cmd_status(args) -> int:
    profile = _load_profile(args.profile)
    print(status_markdown(profile, _today(args, profile)), end="")
    return 0


def cmd_age_ok(args) -> int:
    profile = _load_profile(args.profile)
    today = _today(args, profile)
    birth = birth_date(profile)
    if not birth:
        print("无法判断：profile identity.birth_month 未填写")
        return 2
    try:
        after = parse_day(args.born_after)
    except ValueError as e:
        print(f"错误：--born-after {e}", file=sys.stderr)
        return 2
    if (after.year, after.month) == (birth.year, birth.month) and after.day > 1:
        # profile 只记录到月：划线日落在出生当月（且不是 1 日）时，实际出生日可能在划线日之前也可能之后，不能下结论
        print(f"无法判断：公告划线日 {fmt(after)} 落在候选人出生当月 {birth.strftime('%Y-%m')}，"
              f"profile 只记录到月，请人工核实实际出生日是否在 {fmt(after)} 及以后")
        return 2
    ok = born_after_ok(birth, after)
    print(f"公告要求 {fmt(after)} 及以后出生；候选人出生月 {birth.strftime('%Y-%m')}（保守按 {fmt(birth)}）→ "
          f"{'满足' if ok else '不满足'}（今天 {fmt(today)}，{age_on(birth, today)} 周岁）")
    return 0 if ok else 1


def cmd_keywords(args) -> int:
    import plan_tool  # 延迟导入，避免循环
    profile = _load_profile(args.profile)
    today = _today(args, profile)
    try:
        sources = cfg.load_sources(args.sources)
    except (FileNotFoundError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    cats = [c for c in sources.get("category", []) if not args.category or c.get("id") == args.category]
    if args.category and not cats:
        print(f"错误：sources.toml 中没有类别 {args.category!r}", file=sys.stderr)
        return 2
    day = plan_tool.day_index(today, sources["schedule"]["epoch"])
    print(f"今天 {fmt(today)}（{weekday_zh(today)}，day={day}）各类别展开的关键词（* 为固定检索词）：")
    for c in cats:
        active = cfg.category_active(c, profile)
        if not active:
            # 已关闭的类别不会被检索，只打印状态行，不展开关键词（以免 fork 者误以为它会被查）
            print(f"\n[{c.get('id')}] {c.get('name')}（已关闭：enabled=false、roles 无交集或 special_zone 未定义）")
            continue
        exp = plan_tool.expand_category(c, profile, sources, today)
        due = plan_tool.is_due(c, today, sources["schedule"]["epoch"], profile)
        flag = "今天轮到" if due else "未轮到"
        print(f"\n[{c.get('id')}] {c.get('name')}（{flag}，every={exp['every_eff']}，本轮 {exp['queries_per_turn']} 组）")
        for q in exp["pinned"]:
            print(f"  * {q}")
        for q in exp["queries"]:
            print(f"  - {q}")
        if not exp["pinned"] and not exp["queries"]:
            print("  （本轮无关键词，仅直接访问）" if exp["units"] or (c.get("browse") or [])
                  else "  （本轮无可展开关键词：模板槽位无候选或 cohort_system = none）")
        if exp["units"]:
            print("  点名单位：" + "、".join(exp["units"]))
        if exp["skipped_templates"]:
            print(f"  （{len(exp['skipped_templates'])} 条模板因槽位无候选或 cohort_system = none 而跳过）")
    return 0


def _common_options(p: argparse.ArgumentParser, suppress: bool) -> None:
    """公共选项既可写在子命令前也可写在子命令后（子命令上的版本用 SUPPRESS 以免覆盖）。"""
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--profile", default=d, help="profile.toml 路径（默认 config/profile.toml）")
    p.add_argument("--sources", default=d, help="sources.toml 路径（keywords 用；默认 config/sources.toml）")
    p.add_argument("--today", default=d, help="覆盖「今天」（YYYY-MM-DD；默认 profile 时区的当天）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="候选人状态工具：年龄门槛、学位阶段、届别、应届身份有效期（离线、确定性）")
    _common_options(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="输出 Markdown 块「候选人当前状态」")
    _common_options(s, suppress=True)
    s.set_defaults(func=cmd_status)
    a = sub.add_parser("age-ok", help="判断公告「须 YYYY-MM-DD 后出生」是否满足（退出码 0 满足 / 1 不满足 / 2 无法判断）")
    _common_options(a, suppress=True)
    a.add_argument("--born-after", required=True, help="公告要求的最早出生日期（YYYY-MM-DD 或 YYYY-MM）")
    a.set_defaults(func=cmd_age_ok)
    k = sub.add_parser("keywords", help="预览当天各类别展开的关键词（调用 plan_tool 的展开函数）")
    _common_options(k, suppress=True)
    k.add_argument("--category", default=None, help="只看某一类别 id")
    k.set_defaults(func=cmd_keywords)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
