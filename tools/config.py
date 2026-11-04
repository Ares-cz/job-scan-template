#!/usr/bin/env python3
"""读取与校验 config/profile.toml、config/sources.toml 的公共模块（仅依赖 Python ≥3.11 标准库）。

其他脚本（jobs_tool / plan_tool / fetch_tool / profile_tool / sources_tool）从这里 import：

    from config import load_profile, load_sources, terms_for, normalize_url

命令行：

    python3 tools/config.py --check [--profile P] [--sources S]   # 逐条中文报错，有错误返回 1
    python3 tools/config.py --dump  [--profile P] [--sources S]   # 打印补全默认值后的 JSON（调试用）

设计约定（见 DESIGN.md §2–§3）：
- 必填字段缺失是「错误」；可省字段缺失不报错，由 load_* 补默认值，下游 brief 整段省略相应句子。
- 日期字段必须写成 TOML 日期（如 2026-11-16，不加引号）。
- terms 一律是 {语言: [词, …]}；取词用 terms_for(obj, lang)：按 lang 取，缺则 en，再缺取任一。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit

try:
    import tomllib
except ImportError:  # pragma: no cover - 仅在旧版 Python 上触发
    print("错误：需要 Python ≥3.11（缺少标准库 tomllib 模块）", file=sys.stderr)
    sys.exit(3)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
PROFILE_TOML = os.path.join(CONFIG_DIR, "profile.toml")
SOURCES_TOML = os.path.join(CONFIG_DIR, "sources.toml")

SCHEMA_VERSION = 1

# 模板允许的槽位（DESIGN.md §3）
ALLOWED_SLOTS = {"topic", "domain", "role", "city", "province", "country", "unit", "zone",
                 "year", "year_next", "cohort", "degree"}
SLOT_RE = re.compile(r"\{([^{}]*)\}")

EDUCATION_LEVELS = ("phd", "master", "bachelor")
LEVEL_RANK = {"phd": 3, "master": 2, "bachelor": 1}
COHORT_SYSTEMS = ("cn", "none")
# 已知的岗位类型（profile.target.roles 与 category.requires_roles 共用）；未知值只给警告
KNOWN_ROLES = ("postdoc", "national_lab", "talent_program", "soe_campus", "industry_rd",
               "public_institution", "civil_service")
FETCH_KINDS = ("html_list", "browse", "none")
DATE_KINDS = ("deadline", "published", "range")
SOURCE_STATUSES = ("stable", "experimental")

DEFAULT_TIMEZONE = "Europe/Amsterdam"
DEFAULT_DISMISS_PREFIXES = ["不要"]
URL_RE = re.compile(r"^https?://\S+$")


# --------------------------------------------------------------------------- 通用工具

def normalize_url(url: str) -> str:
    """用于去重的 URL 规范化：去协议、www.、末尾斜杠、#片段和 utm_* 等跟踪参数。

    与 jobs_tool.normalize_url 保持一致（复制到此处以避免循环导入；jobs_tool 可改为从本模块导入）。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"spm", "from", "source"}]
    q = urlencode(sorted(query))
    return host + path + ("?" + q if q else "")


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


def terms_for(obj, lang: str) -> list[str]:
    """从「带 terms 的对象」或「{lang: [...]} 字典」里取某语言的词表。

    顺序：lang → en → 任一非空语言。值为字符串时包装成单元素列表；取不到返回 []。
    """
    if not isinstance(obj, dict):
        return []
    terms = obj.get("terms", obj) if isinstance(obj.get("terms", None), dict) else obj
    if not isinstance(terms, dict):
        return []
    for key in (lang, "en"):
        if key in terms and _as_list(terms[key]):
            return [str(v) for v in _as_list(terms[key])]
    for value in terms.values():
        if isinstance(value, (str, list, tuple)) and _as_list(value):
            return [str(v) for v in _as_list(value)]
    return []


def text_for(obj, lang: str, default: str = "") -> str:
    """terms_for 的单值版：用于 {zh = "…", en = "…"} 形式的字段（summary、国家名等）。"""
    if isinstance(obj, str):
        return obj
    values = terms_for(obj, lang)
    return values[0] if values else default


def template_slots(template: str) -> set[str]:
    """返回模板里出现的槽位名集合（含非法名，供校验用）。"""
    return set(SLOT_RE.findall(template or ""))


def _is_date(value) -> bool:
    return isinstance(value, dt.date)


def _to_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    return value


