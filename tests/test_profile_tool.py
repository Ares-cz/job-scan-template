"""tools/profile_tool.py 的单元测试：python3 -m unittest tests.test_profile_tool

全部使用固定日期与虚构画像，不含任何真实个人信息。
"""
import datetime as dt
import io
import os
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import config as cfg  # noqa: E402
import profile_tool as pt  # noqa: E402

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
institution_en = "Some University"
defense_date = 2026-11-16
conferral_date = 2026-12-01
[[education]]
level = "master"
institution = "另一大学"
[status]
cohort_system = "cn"
cohort_year = 2027
overseas_returnee = true
fresh_grad_grace_months = 24
age_thresholds = [35, 32]
degree_years_thresholds = [3]
[research]
disciplines = ["计算机科学与技术"]
[[research.topics]]
id = "t1"
terms = { zh = ["主题一"], en = ["topic one"] }
[target]
degree_requirements_ok = ["博士"]
[locations]
cities = ["南京"]
"""


def fake_profile(extra: str = "", replace: dict | None = None) -> dict:
    text = FAKE_PROFILE
    for k, v in (replace or {}).items():
        assert k in text, k
        text = text.replace(k, v)
    return cfg.apply_profile_defaults(tomllib.loads(text + extra))


class DateMathTest(unittest.TestCase):
    def test_add_months_clamps_day(self):
        self.assertEqual(pt.add_months(dt.date(2027, 1, 31), 1), dt.date(2027, 2, 28))
        self.assertEqual(pt.add_months(dt.date(2026, 11, 16), 24), dt.date(2028, 11, 16))
        self.assertEqual(pt.add_years(dt.date(2024, 2, 29), 1), dt.date(2025, 2, 28))

    def test_parse_day(self):
        self.assertEqual(pt.parse_day("2026-11-04"), dt.date(2026, 11, 4))
        self.assertEqual(pt.parse_day("1990-11"), dt.date(1990, 11, 1))
        with self.assertRaises(ValueError):
            pt.parse_day("2026/13/01")


class AgeTest(unittest.TestCase):
    def setUp(self):
        self.p = fake_profile()
        self.birth = pt.birth_date(self.p)

    def test_birth_is_first_of_month(self):
        self.assertEqual(self.birth, dt.date(1994, 6, 1))

    def test_age_on(self):
        self.assertEqual(pt.age_on(self.birth, dt.date(2026, 5, 31)), 31)
        self.assertEqual(pt.age_on(self.birth, dt.date(2026, 6, 1)), 32)

    def test_threshold_two_readings(self):
        t = pt.age_threshold(self.birth, 35, dt.date(2026, 11, 4))
        # 宽口径：未满 36 周岁 → 2030-05-31；严口径：未满 35 周岁 → 2029-05-31
        self.assertEqual(t["loose_last_day"], dt.date(2030, 5, 31))
        self.assertEqual(t["strict_last_day"], dt.date(2029, 5, 31))
        self.assertTrue(t["loose_ok"] and t["strict_ok"])
        # 2029-06-01 当天：宽口径仍满足，严口径不再满足
        t2 = pt.age_threshold(self.birth, 35, dt.date(2029, 6, 1))
        self.assertTrue(t2["loose_ok"])
        self.assertFalse(t2["strict_ok"])
        t3 = pt.age_threshold(self.birth, 35, dt.date(2030, 6, 1))
        self.assertFalse(t3["loose_ok"])

    def test_born_after(self):
        self.assertTrue(pt.born_after_ok(self.birth, dt.date(1994, 6, 1)))
        self.assertTrue(pt.born_after_ok(self.birth, dt.date(1990, 1, 1)))
        self.assertFalse(pt.born_after_ok(self.birth, dt.date(1994, 6, 2)))

    def test_status_lists_both_readings(self):
        md = pt.status_markdown(self.p, dt.date(2026, 11, 4))
        self.assertIn("宽口径", md)
        self.assertIn("严口径", md)
        self.assertIn("2030-05-31", md)
        self.assertIn("2029-05-31", md)
        self.assertIn("以公告原文为准", md)
        self.assertIn("32 周岁", md)

    def test_no_birth_month_omits_age(self):
        p = fake_profile(replace={"birth_month = 1994-06-01\n": ""})
        md = pt.status_markdown(p, dt.date(2026, 11, 4))
        self.assertNotIn("年龄", md)
        self.assertNotIn("{", md)


class DegreeStageTest(unittest.TestCase):
    def stage(self, today, **replace):
        p = fake_profile(replace=replace)
        return pt.degree_stage(cfg.highest_education(p), today)

    def test_four_stages(self):
        self.assertEqual(self.stage(dt.date(2026, 11, 4))["stage"], pt.STAGE_NOT_DEFENDED)
        self.assertEqual(self.stage(dt.date(2026, 11, 20))["stage"], pt.STAGE_DEFENDED)
        self.assertEqual(self.stage(dt.date(2026, 12, 1))["stage"], pt.STAGE_CONFERRED)
        with_diploma = {"defense_date": dt.date(2026, 11, 16), "conferral_date": dt.date(2026, 12, 1), "diploma_received": True}
        self.assertEqual(pt.degree_stage(with_diploma, dt.date(2027, 3, 1))["stage"], pt.STAGE_DIPLOMA)
        self.assertIn("证书已取得", pt.stage_wording(pt.degree_stage(with_diploma, dt.date(2027, 3, 1)), "phd", "zh"))

    def test_defense_day_itself_is_conservative(self):
        s = self.stage(dt.date(2026, 11, 16))
        self.assertEqual(s["stage"], pt.STAGE_NOT_DEFENDED)
        self.assertEqual(s["note"], "今天是答辩日")
        self.assertEqual(self.stage(dt.date(2026, 11, 17))["stage"], pt.STAGE_DEFENDED)

    def test_wording_differs_between_dates(self):
        p = fake_profile()
        a = pt.status_markdown(p, dt.date(2026, 11, 4))
        b = pt.status_markdown(p, dt.date(2026, 11, 20))
        c = pt.status_markdown(p, dt.date(2026, 11, 16))
        self.assertIn("未答辩", a)
        self.assertIn("博士在读，预计 2026-11-16 答辩、2026-12-01 授予学位", a)
        self.assertIn("已答辩待授位", b)
        self.assertIn("已于 2026-11-16 通过博士答辩，预计 2026-12-01 正式授予学位", b)
        self.assertIn("今天是答辩日", c)
        self.assertNotEqual(a, b)

    def test_conferral_defaults_to_defense(self):
        p = fake_profile(replace={"conferral_date = 2026-12-01\n": ""})
        edu = cfg.highest_education(p)
        self.assertEqual(edu["conferral_date"], dt.date(2026, 11, 16))
        self.assertEqual(pt.degree_stage(edu, dt.date(2026, 11, 17))["stage"], pt.STAGE_CONFERRED)

    def test_unknown_stage_without_dates(self):
        s = pt.degree_stage({"level": "phd"}, dt.date(2026, 11, 4))
        self.assertEqual(s["stage"], pt.STAGE_UNKNOWN)
        self.assertIn("不详", pt.stage_wording(s, "phd", "zh"))

    def test_english_wording(self):
        p = fake_profile(replace={'language = "zh"': 'language = "en"'})
        md = pt.status_markdown(p, dt.date(2026, 11, 4))
        self.assertIn("PhD candidate; defence scheduled 2026-11-16", md)
        self.assertIn("Some University", md)


class CohortTest(unittest.TestCase):
    def test_season_switches_in_august(self):
        self.assertEqual(pt.season_cohort_year(dt.date(2026, 7, 31)), 2026)
        self.assertEqual(pt.season_cohort_year(dt.date(2026, 8, 1)), 2027)
        self.assertEqual(pt.season_cohort_year(dt.date(2027, 1, 15)), 2027)

    def test_cohort_match_and_mismatch(self):
        p = fake_profile()
        self.assertTrue(pt.cohort_info(p, dt.date(2026, 11, 4))["same"])
        md = pt.status_markdown(p, dt.date(2026, 11, 4))
        self.assertIn("2027 届（与当前校招季一致）", md)
        md2 = pt.status_markdown(p, dt.date(2027, 9, 1))     # 校招季已是 2028 届
        self.assertIn("候选人为 2027 届，当前校招季为 2028 届", md2)
        self.assertIn("早于当前校招季", md2)
        md3 = pt.status_markdown(p, dt.date(2026, 7, 1))     # 校招季还是 2026 届
        self.assertIn("晚于当前校招季", md3)

    def test_cohort_derived_when_missing(self):
        p = fake_profile(replace={"cohort_year = 2027\n": ""})
        self.assertEqual(pt.cohort_info(p, dt.date(2026, 11, 4))["cohort_year"], 2027)  # 12 月授位 → 次年届
        p2 = fake_profile(replace={"cohort_year = 2027\n": "", "conferral_date = 2026-12-01": "conferral_date = 2026-06-30"})
        self.assertEqual(pt.cohort_info(p2, dt.date(2026, 5, 4))["cohort_year"], 2026)

    def test_cohort_none_omits_line(self):
        p = fake_profile(replace={'cohort_system = "cn"': 'cohort_system = "none"'})
        self.assertIsNone(pt.cohort_info(p, dt.date(2026, 11, 4)))
        self.assertNotIn("届别", pt.status_markdown(p, dt.date(2026, 11, 4)))


class ValidityTest(unittest.TestCase):
    def test_fresh_grad_and_degree_years(self):
        p = fake_profile()
        edu = cfg.highest_education(p)
        self.assertEqual(pt.fresh_grad_until(edu, p), dt.date(2028, 12, 1))
        self.assertEqual(pt.degree_years_until(edu, 3), dt.date(2029, 12, 1))
        md = pt.status_markdown(p, dt.date(2026, 11, 4))
        self.assertIn("留学归国应届身份", md)
        self.assertIn("至 2028-12-01", md)
        self.assertIn("「获博士学位 3 年内」：至 2029-12-01", md)
        md_late = pt.status_markdown(p, dt.date(2029, 1, 1))
        self.assertIn("今天已过期", md_late)

    def test_status_block_is_deterministic(self):
        p = fake_profile()
        self.assertEqual(pt.status_markdown(p, dt.date(2026, 11, 4)), pt.status_markdown(p, dt.date(2026, 11, 4)))
        self.assertTrue(pt.status_markdown(p, dt.date(2026, 11, 4)).startswith("## 候选人当前状态"))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile_path = os.path.join(self.tmp, "profile.toml")
        with open(self.profile_path, "w", encoding="utf-8") as f:
            f.write(FAKE_PROFILE)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = pt.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_status_today_either_position(self):
        c1, o1, _ = self.run_cli(["--profile", self.profile_path, "status", "--today", "2026-11-04"])
        c2, o2, _ = self.run_cli(["--profile", self.profile_path, "--today", "2026-11-04", "status"])
        self.assertEqual((c1, c2), (0, 0))
        self.assertEqual(o1, o2)
        self.assertIn("2026-11-04", o1)

    def test_age_ok_exit_codes(self):
        c, o, _ = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "1990-11-01", "--today", "2026-11-04"])
        self.assertEqual(c, 0)
        self.assertIn("满足", o)
        c, o, _ = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "1995-01-01", "--today", "2026-11-04"])
        self.assertEqual(c, 1)
        self.assertIn("不满足", o)
        c, _, e = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "bad"])
        self.assertEqual(c, 2)
        self.assertIn("日期格式", e)

    def test_age_ok_same_month_cannot_judge(self):
        """划线日落在出生当月（fixture 出生月 1994-06）且 day > 1：profile 只记录到月 → 2「无法判断」；当月 1 日仍为满足。"""
        c, o, _ = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "1994-06-15", "--today", "2026-11-04"])
        self.assertEqual(c, 2)
        self.assertIn("无法判断", o)
        self.assertIn("1994-06", o)
        self.assertNotIn("不满足", o)
        c, o, _ = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "1994-06-01", "--today", "2026-11-04"])
        self.assertEqual(c, 0)
        self.assertIn("满足", o)
        c, o, _ = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "1994-06", "--today", "2026-11-04"])
        self.assertEqual(c, 0)   # YYYY-MM 按 1 日
        c, o, _ = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "1994-07-01", "--today", "2026-11-04"])
        self.assertEqual(c, 1)   # 下个月 1 日：确定不满足
        c, o, _ = self.run_cli(["--profile", self.profile_path, "age-ok", "--born-after", "1994-05-20", "--today", "2026-11-04"])
        self.assertEqual(c, 0)   # 上个月：确定满足
        # 未填 birth_month → 2
        path = os.path.join(self.tmp, "nobirth.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(FAKE_PROFILE.replace("birth_month = 1994-06-01\n", ""))
        c, o, _ = self.run_cli(["--profile", path, "age-ok", "--born-after", "1994-06-15"])
        self.assertEqual(c, 2)
        self.assertIn("无法判断", o)

    def test_bad_today(self):
        c, _, e = self.run_cli(["--profile", self.profile_path, "status", "--today", "2026-99-01"])
        self.assertEqual(c, 2)
        self.assertIn("--today", e)

    def test_help(self):
        for argv in (["--help"], ["status", "--help"], ["age-ok", "--help"], ["keywords", "--help"]):
            c, o, _ = self.run_cli(argv)
            self.assertEqual(c, 0)
            self.assertIn("usage", o)

    def test_keywords_uses_real_config(self):
        cid = cfg.load_sources()["category"][0]["id"]   # 不绑定具体类别 id，fork 改了 sources.toml 也能跑
        c, o, _ = self.run_cli(["keywords", "--today", "2026-11-05", "--category", cid])
        self.assertEqual(c, 0)
        self.assertIn(f"[{cid}]", o)
        self.assertNotIn("{", o)
        self.assertNotIn("None", o)
        c, o, _ = self.run_cli(["keywords", "--today", "2026-11-05"])
        self.assertEqual(c, 0)
        self.assertNotIn("{", o)
        c, _, e = self.run_cli(["keywords", "--today", "2026-11-05", "--category", "nope"])
        self.assertEqual(c, 2)
        self.assertIn("没有类别", e)

    def test_keywords_closed_category_only_status_line(self):
        """已关闭的类别（roles 无交集 / enabled=false / special_zone 未定义）只打印状态行，不展开关键词。"""
        sources_path = os.path.join(self.tmp, "sources.toml")
        with open(sources_path, "w", encoding="utf-8") as f:
            f.write('''
schema = 1
[schedule]
epoch = 2026-11-10
[[category]]
id = "open_cat"
name = "开着的"
lang = "zh"
requires_roles = ["postdoc"]
query_templates = ["{topic} 博士后"]
pinned = ["固定词"]
[[category]]
id = "closed_roles"
name = "角色不交"
requires_roles = ["civil_service"]
query_templates = ["{topic} 公务员"]
pinned = ["公务员固定词"]
[[category]]
id = "closed_flag"
name = "显式关闭"
enabled = false
query_templates = ["{topic}"]
[[category]]
id = "closed_zone"
name = "专区未定义"
special_zone = "nowhere"
query_templates = ["{zone} 招聘"]
[[category]]
id = "no_keywords"
name = "展不开"
query_templates = ["{city} 招聘"]
units = ["单位甲"]
''')
        profile_path = os.path.join(self.tmp, "p.toml")
        with open(profile_path, "w", encoding="utf-8") as f:
            f.write(FAKE_PROFILE.replace("[target]\n", '[target]\nroles = ["postdoc"]\n').replace('cities = ["南京"]', "cities = []\n[[locations.countries]]\nname = \"甲国\""))
        c, o, e = self.run_cli(["--profile", profile_path, "--sources", sources_path, "keywords", "--today", "2026-11-10"])
        self.assertEqual(c, 0, e)
        self.assertIn("[open_cat] 开着的（今天轮到", o)
        self.assertIn("  * 固定词", o)
        self.assertIn("  - 主题一 博士后", o)
        for cid in ("closed_roles", "closed_flag", "closed_zone"):
            self.assertIn(f"[{cid}]", o)
            line = next(l for l in o.splitlines() if l.startswith(f"[{cid}]"))
            self.assertIn("已关闭", line)
        self.assertNotIn("公务员", o)          # 已关闭类别的关键词与固定词都不展开
        self.assertNotIn("{", o)
        self.assertIn("[no_keywords] 展不开（今天轮到", o)
        self.assertIn("（本轮无关键词，仅直接访问）", o)
        self.assertIn("点名单位：单位甲", o)
        self.assertIn("1 条模板因槽位无候选", o)


# 个人信息不外泄的检查统一在 tests/test_config.py::RepoConfigTest.test_no_personal_info_outside_profile
#（遍历 git ls-files 减白名单，覆盖 tools/、tests/、PROMPT.md、README.md 等全部文件），此处不再重复维护文件清单。


if __name__ == "__main__":
    unittest.main()
