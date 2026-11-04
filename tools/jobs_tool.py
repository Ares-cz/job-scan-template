#!/usr/bin/env python3
"""jobs.csv 的确定性辅助工具（仅依赖 Python 标准库，3.11+）。

把每日扫描中「机械、易算错」的部分交给脚本完成，检索与判断仍由 Routine 负责：

    python3 tools/jobs_tool.py validate [--strict]    # 校验 jobs.csv 结构与取值（旧版 16 列表头只警告）
    python3 tools/jobs_tool.py migrate [--dry-run]    # 16 列 → 17 列（拆分「截止日期」为 日期 + 截止说明）
    python3 tools/jobs_tool.py status [--dry-run]     # 按日期规则重新判定状态
    python3 tools/jobs_tool.py check URL [--unit 单位 --title 岗位]   # 去重查询
    python3 tools/jobs_tool.py add --unit .. --title .. --link .. --deadline .. --deadline-note .. ...  # 安全追加
    python3 tools/jobs_tool.py marks --from out/sheet_values.json [--dry-run]   # 把在线表格里的「我的标记」合并回来
    python3 tools/jobs_tool.py set <链接> [--mark ..] [--apply-state ..] [--archive 原因] [--restore] ...  # 改一条岗位
    python3 tools/jobs_tool.py runlog --category ID --searches N --hits M [--failed URL ...] [--note ...]
    python3 tools/jobs_tool.py report                 # 生成邮件表格 HTML/纯文本、Sheets 数据与 coverage.md

「今天」默认取 config/profile.toml 中 identity.timezone 的当天日期（找不到 profile 时为 Europe/Amsterdam），
可用 --today YYYY-MM-DD 覆盖。公共选项 --csv/--dismissed/--profile/--today 写在子命令前后均可。
「不感兴趣」标记的前缀来自 profile 的 [marks].dismiss_prefixes（缺省「不要」）。
规则来源：PROMPT.md「状态判定」与 learned_rules.md「我的标记含义与处理规则」。

jobs.csv 共 17 列；旧版 16 列文件在读取时于内存中自动升级，下一次写入即完成迁移（也可运行 migrate 立即完成）。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit

try:  # 文件锁（Linux/macOS 标准库）；Windows 等平台没有时跳过加锁，但仍保留原子替换
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_CSV = os.path.join(ROOT, "jobs.csv")
DISMISSED_CSV = os.path.join(ROOT, "dismissed.csv")
OUT_DIR = os.path.join(ROOT, "out")

COLUMNS = [
    "岗位ID", "首次推送日期", "匹配等级", "单位", "岗位", "城市", "链接", "截止日期", "截止说明",
    "状态", "状态更新日期", "我的标记", "归档日期", "归档原因", "备注", "投递状态", "投递日期",
]
LEGACY_COLUMNS = [c for c in COLUMNS if c != "截止说明"]  # 旧版 16 列（无「截止说明」）
STATUSES = ["新增", "持续", "预警", "过期", "归档"]
LEVELS = ["⭐高匹配", "可考虑", "仅供参考"]
APPLY_STATES = ["", "已投递", "进行中", "进展中", "已结束"]
DATE_COLUMNS = ["首次推送日期", "状态更新日期", "归档日期", "投递日期"]

WARN_DAYS = 7          # 距截止 ≤7 天（含当天）为预警
EXPIRE_ARCHIVE_DAYS = 7  # 过期满 7 天归档
NO_DEADLINE_ARCHIVE_DAYS = 60  # 截止不详且首次推送满 60 天归档

DEFAULT_TIMEZONE = "Europe/Amsterdam"
DEFAULT_DISMISS_PREFIXES = ["不要"]

# learned_rules.md 中「待决策」类标记的关键词
PENDING_KEYWORDS = ("考虑投递", "想投", "待投", "关注", "收藏", "重点")

# 邮件 / 表格配色（与 PROMPT.md 一致）
ROW_BG = {"新增": "#E6F4EA", "持续": "#FFFFFF", "预警": "#FFF4E5", "过期": "#FCE8E6", "当天归档": "#F1F3F4"}
APPLY_BG = {"进展中": "#E8F0FE", "进行中": "#FEF7E0", "已投递": "#FFFFFF", "已结束": "#F1F3F4"}
ARCHIVE_BG = "#F1F3F4"
GREY_TEXT = "#9AA0A6"
ORANGE = "#E8710A"

SUMMARY_ORDER = ["预警", "新增", "持续", "过期", "当天归档"]
APPLY_ORDER = ["进展中", "进行中", "已投递", "已结束"]
SHEET_PRIORITY = ["已投递", "岗位总表", "归档"]  # 「我的标记」冲突时的取值顺序

# 从在线表格读回的单元格清洗：控制字符去除、换行变空格、公式前缀拒收、超长截断
MARK_MAX_LEN = 200
NOTE_MAX_LEN = 1000
FORMULA_PREFIXES = ("=", "+", "-", "@")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# summary.html 超过该字节数时额外生成 summary_compact.html（Gmail 约 102 KB 起裁剪正文）
HTML_SIZE_LIMIT = 90_000
GMAIL_CLIP_KB = 102

ISO_DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})")
ISO_FULL_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
LEGACY_WARNING = "旧版 16 列表头，下次写入时自动升级（可运行 migrate 立即完成）"


# --------------------------------------------------------------------------- profile（时区、标记前缀）

PROFILE_PATH: str | None = None  # None = config/profile.toml
_PROFILE: dict | None = None
_PROFILE_LOADED = False


def set_profile(profile: dict | None) -> None:
    """测试或调用方直接注入画像（None 表示下次重新从文件加载）。"""
    global _PROFILE, _PROFILE_LOADED
    _PROFILE = profile
    _PROFILE_LOADED = profile is not None


def profile() -> dict:
    """懒加载 config/profile.toml（通过 tools/config.py）；找不到或解析失败时返回 {}。"""
    global _PROFILE, _PROFILE_LOADED
    if not _PROFILE_LOADED:
        _PROFILE = {}
        try:
            import config as _config  # tools/config.py
            if hasattr(_config, "load_profile"):
                _PROFILE = _config.load_profile(PROFILE_PATH) or {}
        except Exception:
            _PROFILE = {}
        _PROFILE_LOADED = True
    return _PROFILE or {}


def timezone_name() -> str:
    tz = (profile().get("identity") or {}).get("timezone") if isinstance(profile().get("identity"), dict) else None
    return str(tz).strip() if tz else DEFAULT_TIMEZONE


def dismiss_prefixes() -> list[str]:
    marks = profile().get("marks")
    value = marks.get("dismiss_prefixes") if isinstance(marks, dict) else None
    if isinstance(value, str):
        value = [value]
    value = [str(v).strip() for v in (value or []) if str(v).strip()]
    return value or list(DEFAULT_DISMISS_PREFIXES)


# --------------------------------------------------------------------------- 基础

def today() -> dt.date:
    """profile 时区的当天日期；时区不可用（缺 tzdata 或名字错误）时退回本地日期。"""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(timezone_name())).date()
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date()
        except Exception:
            return dt.date.today()


today_amsterdam = today  # 兼容旧名


def parse_date(text: str) -> dt.date | None:
    """解析字符串开头的 YYYY-MM-DD；其余（空、需核实、长期、Rolling……）视为截止日期不详。"""
    m = ISO_DATE_RE.match(text or "")
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def is_iso_date(text: str) -> bool:
    text = (text or "").strip()
    if not ISO_FULL_RE.fullmatch(text):
        return False
    try:
        dt.date.fromisoformat(text)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- 截止日期拆分

# 日期后出现这些词 → 日期只是「优先审理日」而非真实截止（软截止）。
# 契约示例「2026-12-31（按季度受理）」要保留日期，故「按季度」不算软截止关键词。
# 「招满为止」单独出现只是附注（如「2027-08-29（长期有效，招满为止）」原文截止时间确为该日），不改变日期性质。
SOFT_DEADLINE_RE = re.compile(r"优先审理|开始审阅|开始审理|review", re.I)
ISO_HEAD_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})(?:\s*(\d{1,2}:\d{2}))?")
BRACKET_RE = re.compile(r"[（(]([^（）()]*)[）)]")
# 非日期开头时的受控前缀归一
PREFIX_RULES = [
    (re.compile(r"^(rolling|滚动)", re.I), "Rolling"),
    (re.compile(r"^(招满即止|招完截止|招满为止)"), "招满即止"),
    (re.compile(r"^(长期有效|长期|常年|全年)"), "长期"),
    (re.compile(r"^需核实"), "需核实"),
]
_SEG_STRIP = " \t，,、。：:；;（）()"


def _segments(text: str) -> list[str]:
    """把「正文（括号内容）正文」展开为「；」分隔的片段列表（去空、去首尾标点）。"""
    pieces: list[str] = []
    pos = 0
    for m in BRACKET_RE.finditer(text):
        pieces.append(text[pos:m.start()])
        pieces.append(m.group(1))
        pos = m.end()
    pieces.append(text[pos:])
    out: list[str] = []
    for piece in pieces:
        for part in re.split(r"[;；]", piece):
            part = part.strip(_SEG_STRIP)
            if part:
                out.append(part)
    return out


def split_deadline(text: str) -> tuple[str, str]:
    """把自由文本的截止日期拆成 (YYYY-MM-DD 或空, 截止说明)。

    (a) ISO 日期开头、余文不含软截止关键词 → 日期 + 余文（去括号，时间并入说明开头）；
    (b) ISO 日期开头、余文含软截止关键词 → 日期置空，说明 = 「软截止 日期；余文」；
    (c) 其他 → 日期置空，说明 = 受控前缀（Rolling/长期/招满即止/需核实）+「；」分隔的其余内容。
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    m = ISO_HEAD_RE.match(text)
    if m and is_iso_date(m.group(1)):
        date, time_ = m.group(1), m.group(2)
        rest = text[m.end():]
        segs = ([time_] if time_ else []) + _segments(rest)
        if SOFT_DEADLINE_RE.search(rest):
            return "", "；".join(["软截止 " + date] + segs)
        return date, "；".join(segs)
    if text.startswith("软截止"):
        return "", text  # 已是拆分结果，原样保留
    segs = _segments(text)
    head = segs[0] if segs else ""
    prefix = None
    for rx, canon in PREFIX_RULES:
        mm = rx.match(head)
        if mm:
            prefix = canon
            remainder = head[mm.end():].strip(_SEG_STRIP)
            segs = ([remainder] if remainder else []) + segs[1:]
            break
    if prefix is None:
        prefix = "需核实"  # 没有可识别前缀：日期不详，一律要求核实
    segs = [s for s in segs if s != prefix]
    return "", "；".join([prefix] + segs)


