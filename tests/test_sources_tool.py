"""tools/sources_tool.py 的单元测试：python3 -m unittest tests.test_sources_tool"""
import datetime as dt
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import sources_tool as st  # noqa: E402

SOURCES = """
schema = 1
[schedule]
epoch = 2026-11-10
[[category]]
id = "cat_a"
name = "甲类"
browse = ["https://www.browse.example/page/"]
[[category]]
id = "cat_b"
name = "乙类"
[[source]]
id = "src_a"
category = "cat_a"
url = "https://list.example/search?keywords=robotics&sortOrder=1"
pages = ["https://list.example/page2"]
link_pattern = "/job/"
"""

FOUND = """# 待人工核实的新来源

说明文字，不是条目。

- https://found.example/one ｜ 第一条 ｜ 2026-10-01 ｜ cat_a
- https://found.example/two ｜ 第二条 ｜ 2026-10-02 ｜ 待定
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sources = os.path.join(self.dir, "sources.toml")
        self.found = os.path.join(self.dir, "sources_found.md")
        with open(self.sources, "w", encoding="utf-8") as f:
            f.write(SOURCES)
        with open(self.found, "w", encoding="utf-8") as f:
            f.write(FOUND)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = st.main(["--file", self.found, "--sources", self.sources] + argv)
        return code, buf.getvalue()

    def read_found(self):
        with open(self.found, encoding="utf-8") as f:
            return f.read()


class CheckTest(Base):
    def test_new(self):
        code, out = self.run_main(["check", "https://new.example/x"])
        self.assertEqual(code, 0)
        self.assertIn("新来源", out)

    def test_existing_in_sources_toml(self):
        # 协议、www.、末尾斜杠、参数顺序不同也算同一 URL
        for url, where in [("http://list.example/search?sortOrder=1&keywords=robotics", "src_a"),
                           ("https://list.example/page2/", "src_a"),
                           ("http://browse.example/page", "cat_a")]:
            code, out = self.run_main(["check", url])
            self.assertEqual(code, 2, url)
            self.assertIn(where, out)

    def test_existing_in_found(self):
        code, out = self.run_main(["check", "https://www.found.example/two/?utm_source=a"])
        self.assertEqual(code, 2)
        self.assertIn("第 6 行", out)

    def test_bad_url(self):
        code, _ = self.run_main(["check", "not a url"])
        self.assertEqual(code, 1)


class AddTest(Base):
    def test_add_format(self):
        code, out = self.run_main(["add", "https://new.example/x", "--note", "某校人事处 ｜ 含竖线", "--category", "cat_b", "--today", "2026-11-04"])
        self.assertEqual(code, 0, out)
        text = self.read_found()
        self.assertTrue(text.endswith("- https://new.example/x ｜ 某校人事处 / 含竖线 ｜ 2026-11-04 ｜ cat_b\n"), text)
        # 第二次追加同一 URL → 已有
        code, out = self.run_main(["add", "http://www.new.example/x/", "--note", "再来一次", "--today", "2026-11-04"])
        self.assertEqual(code, 2)
        self.assertEqual(text, self.read_found())

    def test_add_default_category_and_lint_passes(self):
        code, _ = self.run_main(["add", "https://new.example/y", "--note", "没有类别", "--today", "2026-11-04"])
        self.assertEqual(code, 0)
        self.assertIn("｜ 2026-11-04 ｜ 待定\n", self.read_found())
        code, out = self.run_main(["lint"])
        self.assertEqual(code, 0, out)

    def test_add_rejects_flow_note_and_bad_category(self):
        code, out = self.run_main(["add", "https://new.example/z", "--note", "2026-11-01复查无新岗位"])
        self.assertEqual(code, 1)
        self.assertIn("复查流水", out)
        code, out = self.run_main(["add", "https://new.example/z", "--note", "正常说明", "--category", "cat_zzz"])
        self.assertEqual(code, 1)
        self.assertIn("不是 sources.toml 中的类别 id", out)
        code, out = self.run_main(["add", "https://new.example/z", "--note", "正常说明", "--today", "11/04"])
        self.assertEqual(code, 1)
        self.assertNotIn("new.example/z", self.read_found())

    def test_add_default_date_uses_profile_timezone(self):
        """不传 --today 时发现日期取 default_today()（profile 时区当天），与 jobs.csv 的「今天」一致。"""
        with mock.patch("sources_tool.default_today", return_value=dt.date(2026, 11, 5)) as m:
            code, out = self.run_main(["add", "https://new.example/tz", "--note", "默认日期"])
        self.assertEqual(code, 0, out)
        self.assertEqual(m.call_count, 1)
        self.assertIn("- https://new.example/tz ｜ 默认日期 ｜ 2026-11-05 ｜ 待定\n", self.read_found())
        # 传了 --today 则不调用 default_today
        with mock.patch("sources_tool.default_today") as m:
            code, _ = self.run_main(["add", "https://new.example/tz2", "--note", "显式日期", "--today", "2026-11-04"])
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, 0)

    def test_default_today_follows_profile_timezone_and_falls_back(self):
        import config as cfg
        import profile_tool as pt
        fake_profile = {"identity": {"timezone": "Pacific/Kiritimati"}}   # UTC+14：最早进入「明天」的时区
        with mock.patch.object(cfg, "load_profile", return_value=fake_profile):
            self.assertEqual(st.default_today(), pt.today_for(fake_profile))
        with mock.patch.object(cfg, "load_profile", side_effect=FileNotFoundError("找不到配置文件")):
            self.assertEqual(st.default_today(), dt.date.today())

    def test_add_to_missing_file_writes_header(self):
        os.unlink(self.found)
        code, _ = self.run_main(["add", "https://new.example/x", "--note", "首条", "--today", "2026-11-04"])
        self.assertEqual(code, 0)
        text = self.read_found()
        self.assertTrue(text.startswith("# 待人工核实的新来源"))
        self.assertTrue(text.endswith("｜ 待定\n"))

    def test_append_adds_missing_newline(self):
        with open(self.found, "a", encoding="utf-8") as f:
            f.write("- https://found.example/three ｜ 无换行结尾 ｜ 2026-10-03 ｜ 待定")
        code, _ = self.run_main(["add", "https://new.example/x", "--note", "追加", "--today", "2026-11-04"])
        self.assertEqual(code, 0)
        entries, errors = st.parse_sources_found(self.found)
        self.assertEqual(errors, [])
        self.assertEqual([e["url"] for e in entries][-2:], ["https://found.example/three", "https://new.example/x"])


class LintTest(Base):
    def test_clean_passes(self):
        code, out = self.run_main(["lint"])
        self.assertEqual(code, 0, out)
        self.assertIn("2 条", out)

    def test_catches_problems(self):
        with open(self.found, "a", encoding="utf-8") as f:
            f.write("- https://found.example/one ｜ 重复 URL ｜ 2026-10-05 ｜ cat_a\n")
            f.write("- https://www.academictransfer.com/en/jobs/?q=drone 2026-10-31复查：仅见无人机 PhD，无对口岗位\n")
            f.write("- https://found.example/four ｜ 2026-11-01 复查无新岗位 ｜ 2026-11-01 ｜ cat_a\n")
            f.write("- https://found.example/five ｜ 日期错 ｜ 2026/11/01 ｜ cat_a\n")
            f.write("- https://found.example/six ｜ 类别错 ｜ 2026-11-01 ｜ cat_zzz\n")
            f.write("- ftp://found.example/seven ｜ 协议错 ｜ 2026-11-01 ｜ 待定\n")
            f.write("- https://list.example/page2 ｜ 已在 sources.toml ｜ 2026-11-01 ｜ cat_a\n")
        problems = st.lint(self.found, self.sources)
        joined = "\n".join(problems)
        self.assertIn("重复", joined)
        self.assertIn("应为 4 段", joined)
        self.assertIn("复查流水", joined)
        self.assertIn("YYYY-MM-DD", joined)
        self.assertIn("cat_zzz", joined)
        self.assertIn("URL 不合法", joined)
        self.assertIn("已在 sources.toml 来源 src_a 中", joined)
        code, out = self.run_main(["lint"])
        self.assertEqual(code, 1)
        self.assertIn("检查未通过", out)

    def test_ascii_separator_accepted(self):
        with open(self.found, "a", encoding="utf-8") as f:
            f.write("- https://found.example/eight | 半角竖线 | 2026-11-01 | cat_b\n")
        self.assertEqual(st.lint(self.found, self.sources), [])

    def test_repo_file_passes(self):
        """仓库里的 sources_found.md 本身必须通过 lint（CI 同样运行）。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = st.main(["lint"])
        self.assertEqual(code, 0, buf.getvalue())


class FormatTest(unittest.TestCase):
    def test_format_line(self):
        import datetime as dt
        self.assertEqual(st.format_line(" https://a.example/ ", "说明\n换行", dt.date(2026, 11, 4), ""),
                         "- https://a.example/ ｜ 说明 换行 ｜ 2026-11-04 ｜ 待定")


if __name__ == "__main__":
    unittest.main()
