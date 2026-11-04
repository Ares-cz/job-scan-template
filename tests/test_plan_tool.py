"""tools/plan_tool.py 的单元测试：python3 -m unittest tests.test_plan_tool

调度、关键词轮询、预算裁剪、补跑、负面清单与 brief 结构全部用固定日期与虚构配置断言；
最后几条用仓库真实 config 做烟雾测试（离线）。不含任何真实个人信息。
"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import config as cfg  # noqa: E402
import plan_tool as plt  # noqa: E402
import profile_tool as pt  # noqa: E402

EPOCH = dt.date(2026, 11, 10)

FAKE_PROFILE = """
schema = 1
[identity]
name = "测试人"
email = "t@example.com"
timezone = "Europe/Amsterdam"
language = "zh"
birth_month = 1994-06-01
[[education]]
level = "phd"
institution = "某大学"
defense_date = 2026-11-16
[status]
cohort_system = "cn"
cohort_year = 2027
age_thresholds = [35]
[research]
[[research.topics]]
id = "t1"
weight = 3
terms = { zh = ["主题甲"], en = ["topic A"] }
[[research.topics]]
id = "t2"
weight = 1
terms = { zh = ["主题乙"], en = ["topic B"] }
[[target.domains]]
id = "d1"
weight = 2
terms = { zh = ["方向一"], en = ["domain one"] }
[[target.domains]]
id = "d2"
weight = 3
terms = { zh = ["方向二"], en = ["domain two"] }
[[target.domains]]
id = "d3"
weight = 1
terms = { zh = ["方向三"], en = ["domain three"] }
[target]
roles = ["postdoc", "soe_campus", "civil_service"]
role_terms = { zh = ["博士后"], en = ["postdoc"] }
avoid = { zh = ["不想要的方向"], en = ["unwanted direction"] }
hard_requirements = ["门槛一", "门槛二"]
[matching]
high = "高"
consider = "中"
reference = "低"
[locations]
cities = ["甲城", "乙城", "丙城", "丁城", "戊城"]
top_cities = 3
provinces = ["甲省"]
[[locations.countries]]
name = { zh = "乙国", en = "Country B" }
tier = 2
[[locations.countries]]
name = { zh = "甲国", en = "Country A" }
tier = 1
[[locations.special_zones]]
id = "zone1"
name = "专区一"
"""

FAKE_SOURCES = """
schema = 1
[schedule]
epoch = 2026-11-10
max_queries_per_day = 6
min_queries_per_day = 2
weekend_factor = 0.5

[[category]]
id = "daily"
name = "每日类"
lang = "zh"
requires_roles = ["postdoc"]
every = 1
queries_per_turn = 2
weight = 3
query_templates = ["{topic} 博士后 {city}", "{domain} 招聘 {year}", "{unit} 博士后", "{role} {province}"]
units = ["单位A", "单位B", "单位C"]
browse = ["https://example.org/a"]

[[category]]
id = "every2"
name = "隔天类"
lang = "en"
requires_roles = ["postdoc"]
every = 2
offset = 1
queries_per_turn = 2
weight = 2
query_templates = ["{topic} postdoc {country}", "{domain} {year_next}"]
pinned = ["fixed query"]

[[category]]
id = "cohort_cat"
name = "届别类"
lang = "zh"
requires_roles = ["soe_campus"]
every = 1
queries_per_turn = 2
weight = 1
query_templates = ["{cohort}届 校招 {domain}", "{zone} 招聘"]
special_zone = "zone1"

[[category]]
id = "weekday_cat"
name = "工作日类"
requires_roles = ["civil_service"]
every = 1
weekdays = [1, 2, 3, 4, 5]
queries_per_turn = 1
weight = 1
query_templates = ["{degree} {city}"]

[[category]]
id = "season_cat"
name = "季节类"
requires_roles = ["civil_service"]
every = 7
offset = 0
season = { months = [11], every = 1 }
active_months = [11, 12]
queries_per_turn = 1
weight = 1
query_templates = ["{topic}"]

[[category]]
id = "closed_cat"
name = "关闭类"
requires_roles = ["talent_program"]
every = 1
query_templates = ["{topic}"]

[[source]]
id = "src_html"
category = "daily"
url = "https://example.org/list"
fetch = "html_list"
link_pattern = "/job/"

[[source]]
id = "src_browse"
category = "daily"
url = "https://example.org/browse"
pages = ["https://example.org/browse2"]
fetch = "browse"
"""

FAKE_RULES = """# 规律

- 规律甲（样本多）——依据 3 条（2026-10-19 更新）。已达 3 条：直接不推送。
- 规律乙（样本少）——依据 1 条。仅作降级参考。
- 规律丙（样本很多）——依据 4 条（2026-10-23 更新）。
- 规律丁（两条）——依据 2 条；再有 1 条即直接排除。