def merge_note(*parts: str) -> str:
    out: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in out:
            out.append(p)
    return "；".join(out)


# --------------------------------------------------------------------------- 读写

@dataclass
class ReadInfo:
    """read_jobs_detailed 的附加信息：是否需要迁移、拆分明细。"""
    raw_header: list[str] = field(default_factory=list)
    legacy_header: bool = False
    added_rows: int = 0      # 补「截止说明」列的旧 16 列行数
    split_rows: int = 0      # 「截止日期」被拆分的行数
    splits: list[dict] = field(default_factory=list)

    @property
    def soft(self) -> list[dict]:
        return [s for s in self.splits if s["软截止"]]

    @property
    def needs_migration(self) -> bool:
        return self.legacy_header or self.added_rows > 0 or self.split_rows > 0


def _dictreader_like(header: list[str], rec: list[str]) -> dict:
    """模拟 csv.DictReader：缺列为 None，多余列放在 None 键下（供 validate 报列数错误）。"""
    r = {h: (rec[i] if i < len(rec) else None) for i, h in enumerate(header)}
    if len(rec) > len(header):
        r[None] = rec[len(header):]
    return r


def _normalize_deadline(r: dict, info: ReadInfo, line: int) -> None:
    raw = (r.get("截止日期") or "").strip()
    if not raw:
        r["截止日期"] = ""
        return
    if is_iso_date(raw):
        r["截止日期"] = raw
        return
    date, note = split_deadline(raw)
    r["截止日期"] = date
    r["截止说明"] = merge_note(note, r.get("截止说明") or "")
    info.split_rows += 1
    info.splits.append({"行": line, "单位": r.get("单位", ""), "岗位": r.get("岗位", ""), "原值": raw,
                        "截止日期": date, "截止说明": r["截止说明"], "软截止": note.startswith("软截止")})


def read_jobs_detailed(path: str = JOBS_CSV) -> tuple[list[str], list[dict], ReadInfo]:
    """读取 jobs.csv：旧 16 列表头或混入的 16 列行在内存中升级为 17 列，非 ISO 截止日期自动拆分。"""
    info = ReadInfo()
    with open(path, encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f))
    if not data:
        return [], [], info
    raw_header, body = data[0], data[1:]
    info.raw_header = list(raw_header)
    if raw_header == LEGACY_COLUMNS:
        info.legacy_header = True
        header = list(COLUMNS)
    elif raw_header == COLUMNS:
        header = list(COLUMNS)
    else:
        header = list(raw_header)  # 未知表头：原样返回，交给 validate 报错
    known = header == COLUMNS
    rows: list[dict] = []
    for line, rec in enumerate(body, start=2):
        if rec == []:  # DictReader 同样跳过空行
            continue
        if known and len(rec) == len(LEGACY_COLUMNS):
            r = dict(zip(LEGACY_COLUMNS, rec))
            r["截止说明"] = ""
            info.added_rows += 1
            _normalize_deadline(r, info, line)
        elif known and len(rec) == len(COLUMNS):
            r = dict(zip(COLUMNS, rec))
            _normalize_deadline(r, info, line)
        else:
            r = _dictreader_like(header, rec)
        rows.append(r)
    return header, rows, info


def read_jobs(path: str = JOBS_CSV) -> tuple[list[str], list[dict]]:
    """兼容接口：返回 (header, rows)；升级信息见 read_jobs_detailed()。"""
    header, rows, _ = read_jobs_detailed(path)
    return header, rows


def write_jobs(rows: list[dict], path: str = JOBS_CSV) -> None:
    """永远写 17 列：UTF-8 无 BOM、CRLF 换行、最小引号，避免无意义的 diff。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") or "" for c in COLUMNS})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)


def read_dismissed(path: str = DISMISSED_CSV) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r[0].strip() for i, r in enumerate(csv.reader(f)) if r and r[0].strip() and not (i == 0 and r[0].strip() == "链接")]


def _normalize_url_local(url: str) -> str:
    """用于去重的 URL 规范化：去协议、www.、末尾斜杠、#片段和 utm_* 等跟踪参数（与 config.normalize_url 一致）。"""
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


try:  # 与 config.py 共用同一份实现；config 不可用时（如单独拷贝本脚本）回退本地实现
    from config import normalize_url  # type: ignore
except Exception:  # pragma: no cover
    normalize_url = _normalize_url_local


def normalize_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def is_pending_mark(mark: str) -> bool:
    return any(k in (mark or "") for k in PENDING_KEYWORDS)


def dont_want_reason(mark: str, prefixes: list[str] | None = None) -> str | None:
    """标记以「不要」（或 profile 配置的前缀）开头则返回归档原因，否则 None。"""
    m = (mark or "").strip()
    for p in (prefixes if prefixes is not None else dismiss_prefixes()):
        if p and m.startswith(p):
            reason = m[len(p):].lstrip("，,：:、;；。 ").strip()
            return reason or "本人标记不要"
    return None


def days_left(row: dict, today: dt.date) -> int | None:
    d = parse_date(row.get("截止日期", ""))
    return None if d is None else (d - today).days


def clean_mark(raw, max_len: int = MARK_MAX_LEN) -> tuple[str, str | None]:
    """清洗从在线表格读回的文本：返回 (清洗后的文字, 拒收/截断说明或 None)。

    控制字符（NUL 等）直接去除，CR/LF/Tab 变空格；以 = + - @ 开头疑似公式 → 拒收；超长 → 截断并提示。
    """
    s = _CTRL_RE.sub("", str(raw or ""))
    s = re.sub(r"[\r\n\t]+", " ", s).strip()
    if s[:1] in FORMULA_PREFIXES:
        return s, "以 = + - @ 开头，疑似表格公式，未写入（请在表格中改为普通文字）"
    if len(s) > max_len:
        return s[:max_len], f"超过 {max_len} 字，已截断"
    return s, None


