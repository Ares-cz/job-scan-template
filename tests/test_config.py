"""tools/config.py 的单元测试：python3 -m unittest tests.test_config"""
import copy
import datetime as dt
import io
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import config as cfg  # noqa: E402

ROOT = cfg.ROOT

MIN_PROFILE = """
schema = 1
[identity]
name = "测试人"
email = "t@example.com"
timezone = "Europe/Amsterdam"
language = "zh"
[[education]]
level = "phd"
institution = "某大学"
defense_date = 2026-11-16
[status]
cohort_system = "cn"
[[research.topics]]
id = "t1"
terms = { zh = ["主题一"], en = ["topic one"] }
[locations]
cities = ["南京"]
"""

MIN_SOURCES = """
schema = 1
[schedule]
epoch = 2026-11-10
[[category]]
id = "cat_a"
name = "甲类"
lang = "zh"
requires_roles = ["postdoc"]
every = 2
query_templates = ["{topic} 博士后 {city}"]
[[source]]
id = "src_a"
category = "cat_a"
url = "https://example.com/list"
link_pattern = "/job/\\\\d+"
"""


def write_tmp(text: str, suffix: str = ".toml") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class SourceExtraFieldsTest(unittest.TestCase):
    """title_exclude 须为合法正则；access_note 为字符串，对 html_list 来源给警告。"""

    def test_title_exclude_regex_checked(self):
        bad = MIN_SOURCES + 'title_exclude = "("\n'
        errs, _ = cfg.validate_sources(cfg.apply_sources_defaults(tomllib.loads(bad)))
        self.assertTrue(any("title_exclude" in e for e in errs), errs)
        good = MIN_SOURCES + 'title_exclude = "Lecturer|PhD"\n'
        errs, _ = cfg.validate_sources(cfg.apply_sources_defaults(tomllib.loads(good)))
        self.assertFalse(any("title_exclude" in e for e in errs), errs)

    def test_access_note_defaults_and_warning(self):
        d = cfg.apply_sources_defaults(tomllib.loads(MIN_SOURCES))
        self.assertEqual(d["source"][0]["access_note"], "")
        self.assertEqual(d["source"][0]["title_exclude"], "")
        noisy = MIN_SOURCES + 'access_note = "JS 页面"\n'
        _, warns = cfg.validate_sources(cfg.apply_sources_defaults(tomllib.loads(noisy)))
        self.assertTrue(any("access_note" in w for w in warns), warns)
        browse = MIN_SOURCES.replace('link_pattern = "/job/\\\\d+"', 'fetch = "browse"') + 'access_note = "JS 页面"\n'
        errs, warns = cfg.validate_sources(cfg.apply_sources_defaults(tomllib.loads(browse)))
        self.assertEqual(errs, [])
        self.assertFalse(any("access_note" in w for w in warns), warns)


class TermsTest(unittest.TestCase):
    def test_terms_for_fallback(self):
        topic = {"id": "x", "terms": {"zh": ["无人机"], "en": ["UAV", "drone"]}}
        self.assertEqual(cfg.terms_for(topic, "zh"), ["无人机"])
        self.assertEqual(cfg.terms_for(topic, "de"), ["UAV", "drone"])  # 缺 de → en
        only_de = {"terms": {"de": ["Drohne"]}}
        self.assertEqual(cfg.terms_for(only_de, "zh"), ["Drohne"])  # 缺 zh、en → 任一
        self.assertEqual(cfg.terms_for({"zh": ["博士后"]}, "zh"), ["博士后"])  # 直接传 {lang: [...]}
        self.assertEqual(cfg.terms_for({}, "zh"), [])
        self.assertEqual(cfg.terms_for(None, "zh"), [])

    def test_text_for(self):
        self.assertEqual(cfg.text_for({"zh": "荷兰", "en": "Netherlands"}, "en"), "Netherlands")
        self.assertEqual(cfg.text_for({"zh": "荷兰"}, "en"), "荷兰")
        self.assertEqual(cfg.text_for("Germany", "zh"), "Germany")
        self.assertEqual(cfg.text_for({}, "zh", "-"), "-")

    def test_template_slots(self):
        self.assertEqual(cfg.template_slots("{topic} postdoc {country} {year}"), {"topic", "country", "year"})
        self.assertEqual(cfg.template_slots("没有槽位"), set())

    def test_normalize_url_matches_jobs_tool(self):
        import jobs_tool
        for u in ["https://www.gwy.com/gqzp/413546.html/?utm_source=x#top", "http://a.b/c", "jobs.ac.uk/search/?keywords=robotics&sortOrder=1"]:
            self.assertEqual(cfg.normalize_url(u), jobs_tool.normalize_url(u))