def load_toml(path: str) -> dict:
    """读取 TOML；文件不存在或语法错误时抛出带中文说明的异常。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到配置文件：{path}")
    with open(path, "rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"TOML 语法错误（{path}）：{e}") from e


# --------------------------------------------------------------------------- profile

def highest_education(profile: dict) -> dict | None:
    """按 level（phd > master > bachelor）取最高学位；同级取先出现者。"""
    edus = [e for e in _as_list(profile.get("education")) if isinstance(e, dict)]
    if not edus:
        return None
    return max(edus, key=lambda e: (LEVEL_RANK.get(str(e.get("level", "")).lower(), 0), -edus.index(e)))


def derive_cohort_year(conferral: dt.date | None) -> int | None:
    """由学位授予日期推算国内「届别」：7 月及以后 → 次年届，否则当年届。"""
    if not _is_date(conferral):
        return None
    return conferral.year + 1 if conferral.month >= 7 else conferral.year


def apply_profile_defaults(d: dict) -> dict:
    """就地补全可省字段的默认值（对类型不对的字段不动，留给 validate_profile 报错）。"""
    d.setdefault("schema", SCHEMA_VERSION)
    identity = d.setdefault("identity", {})
    if isinstance(identity, dict):
        # timezone 是必填项（DESIGN §2）：这里不补默认值，缺失由 validate_profile 报错；
        # 运行时兜底（jobs_tool.timezone_name / profile_tool.today_for）另行退回 DEFAULT_TIMEZONE
        identity.setdefault("name_en", "")
        if "birth_month" in identity:
            identity["birth_month"] = _to_date(identity["birth_month"])

    delivery = d.setdefault("delivery", {})
    if isinstance(delivery, dict):
        delivery.setdefault("email", True)
        delivery.setdefault("sheets", False)
        delivery.setdefault("sheet_name", "")
        delivery.setdefault("legacy_history_sheets", False)

    edus = d.setdefault("education", [])
    if isinstance(edus, list):
        for e in edus:
            if not isinstance(e, dict):
                continue
            for k in ("defense_date", "conferral_date"):
                if k in e:
                    e[k] = _to_date(e[k])
            if "conferral_date" not in e and _is_date(e.get("defense_date")):
                e["conferral_date"] = e["defense_date"]
            e.setdefault("diploma_received", False)
            e.setdefault("topics", [])
            e.setdefault("discipline_codes", [])
            e.setdefault("note", "")
            e.setdefault("institution_en", "")
            e.setdefault("country", "")
            e.setdefault("field", "")
            e.setdefault("period", "")

    status = d.setdefault("status", {})
    if isinstance(status, dict):
        status.setdefault("cohort_system", "none")
        status.setdefault("overseas_returnee", False)
        status.setdefault("fresh_grad_grace_months", 24)
        status.setdefault("age_thresholds", [])
        status.setdefault("degree_years_thresholds", [])
        if status.get("cohort_system") == "cn" and "cohort_year" not in status:
            top = highest_education(d) if isinstance(edus, list) else None
            year = derive_cohort_year(top.get("conferral_date")) if top else None
            if year:
                status["cohort_year"] = year

    research = d.setdefault("research", {})
    if isinstance(research, dict):
        research.setdefault("summary", {})
        research.setdefault("disciplines", [])
        research.setdefault("skills", [])
        research.setdefault("topics", [])
        for t in _as_list(research.get("topics")):
            if isinstance(t, dict):
                t.setdefault("weight", 1)

    target = d.setdefault("target", {})
    if isinstance(target, dict):
        target.setdefault("domains", [])
        for t in _as_list(target.get("domains")):
            if isinstance(t, dict):
                t.setdefault("weight", 1)
        target.setdefault("roles", list(KNOWN_ROLES))
        target.setdefault("role_terms", {})
        target.setdefault("orientation", "")
        target.setdefault("avoid", {})
        target.setdefault("degree_requirements_ok", [])
        target.setdefault("hard_requirements", [])

    d.setdefault("matching", {})

    loc = d.setdefault("locations", {})
    if isinstance(loc, dict):
        loc.setdefault("cities", [])
        loc.setdefault("provinces", [])
        loc.setdefault("countries", [])
        loc.setdefault("special_zones", [])
        cities = loc.get("cities")
        loc.setdefault("top_cities", min(4, len(cities)) if isinstance(cities, list) else 4)
        for c in _as_list(loc.get("countries")):
            if isinstance(c, dict):
                c.setdefault("tier", 1)
        for z in _as_list(loc.get("special_zones")):
            if isinstance(z, dict):
                z.setdefault("city", "")
                z.setdefault("note", "")

    d.setdefault("resumes", [])
    marks = d.setdefault("marks", {})
    if isinstance(marks, dict):
        marks.setdefault("dismiss_prefixes", list(DEFAULT_DISMISS_PREFIXES))
    return d


def load_profile(path: str | None = None) -> dict:
    """读取 profile.toml 并补全默认值。文件不存在抛 FileNotFoundError，语法错误抛 ValueError。"""
    return apply_profile_defaults(load_toml(path or PROFILE_TOML))


def _check_terms(terms, where: str, errors: list[str]) -> None:
    if not isinstance(terms, dict) or not terms:
        errors.append(f"{where} 必须是 {{语言 = [词, …]}} 形式的非空表")
        return
    for lang, words in terms.items():
        if not isinstance(words, list) or not words or not all(isinstance(w, str) and w.strip() for w in words):
            errors.append(f"{where}.{lang} 必须是非空字符串列表")


def _check_str_list(value, where: str, errors: list[str], required: bool = False) -> None:
    if value is None:
        if required:
            errors.append(f"{where} 缺失")
        return
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where} 必须是字符串列表")
    elif required and not value:
        errors.append(f"{where} 不能为空")


def validate_profile(d: dict) -> tuple[list[str], list[str]]:
    """校验 profile；返回 (errors, warnings)，均为中文句子列表。可传原始或补全默认值后的字典。"""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(d, dict):
        return ["profile 顶层必须是表"], warnings
    if d.get("schema", SCHEMA_VERSION) != SCHEMA_VERSION:
        warnings.append(f"schema = {d.get('schema')!r}，当前工具只认识 {SCHEMA_VERSION}")

    identity = d.get("identity")
    if not isinstance(identity, dict):
        errors.append("[identity] 缺失")
        identity = {}
    for key in ("name", "email", "language"):
        if not isinstance(identity.get(key), str) or not identity.get(key, "").strip():
            errors.append(f"identity.{key} 缺失或为空（必填）")
    if isinstance(identity.get("email"), str) and identity.get("email") and "@" not in identity["email"]:
        errors.append("identity.email 不是邮箱地址")
    tz = identity.get("timezone")
    if not isinstance(tz, str) or not tz.strip():
        errors.append("identity.timezone 缺失或为空（必填，IANA 时区名）")
    else:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            warnings.append(f"identity.timezone = {tz!r} 无法解析（本机可能缺少 tzdata），运行时将退回本地日期")
    if "birth_month" in identity and not _is_date(identity["birth_month"]):
        errors.append("identity.birth_month 必须是 TOML 日期（如 1990-01-01，不加引号）")

    edus = d.get("education")
    if not isinstance(edus, list) or not edus:
        errors.append("[[education]] 至少需要 1 条")
        edus = []
    for i, e in enumerate(edus, 1):
        where = f"education[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{where} 必须是表")
            continue
        if str(e.get("level", "")).lower() not in EDUCATION_LEVELS:
            errors.append(f"{where}.level 必须是 phd|master|bachelor，当前为 {e.get('level')!r}")
        if not isinstance(e.get("institution"), str) or not e.get("institution"):
            errors.append(f"{where}.institution 缺失")
        for k in ("defense_date", "conferral_date"):
            if k in e and not _is_date(e[k]):
                errors.append(f"{where}.{k} 必须是 TOML 日期（如 2026-11-16，不加引号）")
        if "qs_rank" in e and not isinstance(e["qs_rank"], int):
            errors.append(f"{where}.qs_rank 必须是整数")
        if "diploma_received" in e and not isinstance(e["diploma_received"], bool):
            errors.append(f"{where}.diploma_received 必须是 true/false")
        _check_str_list(e.get("topics"), f"{where}.topics", errors)
        _check_str_list(e.get("discipline_codes"), f"{where}.discipline_codes", errors)

    status = d.get("status", {})
    if not isinstance(status, dict):
        errors.append("[status] 必须是表")
        status = {}
    cs = status.get("cohort_system", "none")
    if cs not in COHORT_SYSTEMS:
        errors.append(f"status.cohort_system 必须是 cn|none，当前为 {cs!r}")
    if "cohort_year" in status and not isinstance(status["cohort_year"], int):
        errors.append("status.cohort_year 必须是整数年份")
    if cs == "cn" and "cohort_year" not in status:
        top = highest_education(d)
        if not (top and derive_cohort_year(top.get("conferral_date") or top.get("defense_date"))):
            warnings.append("status.cohort_system = cn 但未写 cohort_year，且最高学位缺少 conferral_date/defense_date，无法推算届别")
    for key in ("age_thresholds", "degree_years_thresholds"):
        v = status.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, int) for x in v):
            errors.append(f"status.{key} 必须是整数列表")
    if "fresh_grad_grace_months" in status and not isinstance(status["fresh_grad_grace_months"], int):
        errors.append("status.fresh_grad_grace_months 必须是整数（月）")

    research = d.get("research")
    if not isinstance(research, dict):
        errors.append("[research] 缺失")
        research = {}
    topics = research.get("topics")
    if not isinstance(topics, list) or not topics:
        errors.append("[[research.topics]] 至少需要 1 条（必填）")
        topics = []
    _check_str_list(research.get("disciplines"), "research.disciplines", errors)
    _check_str_list(research.get("skills"), "research.skills", errors)
    if "summary" in research and not isinstance(research["summary"], (dict, str)):
        errors.append("research.summary 必须是字符串或 {语言 = 文字}")

    target = d.get("target", {})
    if not isinstance(target, dict):
        errors.append("[target] 必须是表")
        target = {}
    domains = target.get("domains", [])
    if not isinstance(domains, list):
        errors.append("[[target.domains]] 必须是数组表")
        domains = []
    seen_ids: set[str] = set()
    for name, items in (("research.topics", topics), ("target.domains", domains)):
        for i, t in enumerate(items, 1):
            where = f"{name}[{i}]"
            if not isinstance(t, dict):
                errors.append(f"{where} 必须是表")
                continue
            tid = t.get("id")
            if not isinstance(tid, str) or not tid:
                errors.append(f"{where}.id 缺失")
            elif tid in seen_ids:
                errors.append(f"{where}.id = {tid!r} 重复")
            else:
                seen_ids.add(tid)
            if "weight" in t and not isinstance(t["weight"], int):
                errors.append(f"{where}.weight 必须是整数")
            _check_terms(t.get("terms"), f"{where}.terms", errors)
    roles = target.get("roles", list(KNOWN_ROLES))
    _check_str_list(roles, "target.roles", errors, required=True)
    if isinstance(roles, list):
        for r in roles:
            if isinstance(r, str) and r not in KNOWN_ROLES:
                warnings.append(f"target.roles 含未知岗位类型 {r!r}（已知：{', '.join(KNOWN_ROLES)}）")
    if "role_terms" in target and target["role_terms"]:
        _check_terms(target["role_terms"], "target.role_terms", errors)
    if "avoid" in target and target["avoid"]:
        _check_terms(target["avoid"], "target.avoid", errors)
    _check_str_list(target.get("degree_requirements_ok"), "target.degree_requirements_ok", errors)
    _check_str_list(target.get("hard_requirements"), "target.hard_requirements", errors)
    if "overseas_qs_max" in target and not isinstance(target["overseas_qs_max"], int):
        errors.append("target.overseas_qs_max 必须是整数")

    matching = d.get("matching", {})
    if not isinstance(matching, dict):
        errors.append("[matching] 必须是表")
    else:
        for k, v in matching.items():
            if not isinstance(v, str):
                errors.append(f"matching.{k} 必须是字符串")

    loc = d.get("locations")
    if not isinstance(loc, dict):
        errors.append("[locations] 缺失")
        loc = {}
    cities = loc.get("cities", [])
    countries = loc.get("countries", [])
    _check_str_list(cities, "locations.cities", errors)
    _check_str_list(loc.get("provinces"), "locations.provinces", errors)
    if not isinstance(countries, list):
        errors.append("[[locations.countries]] 必须是数组表")
        countries = []
    if not (isinstance(cities, list) and cities) and not countries:
        errors.append("locations.cities 与 locations.countries 至少要有一个非空")
    if "top_cities" in loc and (not isinstance(loc["top_cities"], int) or loc["top_cities"] < 0):
        errors.append("locations.top_cities 必须是非负整数")
    for i, c in enumerate(countries, 1):
        if not isinstance(c, dict) or not c.get("name"):
            errors.append(f"locations.countries[{i}] 需要 name（字符串或 {{语言 = 名称}}）")
        elif "tier" in c and not isinstance(c["tier"], int):
            errors.append(f"locations.countries[{i}].tier 必须是整数")
    zones = loc.get("special_zones", [])
    if not isinstance(zones, list):
        errors.append("[[locations.special_zones]] 必须是数组表")
        zones = []
    zone_ids: set[str] = set()
    for i, z in enumerate(zones, 1):
        if not isinstance(z, dict) or not isinstance(z.get("id"), str) or not z.get("id"):
            errors.append(f"locations.special_zones[{i}].id 缺失")
            continue
        if not isinstance(z.get("name"), str) or not z.get("name"):
            errors.append(f"locations.special_zones[{i}].name 缺失")
        if z["id"] in zone_ids:
            errors.append(f"locations.special_zones[{i}].id = {z['id']!r} 重复")
        zone_ids.add(z["id"])

    resumes = d.get("resumes", [])
    if not isinstance(resumes, list):
        errors.append("[[resumes]] 必须是数组表")
        resumes = []
    for i, r in enumerate(resumes, 1):
        if not isinstance(r, dict) or not r.get("lang") or not r.get("path"):
            errors.append(f"resumes[{i}] 需要 lang 与 path")
            continue
        p = r["path"] if os.path.isabs(r["path"]) else os.path.join(ROOT, r["path"])
        if not os.path.exists(p):
            warnings.append(f"resumes[{i}].path = {r['path']!r} 文件不存在（仅供评估匹配度，可稍后补）")

    marks = d.get("marks", {})
    if not isinstance(marks, dict):
        errors.append("[marks] 必须是表")
    else:
        _check_str_list(marks.get("dismiss_prefixes"), "marks.dismiss_prefixes", errors)

    delivery = d.get("delivery", {})
    if isinstance(delivery, dict):
        for k in ("email", "sheets", "legacy_history_sheets"):
            if k in delivery and not isinstance(delivery[k], bool):
                errors.append(f"delivery.{k} 必须是 true/false")
        if delivery.get("sheets") and not delivery.get("sheet_name"):
            warnings.append("delivery.sheets = true 但未写 sheet_name")
    else:
        errors.append("[delivery] 必须是表")

    return errors, warnings


# --------------------------------------------------------------------------- sources

def apply_sources_defaults(d: dict) -> dict:
    """就地补全 sources.toml 的默认值。"""
    d.setdefault("schema", SCHEMA_VERSION)
    sch = d.setdefault("schedule", {})
    if isinstance(sch, dict):
        sch.setdefault("max_queries_per_day", 15)
        sch.setdefault("min_queries_per_day", 8)
        sch.setdefault("weekend_factor", 0.5)
        sch.setdefault("fetch_timeout_sec", 20)
        sch.setdefault("fetch_total_sec", 120)
        sch.setdefault("fetch_max_items_per_source", 40)
        if "epoch" in sch:
            sch["epoch"] = _to_date(sch["epoch"])
    cats = d.setdefault("category", [])
    if isinstance(cats, list):
        for c in cats:
            if not isinstance(c, dict):
                continue
            c.setdefault("lang", "en")
            c.setdefault("requires_roles", [])
            c.setdefault("enabled", True)
            c.setdefault("every", 1)
            c.setdefault("offset", 0)
            c.setdefault("queries_per_turn", 2)
            if isinstance(c.get("every"), int) and "lookback_days" not in c:
                c["lookback_days"] = c["every"] + 1
            c.setdefault("weight", 1)
            c.setdefault("query_templates", [])
            c.setdefault("pinned", [])
            c.setdefault("units", [])
            c.setdefault("browse", [])
            c.setdefault("level_hint", "")
            c.setdefault("extra_instructions", "")
    srcs = d.setdefault("source", [])
    if isinstance(srcs, list):
        for s in srcs:
            if not isinstance(s, dict):
                continue
            s.setdefault("pages", [])
            s.setdefault("every", 1)
            s.setdefault("fetch", "html_list")
            s.setdefault("link_pattern", "")
            s.setdefault("block_pattern", "")
            s.setdefault("date_pattern", "")
            s.setdefault("date_format", "")
            s.setdefault("date_kind", "deadline")
            s.setdefault("date_from_url", "")
            s.setdefault("time_attr", True)
            s.setdefault("closed_markers", [])
            s.setdefault("title_filter", "profile")
            s.setdefault("title_exclude", "")
            s.setdefault("access_note", "")
            s.setdefault("max_age_days", 60)
            s.setdefault("status", "stable")
            s.setdefault("notes", "")
            s.setdefault("name", s.get("id", ""))
    return d


def load_sources(path: str | None = None) -> dict:
    """读取 sources.toml 并补全默认值。"""
    return apply_sources_defaults(load_toml(path or SOURCES_TOML))


def _check_regex(pattern, where: str, errors: list[str], named: tuple[str, ...] = ()) -> None:
    if pattern in (None, ""):
        return
    if not isinstance(pattern, str):
        errors.append(f"{where} 必须是字符串正则")
        return
    try:
        rx = re.compile(pattern)
    except re.error as e:
        errors.append(f"{where} 不是合法正则：{e}")
        return
    missing = [n for n in named if n not in rx.groupindex]
    if missing:
        errors.append(f"{where} 缺少命名组 {', '.join('(?P<' + n + '>…)' for n in missing)}")


def _check_url_list(value, where: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        errors.append(f"{where} 必须是 URL 列表")
        return
    for u in value:
        if not isinstance(u, str) or not URL_RE.match(u):
            errors.append(f"{where} 含非法 URL：{u!r}（须以 http:// 或 https:// 开头）")


def category_active(cat: dict, profile: dict | None) -> bool:
    """类别是否启用：enabled，且（无 requires_roles 或与 profile.target.roles 有交集），
    且（无 special_zone 或 profile.locations.special_zones 中定义了该 id）。"""
    if not cat.get("enabled", True):
        return False
    if profile is None:
        return True
    req = [r for r in _as_list(cat.get("requires_roles")) if isinstance(r, str)]
    if req:
        roles = set(_as_list((profile.get("target") or {}).get("roles")))
        if not (roles & set(req)):
            return False
    sz = cat.get("special_zone")
    if sz:
        zone_ids = {z.get("id") for z in _as_list((profile.get("locations") or {}).get("special_zones")) if isinstance(z, dict)}
        if sz not in zone_ids:
            return False
    return True


def validate_sources(d: dict, profile: dict | None = None) -> tuple[list[str], list[str]]:
    """校验 sources；传入 profile 时额外检查 requires_roles 交集与 special_zone 引用。"""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(d, dict):
        return ["sources 顶层必须是表"], warnings
    if d.get("schema", SCHEMA_VERSION) != SCHEMA_VERSION:
        warnings.append(f"schema = {d.get('schema')!r}，当前工具只认识 {SCHEMA_VERSION}")

    sch = d.get("schedule")
    if not isinstance(sch, dict):
        errors.append("[schedule] 缺失")
        sch = {}
    if not _is_date(sch.get("epoch")):
        errors.append("schedule.epoch 必须是 TOML 日期（如 2026-11-10，不加引号）")
    mx, mn = sch.get("max_queries_per_day", 15), sch.get("min_queries_per_day", 8)
    for k, v in (("max_queries_per_day", mx), ("min_queries_per_day", mn), ("fetch_timeout_sec", sch.get("fetch_timeout_sec", 20)),
                 ("fetch_total_sec", sch.get("fetch_total_sec", 120)), ("fetch_max_items_per_source", sch.get("fetch_max_items_per_source", 40))):
        if not isinstance(v, int) or v < 1:
            errors.append(f"schedule.{k} 必须是正整数")
    if isinstance(mx, int) and isinstance(mn, int) and mn > mx:
        errors.append("schedule.min_queries_per_day 不能大于 max_queries_per_day")
    wf = sch.get("weekend_factor", 0.5)
    if not isinstance(wf, (int, float)) or isinstance(wf, bool) or not (0 < wf <= 1):
        errors.append("schedule.weekend_factor 必须是 (0, 1] 之间的数")

    profile_roles = set(_as_list(((profile or {}).get("target") or {}).get("roles"))) if profile else None
    zone_ids = {z.get("id") for z in _as_list(((profile or {}).get("locations") or {}).get("special_zones")) if isinstance(z, dict)} if profile else None
    cohort_none = bool(profile) and ((profile or {}).get("status") or {}).get("cohort_system", "none") == "none"

    cats = d.get("category")
    if not isinstance(cats, list) or not cats:
        errors.append("[[category]] 至少需要 1 条")
        cats = []
    cat_ids: set[str] = set()
    for i, c in enumerate(cats, 1):
        where = f"category[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{where} 必须是表")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9_]+", cid or ""):
            errors.append(f"{where}.id 缺失或含非法字符（只允许小写字母、数字、下划线）")
        elif cid in cat_ids:
            errors.append(f"{where}.id = {cid!r} 重复")
        else:
            cat_ids.add(cid)
            where = f"category {cid}"
        if not isinstance(c.get("name"), str) or not c.get("name"):
            errors.append(f"{where}.name 缺失")
        if not isinstance(c.get("lang", "en"), str):
            errors.append(f"{where}.lang 必须是语言代码字符串")
        _check_str_list(c.get("requires_roles"), f"{where}.requires_roles", errors)
        for r in _as_list(c.get("requires_roles")):
            if isinstance(r, str) and r not in KNOWN_ROLES:
                warnings.append(f"{where}.requires_roles 含未知岗位类型 {r!r}")
        if "enabled" in c and not isinstance(c["enabled"], bool):
            errors.append(f"{where}.enabled 必须是 true/false")
        for k in ("every", "queries_per_turn", "weight", "lookback_days"):
            if k in c and (not isinstance(c[k], int) or isinstance(c[k], bool) or c[k] < 1):
                errors.append(f"{where}.{k} 必须是正整数")
        if "offset" in c and (not isinstance(c["offset"], int) or c["offset"] < 0):
            errors.append(f"{where}.offset 必须是非负整数")
        wd = c.get("weekdays")
        if wd is not None and (not isinstance(wd, list) or not wd or not all(isinstance(x, int) and 1 <= x <= 7 for x in wd)):
            errors.append(f"{where}.weekdays 必须是 1–7（isoweekday）的非空整数列表")
        season = c.get("season")
        if season is not None:
            months = season.get("months") if isinstance(season, dict) else None
            if not isinstance(season, dict) or not isinstance(months, list) or not months \
                    or not all(isinstance(m, int) and 1 <= m <= 12 for m in months) \
                    or not isinstance(season.get("every"), int) or season.get("every") < 1:
                errors.append(f"{where}.season 必须形如 {{ months = [10, 11], every = 1 }}")
        am = c.get("active_months")
        if am is not None and (not isinstance(am, list) or not am or not all(isinstance(m, int) and 1 <= m <= 12 for m in am)):
            errors.append(f"{where}.active_months 必须是 1–12 的非空整数列表")
        for k in ("query_templates", "pinned", "units"):
            _check_str_list(c.get(k), f"{where}.{k}", errors)
        active = category_active(c, profile) if profile else bool(c.get("enabled", True))
        cohort_templates = 0
        for key in ("query_templates", "pinned"):
            for t in _as_list(c.get(key)):
                if not isinstance(t, str):
                    continue
                slots = template_slots(t)
                bad = slots - ALLOWED_SLOTS
                if bad:
                    errors.append(f"{where}.{key} 模板 {t!r} 含不允许的槽位 {{{'}, {'.join(sorted(bad))}}}；允许："
                                  + " ".join("{" + s + "}" for s in sorted(ALLOWED_SLOTS)))
                # 去掉合法形态的 {slot} 后仍残留花括号 → 不配对或嵌套（如 "{unit postdoc"、"{topic}} x"、"{{topic}}"）
                rest = SLOT_RE.sub("", t)
                if "{" in rest or "}" in rest:
                    errors.append(f"{where}.{key} 模板 {t!r} 花括号不配对或嵌套，请检查槽位写法")
                if "cohort" in slots:
                    cohort_templates += 1
        if cohort_templates and cohort_none and active:
            warnings.append(f"{where} 有 {cohort_templates} 条模板/固定检索词含 {{cohort}}，而 profile.status.cohort_system = none，这些将被跳过")
        if not _as_list(c.get("query_templates")) and not _as_list(c.get("pinned")) and not _as_list(c.get("units")):
            warnings.append(f"{where} 没有 query_templates / pinned / units，轮到时不会产生检索词")
        _check_url_list(c.get("browse"), f"{where}.browse", errors)
        if "special_zone" in c:
            sz = c["special_zone"]
            if not isinstance(sz, str) or not sz:
                errors.append(f"{where}.special_zone 必须是 profile.locations.special_zones 中的 id")
            elif zone_ids is not None and sz not in zone_ids and isinstance(cid, str) and c.get("enabled", True):
                warnings.append(f"类别 {cid} 将关闭（special_zone = {sz!r} 未在 profile.locations.special_zones 中定义）")
        for k in ("level_hint", "extra_instructions", "name"):
            if k in c and not isinstance(c[k], str):
                errors.append(f"{where}.{k} 必须是字符串")
        if profile_roles is not None and isinstance(cid, str) and c.get("enabled", True):
            req = set(r for r in _as_list(c.get("requires_roles")) if isinstance(r, str))
            if req and not (req & profile_roles):
                warnings.append(f"类别 {cid} 将关闭（requires_roles {sorted(req)} 与 profile.target.roles 无交集）")

    srcs = d.get("source", [])
    if not isinstance(srcs, list):
        errors.append("[[source]] 必须是数组表")
        srcs = []
    src_ids: set[str] = set()
    for i, s in enumerate(srcs, 1):
        where = f"source[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where} 必须是表")
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9_]+", sid or ""):
            errors.append(f"{where}.id 缺失或含非法字符（只允许小写字母、数字、下划线）")
        elif sid in src_ids:
            errors.append(f"{where}.id = {sid!r} 重复")
        else:
            src_ids.add(sid)
            where = f"source {sid}"
        if s.get("category") not in cat_ids:
            errors.append(f"{where}.category = {s.get('category')!r} 不是已定义的类别 id")
        if not isinstance(s.get("url"), str) or not URL_RE.match(s.get("url") or ""):
            errors.append(f"{where}.url 缺失或不是 http(s) URL")
        _check_url_list(s.get("pages"), f"{where}.pages", errors)
        fetch = s.get("fetch", "html_list")
        if fetch not in FETCH_KINDS:
            errors.append(f"{where}.fetch 必须是 html_list|browse|none，当前为 {fetch!r}")
        if fetch == "html_list" and not s.get("link_pattern"):
            errors.append(f"{where} fetch = html_list 时必须给出 link_pattern")
        _check_regex(s.get("link_pattern"), f"{where}.link_pattern", errors)
        _check_regex(s.get("date_pattern"), f"{where}.date_pattern", errors)
        _check_regex(s.get("date_from_url"), f"{where}.date_from_url", errors, named=("y", "m", "d"))
        if s.get("date_pattern") and not s.get("date_format"):
            errors.append(f"{where} 给了 date_pattern 就必须给 date_format（strptime 格式）")
        if s.get("date_kind", "deadline") not in DATE_KINDS:
            errors.append(f"{where}.date_kind 必须是 deadline|published|range")
        if s.get("status", "stable") not in SOURCE_STATUSES:
            errors.append(f"{where}.status 必须是 stable|experimental")
        if "every" in s and (not isinstance(s["every"], int) or isinstance(s["every"], bool) or s["every"] < 1):
            errors.append(f"{where}.every 必须是正整数")
        if "max_age_days" in s and (not isinstance(s["max_age_days"], int) or s["max_age_days"] < 1):
            errors.append(f"{where}.max_age_days 必须是正整数")
        if "time_attr" in s and not isinstance(s["time_attr"], bool):
            errors.append(f"{where}.time_attr 必须是 true/false")
        _check_str_list(s.get("closed_markers"), f"{where}.closed_markers", errors)
        tf = s.get("title_filter", "profile")
        if not isinstance(tf, str):
            errors.append(f"{where}.title_filter 必须是字符串（profile / 正则 / 空）")
        elif tf and tf != "profile":
            _check_regex(tf, f"{where}.title_filter", errors)
        te = s.get("title_exclude", "")
        if not isinstance(te, str):
            errors.append(f"{where}.title_exclude 必须是正则字符串（空表示不排除）")
        elif te:
            _check_regex(te, f"{where}.title_exclude", errors)
        for k in ("block_pattern", "notes", "name", "date_format", "access_note"):
            if k in s and not isinstance(s[k], str):
                errors.append(f"{where}.{k} 必须是字符串")
        if s.get("access_note") and s.get("fetch") == "html_list":
            warnings.append(f"{where}.access_note 只对 fetch = browse/none 的来源有意义（html_list 来源由脚本抓取）")

    return errors, warnings


# --------------------------------------------------------------------------- CLI

def _print_results(label: str, errors: list[str], warnings: list[str]) -> None:
    for e in errors:
        print(f"错误：[{label}] {e}")
    for w in warnings:
        print(f"警告：[{label}] {w}")


def cmd_check(profile_path: str, sources_path: str) -> int:
    total_errors = 0
    profile = None
    try:
        profile = load_profile(profile_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"错误：[profile] {e}")
        total_errors += 1
    else:
        errs, warns = validate_profile(profile)
        _print_results("profile", errs, warns)
        total_errors += len(errs)
        print(f"profile：{profile_path}（{len(errs)} 错误，{len(warns)} 警告）")
    try:
        sources = load_sources(sources_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"错误：[sources] {e}")
        total_errors += 1
    else:
        errs, warns = validate_sources(sources, profile)
        _print_results("sources", errs, warns)
        total_errors += len(errs)
        print(f"sources：{sources_path}（{len(errs)} 错误，{len(warns)} 警告）")
    if total_errors:
        print(f"配置检查未通过：共 {total_errors} 个错误")
        return 1
    print("配置检查通过")
    return 0


def _json_default(o):
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    raise TypeError(str(o))


def cmd_dump(profile_path: str, sources_path: str) -> int:
    out = {}
    try:
        out["profile"] = load_profile(profile_path)
    except (FileNotFoundError, ValueError) as e:
        out["profile_error"] = str(e)
    try:
        out["sources"] = load_sources(sources_path)
    except (FileNotFoundError, ValueError) as e:
        out["sources_error"] = str(e)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=_json_default))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="读取并校验 config/profile.toml 与 config/sources.toml")
    p.add_argument("--profile", default=PROFILE_TOML, help="profile.toml 路径（默认 config/profile.toml）")
    p.add_argument("--sources", default=SOURCES_TOML, help="sources.toml 路径（默认 config/sources.toml）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="校验两份配置，逐条打印中文错误/警告；有错误时返回 1")
    g.add_argument("--dump", action="store_true", help="打印补全默认值后的配置 JSON（调试用）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dump:
        return cmd_dump(args.profile, args.sources)
    return cmd_check(args.profile, args.sources)


if __name__ == "__main__":
    sys.exit(main())