## 「我的标记」含义与处理规则

- 依据 9 条 这行在表格节之后，不应被解析
"""

# 「依据 N 条」的写法变体：单个破折号带空格、括号、句号、冒号、* / + 列表符、「各依据」
FAKE_RULES_VARIANTS = """# 规律

- 规律甲 — 依据3条
- 规律乙（依据 5 条）
- 规律丙。依据4条
* 规律丁——依据 3 条
+ 规律戊：依据 3 条
- 规律己、规律庚——各依据 3 条
- 规律辛——依据 2 条
不是列表项 依据 7 条
"""


def load_fake():
    profile = cfg.apply_profile_defaults(tomllib.loads(FAKE_PROFILE))
    sources = cfg.apply_sources_defaults(tomllib.loads(FAKE_SOURCES))
    errs, _ = cfg.validate_profile(profile)
    assert not errs, errs
    errs, _ = cfg.validate_sources(sources, profile)
    assert not errs, errs
    return profile, sources


def cat(sources, cid):
    return next(c for c in sources["category"] if c["id"] == cid)


def write_jobs_csv(path, dates, columns=16):
    header = ["岗位ID", "首次推送日期", "匹配等级", "单位", "岗位", "城市", "链接", "截止日期"]
    if columns == 17:
        header.append("截止说明")
    header += ["状态", "状态更新日期", "我的标记", "归档日期", "归档原因", "备注", "投递状态", "投递日期"]
    col = header.index("状态更新日期")
    lines = [",".join(header)]
    for d in dates:
        row = [""] * len(header)
        row[0] = "https://x.example/" + d.replace("-", "")
        row[col] = d
        lines.append(",".join(row))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.profile, self.sources = load_fake()

    def test_day_index_and_every_eff(self):
        self.assertEqual(plt.day_index(dt.date(2026, 11, 12), EPOCH), 2)
        self.assertEqual(plt.day_index(dt.date(2026, 11, 5), EPOCH), -5)
        c = cat(self.sources, "season_cat")
        self.assertEqual(plt.every_eff(c, dt.date(2026, 11, 3)), 1)     # 旺季
        self.assertEqual(plt.every_eff(c, dt.date(2026, 12, 3)), 7)     # active 但非旺季 → every
        self.assertEqual(plt.every_eff(c, dt.date(2027, 1, 3)), 7)      # 淡季 → 7
        self.assertEqual(plt.every_eff(cat(self.sources, "daily"), dt.date(2027, 1, 3)), 1)

    def test_due_with_offset(self):
        c = cat(self.sources, "every2")   # every 2, offset 1 → 奇数 day
        self.assertTrue(plt.is_due(c, EPOCH + dt.timedelta(days=1), EPOCH, self.profile))
        self.assertFalse(plt.is_due(c, EPOCH, EPOCH, self.profile))
        self.assertTrue(plt.is_due(c, EPOCH - dt.timedelta(days=1), EPOCH, self.profile))  # 负 day 也正确

    def test_due_weekdays(self):
        c = cat(self.sources, "weekday_cat")
        self.assertTrue(plt.is_due(c, dt.date(2026, 11, 13), EPOCH, self.profile))   # 周五
        self.assertFalse(plt.is_due(c, dt.date(2026, 11, 14), EPOCH, self.profile))  # 周六
        self.assertEqual(plt.next_due(c, dt.date(2026, 11, 13), EPOCH, self.profile), dt.date(2026, 11, 16))

    def test_due_season_and_active_months(self):
        c = cat(self.sources, "season_cat")
        self.assertTrue(plt.is_due(c, dt.date(2026, 11, 13), EPOCH, self.profile))   # 旺季每天
        self.assertFalse(plt.is_due(c, dt.date(2026, 12, 11), EPOCH, self.profile))  # day=31, every 7
        self.assertTrue(plt.is_due(c, dt.date(2026, 12, 15), EPOCH, self.profile))   # day=35
        self.assertFalse(plt.is_due(c, dt.date(2027, 1, 14), EPOCH, self.profile))   # day=65 淡季 every 7
        self.assertTrue(plt.is_due(c, dt.date(2027, 1, 12), EPOCH, self.profile))    # day=63

    def test_closed_category_never_due(self):
        c = cat(self.sources, "closed_cat")
        self.assertFalse(plt.is_due(c, EPOCH, EPOCH, self.profile))
        self.assertIsNone(plt.next_due(c, EPOCH, EPOCH, self.profile))
        plan = plt.build_plan(self.profile, self.sources, EPOCH, jobs_csv=None, rules_path=None, fetched_md="")
        closed = next(n for n in plan["not_due"] if n["id"] == "closed_cat")
        self.assertFalse(closed["active"])
        self.assertIn("已关闭", closed["reason"])

    def test_weekend_factor(self):
        c = cat(self.sources, "daily")
        self.assertEqual(plt.queries_for_day(c, self.sources["schedule"], dt.date(2026, 11, 13)), 2)
        self.assertEqual(plt.queries_for_day(c, self.sources["schedule"], dt.date(2026, 11, 14)), 1)
        self.assertEqual(plt.queries_for_day(cat(self.sources, "weekday_cat"), self.sources["schedule"], dt.date(2026, 11, 15)), 1)


class KeywordTest(unittest.TestCase):
    def setUp(self):
        self.profile, self.sources = load_fake()

    def test_slot_candidates_ordering(self):
        c = cat(self.sources, "daily")
        cand = plt.slot_candidates(c, self.profile, dt.date(2026, 11, 12), ["单位A"])
        self.assertEqual(cand["topic"], ["主题甲", "主题乙"])      # weight 3 在前
        self.assertEqual(cand["domain"], ["方向二", "方向一", "方向三"])   # weight 3 在前
        self.assertEqual(cand["city"], ["甲城", "乙城", "丙城"])          # top_cities = 3
        self.assertEqual(cand["country"], ["甲国", "乙国"])        # tier 1 在前
        self.assertEqual(cand["cohort"], ["2027"])
        self.assertEqual(cand["degree"], ["博士"])
        self.assertEqual(cand["year"], ["2026"])
        cand_en = plt.slot_candidates(cat(self.sources, "every2"), self.profile, dt.date(2026, 11, 12), [])
        self.assertEqual(cand_en["topic"], ["topic A", "topic B"])
        self.assertEqual(cand_en["country"], ["Country A", "Country B"])
        self.assertEqual(cand_en["degree"], ["PhD"])

    def test_fill_template_skips_empty_slot(self):
        self.assertIsNone(plt.fill_template("{cohort} x", {"cohort": []}, 0))
        self.assertEqual(plt.fill_template("{a}  {b}", {"a": ["1", "2"], "b": ["x"]}, 3), "2 x")

    def test_rotation_covers_all_templates_without_repeat(self):
        """every=1, q=2, 4 个模板：连续两天各取 2 个不同模板，合起来覆盖全部 4 个；且连续几天的关键词不重复。"""
        c = cat(self.sources, "daily")
        seen_days = []
        for i in range(4):
            d = EPOCH + dt.timedelta(days=i)
            exp = plt.expand_category(c, self.profile, self.sources, d)
            self.assertEqual(len(exp["queries"]), 2)
            seen_days.append(exp["queries"])
        self.assertEqual(len(set(seen_days[0] + seen_days[1])), 4)
        self.assertEqual(len(set(seen_days[0] + seen_days[1] + seen_days[2])), 6)
        # 第 0 天起点 0：模板 0、1；第 1 天起点 2：模板 2、3
        self.assertTrue(seen_days[0][0].endswith(("甲城", "乙城", "丙城")))
        self.assertIn("招聘 2026", seen_days[0][1])
        self.assertIn("博士后", seen_days[1][0])

    def test_rotation_is_deterministic_and_uses_slot_diagonal(self):
        c = cat(self.sources, "daily")
        d = EPOCH + dt.timedelta(days=2)
        a = plt.expand_category(c, self.profile, self.sources, d)
        b = plt.expand_category(c, self.profile, self.sources, d)
        self.assertEqual(a, b)
        # day=0 与 day=2 都取模板 0、1，但槽位第 (day+idx) 个不同 → 关键词不同
        a0 = plt.expand_category(c, self.profile, self.sources, EPOCH)["queries"]
        self.assertEqual(a0[0], "主题甲 博士后 甲城")
        self.assertEqual(a0[1], "方向一 招聘 2026")               # idx 1 → domain[(0+1)%3]
        self.assertEqual(a["queries"][0], "主题甲 博士后 丙城")    # topic[(2+0)%2], city[(2+0)%3]
        self.assertEqual(a["queries"][1], "方向二 招聘 2026")      # domain[(2+1)%3]
        a4 = plt.expand_category(c, self.profile, self.sources, EPOCH + dt.timedelta(days=1))["queries"]
        self.assertNotEqual(a0, a4)

    def test_units_window_rolls(self):
        self.assertEqual(plt.units_window(["A", "B", "C"], 0, 2), ["A", "B"])
        self.assertEqual(plt.units_window(["A", "B", "C"], 1, 2), ["C", "A"])
        self.assertEqual(plt.units_window(["A", "B", "C"], 2, 2), ["B", "C"])
        self.assertEqual(plt.units_window(["A", "B"], 5, 3), ["A", "B"])
        self.assertEqual(plt.units_window([], 1, 2), [])
        c = cat(self.sources, "daily")
        exp = plt.expand_category(c, self.profile, self.sources, EPOCH + dt.timedelta(days=1))
        self.assertEqual(exp["units"], ["单位C", "单位A"])
        self.assertTrue(any(q.startswith(("单位C", "单位A")) for q in exp["queries"]))

    def test_pinned_first_and_not_counted(self):
        c = cat(self.sources, "every2")
        exp = plt.expand_category(c, self.profile, self.sources, EPOCH + dt.timedelta(days=1))
        self.assertEqual(exp["pinned"], ["fixed query"])
        self.assertEqual(len(exp["queries"]), 2)
        self.assertIn("topic", exp["queries"][0] + exp["queries"][1])

    def test_cohort_none_skips_template_and_zone_filled(self):
        c = cat(self.sources, "cohort_cat")
        exp = plt.expand_category(c, self.profile, self.sources, EPOCH)
        self.assertEqual(exp["queries"], ["2027届 校招 方向二", "专区一 招聘"])
        p2 = cfg.apply_profile_defaults(tomllib.loads(FAKE_PROFILE.replace('cohort_system = "cn"', 'cohort_system = "none"')))
        exp2 = plt.expand_category(c, p2, self.sources, EPOCH)
        self.assertEqual(exp2["queries"], ["专区一 招聘"])
        self.assertEqual(exp2["skipped_templates"], ["{cohort}届 校招 {domain}"])

    def test_skipped_pinned_recorded(self):
        """固定检索词含无候选槽位（如 cohort_system = none 下的 {cohort}）时不再静默丢弃，而是记入 skipped_templates。"""
        c = dict(cat(self.sources, "cohort_cat"), pinned=["{cohort}届 固定", "{zone} 固定"])
        p2 = cfg.apply_profile_defaults(tomllib.loads(FAKE_PROFILE.replace('cohort_system = "cn"', 'cohort_system = "none"')))
        exp = plt.expand_category(c, p2, self.sources, EPOCH)
        self.assertEqual(exp["pinned"], ["专区一 固定"])
        self.assertEqual(exp["skipped_templates"], ["{cohort}届 校招 {domain}", "{cohort}届 固定"])
        exp_cn = plt.expand_category(c, self.profile, self.sources, EPOCH)
        self.assertEqual(exp_cn["pinned"], ["2027届 固定", "专区一 固定"])
        self.assertEqual(exp_cn["skipped_templates"], [])


class BudgetTest(unittest.TestCase):
    def test_cut_lowest_weight_first(self):
        entries = [
            {"name": "高", "weight": 3, "queries": ["a1", "a2", "a3"], "pinned": ["p"]},
            {"name": "低", "weight": 1, "queries": ["b1", "b2", "b3"], "pinned": []},
            {"name": "中", "weight": 2, "queries": ["c1", "c2"], "pinned": []},
        ]
        notes = plt.apply_budget(entries, 6)   # 9 → 6：低 3→1，再中 2→1
        self.assertEqual([len(e["queries"]) for e in entries], [3, 1, 1])
        self.assertEqual(entries[0]["pinned"], ["p"])
        self.assertTrue(any("低" in n for n in notes))

    def test_cut_keeps_one_per_category_and_pinned(self):
        entries = [{"name": "x", "weight": 1, "queries": ["a"], "pinned": ["p1", "p2"]},
                   {"name": "y", "weight": 1, "queries": ["b", "c"], "pinned": []}]
        notes = plt.apply_budget(entries, 1)
        self.assertEqual([len(e["queries"]) for e in entries], [1, 1])
        self.assertTrue(any("无法再砍" in n for n in notes))

    def test_no_cut_when_under_limit(self):
        entries = [{"name": "x", "weight": 1, "queries": ["a", "b"], "pinned": []}]
        self.assertEqual(plt.apply_budget(entries, 2), [])
        self.assertEqual(len(entries[0]["queries"]), 2)

    def test_plan_respects_max(self):
        profile, sources = load_fake()
        # day=1（2026-11-11 周三）：daily 2 + every2 3 + cohort 2 + weekday 1 + season 1 = 9 > 6
        plan = plt.build_plan(profile, sources, EPOCH + dt.timedelta(days=1), jobs_csv=None, rules_path=None, fetched_md="")
        self.assertEqual(plan["budget"]["planned"], 6)
        self.assertEqual(plan["budget"]["max"], 6)
        self.assertTrue(plan["budget"]["notes"])
        for e in plan["categories"]:
            self.assertGreaterEqual(len(e["queries"]), 1)


class CatchUpTest(unittest.TestCase):
    def setUp(self):
        self.profile, self.sources = load_fake()
        self.tmp = tempfile.mkdtemp()
        self.jobs = os.path.join(self.tmp, "jobs.csv")

    def test_last_run_date_tolerates_16_and_17_columns(self):
        write_jobs_csv(self.jobs, ["2026-11-01", "2026-11-03", "坏日期", ""], columns=16)
        self.assertEqual(plt.last_run_date(self.jobs), dt.date(2026, 11, 3))
        write_jobs_csv(self.jobs, ["2026-10-30", "2026-11-02"], columns=17)
        self.assertEqual(plt.last_run_date(self.jobs), dt.date(2026, 11, 2))
        self.assertIsNone(plt.last_run_date(os.path.join(self.tmp, "missing.csv")))
        with open(self.jobs, "w", encoding="utf-8") as f:
            f.write("a,b\r\n1,2\r\n")
        self.assertIsNone(plt.last_run_date(self.jobs))

    def test_catch_up_union(self):
        today = EPOCH + dt.timedelta(days=4)            # 2026-11-14 周六，day=4
        write_jobs_csv(self.jobs, ["2026-11-11"])        # 上次运行 day=1 → 漏掉 day 2、3
        plan = plt.build_plan(self.profile, self.sources, today, jobs_csv=self.jobs, rules_path=None, fetched_md="")
        self.assertEqual(plan["catch_up_days"], 2)
        self.assertEqual(plan["catch_up_dates"], ["2026-11-12", "2026-11-13"])
        ids = {e["id"]: e for e in plan["categories"]}
        # every2（奇数 day）今天 day=4 不轮到，但 day=3 轮到 → 补跑并入
        self.assertIn("every2", ids)
        self.assertFalse(ids["every2"]["due_today"])
        self.assertEqual(ids["every2"]["catch_up_from"], "2026-11-13")
        # weekday_cat 周六不轮到，但周四/五轮到 → 并入
        self.assertIn("weekday_cat", ids)
        # lookback 延长到 today − L + 1 = 4
        self.assertTrue(all(e["lookback_days"] >= 4 for e in plan["categories"]))
        brief = plt.render_brief(plan)
        self.assertIn("补跑 2 天", brief)
        self.assertIn("补跑：2026-11-13 起漏查", brief)

    def test_no_catch_up_when_yesterday(self):
        today = EPOCH + dt.timedelta(days=4)
        write_jobs_csv(self.jobs, ["2026-11-13"])
        plan = plt.build_plan(self.profile, self.sources, today, jobs_csv=self.jobs, rules_path=None, fetched_md="")
        self.assertEqual(plan["catch_up_days"], 0)
        self.assertNotIn("every2", {e["id"] for e in plan["categories"]})
        self.assertIn("无需补跑", plt.render_brief(plan))
        plan2 = plt.build_plan(self.profile, self.sources, today, jobs_csv=self.jobs, rules_path=None, fetched_md="", catch_up=False)
        self.assertIsNone(plan2["last_run"])


class NegativeListTest(unittest.TestCase):
    def test_parse_rules_min_three(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "learned_rules.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(FAKE_RULES)
        self.assertEqual(plt.parse_negative_rules(path), ["规律甲（样本多）", "规律丙（样本很多）"])
        self.assertEqual(plt.parse_negative_rules(os.path.join(tmp, "none.md")), [])
        profile, _ = load_fake()
        items = plt.negative_list(profile, path)
        self.assertEqual(items, ["规律甲（样本多）", "规律丙（样本很多）", "不想要的方向"])

    def test_parse_rules_count_marker_variants(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "learned_rules.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(FAKE_RULES_VARIANTS)
        self.assertEqual(plt.parse_negative_rules(path), ["规律甲", "规律乙", "规律丙", "规律丁", "规律戊", "规律己、规律庚"])

    def test_real_learned_rules_parse(self):
        items = plt.parse_negative_rules(plt.LEARNED_RULES)
        self.assertIsInstance(items, list)   # fork 后 learned_rules.md 可能没有规律条目（固定样本见 test_parse_rules_min_three）
        for it in items:
            self.assertNotIn("依据", it)
            self.assertFalse(it.endswith(("—", "–", "-", "（", "(", "。", "；", "，", "：")), it)


class BriefTest(unittest.TestCase):
    SECTIONS = ["## 候选人当前状态", "## 今日计划", "## 直接抓取结果", "## 未轮到的类别与下次日期",
                "## 负面清单", "## 硬门槛清单", "## 分级口径", "## 预算", "## 补跑说明"]

    def setUp(self):
        self.profile, self.sources = load_fake()
        self.tmp = tempfile.mkdtemp()

    def test_sections_in_fixed_order_and_no_slot_residue(self):
        plan = plt.build_plan(self.profile, self.sources, EPOCH, jobs_csv=None, rules_path=None, fetched_md="")
        brief = plt.render_brief(plan)
        pos = [brief.index(s) for s in self.SECTIONS]
        self.assertEqual(pos, sorted(pos))
        self.assertNotIn("{", brief)
        self.assertNotIn("}", brief)
        self.assertIn("本次未运行抓取", brief)
        self.assertIn("门槛一", brief)
        self.assertIn("⭐高匹配：高", brief)
        self.assertIn("不想要的方向", brief)
        self.assertIn("https://example.org/a", brief)
        self.assertIn("https://example.org/browse2", brief)   # fetch=browse 的 source 并入直接访问页
        self.assertIn("src_html（html_list）", brief)
        self.assertIn("关闭类（closed_cat）：已关闭", brief)

    def test_skipped_templates_not_echoed_only_counted(self):
        """brief 不回显含花括号的模板原文，只给计数；原文保留在 plan.json。"""
        p2 = cfg.apply_profile_defaults(tomllib.loads(FAKE_PROFILE.replace('cohort_system = "cn"', 'cohort_system = "none"')))
        plan = plt.build_plan(p2, self.sources, EPOCH, jobs_csv=None, rules_path=None, fetched_md="")
        e = next(e for e in plan["categories"] if e["id"] == "cohort_cat")
        self.assertEqual(e["skipped_templates"], ["{cohort}届 校招 {domain}"])
        brief = plt.render_brief(plan)
        self.assertNotIn("{", brief)
        self.assertIn("1 条模板因槽位无候选", brief)
        self.assertIn("详见 out/plan.json", brief)

    def test_browse_access_note_rendered(self):
        """fetch = browse 的来源带 access_note 时，brief 的直接访问页标注「需人工打开：…」。"""
        profile = cfg.apply_profile_defaults(tomllib.loads(FAKE_PROFILE))
        sources = cfg.apply_sources_defaults(tomllib.loads(FAKE_SOURCES))
        c = cat(sources, "weekday_cat")
        sources.setdefault("source", []).append(cfg.apply_sources_defaults({"schedule": {"epoch": dt.date(2026, 11, 10)}, "category": [], "source": [
            {"id": "js_portal", "category": c["id"], "url": "https://portal.example/", "fetch": "browse", "access_note": "JS 单页应用"}]})["source"][0])
        d = dt.date(2026, 11, 13)
        plan = plt.build_plan(profile, sources, d, jobs_csv=None, rules_path=None, fetched_md="")
        e = next(e for e in plan["categories"] if e["id"] == "weekday_cat")
        self.assertIn("https://portal.example/", e["browse"])
        self.assertEqual(e["browse_notes"]["https://portal.example/"], "JS 单页应用")
        brief = plt.render_brief(plan)
        self.assertIn("https://portal.example/（需人工打开：JS 单页应用）", brief)

    def test_empty_expansion_moves_to_not_due_unless_browse_or_units(self):
        """展开后 queries 与 pinned 皆空：无 browse/units → 移入 not_due 并给 reason；有 → 保留并注明「本轮无关键词，仅直接访问」。"""
        no_city = cfg.apply_profile_defaults(tomllib.loads(FAKE_PROFILE.replace('cities = ["甲城", "乙城", "丙城", "丁城", "戊城"]', "cities = []")
                                                           .replace("top_cities = 3\n", "")))
        sources = cfg.apply_sources_defaults(tomllib.loads(FAKE_SOURCES))
        wk = cat(sources, "weekday_cat")          # 只有 "{degree} {city}"，cities 为空 → 一组都展不开
        d = dt.date(2026, 11, 13)                 # 周五，weekday_cat 轮到
        plan = plt.build_plan(no_city, sources, d, jobs_csv=None, rules_path=None, fetched_md="")
        self.assertNotIn("weekday_cat", {e["id"] for e in plan["categories"]})
        nd = next(n for n in plan["not_due"] if n["id"] == "weekday_cat")
        self.assertTrue(nd["active"])
        self.assertIn("本轮无可展开关键词", nd["reason"])
        self.assertEqual(nd["next_due"], "2026-11-16")
        brief = plt.render_brief(plan)
        self.assertIn("工作日类（weekday_cat）：本轮无可展开关键词", brief)
        self.assertNotIn("{", brief)
        # 同样的类别有 browse → 保留，注明仅直接访问，且计划总数不计它
        wk["browse"] = ["https://example.org/wk"]
        plan2 = plt.build_plan(no_city, sources, d, jobs_csv=None, rules_path=None, fetched_md="")
        e = next(e for e in plan2["categories"] if e["id"] == "weekday_cat")
        self.assertEqual((e["queries"], e["pinned"]), ([], []))
        self.assertEqual(e["keywords_note"], "本轮无关键词，仅直接访问")
        brief2 = plt.render_brief(plan2)
        self.assertIn("本轮无关键词，仅直接访问", brief2)
        self.assertIn("https://example.org/wk", brief2)
        self.assertEqual(plan2["budget"]["planned"], plan["budget"]["planned"])
        # 有 units 也保留
        wk["browse"], wk["units"] = [], ["单位X"]
        plan3 = plt.build_plan(no_city, sources, d, jobs_csv=None, rules_path=None, fetched_md="")
        e3 = next(e for e in plan3["categories"] if e["id"] == "weekday_cat")
        self.assertEqual(e3["units"], ["单位X"])
        self.assertEqual(e3["keywords_note"], "本轮无关键词，仅直接访问")
        # 有关键词的类别 keywords_note 为空
        self.assertEqual(next(e for e in plan3["categories"] if e["id"] == "daily")["keywords_note"], "")

    def test_fetched_md_referenced_when_present(self):
        fetched = os.path.join(self.tmp, "fetched.md")
        with open(fetched, "w", encoding="utf-8") as f:
            f.write("# 抓取\n- 候选 1\n")
        plan = plt.build_plan(self.profile, self.sources, EPOCH, jobs_csv=None, rules_path=None, fetched_md=fetched)
        self.assertEqual(plan["fetched_md"], fetched)
        self.assertIn("2 行", plt.render_brief(plan))

    def test_plan_json_structure(self):
        plan = plt.build_plan(self.profile, self.sources, EPOCH, jobs_csv=None, rules_path=None, fetched_md="")
        for k in ("today", "generated_at_note", "catch_up_days", "budget", "categories", "negative_list", "candidate_status_md"):
            self.assertIn(k, plan)
        for k in ("planned", "min", "max"):
            self.assertIn(k, plan["budget"])
        e = plan["categories"][0]
        for k in ("id", "name", "due", "next_due", "every_eff", "lookback_days", "queries", "pinned", "units",
                  "browse", "sources", "level_hint", "extra_instructions"):
            self.assertIn(k, e)
        daily = next(e for e in plan["categories"] if e["id"] == "daily")
        self.assertEqual(daily["sources"], ["src_html", "src_browse"])
        self.assertEqual(daily["next_due"], "2026-11-11")
        self.assertTrue(plan["candidate_status_md"].startswith("## 候选人当前状态"))
        json.dumps(plan, ensure_ascii=False)  # 可序列化
        self.assertNotIn("时间", plan["generated_at_note"].split("不含")[0])

    def test_same_day_twice_identical(self):
        a = plt.build_plan(self.profile, self.sources, EPOCH, jobs_csv=None, rules_path=None, fetched_md="")
        b = plt.build_plan(self.profile, self.sources, EPOCH, jobs_csv=None, rules_path=None, fetched_md="")
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(plt.render_brief(a), plt.render_brief(b))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile_path = os.path.join(self.tmp, "profile.toml")
        self.sources_path = os.path.join(self.tmp, "sources.toml")
        self.rules_path = os.path.join(self.tmp, "rules.md")
        self.jobs_path = os.path.join(self.tmp, "jobs.csv")
        for path, text in ((self.profile_path, FAKE_PROFILE), (self.sources_path, FAKE_SOURCES), (self.rules_path, FAKE_RULES)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        write_jobs_csv(self.jobs_path, ["2026-11-09"])
        self.common = ["--profile", self.profile_path, "--sources", self.sources_path, "--rules", self.rules_path,
                       "--jobs", self.jobs_path, "--out", os.path.join(self.tmp, "out")]

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = plt.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_brief_writes_files(self):
        code, out, err = self.run_cli(self.common + ["brief", "--today", "2026-11-10"])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "out", "brief.md")))
        with open(os.path.join(self.tmp, "out", "plan.json"), encoding="utf-8") as f:
            plan = json.load(f)
        self.assertEqual(plan["today"], "2026-11-10")
        self.assertEqual(plan["last_run"], "2026-11-09")
        self.assertIn("## 今日计划", out)
        self.assertIn("规律甲", out)

    def test_brief_quiet_prints_only_written_line(self):
        code, out, err = self.run_cli(self.common + ["brief", "--quiet", "--today", "2026-11-10"])
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        self.assertTrue(lines[0].startswith("已写入 "), out)
        self.assertIn("brief.md", lines[0])
        self.assertIn("plan.json", lines[0])
        self.assertNotIn("## 今日计划", out + err)
        with open(os.path.join(self.tmp, "out", "brief.md"), encoding="utf-8") as f:
            self.assertIn("## 今日计划", f.read())
        # 选项也可写在子命令后
        code, out, _ = self.run_cli(["brief"] + self.common + ["--today", "2026-11-10", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)

    def test_options_after_subcommand(self):
        code, out, _ = self.run_cli(["plan"] + self.common + ["--today", "2026-11-10"])
        self.assertEqual(code, 0)
        self.assertIn("plan.json", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out", "brief.md")))

    def test_schedule_days(self):
        code, out, _ = self.run_cli(self.common + ["schedule", "--days", "3", "--today", "2026-11-10"])
        self.assertEqual(code, 0)
        lines = [l for l in out.splitlines() if l.startswith("2026-")]
        self.assertEqual(len(lines), 3)
        self.assertIn("daily×2", lines[0])
        self.assertNotIn("every2", lines[0])       # day=0 偶数不轮到
        self.assertIn("every2×", lines[1])         # day=1 轮到（含固定检索；已按 max=6 裁剪）
        self.assertIn("合计  6", lines[1])

    def test_bad_config_exits_2(self):
        with open(self.sources_path, "w", encoding="utf-8") as f:
            f.write("schema = 1\n[schedule]\nepoch = 2026-11-10\n")
        code, _, err = self.run_cli(self.common + ["plan", "--today", "2026-11-10"])
        self.assertEqual(code, 2)
        self.assertIn("配置不合法", err)

    def test_help(self):
        for argv in (["--help"], ["brief", "--help"], ["plan", "--help"], ["schedule", "--help"]):
            code, out, _ = self.run_cli(argv)
            self.assertEqual(code, 0)
            self.assertIn("usage", out)


class RealConfigTest(unittest.TestCase):
    """仓库真实配置的离线烟雾测试（不读 jobs.csv，避免受日常提交影响）。"""

    def setUp(self):
        self.profile = cfg.load_profile()
        self.sources = cfg.load_sources()

    def test_two_weeks_within_budget_and_no_residue(self):
        sch = self.sources["schedule"]
        for i in range(14):
            d = dt.date(2026, 11, 5) + dt.timedelta(days=i)
            plan = plt.build_plan(self.profile, self.sources, d, jobs_csv=None, rules_path=plt.LEARNED_RULES, fetched_md="")
            self.assertLessEqual(plan["budget"]["planned"], sch["max_queries_per_day"], d)
            self.assertGreaterEqual(plan["budget"]["planned"], 1, d)
            brief = plt.render_brief(plan)
            self.assertNotIn("{", brief, d)
            for e in plan["categories"]:
                for qq in e["queries"] + e["pinned"]:
                    self.assertNotIn("{", qq, (d, e["id"]))
                # 列入今日计划的类别要么有关键词，要么至少有点名单位/直接访问页（否则应已移入 not_due）
                self.assertTrue(e["queries"] or e["pinned"] or e["units"] or e["browse"], (d, e["id"]))
            for n in plan["not_due"]:
                self.assertTrue(n["reason"], (d, n["id"]))

    def test_keywords_do_not_repeat_on_consecutive_turns(self):
        """可展开模板数多于每轮组数的类别，连续轮次的关键词不应完全相同（fork 后关闭/模板很少的类别跳过）。"""
        d0 = dt.date(2026, 11, 10)
        for c in self.sources["category"]:
            if not cfg.category_active(c, self.profile) or not c.get("query_templates"):
                continue
            cand = plt.slot_candidates(c, self.profile, d0, list(c.get("units") or []))
            fillable = [t for t in c["query_templates"] if all(cand.get(s) for s in cfg.template_slots(t))]
            q = plt.queries_for_day(c, self.sources["schedule"], d0)
            if len(fillable) <= q:
                continue
            seen = []
            for i in range(3):
                d = d0 + dt.timedelta(days=i * plt.every_eff(c, d0))
                seen.append(tuple(plt.expand_category(c, self.profile, self.sources, d)["queries"]))
            self.assertNotEqual(seen[0], seen[1], c["id"])
            self.assertNotEqual(seen[1], seen[2], c["id"])

    def test_ci_date_brief(self):
        plan = plt.build_plan(self.profile, self.sources, dt.date(2026, 11, 20), jobs_csv=None, rules_path=plt.LEARNED_RULES, fetched_md="")
        brief = plt.render_brief(plan)
        self.assertIn("## 候选人当前状态", brief)
        self.assertIn("## 补跑说明", brief)


if __name__ == "__main__":
    unittest.main()