class ProfileDefaultsTest(unittest.TestCase):
    def test_defaults_and_cohort(self):
        path = write_tmp(MIN_PROFILE)
        try:
            p = cfg.load_profile(path)
        finally:
            os.unlink(path)
        edu = p["education"][0]
        self.assertEqual(edu["conferral_date"], dt.date(2026, 11, 16))  # 缺省取 defense_date
        self.assertFalse(edu["diploma_received"])
        self.assertEqual(p["status"]["cohort_year"], 2027)  # 11 月授位 → 次年届
        self.assertEqual(p["marks"]["dismiss_prefixes"], ["不要"])
        self.assertEqual(p["locations"]["top_cities"], 1)
        self.assertEqual(p["target"]["roles"], list(cfg.KNOWN_ROLES))
        self.assertEqual(p["research"]["topics"][0]["weight"], 1)
        self.assertEqual(cfg.highest_education(p)["level"], "phd")

    def test_derive_cohort_year(self):
        self.assertEqual(cfg.derive_cohort_year(dt.date(2026, 6, 30)), 2026)
        self.assertEqual(cfg.derive_cohort_year(dt.date(2026, 7, 1)), 2027)
        self.assertIsNone(cfg.derive_cohort_year(None))

    def test_highest_education_order(self):
        p = {"education": [{"level": "master", "institution": "a"}, {"level": "phd", "institution": "b"}]}
        self.assertEqual(cfg.highest_education(p)["institution"], "b")

    def test_missing_file_and_bad_toml(self):
        with self.assertRaises(FileNotFoundError):
            cfg.load_profile("/nonexistent/profile.toml")
        path = write_tmp("[identity\nname = ")
        try:
            with self.assertRaises(ValueError):
                cfg.load_profile(path)
        finally:
            os.unlink(path)


class ProfileValidateTest(unittest.TestCase):
    def load(self, text):
        path = write_tmp(text)
        try:
            return cfg.load_profile(path)
        finally:
            os.unlink(path)

    def test_minimal_passes(self):
        errs, _ = cfg.validate_profile(self.load(MIN_PROFILE))
        self.assertEqual(errs, [])

    def test_missing_required(self):
        p = self.load(MIN_PROFILE)
        del p["identity"]["email"]
        p["research"]["topics"] = []
        p["education"] = []
        p["locations"]["cities"] = []
        errs, _ = cfg.validate_profile(p)
        joined = "\n".join(errs)
        self.assertIn("identity.email", joined)
        self.assertIn("research.topics", joined)
        self.assertIn("education", joined)
        self.assertIn("locations.cities 与 locations.countries", joined)

    def test_timezone_required_no_silent_default(self):
        """identity.timezone 是必填项：缺失时 load_profile 不补默认值，validate_profile 报错（运行时兜底在 jobs_tool/profile_tool）。"""
        p = self.load(MIN_PROFILE.replace('timezone = "Europe/Amsterdam"\n', ""))
        self.assertNotIn("timezone", p["identity"])
        errs, _ = cfg.validate_profile(p)
        self.assertTrue(any("identity.timezone" in e and "必填" in e for e in errs), errs)
        p["identity"]["timezone"] = "   "
        errs, _ = cfg.validate_profile(p)
        self.assertTrue(any("identity.timezone" in e for e in errs), errs)
        p["identity"]["timezone"] = "Asia/Shanghai"
        errs, _ = cfg.validate_profile(p)
        self.assertEqual(errs, [])

    def test_bad_date_and_level(self):
        text = MIN_PROFILE.replace("defense_date = 2026-11-16", 'defense_date = "2026-11-16"').replace('level = "phd"', 'level = "doctor"')
        text += '\nbirth_month = "1990-01"\n'  # 挂在 [locations] 下不影响；单独测 identity 下的字符串日期
        p = self.load(text)
        p["identity"]["birth_month"] = "1990-01-01"
        errs, _ = cfg.validate_profile(p)
        joined = "\n".join(errs)
        self.assertIn("defense_date 必须是 TOML 日期", joined)
        self.assertIn("level 必须是 phd|master|bachelor", joined)
        self.assertIn("birth_month 必须是 TOML 日期", joined)

    def test_bad_terms_and_duplicate_ids(self):
        p = self.load(MIN_PROFILE)
        p["research"]["topics"].append({"id": "t1", "terms": {"zh": "不是列表"}})
        p["target"]["role_terms"] = {"zh": []}
        errs, _ = cfg.validate_profile(p)
        joined = "\n".join(errs)
        self.assertIn("重复", joined)
        self.assertIn("research.topics[2].terms.zh", joined)
        self.assertIn("target.role_terms.zh", joined)

    def test_unknown_role_warns(self):
        p = self.load(MIN_PROFILE)
        p["target"]["roles"] = ["postdoc", "astronaut"]
        errs, warns = cfg.validate_profile(p)
        self.assertEqual(errs, [])
        self.assertTrue(any("astronaut" in w for w in warns))

    def test_cohort_cn_without_dates_warns(self):
        p = self.load(MIN_PROFILE.replace("defense_date = 2026-11-16\n", ""))
        self.assertNotIn("cohort_year", p["status"])
        _, warns = cfg.validate_profile(p)
        self.assertTrue(any("无法推算届别" in w for w in warns))