def is_rejected_mark(why: str | None) -> bool:
    return bool(why) and "疑似表格公式" in why


# --------------------------------------------------------------------------- validate

def validate(header: list[str], rows: list[dict], info: ReadInfo | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if header == LEGACY_COLUMNS:
        warnings.append(LEGACY_WARNING)
    elif header != COLUMNS:
        errors.append(f"表头与约定不一致：\n  期望 {COLUMNS}\n  实际 {header}")
    if info is not None and info.needs_migration and LEGACY_WARNING not in warnings:
        if info.legacy_header or info.added_rows:
            warnings.append(LEGACY_WARNING)
        else:
            warnings.append(f"{info.split_rows} 行「截止日期」不是 YYYY-MM-DD，下次写入时自动拆分到「截止说明」（可运行 migrate 立即完成）")
    seen_id: dict[str, int] = {}
    seen_link: dict[str, int] = {}
    for i, r in enumerate(rows, start=2):  # 第 1 行是表头
        where = f"第{i}行（{r.get('单位', '')}｜{r.get('岗位', '')}）"
        if None in r or any(v is None for v in r.values()) or any(c not in r for c in COLUMNS):
            errors.append(f"{where}：列数与表头不一致（检查未加引号的英文逗号）")
            continue
        jid = r["岗位ID"].strip()
        if not jid:
            errors.append(f"{where}：岗位ID 为空")
        elif jid in seen_id:
            errors.append(f"{where}：岗位ID 与第{seen_id[jid]}行重复")
        else:
            seen_id[jid] = i
        link = normalize_url(r["链接"])
        if link:
            if link in seen_link:
                warnings.append(f"{where}：链接与第{seen_link[link]}行指向同一页面")
            else:
                seen_link[link] = i
        if r["状态"] not in STATUSES:
            errors.append(f"{where}：状态「{r['状态']}」不在 {STATUSES} 中")
        if r["匹配等级"] not in LEVELS:
            errors.append(f"{where}：匹配等级「{r['匹配等级']}」不在 {LEVELS} 中")
        if r["投递状态"] not in APPLY_STATES:
            errors.append(f"{where}：投递状态「{r['投递状态']}」不在 {APPLY_STATES[1:]} 中")
        for col in DATE_COLUMNS:
            v = r[col].strip()
            if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                errors.append(f"{where}：{col}「{v}」不是 YYYY-MM-DD")
            elif v:
                try:
                    dt.date.fromisoformat(v)
                except ValueError:
                    errors.append(f"{where}：{col}「{v}」不是有效日期")
        ddl = r["截止日期"].strip()
        if ddl and not is_iso_date(ddl):
            errors.append(f"{where}：截止日期「{ddl}」不是 YYYY-MM-DD（说明文字请写在「截止说明」列，或运行 migrate 自动拆分）")
        if not r["首次推送日期"].strip():
            errors.append(f"{where}：首次推送日期为空")
        if r["状态"] == "归档":
            if not r["归档日期"].strip():
                warnings.append(f"{where}：状态为归档但缺少归档日期")
            if not r["归档原因"].strip():
                warnings.append(f"{where}：状态为归档但缺少归档原因")
        elif r["归档日期"].strip():
            warnings.append(f"{where}：未归档却填写了归档日期（若为恢复的岗位可忽略）")
        if r["投递状态"] and not r["我的标记"].strip():
            warnings.append(f"{where}：有投递状态但「我的标记」为空")
        if not r["链接"].strip().lower().startswith(("http://", "https://")):
            warnings.append(f"{where}：链接不是 http(s) 网址")
        for col, limit in (("我的标记", MARK_MAX_LEN), ("备注", NOTE_MAX_LEN)):
            v = r[col]
            if _CTRL_RE.search(v):
                warnings.append(f"{where}：「{col}」含控制字符")
            if v.strip()[:1] in FORMULA_PREFIXES and v.strip():
                warnings.append(f"{where}：「{col}」以公式前缀开头，Excel/Sheets 可能当作公式")
            if len(v) > limit:
                warnings.append(f"{where}：「{col}」过长（{len(v)} 字，建议不超过 {limit} 字）")
    return errors, warnings


# --------------------------------------------------------------------------- status

def judge(row: dict, today: dt.date, dismissed: set[str]) -> tuple[str, str]:
    """返回 (新状态, 归档原因)。已归档的岗位由调用方跳过（恢复需人工/Routine 判断）。"""
    reason = dont_want_reason(row.get("我的标记", ""))
    if reason:
        return "归档", reason
    if normalize_url(row.get("链接", "")) in dismissed:
        return "归档", "本人标记不要"

    apply_state = row.get("投递状态", "")
    mark = row.get("我的标记", "")
    left = days_left(row, today)
    if left is None:
        first = parse_date(row.get("首次推送日期", ""))
        stale = first is not None and (today - first).days >= NO_DEADLINE_ARCHIVE_DAYS
        # 有任何标记或投递状态的岗位不按 60 天规则归档（learned_rules：标记含糊时取最保守处理）
        if stale and not mark.strip() and not apply_state:
            return "归档", "长期无截止日期"
    elif left <= -EXPIRE_ARCHIVE_DAYS and apply_state != "进展中":
        if apply_state in ("已投递", "已结束"):
            return "归档", "已过期，已投递"
        if is_pending_mark(mark):
            return "归档", "已过期，未投递"
        return "归档", "已过期"

    if row.get("首次推送日期", "") == today.isoformat():
        return "新增", ""
    if left is None or left > WARN_DAYS:
        return "持续", ""
    if left >= 0:
        return "预警", ""
    return "过期", ""


def update_status(rows: list[dict], today: dt.date, dismissed_links: list[str]) -> list[str]:
    dismissed = {normalize_url(u) for u in dismissed_links}
    t = today.isoformat()
    changes = []
    for r in rows:
        if r["状态"] == "归档":
            continue
        new, reason = judge(r, today, dismissed)
        if new != r["状态"]:
            changes.append(f"{r['状态']} → {new}{'（' + reason + '）' if reason else ''}：{r['单位']}｜{r['岗位']}")
        r["状态"] = new
        r["状态更新日期"] = t
        if new == "归档":
            r["归档日期"] = t
            r["归档原因"] = reason
    return changes


# --------------------------------------------------------------------------- report

def _deadline_key(row: dict, today: dt.date) -> tuple:
    left = days_left(row, today)
    return (left is None, left if left is not None else 0)


def summary_rows(rows: list[dict], today: dt.date) -> list[tuple[str, dict]]:
    """岗位总表：新增/预警/持续/过期 + 当天新归档，按约定顺序排序。"""
    t = today.isoformat()
    out = []
    for r in rows:
        if r["状态"] in ("新增", "预警", "持续", "过期"):
            out.append((r["状态"], r))
        elif r["状态"] == "归档" and r["归档日期"] == t:
            out.append(("当天归档", r))
    out.sort(key=lambda x: (
        SUMMARY_ORDER.index(x[0]),
        not (x[0] == "预警" and is_pending_mark(x[1]["我的标记"]) and not x[1]["投递状态"]),
        _deadline_key(x[1], today),
    ))
    return out


def applied_rows(rows: list[dict], today: dt.date) -> list[dict]:
    out = [r for r in rows if r["投递状态"]]
    out.sort(key=lambda r: (APPLY_ORDER.index(r["投递状态"]) if r["投递状态"] in APPLY_ORDER else 99,
                            _deadline_key(r, today)))
    return out


def archived_rows(rows: list[dict]) -> list[dict]:
    out = [r for r in rows if r["状态"] == "归档"]
    out.sort(key=lambda r: r["归档日期"], reverse=True)
    return out


def _left_text(row: dict, today: dt.date) -> str:
    left = days_left(row, today)
    return "" if left is None else str(left)


def deadline_text(row: dict) -> str:
    """纯文本的截止列：「日期（说明）」；无日期时只有说明。"""
    date = (row.get("截止日期") or "").strip()
    note = (row.get("截止说明") or "").strip()
    if date and note:
        return f"{date}（{note}）"
    return date or note


def deadline_html(row: dict) -> str:
    """HTML 的截止单元格：日期 + 换行的灰色小字说明；无日期时只显示说明。"""
    date = html.escape((row.get("截止日期") or "").strip())
    note = (row.get("截止说明") or "").strip()
    span = f'<span style="color:{GREY_TEXT};font-size:11px">{html.escape(note)}</span>' if note else ""
    if date and span:
        return date + "<br>" + span
    return date or span


def render_html(rows: list[dict], today: dt.date, *, compact: bool = False) -> str:
    """邮件「岗位总表」部分：全部内联样式（Gmail 不支持 <style>）。

    样式尽量放在 <table>/<tr> 上以压缩体积（字号靠继承，垂直对齐放在行上）。
    compact=True 时「持续」行不逐条输出，折叠为一行计数并指向在线表格（其余状态照常），供 summary.html 超过
    Gmail 裁剪阈值时作为 htmlBody。
    """
    e = html.escape
    cell = "padding:4px 6px;border:1px solid #dadce0;"
    legend_items = "".join(
        f'<span style="display:inline-block;margin-right:10px;padding:1px 6px;border:1px solid #dadce0;'
        f'background:{ROW_BG[s]};{"color:" + GREY_TEXT + ";text-decoration:line-through;" if s == "当天归档" else ""}">{s}</span>'
        for s in SUMMARY_ORDER)
    headers = ["状态", "等级", "单位", "岗位", "城市", "截止日期", "剩余天数", "链接"]
    parts = [f'<p style="font-size:13px;margin:6px 0;">图例：{legend_items}</p>',
             '<table style="width:100%;border-collapse:collapse;font-size:13px;">',
             '<tr style="vertical-align:top;">' + "".join(
                 f'<th style="{cell}background:#F8F9FA;text-align:left;">{h}</th>' for h in headers) + "</tr>"]
    nowrap = {0, 6}  # 状态、剩余天数列不换行（其余列允许换行以省字节）
    collapsed = 0
    for status, r in summary_rows(rows, today):
        if compact and status == "持续":
            if collapsed == 0:
                parts.append("__COMPACT_PLACEHOLDER__")
            collapsed += 1
            continue
        style = "vertical-align:top;"
        if ROW_BG[status] != "#FFFFFF":
            style += f"background:{ROW_BG[status]};"
        if status == "当天归档":
            style += f"color:{GREY_TEXT};text-decoration:line-through;"
        left = _left_text(r, today)
        left_html = e(left) if left else "—"
        if status == "预警":
            left_html = f'<b style="color:{ORANGE};">{left_html}</b>'
        label = status
        if status == "预警" and is_pending_mark(r["我的标记"]) and not r["投递状态"]:
            label += "（你标记了考虑投递）"
        tds = [e(label), e(r["匹配等级"]), e(r["单位"]), e(r["岗位"]), e(r["城市"]), deadline_html(r), left_html,
               f'<a href="{e(r["链接"], quote=True)}">原文</a>']
        parts.append(f'<tr style="{style}">' + "".join(
            f'<td style="{cell}{"white-space:nowrap;" if i in nowrap else ""}">{td}</td>'
            for i, td in enumerate(tds)) + "</tr>")
    if collapsed:
        parts[parts.index("__COMPACT_PLACEHOLDER__")] = (
            f'<tr><td colspan="{len(headers)}" style="{cell}color:{GREY_TEXT};">'
            f"持续 {collapsed} 条未在邮件中逐条列出，见在线表格「岗位总表」</td></tr>")
    parts.append("</table>")
    return "\n".join(parts)


def render_text(rows: list[dict], today: dt.date) -> str:
    lines = ["状态｜等级｜单位｜岗位｜城市｜截止日期｜剩余天数｜链接"]
    for status, r in summary_rows(rows, today):
        lines.append("｜".join([status, r["匹配等级"], r["单位"], r["岗位"], r["城市"], deadline_text(r),
                               _left_text(r, today) or "—", r["链接"]]))
    return "\n".join(lines)


def sheets_payload(rows: list[dict], today: dt.date) -> dict:
    """三个工作表的数据与行格式，供 Routine 用 Google Sheets 工具写入。

    每行 values 中「链接」列写「原文」，真实网址在 link_uri，按 PROMPT.md 用 updateCells 写富文本超链接。
    每个工作表给出 link_column / days_left_column / mark_column（「我的标记」列索引，读回标记时用）。
    """
    stamp = f"最后更新：{today.isoformat()}"

    def meta(title: str, header: list[str], note: str, data: list[dict]) -> dict:
        return {"title": title, "header": header, "link_column": header.index("链接"),
                "days_left_column": header.index("剩余天数") if "剩余天数" in header else None,
                "mark_column": header.index("我的标记"), "deadline_note_column": header.index("截止说明"),
                "note": note, "rows": data}

    h1 = ["状态", "等级", "单位", "岗位", "城市", "截止日期", "截止说明", "剩余天数", "首次推送日期", "链接", "我的标记", "备注"]
    s1 = []
    for status, r in summary_rows(rows, today):
        s1.append({
            "values": [status, r["匹配等级"], r["单位"], r["岗位"], r["城市"], r["截止日期"], r["截止说明"],
                       _left_text(r, today), r["首次推送日期"], "原文", r["我的标记"], r["备注"]],
            "link_uri": r["链接"],
            "background": ROW_BG[status],
            "text_color": GREY_TEXT if status == "当天归档" else None,
            "strikethrough": status == "当天归档",
            "emphasize_days_left": status == "预警",
        })

    h2 = ["投递状态", "我的标记", "单位", "岗位", "城市", "截止日期", "截止说明", "剩余天数", "岗位状态", "投递日期",
          "首次推送日期", "链接", "备注"]
    s2 = []
    for r in applied_rows(rows, today):
        left = days_left(r, today)
        s2.append({
            "values": [r["投递状态"], r["我的标记"], r["单位"], r["岗位"], r["城市"], r["截止日期"], r["截止说明"],
                       _left_text(r, today), r["状态"], r["投递日期"], r["首次推送日期"], "原文", r["备注"]],
            "link_uri": r["链接"],
            "background": APPLY_BG.get(r["投递状态"], "#FFFFFF"),
            "text_color": GREY_TEXT if r["投递状态"] == "已结束" else None,
            "strikethrough": False,
            "emphasize_days_left": r["投递状态"] == "已投递" and left is not None and 0 <= left <= 3,
        })
    counts = {k: sum(1 for r in s2 if r["values"][0] == k) for k in APPLY_ORDER}

    h3 = ["归档日期", "归档原因", "等级", "单位", "岗位", "城市", "截止日期", "截止说明", "首次推送日期", "链接", "我的标记", "备注"]
    s3 = [{
        "values": [r["归档日期"], r["归档原因"], r["匹配等级"], r["单位"], r["岗位"], r["城市"], r["截止日期"],
                   r["截止说明"], r["首次推送日期"], "原文", r["我的标记"], r["备注"]],
        "link_uri": r["链接"],
        "background": ARCHIVE_BG,
        "text_color": None,
        "strikethrough": False,
        "emphasize_days_left": False,
    } for r in archived_rows(rows)]

    return {
        "generated_for": today.isoformat(),
        "colors": {"orange": ORANGE, "grey_text": GREY_TEXT},
        "sheets": [
            meta("岗位总表", h1, stamp, s1),
            meta("已投递", h2, stamp + "；" + "，".join(f"{k}{v}条" for k, v in counts.items()), s2),
            meta("归档", h3, stamp, s3),
        ],
    }


# --------------------------------------------------------------------------- check / add

def find_duplicates(rows: list[dict], dismissed: list[str], url: str = "", unit: str = "", title: str = "") -> list[str]:
    hits = []
    nu = normalize_url(url)
    if nu:
        for r in rows:
            if nu in (normalize_url(r["链接"]), normalize_url(r["岗位ID"])):
                hits.append(f"链接已存在：{r['状态']}｜{r['单位']}｜{r['岗位']}（首次推送 {r['首次推送日期']}）")
        if nu in {normalize_url(d) for d in dismissed}:
            hits.append("链接在 dismissed.csv 中（本人标记不要）")
    if unit and title:
        nunit, ntitle = normalize_text(unit), normalize_text(title)
        for r in rows:
            ru, rt = normalize_text(r["单位"]), normalize_text(r["岗位"])
            if ru and rt and (nunit in ru or ru in nunit) and (ntitle in rt or rt in ntitle):
                msg = f"单位+岗位相近：{r['状态']}｜{r['单位']}｜{r['岗位']}｜{r['链接']}"
                if msg not in hits:
                    hits.append(msg)
    return hits


# --------------------------------------------------------------------------- marks（从在线表格合并「我的标记」）

URL_IN_CELL_RE = re.compile(r"https?://[^\s\"'<>)）]+")


def extract_url(cell) -> str:
    """从单元格文本（纯网址或 =HYPERLINK("…") 公式）中取出网址；取不到返回空串。"""
    m = URL_IN_CELL_RE.search(str(cell or ""))
    return m.group(0) if m else ""


def _sheet_table(value) -> list[list]:
    """接受二维数组，或 Sheets API get_values 的原样返回 {"values": [[...]]}。"""
    if isinstance(value, dict):
        value = value.get("values", [])
    return value if isinstance(value, list) else []


def merge_marks(rows: list[dict], sheets: dict, allow_clear: bool = False) -> dict:
    """把三个工作表里的「我的标记」合并进 rows（就地修改）。

    规则：与 jobs.csv 现值不同者为新值；多个工作表都不同时按 已投递 > 岗位总表 > 归档 取。
    空值默认不覆盖已有标记（allow_clear=True 时才允许清空）。
    例外（恢复手势）：「归档」表中把以「不要」（dismiss 前缀）开头的标记清空或改为非「不要」文字，视为显式的恢复请求，
    默认放行写回并列入 restore（未过期的岗位随后用 `set <链接> --restore` 恢复）。
    单元格经 clean_mark 清洗：公式前缀拒收（rejected）、控制字符去除、换行变空格、超长截断。
    返回 {"changes", "unmatched", "skipped_clear", "restore", "rejected", "notes": [...], "link_without_url": bool}
    """
    by_link: dict[str, dict] = {}
    by_unit_title: dict[tuple[str, str], dict] = {}
    for r in rows:
        for key in (normalize_url(r.get("链接", "")), normalize_url(r.get("岗位ID", ""))):
            if key and key not in by_link:
                by_link[key] = r
        ut = (normalize_text(r.get("单位", "")), normalize_text(r.get("岗位", "")))
        if all(ut) and ut not in by_unit_title:
            by_unit_title[ut] = r

    order = SHEET_PRIORITY + [k for k in sheets if k not in SHEET_PRIORITY]
    proposals: dict[int, list[tuple[int, str, str, dict, bool, str]]] = {}
    result = {"changes": [], "unmatched": [], "skipped_clear": [], "restore": [], "rejected": [], "notes": [],
              "link_without_url": False}
    for prio, name in enumerate(order):
        table = _sheet_table(sheets.get(name))
        if not table:
            continue
        header = [str(h or "").strip() for h in table[0]]
        if "我的标记" not in header:
            result["notes"].append(f"工作表「{name}」没有「我的标记」列，已跳过")
            continue
        mark_col = header.index("我的标记")
        link_col = header.index("链接") if "链接" in header else None
        unit_col = header.index("单位") if "单位" in header else None
        title_col = header.index("岗位") if "岗位" in header else None
        for raw in table[1:]:
            cells = [str(c) if c is not None else "" for c in raw] + [""] * (len(header) - len(raw))
            if not any(c.strip() for c in cells):
                continue
            unit = cells[unit_col].strip() if unit_col is not None else ""
            title = cells[title_col].strip() if title_col is not None else ""
            job = None
            if link_col is not None:
                url = extract_url(cells[link_col])
                if url:
                    job = by_link.get(normalize_url(url))
                elif cells[link_col].strip():
                    result["link_without_url"] = True
            if job is None and unit and title:
                job = by_unit_title.get((normalize_text(unit), normalize_text(title)))
            if job is None:
                result["unmatched"].append(f"{name}：{unit or '?'}｜{title or '?'}")
                continue
            mark, why = clean_mark(cells[mark_col])
            old_mark = (job.get("我的标记") or "").strip()
            if mark == old_mark:
                continue
            if is_rejected_mark(why):
                result["rejected"].append(f"{name}：{job['单位']}｜{job['岗位']}：「{mark[:40]}」{why}")
                continue
            was_dismissed = dont_want_reason(old_mark) is not None
            # 「归档」表里清掉/改写「不要…」= 恢复手势，默认放行；其他表中为空仍默认不清空
            restoring = name == "归档" and was_dismissed and dont_want_reason(mark) is None
            if not mark and not allow_clear and not restoring:
                result["skipped_clear"].append(f"{name}：{job['单位']}｜{job['岗位']}（表中为空，保留「{old_mark}」）")
                continue
            proposals.setdefault(id(job), []).append((prio, name, mark, job, restoring, why or ""))
    for cands in proposals.values():
        cands.sort(key=lambda c: c[0])
        prio, name, mark, job, restoring, why = cands[0]
        old = job.get("我的标记") or ""
        job["我的标记"] = mark
        extra = f"；另有 {len(cands) - 1} 个工作表取值不同，已按优先级取「{name}」" if len(cands) > 1 else ""
        if why:
            extra += f"；{why}"
        result["changes"].append(f"{job['单位']}｜{job['岗位']}：「{old}」→「{mark}」（来自「{name}」{extra}）")
        if restoring:
            result["restore"].append(f"{job['单位']}｜{job['岗位']}：「{old}」→「{mark}」，未过期的请运行 "
                                     f"set {job.get('链接') or job.get('岗位ID') or ''} --restore")
    return result


# --------------------------------------------------------------------------- runlog / coverage

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _append_runlog_unlocked(path: str, entry: dict) -> list[dict]:
    log = load_json(path, [])
    if not isinstance(log, list):
        raise ValueError(f"{path} 不是 JSON 数组，无法追加")
    log.append(entry)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # 原子替换，读方不会看到半截文件
    return log


def append_runlog(path: str, entry: dict) -> list[dict]:
    """追加一条记录。Routine 常并行执行多个 runlog，故用文件锁串行化读-改-写（fcntl 不可用时跳过加锁），并原子替换。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if fcntl is None:  # pragma: no cover
        return _append_runlog_unlocked(path, entry)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _append_runlog_unlocked(path, entry)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def render_coverage(plan: dict | None, runlog: list[dict], today: dt.date, fetched: dict | None = None) -> str:
    """按类别汇总 计划检索数/实际/命中/未轮到（下次日期）/未能访问来源，输出 Markdown。

    fetched 为 out/fetched.json（fetch_tool 输出）；其 failures[] 并入「未能访问的来源」。
    """
    t = today.isoformat()
    entries = [e for e in runlog if isinstance(e, dict) and e.get("date") == t]
    agg: dict[str, dict] = {}
    for e in entries:
        a = agg.setdefault(str(e.get("category", "")), {"searches": 0, "hits": 0, "failed": [], "notes": []})
        a["searches"] += int(e.get("searches") or 0)
        a["hits"] += int(e.get("hits") or 0)
        for u in e.get("failed") or []:
            if u not in a["failed"]:
                a["failed"].append(u)
        if e.get("note"):
            a["notes"].append(str(e["note"]))

    lines = [f"# 检索覆盖情况（{t}）", ""]
    names: dict[str, str] = {}
    planned_total = actual_total = hits_total = 0
    if plan:
        if plan.get("today") and plan.get("today") != t:
            lines.append(f"> 注意：out/plan.json 是 {plan['today']} 生成的，与今天不同。")
            lines.append("")
        cats = [c for c in plan.get("categories", []) if isinstance(c, dict)]
        # plan_tool 把未轮到的类别放在 not_due；也兼容 categories 里 due=false 的写法
        skipped = [c for c in plan.get("not_due", []) if isinstance(c, dict)]
        skipped += [c for c in cats if not c.get("due", True)]
        due = [c for c in cats if c.get("due", True)]
        for c in cats + skipped:
            names[str(c.get("id", ""))] = str(c.get("name") or c.get("id", ""))
        lines += ["| 类别 | 计划检索 | 实际检索 | 命中 | 说明 |", "|---|---|---|---|---|"]
        for c in due:
            cid = str(c.get("id", ""))
            planned = len(c.get("queries") or []) + len(c.get("pinned") or [])  # 固定检索词也计入计划
            a = agg.get(cid)
            actual, hits = (a["searches"], a["hits"]) if a else (0, 0)
            note = "；".join(a["notes"]) if a and a["notes"] else ("" if a else "未记录 runlog")
            if a and actual < planned:
                note = merge_note(note, f"少于计划 {planned - actual} 组")
            if c.get("catch_up_from"):
                note = merge_note(note, f"补跑自 {c['catch_up_from']}")
            lines.append(f"| {names[cid]}（{cid}） | {planned} | {actual} | {hits} | {note} |")
            planned_total += planned
            actual_total += actual
            hits_total += hits
        due_ids = {str(c.get("id", "")) for c in due}
        for cid, a in agg.items():
            if cid not in due_ids:
                lines.append(f"| {names.get(cid, cid)}（{cid}，计划外） | — | {a['searches']} | {a['hits']} | "
                             f"{'；'.join(a['notes'])} |")
                actual_total += a["searches"]
                hits_total += a["hits"]
        lines += ["", f"合计：计划 {planned_total} 组，实际 {actual_total} 组，命中 {hits_total} 条。"]
        lines += ["", "## 未轮到的类别", ""]
        if skipped:
            for c in skipped:
                nxt = c.get("next_due") or "—"
                reason = f"，{c['reason']}" if c.get("reason") else ""
                lines.append(f"- {c.get('name') or c.get('id')}（{c.get('id')}）：下次 {nxt}{reason}")
        else:
            lines.append("- 无")
    else:
        lines.append("> 未找到 out/plan.json，只汇总 runlog。")
        lines += ["", "| 类别 | 实际检索 | 命中 | 说明 |", "|---|---|---|---|"]
        for cid, a in agg.items():
            lines.append(f"| {cid} | {a['searches']} | {a['hits']} | {'；'.join(a['notes'])} |")
            actual_total += a["searches"]
            hits_total += a["hits"]
        lines += ["", f"合计：实际 {actual_total} 组，命中 {hits_total} 条。"]

    failed = [(u, cid) for cid, a in agg.items() for u in a["failed"]]
    lines += ["", "## 未能访问的来源", ""]
    if failed:
        lines += [f"- {u}（{names.get(cid, cid)}）" for u, cid in failed]
    fetch_failed = [f for f in (fetched or {}).get("failures", []) if isinstance(f, dict)] if isinstance(fetched, dict) else []
    for f in fetch_failed:
        lines.append(f"- {f.get('url', '')}（脚本抓取：{f.get('name') or f.get('source', '')}；{f.get('reason', '失败')}）")
    if not failed and not fetch_failed:
        lines.append("- 无")
    if not entries:
        lines += ["", "> 今天没有 runlog 记录（每类完成后运行 `jobs_tool.py runlog --category ID --searches N --hits M`）。"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI

def _today(args) -> dt.date:
    return dt.date.fromisoformat(args.today) if args.today else today()


def cmd_validate(args) -> int:
    header, rows, info = read_jobs_detailed(args.csv)
    errors, warnings = validate(header, rows, info)
    for w in warnings:
        print("警告：" + w)
    for e in errors:
        print("错误：" + e)
    print(f"共 {len(rows)} 条记录，{len(errors)} 个错误，{len(warnings)} 个警告。")
    return 1 if errors or (args.strict and warnings) else 0


def cmd_migrate(args) -> int:
    header, rows, info = read_jobs_detailed(args.csv)
    if header != COLUMNS:
        print(f"错误：表头无法识别，不能迁移：{header}")
        return 1
    if not info.needs_migration:
        print("无需迁移：已是 17 列，且「截止日期」均为 YYYY-MM-DD 或空。")
        return 0
    errors, _ = validate(header, rows)
    if errors:
        print(f"注意：校验发现 {len(errors)} 个错误（迁移不处理，请之后运行 validate 修复）")
    soft = info.soft
    print(f"迁移 {len(rows)} 行：补列 {info.added_rows}，拆分 {info.split_rows}；软截止 {len(soft)} 条如下：")
    for s in soft:
        print(f"  第{s['行']}行 {s['单位']}｜{s['岗位']}：「{s['原值']}」→ 截止说明「{s['截止说明']}」")
    if info.splits:
        print("拆分明细（原值 → 截止日期｜截止说明）：")
        for s in info.splits:
            print(f"  第{s['行']}行 {s['单位']}｜{s['岗位']}：「{s['原值']}」→ 「{s['截止日期']}」｜「{s['截止说明']}」")
    if args.dry_run:
        print("（--dry-run，未写入文件）")
        return 0
    write_jobs(rows, args.csv)
    print(f"已写入 {args.csv}（17 列）。")
    return 0


def cmd_status(args) -> int:
    header, rows, info = read_jobs_detailed(args.csv)
    errors, _ = validate(header, rows, info)
    if errors:
        print("jobs.csv 存在结构错误，请先运行 validate 修复：", *errors, sep="\n  ")
        return 1
    today_ = _today(args)
    changes = update_status(rows, today_, read_dismissed(args.dismissed))
    counts = {s: sum(1 for r in rows if r["状态"] == s) for s in STATUSES}
    print(f"今天：{today_}；状态变化 {len(changes)} 条")
    for c in changes:
        print("  " + c)
    print("当前统计：" + "，".join(f"{k}{v}" for k, v in counts.items()))
    if info.needs_migration:
        print("jobs.csv 为旧版格式，本次写入将升级为 17 列。" if not args.dry_run else "jobs.csv 为旧版格式（--dry-run 未升级）。")
    if not args.dry_run:
        write_jobs(rows, args.csv)
    return 0


def cmd_check(args) -> int:
    _, rows = read_jobs(args.csv)
    hits = find_duplicates(rows, read_dismissed(args.dismissed), args.url or "", args.unit or "", args.title or "")
    if hits:
        print("可能重复：", *hits, sep="\n  ")
        return 2
    print("未发现重复，可作为新增。")
    return 0


def cmd_add(args) -> int:
    header, rows = read_jobs(args.csv)
    today_ = _today(args)
    if args.level not in LEVELS:
        print(f"匹配等级须为 {LEVELS} 之一")
        return 1
    hits = find_duplicates(rows, read_dismissed(args.dismissed), args.link, args.unit, args.title)
    if hits and not args.force:
        print("拒绝追加，可能重复（确认不是同一岗位可加 --force）：", *hits, sep="\n  ")
        return 2
    raw_deadline = (args.deadline or "").strip()
    if raw_deadline and not is_iso_date(raw_deadline):
        date, note = split_deadline(raw_deadline)
        print(f"提示：--deadline「{raw_deadline}」不是 YYYY-MM-DD，已拆分为 截止日期「{date}」+ 截止说明「{note}」")
    else:
        date, note = raw_deadline, ""
    note = merge_note(note, args.deadline_note or "")
    if not date and not note:
        note = "需核实"
    d = parse_date(date)
    if d is not None and d < today_:
        print(f"截止日期 {d} 已过，按规则不推送。")
        return 1
    row = {c: "" for c in COLUMNS}
    row.update({
        "岗位ID": args.link.strip() or f"{args.unit}+{args.title}",
        "首次推送日期": today_.isoformat(),
        "匹配等级": args.level,
        "单位": args.unit, "岗位": args.title, "城市": args.city, "链接": args.link.strip(),
        "截止日期": date, "截止说明": note, "状态": "新增", "状态更新日期": today_.isoformat(),
        "备注": args.note or "",
    })
    rows.append(row)
    errors, _ = validate(header, rows)
    if errors:
        print("追加后校验失败，未写入：", *errors, sep="\n  ")
        return 1
    write_jobs(rows, args.csv)
    print(f"已追加：{args.level}｜{args.unit}｜{args.title}（截止 {deadline_text(row) or '—'}）")
    return 0


def cmd_marks(args) -> int:
    header, rows, info = read_jobs_detailed(args.csv)
    try:
        sheets = load_json(args.from_file, None)
    except (OSError, ValueError) as exc:
        print(f"错误：无法读取 {args.from_file}：{exc}")
        return 1
    if not isinstance(sheets, dict):
        print("错误：文件格式应为 JSON 对象 {\"岗位总表\": [[...]], \"已投递\": [[...]], \"归档\": [[...]]}，首行为表头")
        return 1
    result = merge_marks(rows, sheets, allow_clear=args.allow_clear)
    for n in result["notes"]:
        print("注意：" + n)
    print(f"「我的标记」变更 {len(result['changes'])} 条：")
    for c in result["changes"]:
        print("  " + c)
    if result["restore"]:
        print(f"待恢复（{len(result['restore'])} 条）：「归档」表中的「不要」标记被清除或改写，已写回标记；"
              "status 不会改动归档行，请对未过期的岗位运行 set <链接> --restore：")
        for s in result["restore"]:
            print("  " + s)
    if result["rejected"]:
        print(f"已拒收（{len(result['rejected'])} 条，需人工核对）：")
        for s in result["rejected"]:
            print("  " + s)
    if result["skipped_clear"]:
        print(f"表中为空而 jobs.csv 有值的 {len(result['skipped_clear'])} 条未清空（确需清空请加 --allow-clear）：")
        for s in result["skipped_clear"]:
            print("  " + s)
    if result["unmatched"]:
        print(f"未能对应到 jobs.csv 的 {len(result['unmatched'])} 行（可能已删除或单位/岗位名被改动）：")
        for u in result["unmatched"][:50]:
            print("  " + u)
    if result["link_without_url"]:
        print("说明：链接列是「原文」富文本，get_values 取不到网址（任何 valueRenderOption 都一样），已按「单位+岗位」匹配；"
              "请勿改动表中「单位」「岗位」两列文字。如需按网址匹配，可用 get_spreadsheet(includeGridData=true, "
              "fields=sheets.data.rowData.values.hyperlink) 读出网址后填入链接列再运行。")
    if args.dry_run:
        print("（--dry-run，未写入文件）")
        return 0
    if result["changes"] or info.needs_migration:
        write_jobs(rows, args.csv)
        print(f"已写入 {args.csv}。")
    else:
        print("无变更，未写入。")
    return 0


def find_job(rows: list[dict], url: str = "", unit: str = "", title: str = "") -> list[dict]:
    """按链接（或岗位ID）精确定位岗位；没有链接时按「单位+岗位」（忽略空白与标点）定位。返回全部命中行。"""
    nu = normalize_url(url)
    if nu:
        return [r for r in rows if nu in (normalize_url(r.get("链接", "")), normalize_url(r.get("岗位ID", "")))]
    if unit and title:
        key = (normalize_text(unit), normalize_text(title))
        return [r for r in rows if (normalize_text(r.get("单位", "")), normalize_text(r.get("岗位", ""))) == key]
    return []


def cmd_set(args) -> int:
    header, rows, info = read_jobs_detailed(args.csv)
    today_ = _today(args)
    t = today_.isoformat()
    if not (args.url or (args.unit and args.title)):
        print("错误：请给出 <链接>，或同时给出 --unit 与 --title 定位岗位")
        return 1
    edits = [args.mark is not None, args.apply_state is not None, args.apply_date is not None,
             bool(args.append_note), args.archive is not None, args.restore]
    if not any(edits):
        print("错误：至少给出一个修改项（--mark / --apply-state / --apply-date / --append-note / --archive / --restore）")
        return 1
    if args.archive is not None and args.restore:
        print("错误：--archive 与 --restore 不能同时使用")
        return 1
    if args.archive is not None and not args.archive.strip():
        print("错误：--archive 需要给出归档原因")
        return 1
    if args.apply_state is not None and args.apply_state not in APPLY_STATES[1:]:
        print(f"错误：--apply-state 须为 {'|'.join(APPLY_STATES[1:])} 之一")
        return 1
    if args.apply_date is not None and not is_iso_date(args.apply_date):
        print(f"错误：--apply-date「{args.apply_date}」不是 YYYY-MM-DD")
        return 1
    if args.mark is not None:
        mark, why = clean_mark(args.mark)
        if is_rejected_mark(why):
            print(f"错误：--mark {why}")
            return 1
        if why:
            print(f"提示：--mark {why}")
        args.mark = mark

    hits = find_job(rows, args.url or "", args.unit or "", args.title or "")
    if not hits:
        print(f"错误：jobs.csv 中找不到该岗位：{args.url or (args.unit + '｜' + args.title)}")
        return 1
    job = hits[0]
    if len(hits) > 1:
        print(f"注意：有 {len(hits)} 条岗位匹配，已取第一条：{job['单位']}｜{job['岗位']}（{job['链接']}）")
    label = f"{job['单位']}｜{job['岗位']}"

    changes: list[tuple[str, str, str]] = []  # (字段, 旧, 新)

    def put(col: str, new: str) -> None:
        old = job.get(col) or ""
        if old != new:
            changes.append((col, old, new))
            job[col] = new

    if args.restore:
        if job["状态"] != "归档":
            print(f"错误：{label} 当前状态为「{job['状态']}」而非归档，无需恢复")
            return 1
        left = days_left(job, today_)
        if left is not None and left < 0:
            print(f"错误：{label} 截止日期 {job['截止日期']} 已过（{-left} 天），不能恢复；过期岗位不回到岗位总表")
            return 1
        old_reason = (job.get("归档原因") or "").strip() or "未记录"
        put("状态", "持续")
        put("归档日期", "")
        put("归档原因", "")
        put("备注", merge_note(job.get("备注") or "", f"{t} 从归档恢复（原因：{old_reason}）"))
    if args.archive is not None:
        put("状态", "归档")
        put("归档日期", t)
        put("归档原因", args.archive.strip())
    if args.mark is not None:
        put("我的标记", args.mark)
    if args.apply_state is not None:
        first_time = not (job.get("投递状态") or "")
        put("投递状态", args.apply_state)
        if args.apply_date is None and first_time and not (job.get("投递日期") or ""):
            put("投递日期", t)
    if args.apply_date is not None:
        put("投递日期", args.apply_date)
    if args.append_note:
        put("备注", merge_note(job.get("备注") or "", args.append_note))
    if any(col == "状态" for col, _, _ in changes):
        put("状态更新日期", t)

    if not changes:
        print(f"无变更：{label} 各字段已是目标值。")
        return 0
    desc = "；".join(f"{col} 「{old}」→「{new}」" for col, old, new in changes)
    print(f"已更新：{label}：{desc}")
    if args.restore:
        print("提示：恢复后请运行 status，让脚本按截止日期把状态定为 持续/预警/新增。")
    if dont_want_reason(job.get("我的标记", "")) is not None and job["状态"] != "归档":
        print(f"提示：「我的标记」仍以 {dismiss_prefixes()} 开头，下次 status 会再次归档；如要保留请同时用 --mark 改掉。")
    errors, _ = validate(header, rows)
    if errors:
        print("修改后校验失败，未写入：", *errors, sep="\n  ")
        return 1
    if args.dry_run:
        print("（--dry-run，未写入文件）")
        return 0
    write_jobs(rows, args.csv)
    print(f"已写入 {args.csv}。")
    return 0


def cmd_runlog(args) -> int:
    if args.searches < 0 or args.hits < 0:
        print("错误：--searches 与 --hits 不能为负数")
        return 1
    today_ = _today(args)
    entry = {"date": today_.isoformat(), "category": args.category, "searches": args.searches, "hits": args.hits,
             "failed": list(args.failed or []), "note": args.note or "",
             "recorded_at": dt.datetime.now().isoformat(timespec="seconds")}
    path = os.path.join(args.out, "runlog.json")
    try:
        log = append_runlog(path, entry)
    except ValueError as exc:
        print(f"错误：{exc}")
        return 1
    todays = [e for e in log if isinstance(e, dict) and e.get("date") == entry["date"]]
    print(f"已记录：{args.category} 检索 {args.searches} 组，命中 {args.hits} 条"
          f"{'，失败来源 ' + str(len(entry['failed'])) + ' 个' if entry['failed'] else ''}；"
          f"今天累计 {len(todays)} 条记录 → {path}")
    return 0


def cmd_report(args) -> int:
    _, rows = read_jobs(args.csv)
    today_ = _today(args)
    os.makedirs(args.out, exist_ok=True)
    html_text = render_html(rows, today_)
    with open(os.path.join(args.out, "summary.html"), "w", encoding="utf-8") as f:
        f.write(html_text)
    with open(os.path.join(args.out, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(render_text(rows, today_))
    with open(os.path.join(args.out, "sheets.json"), "w", encoding="utf-8") as f:
        json.dump(sheets_payload(rows, today_), f, ensure_ascii=False, indent=1)
    n = len(summary_rows(rows, today_))
    print(f"已生成 {args.out}/summary.html、summary.txt（岗位总表 {n} 行）和 sheets.json（三个工作表数据）")
    size = len(html_text.encode("utf-8"))
    compact_path = os.path.join(args.out, "summary_compact.html")
    if size > HTML_SIZE_LIMIT:
        with open(compact_path, "w", encoding="utf-8") as f:
            f.write(render_html(rows, today_, compact=True))
        shown = os.path.relpath(compact_path, ROOT) if os.path.abspath(compact_path).startswith(ROOT + os.sep) else compact_path
        print(f"summary.html 约 {round(size / 1024)} KB，超过 {HTML_SIZE_LIMIT // 1000} KB 安全阈值（Gmail 约 {GMAIL_CLIP_KB} KB 起裁剪正文），"
              f"请用 {shown} 作为 htmlBody（持续行只给计数并指向在线表格，其余状态照常列出）")
    elif os.path.exists(compact_path):
        os.remove(compact_path)  # 上次的精简版已过时，避免 Routine 误用
    plan_path = os.path.join(args.out, "plan.json")
    runlog_path = os.path.join(args.out, "runlog.json")
    if os.path.exists(plan_path) or os.path.exists(runlog_path):
        try:
            plan = load_json(plan_path, None)
            runlog = load_json(runlog_path, [])
            if not isinstance(runlog, list):
                runlog = []
        except ValueError as exc:
            print(f"警告：plan.json / runlog.json 解析失败，未生成 coverage.md：{exc}")
            return 0
        with open(os.path.join(args.out, "coverage.md"), "w", encoding="utf-8") as f:
            fetched = load_json(os.path.join(args.out, "fetched.json"), None)
            f.write(render_coverage(plan if isinstance(plan, dict) else None, runlog, today_,
                                    fetched if isinstance(fetched, dict) else None))
        print(f"已生成 {args.out}/coverage.md（检索覆盖情况"
              f"{'' if os.path.exists(plan_path) else '，未找到 plan.json，仅按 runlog 汇总'}）")
    return 0


def _common_options(p: argparse.ArgumentParser, suppress: bool) -> None:
    """公共选项既可写在子命令前也可写在子命令后（子命令上的版本用 SUPPRESS，以免 None 覆盖顶层值）。"""
    S = argparse.SUPPRESS
    p.add_argument("--csv", default=S if suppress else JOBS_CSV, help="jobs.csv 路径")
    p.add_argument("--dismissed", default=S if suppress else DISMISSED_CSV, help="dismissed.csv 路径")
    p.add_argument("--profile", default=S if suppress else None,
                   help="profile.toml 路径（默认 config/profile.toml；提供时区与标记前缀）")
    p.add_argument("--today", default=S if suppress else None, help="覆盖今天日期 YYYY-MM-DD（默认 profile 时区当天）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="jobs.csv 辅助工具（17 列；旧版 16 列自动升级）")
    _common_options(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="校验 jobs.csv（旧版 16 列表头只警告）")
    v.add_argument("--strict", action="store_true", help="有警告也返回非零（含旧表头警告）")
    v.set_defaults(func=cmd_validate)

    m = sub.add_parser("migrate", help="把旧版 16 列 jobs.csv 升级为 17 列并拆分「截止日期」；幂等")
    m.add_argument("--dry-run", action="store_true", help="只打印迁移清单，不写文件")
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser("status", help="按日期规则重新判定所有未归档岗位的状态")
    s.add_argument("--dry-run", action="store_true", help="只打印变化，不写文件")
    s.set_defaults(func=cmd_status)

    c = sub.add_parser("check", help="查询岗位是否已推送或被标记不要")
    c.add_argument("url", nargs="?", default="")
    c.add_argument("--unit")
    c.add_argument("--title")
    c.set_defaults(func=cmd_check)

    e = sub.add_parser("set", help="修改一条已有岗位：标记、投递状态/日期、追加备注、归档或从归档恢复")
    e.add_argument("url", nargs="?", default="", help="岗位链接（或岗位ID）；也可用 --unit + --title 定位")
    e.add_argument("--unit", help="与 --title 一起按「单位+岗位」定位")
    e.add_argument("--title")
    e.add_argument("--mark", help="写入「我的标记」")
    e.add_argument("--apply-state", help="投递状态：已投递|进行中|进展中|已结束")
    e.add_argument("--apply-date", help="投递日期 YYYY-MM-DD（省略且 --apply-state 首次非空时填今天）")
    e.add_argument("--append-note", help="追加到「备注」，用「；」连接")
    e.add_argument("--archive", metavar="原因", help="立即归档，归档日期=今天")
    e.add_argument("--restore", action="store_true",
                   help="从归档恢复（须为归档且截止未过）：状态改为持续、清空归档日期与原因、备注追加恢复记录；随后应运行 status")
    e.add_argument("--dry-run", action="store_true", help="只打印变更，不写文件")
    e.set_defaults(func=cmd_set)

    a = sub.add_parser("add", help="追加一条新岗位（自动去重、正确转义）")
    a.add_argument("--unit", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--city", default="需核实")
    a.add_argument("--link", required=True)
    a.add_argument("--deadline", default="", help="YYYY-MM-DD；传入其他文字时自动拆分到截止说明")
    a.add_argument("--deadline-note", default="", help="截止说明：需核实 / 长期 / Rolling / 招满即止 / 软截止 + 余文")
    a.add_argument("--level", required=True, help="⭐高匹配 / 可考虑 / 仅供参考")
    a.add_argument("--note", default="")
    a.add_argument("--force", action="store_true", help="忽略疑似重复")
    a.set_defaults(func=cmd_add)

    k = sub.add_parser("marks", help="把在线表格读回的「我的标记」合并进 jobs.csv")
    k.add_argument("--from", dest="from_file", required=True,
                   help='JSON 文件：{"岗位总表": [[...]], "已投递": [[...]], "归档": [[...]]}，首行为表头')
    k.add_argument("--allow-clear", action="store_true", help="允许用表中的空值清空 jobs.csv 已有标记")
    k.add_argument("--dry-run", action="store_true", help="只打印变更，不写文件")
    k.set_defaults(func=cmd_marks)

    g = sub.add_parser("runlog", help="记录一个类别的检索情况到 out/runlog.json")
    g.add_argument("--category", required=True, help="sources.toml 中的类别 id")
    g.add_argument("--searches", type=int, required=True, help="实际检索组数")
    g.add_argument("--hits", type=int, required=True, help="新增命中岗位数")
    g.add_argument("--failed", nargs="*", default=[], help="未能访问的来源 URL")
    g.add_argument("--note", default="", help="附注（计划外检索、异常等）")
    g.add_argument("--out", default=OUT_DIR, help="输出目录（默认 out/）")
    g.set_defaults(func=cmd_runlog)

    r = sub.add_parser("report", help="生成邮件岗位总表、Sheets 数据与 coverage.md（体积超限时另生成 summary_compact.html）")
    r.add_argument("--out", default=OUT_DIR)
    r.set_defaults(func=cmd_report)

    for x in (v, m, s, c, e, a, k, g, r):
        _common_options(x, suppress=True)
    return p


def main(argv: list[str] | None = None) -> int:
    global PROFILE_PATH
    args = build_parser().parse_args(argv)
    if getattr(args, "profile", None):
        PROFILE_PATH = args.profile
        set_profile(None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