class SourcesValidateTest(unittest.TestCase):
    def load_sources(self, text):
        path = write_tmp(text)
        try:
            return cfg.load_sources(path)
        finally:
            os.unlink(path)

    def load_profile(self, text=MIN_PROFILE):
        path = write_tmp(text)
        try:
            return cfg.load_profile(path)
        finally:
            os.unlink(path)

    def test_minimal_passes_and_defaults(self):
        s = self.load_sources(MIN_SOURCES)
        errs, warns = cfg.validate_sources(s, self.load_profile())
        self.assertEqual(errs, [])
        self.assertEqual(warns, [])
        cat = s["category"][0]
        self.assertEqual(cat["lookback_days"], 3)  # every + 1
        self.assertEqual(cat["queries_per_turn"], 2)
        self.assertEqual(s["source"][0]["fetch"], "html_list")
        self.assertEqual(s["source"][0]["title_filter"], "profile")
        self.assertEqual(s["schedule"]["max_queries_per_day"], 15)

    def test_bad_slot(self):
        s = self.load_sources(MIN_SOURCES.replace("{topic} 博士后 {city}", "{topic} 博士后 {planet}"))
        errs, _ = cfg.validate_sources(s)
        self.assertTrue(any("{planet}" in e and "不允许的槽位" in e for e in errs), errs)

    def test_bad_slot_in_pinned(self):
        s = self.load_sources(MIN_SOURCES.replace("every = 2\n", 'every = 2\npinned = ["{topic} 固定", "{planet} 招聘"]\n'))
        errs, _ = cfg.validate_sources(s)
        self.assertTrue(any("pinned" in e and "{planet}" in e and "不允许的槽位" in e for e in errs), errs)
        self.assertFalse(any("{topic} 固定" in e for e in errs), errs)

    def test_unbalanced_braces(self):
        bad = ["{unit postdoc", "{topic}} x", "{{topic}}", "postdoc }"]
        text = MIN_SOURCES.replace("every = 2\n", 'every = 2\npinned = ["{unit postdoc"]\n') \
            .replace('query_templates = ["{topic} 博士后 {city}"]',
                     'query_templates = ["{topic} 博士后 {city}", "{topic}} x", "{{topic}}", "postdoc }"]')
        s = self.load_sources(text)
        errs, _ = cfg.validate_sources(s)
        for t in bad:
            self.assertTrue(any(repr(t) in e and "花括号不配对" in e for e in errs), (t, errs))
        self.assertTrue(any("pinned" in e and "{unit postdoc" in e for e in errs), errs)
        self.assertFalse(any("{topic} 博士后 {city}" in e for e in errs), errs)
        # 合法模板不会误报
        errs, _ = cfg.validate_sources(self.load_sources(MIN_SOURCES))
        self.assertEqual(errs, [])

    def test_cohort_in_pinned_warns_when_cohort_none(self):
        s = self.load_sources(MIN_SOURCES.replace("every = 2\n", 'every = 2\npinned = ["{cohort}届 校招"]\n'))
        p = self.load_profile(MIN_PROFILE.replace('cohort_system = "cn"', 'cohort_system = "none"'))
        errs, warns = cfg.validate_sources(s, p)
        self.assertEqual(errs, [])
        self.assertTrue(any("{cohort}" in w and "将被跳过" in w for w in warns), warns)

    def test_roles_no_intersection_warns_and_closes(self):
        s = self.load_sources(MIN_SOURCES)
        p = self.load_profile()
        p["target"]["roles"] = ["civil_service"]
        errs, warns = cfg.validate_sources(s, p)
        self.assertEqual(errs, [])
        self.assertTrue(any("类别 cat_a 将关闭" in w for w in warns), warns)
        self.assertFalse(cfg.category_active(s["category"][0], p))
        p["target"]["roles"] = ["postdoc"]
        self.assertTrue(cfg.category_active(s["category"][0], p))

    def test_special_zone_missing_warns_and_closes(self):
        s = self.load_sources(MIN_SOURCES.replace('every = 2\n', 'every = 2\nspecial_zone = "zone_x"\n'))
        p = self.load_profile()
        errs, warns = cfg.validate_sources(s, p)
        self.assertEqual(errs, [])
        self.assertTrue(any("special_zone" in w and "将关闭" in w for w in warns), warns)
        self.assertFalse(cfg.category_active(s["category"][0], p))

    def test_source_errors(self):
        text = MIN_SOURCES.replace('url = "https://example.com/list"', 'url = "ftp://example.com"') \
            .replace('link_pattern = "/job/\\\\d+"', 'link_pattern = "("\ndate_pattern = "(\\\\d+)"\nfetch = "magic"\ncategory_x = 1')
        s = self.load_sources(text)
        s["source"][0]["category"] = "nope"
        errs, _ = cfg.validate_sources(s)
        joined = "\n".join(errs)
        self.assertIn("不是已定义的类别 id", joined)
        self.assertIn("不是 http(s) URL", joined)
        self.assertIn("不是合法正则", joined)
        self.assertIn("fetch 必须是", joined)
        self.assertIn("date_format", joined)

    def test_html_list_requires_link_pattern(self):
        s = self.load_sources(MIN_SOURCES.replace('link_pattern = "/job/\\\\d+"\n', ""))
        errs, _ = cfg.validate_sources(s)
        self.assertTrue(any("必须给出 link_pattern" in e for e in errs), errs)

    def test_schedule_errors(self):
        s = self.load_sources(MIN_SOURCES.replace("epoch = 2026-11-10", 'epoch = "2026-11-10"\nmax_queries_per_day = 5\nmin_queries_per_day = 8\nweekend_factor = 0'))
        errs, _ = cfg.validate_sources(s)
        joined = "\n".join(errs)
        self.assertIn("epoch", joined)
        self.assertIn("min_queries_per_day 不能大于", joined)
        self.assertIn("weekend_factor", joined)

    def test_duplicate_ids_and_bad_browse(self):
        text = MIN_SOURCES + '\n[[category]]\nid = "cat_a"\nname = "重复"\nbrowse = ["not-a-url"]\n'
        s = self.load_sources(text)
        errs, _ = cfg.validate_sources(s)
        joined = "\n".join(errs)
        self.assertIn("重复", joined)
        self.assertIn("非法 URL", joined)


class RepoConfigTest(unittest.TestCase):
    """仓库里的两份 profile 与 sources.toml 必须通过校验。"""

    def test_real_profile(self):
        p = cfg.load_profile()
        errs, warns = cfg.validate_profile(p)
        self.assertEqual(errs, [])
        s = cfg.load_sources()
        errs, warns = cfg.validate_sources(s, p)
        self.assertEqual(errs, [])
        # fork 者可按 README 关掉类别（删 roles / enabled=false / 无 special_zone），只产生警告；至少要有一个类别开启
        self.assertTrue(any(cfg.category_active(c, p) for c in s["category"]))

    def test_example_profile(self):
        p = cfg.load_profile(os.path.join(cfg.CONFIG_DIR, "profile.example.toml"))
        errs, _ = cfg.validate_profile(p)
        self.assertEqual(errs, [])
        self.assertEqual(p["status"]["cohort_system"], "none")
        self.assertNotIn("cohort_year", p["status"])
        errs, warns = cfg.validate_sources(cfg.load_sources(), p)
        self.assertEqual(errs, [])
        self.assertTrue(any("将关闭" in w for w in warns))

    # 允许含个人信息的文件（README「隐私」）：画像、简历与历史数据；其余所有纳入 git 的文件都不得出现
    PERSONAL_INFO_ALLOWED = {"config/profile.toml", "config/profile.example.toml", "resume_en.md", "resume_zh.md",
                             "jobs.csv", "dismissed.csv", "sources_found.md", "sheet_id.txt"}
    # profile.example.toml 是虚构人物，但 fork 者常从它复制 profile.toml，二者相同的字段值不算泄露

    def test_no_personal_info_outside_profile(self):
        """姓名、邮箱、出生年月、学校只允许出现在 profile.toml、简历与历史数据中（README「隐私」）。

        遍历 `git ls-files` 减白名单（PROMPT.md、README.md、tools/**、tests/** 等全部覆盖）；
        失败时只报文件名与字段名，不回显值，以免个人信息进入 CI 日志。
        """
        p = cfg.load_profile()
        ident = p["identity"]
        needles = {"identity.name": ident["name"],
                   "identity.name_en": ident.get("name_en") or "",
                   "identity.email": ident["email"],
                   "identity.birth_month": str(ident.get("birth_month") or "")[:7]}
        for i, e in enumerate(p["education"]):
            needles[f"education[{i}].institution"] = e["institution"]
            needles[f"education[{i}].institution_en"] = e.get("institution_en") or ""
        # 与虚构示例相同的值（fork 者直接复制 profile.example.toml 时）不算个人信息
        example = cfg.load_profile(os.path.join(ROOT, "config", "profile.example.toml"))
        fictional = {example["identity"].get(k) or "" for k in ("name", "name_en", "email")}
        fictional |= {str(example["identity"].get("birth_month") or "")[:7]}
        for e in example.get("education", []):
            fictional |= {e.get("institution") or "", e.get("institution_en") or ""}
        needles = {k: v for k, v in needles.items() if v and v not in fictional}
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        self.assertTrue(files, "git ls-files 为空")
        for rel in files:
            if rel in self.PERSONAL_INFO_ALLOWED:
                continue
            path = os.path.join(ROOT, rel)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for field, val in needles.items():
                self.assertFalse(bool(val) and val in text, f"{rel} 含个人信息（字段 {field}），值不在此回显")

    def test_sources_frequency_budget(self):
        """按 sources.toml 自己的调度字段估算：全周平均每天 ≤ max_queries_per_day（11 月旺季）。"""
        import math
        s = cfg.load_sources()
        epoch = s["schedule"]["epoch"]
        total = 0.0
        days = 14
        for i in range(days):
            day = dt.date(2026, 11, 10) + dt.timedelta(days=i)
            d = (day - epoch).days
            for c in s["category"]:
                every = c["every"]
                season = c.get("season")
                if season and day.month in season["months"]:
                    every = season["every"]
                elif c.get("active_months") and day.month not in c["active_months"]:
                    every = 7
                if (d + c["offset"]) % every != 0:
                    continue
                q = c["queries_per_turn"] + len(c["pinned"])
                if day.isoweekday() >= 6:
                    q = max(1, math.ceil(q * s["schedule"]["weekend_factor"]))
                total += q
        self.assertLessEqual(total / days, s["schedule"]["max_queries_per_day"], total / days)


class CliTest(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cfg.main(argv)
        return code, buf.getvalue()

    def test_check_ok(self):
        code, out = self.run_main(["--check"])
        self.assertEqual(code, 0)
        self.assertIn("配置检查通过", out)

    def test_check_example(self):
        code, out = self.run_main(["--check", "--profile", os.path.join(cfg.CONFIG_DIR, "profile.example.toml")])
        self.assertEqual(code, 0, out)

    def test_check_errors_exit_1(self):
        path = write_tmp(MIN_PROFILE.replace('email = "t@example.com"\n', ""))
        try:
            code, out = self.run_main(["--check", "--profile", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 1)
        self.assertIn("错误：[profile] identity.email", out)
        path = write_tmp(MIN_PROFILE.replace('timezone = "Europe/Amsterdam"\n', ""))
        try:
            code, out = self.run_main(["--check", "--profile", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 1)
        self.assertIn("错误：[profile] identity.timezone 缺失或为空（必填，IANA 时区名）", out)
        code, out = self.run_main(["--check", "--profile", "/nonexistent.toml"])
        self.assertEqual(code, 1)
        self.assertIn("找不到配置文件", out)

    def test_dump(self):
        code, out = self.run_main(["--dump"])
        self.assertEqual(code, 0)
        import json
        data = json.loads(out)
        self.assertIn("profile", data)
        self.assertIn("sources", data)


if __name__ == "__main__":
    unittest.main()
