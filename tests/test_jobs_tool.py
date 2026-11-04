"""jobs_tool 的单元测试：python3 -m unittest discover -s tests"""
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import jobs_tool as jt  # noqa: E402

TODAY = dt.date(2026, 11, 4)


def row(**kw):
    r = {c: "" for c in jt.COLUMNS}
    r.update({"岗位ID": kw.get("链接", "https://a.example/1"), "链接": "https://a.example/1",
              "首次推送日期": "2026-10-20", "匹配等级": "可考虑", "单位": "某单位", "岗位": "某岗位",
              "城市": "南京", "截止日期": "", "截止说明": "需核实", "状态": "持续"})
    r.update(kw)
    return r


class ParseTest(unittest.TestCase):
    def test_columns(self):
        self.assertEqual(len(jt.COLUMNS), 17)
        self.assertEqual(len(jt.LEGACY_COLUMNS), 16)
        self.assertEqual(jt.COLUMNS.index("截止说明"), jt.COLUMNS.index("截止日期") + 1)
        self.assertEqual([c for c in jt.COLUMNS if c != "截止说明"], jt.LEGACY_COLUMNS)

    def test_parse_date(self):
        self.assertEqual(jt.parse_date("2026-11-05（网申09-06至11-05）"), dt.date(2026, 11, 5))
        self.assertEqual(jt.parse_date("2026-10-30 17:00"), dt.date(2026, 10, 30))
        for text in ["需核实", "Rolling", "长期（2026年度有效）", "2026-10下旬（需核实）", "", "2026-13-01"]:
            self.assertIsNone(jt.parse_date(text), text)

    def test_normalize_url(self):
        a = jt.normalize_url("https://www.gwy.com/gqzp/413546.html/?utm_source=x#top")
        self.assertEqual(a, jt.normalize_url("http://gwy.com/gqzp/413546.html"))
        self.assertNotEqual(a, jt.normalize_url("https://gwy.com/gqzp/413547.html"))

    def test_dont_want_reason(self):
        self.assertEqual(jt.dont_want_reason("不要，qs太低"), "qs太低")
        self.assertEqual(jt.dont_want_reason("不要"), "本人标记不要")
        self.assertIsNone(jt.dont_want_reason("已经投递"))
        # 前缀可由 profile 配置
        self.assertEqual(jt.dont_want_reason("pass: too far", ["不要", "pass"]), "too far")
        self.assertIsNone(jt.dont_want_reason("pass: too far", ["不要"]))


class ProfileTest(unittest.TestCase):
    def tearDown(self):
        jt.set_profile(None)

    def test_defaults_without_profile(self):
        jt.set_profile({})
        self.assertEqual(jt.timezone_name(), "Europe/Amsterdam")
        self.assertEqual(jt.dismiss_prefixes(), ["不要"])
        self.assertIsInstance(jt.today(), dt.date)

    def test_profile_values(self):
        jt.set_profile({"identity": {"timezone": "Asia/Shanghai"}, "marks": {"dismiss_prefixes": ["不要", "pass"]}})
        self.assertEqual(jt.timezone_name(), "Asia/Shanghai")
        self.assertEqual(jt.dismiss_prefixes(), ["不要", "pass"])
        self.assertEqual(jt.judge(row(我的标记="pass：太远"), TODAY, set()), ("归档", "太远"))
        self.assertIsInstance(jt.today(), dt.date)

    def test_bad_timezone_falls_back(self):
        jt.set_profile({"identity": {"timezone": "Mars/Olympus"}})
        self.assertIsInstance(jt.today(), dt.date)

    def test_repo_profile_loads(self):
        """仓库 config/profile.toml 存在时应能通过 tools/config.py 读到时区。"""
        if not os.path.exists(os.path.join(jt.ROOT, "config", "profile.toml")):
            self.skipTest("no profile")
        jt.set_profile(None)
        self.assertTrue(jt.timezone_name())
        self.assertTrue(jt.dismiss_prefixes())


class JudgeTest(unittest.TestCase):
    def judge(self, **kw):
        return jt.judge(row(**kw), TODAY, set())

    def test_deadline_windows(self):
        self.assertEqual(self.judge(截止日期="2026-11-12"), ("持续", ""))   # 8 天
        self.assertEqual(self.judge(截止日期="2026-11-11"), ("预警", ""))   # 7 天
        self.assertEqual(self.judge(截止日期="2026-11-04"), ("预警", ""))   # 当天
        self.assertEqual(self.judge(截止日期="2026-11-03"), ("过期", ""))
        self.assertEqual(self.judge(截止日期="2026-10-29"), ("过期", ""))   # 过期 6 天
        self.assertEqual(self.judge(截止日期="2026-10-28"), ("归档", "已过期"))  # 满 7 天

    def test_expired_reasons_by_mark(self):
        self.assertEqual(self.judge(截止日期="2026-10-01", 投递状态="已投递", 我的标记="已经投递"),
                         ("归档", "已过期，已投递"))
        self.assertEqual(self.judge(截止日期="2026-10-01", 我的标记="考虑投递"), ("归档", "已过期，未投递"))
        # 进展中（笔试/面试）不因过期自动归档
        self.assertEqual(self.judge(截止日期="2026-10-01", 投递状态="进展中", 我的标记="面试"), ("过期", ""))

    def test_new_today(self):
        self.assertEqual(self.judge(首次推送日期="2026-11-04"), ("新增", ""))

    def test_no_deadline_60_days(self):
        self.assertEqual(self.judge(首次推送日期="2026-09-05"), ("归档", "长期无截止日期"))
        self.assertEqual(self.judge(首次推送日期="2026-09-06"), ("持续", ""))
        # 软截止（日期为空、说明以「软截止」开头）同样按不详处理
        self.assertEqual(self.judge(首次推送日期="2026-09-05", 截止说明="软截止 2026-10-01；招满为止"), ("归档", "长期无截止日期"))
        # 有标记或投递状态的不按 60 天规则归档
        self.assertEqual(self.judge(首次推送日期="2026-09-01", 我的标记="尝试", 投递状态="进行中"), ("持续", ""))
        self.assertEqual(self.judge(首次推送日期="2026-09-01", 我的标记="关注"), ("持续", ""))

    def test_dismissed(self):
        self.assertEqual(self.judge(我的标记="不要：限本科双一流"), ("归档", "限本科双一流"))
        r = row(链接="https://www.b.example/job/")
        self.assertEqual(jt.judge(r, TODAY, {jt.normalize_url("http://b.example/job")}), ("归档", "本人标记不要"))

    def test_update_status_skips_archived(self):
        rows = [row(状态="归档", 归档日期="2026-10-01", 归档原因="x", 截止日期="2027-01-01"),
                row(岗位ID="b", 链接="https://a.example/2", 截止日期="2026-10-20")]
        changes = jt.update_status(rows, TODAY, [])
        self.assertEqual(rows[0]["状态"], "归档")
        self.assertEqual(rows[0]["归档日期"], "2026-10-01")
        self.assertEqual(rows[1]["状态"], "归档")
        self.assertEqual(rows[1]["归档日期"], "2026-11-04")
        self.assertEqual(rows[1]["状态更新日期"], "2026-11-04")
        self.assertEqual(len(changes), 1)


class ValidateTest(unittest.TestCase):
    def test_ok(self):
        errors, warnings = jt.validate(jt.COLUMNS, [row()])
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_errors(self):
        rows = [row(), row(状态="待定", 首次推送日期="10/20")]
        errors, _ = jt.validate(jt.COLUMNS, rows)
        text = "\n".join(errors)
        self.assertIn("重复", text)
        self.assertIn("状态「待定」", text)
        self.assertIn("首次推送日期「10/20」", text)

    def test_deadline_must_be_iso_or_empty(self):
        errors, _ = jt.validate(jt.COLUMNS, [row(截止日期="需核实")])
        self.assertTrue(any("截止日期「需核实」" in e for e in errors))
        errors, _ = jt.validate(jt.COLUMNS, [row(截止日期="2026-12-31", 截止说明="按季度受理")])
        self.assertEqual(errors, [])

    def test_header(self):
        errors, _ = jt.validate(jt.COLUMNS[:-1], [])
        self.assertTrue(errors)
        # 旧版 16 列表头只警告
        errors, warnings = jt.validate(jt.LEGACY_COLUMNS, [])
        self.assertEqual(errors, [])
        self.assertTrue(any("旧版 16 列" in w for w in warnings))

    def test_legacy_info_warning(self):
        info = jt.ReadInfo(legacy_header=True)
        errors, warnings = jt.validate(jt.COLUMNS, [row()], info)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("migrate", warnings[0])

    def test_mark_and_note_hygiene_warnings(self):
        """「我的标记」「备注」含控制字符 / 公式前缀 / 过长 → 只警告，不报错。"""
        rows = [row(我的标记="=HYPERLINK(\"x\")"),
                row(岗位ID="2", 链接="https://a.example/2", 备注="含\x00控制字符"),
                row(岗位ID="3", 链接="https://a.example/3", 我的标记="长" * 201),
                row(岗位ID="4", 链接="https://a.example/4", 备注="-" + "x" * 1000)]
        errors, warnings = jt.validate(jt.COLUMNS, rows)
        self.assertEqual(errors, [])
        text = "\n".join(warnings)
        self.assertIn("「我的标记」以公式前缀开头", text)
        self.assertIn("「备注」含控制字符", text)
        self.assertIn("「我的标记」过长", text)
        self.assertIn("「备注」以公式前缀开头", text)
        self.assertIn("「备注」过长", text)
        self.assertEqual(len(warnings), 5)
        # 正常文字与 200 字以内不警告；「不要：」等中文前缀不是公式
        _, warnings = jt.validate(jt.COLUMNS, [row(我的标记="不要：太远", 备注="x" * 200)])
        self.assertEqual(warnings, [])


class ReportTest(unittest.TestCase):
    def test_summary_order(self):
        rows = [
            row(岗位ID="1", 链接="https://x/1", 状态="持续", 截止日期="2026-12-01"),
            row(岗位ID="2", 链接="https://x/2", 状态="过期", 截止日期="2026-11-01"),
            row(岗位ID="3", 链接="https://x/3", 状态="预警", 截止日期="2026-11-08"),
            row(岗位ID="4", 链接="https://x/4", 状态="预警", 截止日期="2026-11-10", 我的标记="考虑投递"),
            row(岗位ID="5", 链接="https://x/5", 状态="新增", 首次推送日期="2026-11-04"),
            row(岗位ID="6", 链接="https://x/6", 状态="归档", 归档日期="2026-11-04", 归档原因="已过期"),
            row(岗位ID="7", 链接="https://x/7", 状态="归档", 归档日期="2026-11-01", 归档原因="已过期"),
            row(岗位ID="8", 链接="https://x/8", 状态="持续", 截止日期="", 截止说明="需核实"),
        ]
        ids = [r["岗位ID"] for _, r in jt.summary_rows(rows, TODAY)]
        self.assertEqual(ids, ["4", "3", "5", "1", "8", "2", "6"])
        html = jt.render_html(rows, TODAY)
        self.assertIn("你标记了考虑投递", html)
        self.assertIn("text-decoration:line-through", html)
        self.assertNotIn("<style", html)
        # 样式压缩：字号只写在 <table> 上，单元格不再重复 font-size / vertical-align
        self.assertEqual(html.count("font-size:13px"), 2)  # 图例段落 + 表格
        self.assertNotIn("vertical-align:top;\">", html.split("<td", 1)[1].split("</td>")[0])
        self.assertIn('<tr style="vertical-align:top;">', html)  # 持续行：白底不写 background
        self.assertEqual(html.count("<tr"), 1 + 7)

    def test_render_html_compact_collapses_continuing_rows(self):
        rows = [row(岗位ID=str(i), 链接=f"https://x/{i}", 状态="持续", 截止日期="2026-12-01", 单位="单位" * 10, 岗位="岗位" * 10)
                for i in range(300)]
        rows += [row(岗位ID="w", 链接="https://x/w", 状态="预警", 截止日期="2026-11-08"),
                 row(岗位ID="n", 链接="https://x/n", 状态="新增", 首次推送日期="2026-11-04"),
                 row(岗位ID="e", 链接="https://x/e", 状态="过期", 截止日期="2026-11-01"),
                 row(岗位ID="a", 链接="https://x/a", 状态="归档", 归档日期="2026-11-04", 归档原因="已过期")]
        full = jt.render_html(rows, TODAY)
        compact = jt.render_html(rows, TODAY, compact=True)
        self.assertGreater(len(full.encode()), jt.HTML_SIZE_LIMIT)
        self.assertLess(len(compact.encode()), 60_000)
        self.assertIn("持续 300 条未在邮件中逐条列出，见在线表格「岗位总表」", compact)
        self.assertEqual(compact.count("<tr"), 1 + 4 + 1)  # 表头 + 预警/新增/过期/当天归档 + 折叠行
        self.assertNotIn("https://x/5", compact)
        for link in ("https://x/w", "https://x/n", "https://x/e", "https://x/a"):
            self.assertIn(link, compact)
        # 折叠行位于新增之后、过期之前（与 SUMMARY_ORDER 一致）
        self.assertLess(compact.index("https://x/n"), compact.index("持续 300 条"))
        self.assertLess(compact.index("持续 300 条"), compact.index("https://x/e"))
        # 没有持续行时 compact 与完整版一致
        few = rows[300:]
        self.assertEqual(jt.render_html(few, TODAY, compact=True), jt.render_html(few, TODAY))

    def test_deadline_rendering(self):
        rows = [row(岗位ID="1", 链接="https://x/1", 截止日期="2026-11-05", 截止说明="网申09-06至11-05"),
                row(岗位ID="2", 链接="https://x/2", 截止日期="", 截止说明="Rolling；open until filled"),
                row(岗位ID="3", 链接="https://x/3", 截止日期="2026-12-01", 截止说明="")]
        html = jt.render_html(rows, TODAY)
        self.assertIn('2026-11-05<br><span style="color:#9AA0A6;font-size:11px">网申09-06至11-05</span>', html)
        self.assertIn('<span style="color:#9AA0A6;font-size:11px">Rolling；open until filled</span>', html)
        self.assertNotIn("<br><span", html.split("Rolling")[0].rsplit("<tr", 1)[1])  # 无日期时不出现 <br>
        text = jt.render_text(rows, TODAY)
        self.assertIn("｜2026-11-05（网申09-06至11-05）｜1｜", text)
        self.assertIn("｜Rolling；open until filled｜—｜", text)
        self.assertIn("｜2026-12-01｜27｜", text)
        self.assertEqual(jt.deadline_text(rows[2]), "2026-12-01")
        self.assertEqual(jt.deadline_html(rows[2]), "2026-12-01")

    def test_sheets_payload(self):
        rows = [row(投递状态="已投递", 我的标记="已经投递", 截止日期="2026-11-06", 截止说明="17:00")]
        payload = jt.sheets_payload(rows, TODAY)
        titles = [s["title"] for s in payload["sheets"]]
        self.assertEqual(titles, ["岗位总表", "已投递", "归档"])
        applied = payload["sheets"][1]["rows"][0]
        self.assertTrue(applied["emphasize_days_left"])
        self.assertEqual(applied["values"][payload["sheets"][1]["link_column"]], "原文")
        for sheet in payload["sheets"]:
            h = sheet["header"]
            self.assertEqual(h.index("截止说明"), h.index("截止日期") + 1)
            self.assertEqual(sheet["mark_column"], h.index("我的标记"))
            self.assertEqual(sheet["deadline_note_column"], h.index("截止说明"))
            for r in sheet["rows"]:
                self.assertEqual(len(r["values"]), len(h))
                self.assertEqual(r["values"][sheet["mark_column"]], "已经投递")
                self.assertEqual(r["values"][h.index("截止说明")], "17:00")
        json.dumps(payload, ensure_ascii=False)


class FileRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "jobs.csv")
        self.dismissed = os.path.join(self.dir, "dismissed.csv")
        with open(self.dismissed, "w", encoding="utf-8") as f:
            f.write("链接\n")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *args):
        return jt.main(["--csv", self.csv, "--dismissed", self.dismissed, "--today", "2026-11-04", *args])

    def test_roundtrip_preserves_bytes(self):
        jt.write_jobs([row(备注="含,逗号与\"引号\"")], self.csv)
        with open(self.csv, "rb") as f:
            before = f.read()
        self.assertIn(b"\r\n", before)
        self.assertTrue(before.startswith(",".join(jt.COLUMNS).encode("utf-8") + b"\r\n"))
        _, rows = jt.read_jobs(self.csv)
        jt.write_jobs(rows, self.csv)
        with open(self.csv, "rb") as f:
            self.assertEqual(before, f.read())

    def test_add_and_duplicate(self):
        jt.write_jobs([], self.csv)
        base = ["add", "--unit", "紫金山实验室", "--title", "博士后（具身智能）", "--link",
                "https://zjs.example/job/1", "--level", "⭐高匹配", "--deadline", "2026-11-30", "--city", "南京"]
        self.assertEqual(self.run_cli(*base), 0)
        self.assertEqual(self.run_cli(*base), 2)  # 重复被拒
        _, rows = jt.read_jobs(self.csv)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["状态"], "新增")
        self.assertEqual(rows[0]["首次推送日期"], "2026-11-04")
        self.assertEqual(rows[0]["截止日期"], "2026-11-30")
        self.assertEqual(rows[0]["截止说明"], "")
        # 已过截止的不追加
        expired = base[:]
        expired[expired.index("https://zjs.example/job/1")] = "https://zjs.example/job/2"
        expired[expired.index("博士后（具身智能）")] = "另一岗位"
        expired[expired.index("2026-11-30")] = "2026-11-01"
        self.assertEqual(self.run_cli(*expired), 1)
        self.assertEqual(self.run_cli("validate"), 0)

    def test_add_deadline_note_and_split(self):
        jt.write_jobs([], self.csv)
        common = ["--level", "可考虑", "--city", "北京"]
        # 省略 --deadline → 说明「需核实」
        self.assertEqual(self.run_cli("add", "--unit", "甲", "--title", "岗1", "--link", "https://x/1", *common), 0)
        # ISO 日期 + 说明
        self.assertEqual(self.run_cli("add", "--unit", "乙", "--title", "岗2", "--link", "https://x/2",
                                      "--deadline", "2026-11-30", "--deadline-note", "17:00", *common), 0)
        # 非 ISO 自动拆分
        self.assertEqual(self.run_cli("add", "--unit", "丙", "--title", "岗3", "--link", "https://x/3",
                                      "--deadline", "需核实（原文未写；2026-10-29发布）", *common), 0)
        # 软截止：日期置空，且不因「已过」被拒
        self.assertEqual(self.run_cli("add", "--unit", "丁", "--title", "岗4", "--link", "https://x/4",
                                      "--deadline", "2026-10-19（开始审阅，招满为止）", "--deadline-note", "另见邮件", *common), 0)
        _, rows = jt.read_jobs(self.csv)
        got = [(r["截止日期"], r["截止说明"]) for r in rows]
        self.assertEqual(got, [("", "需核实"), ("2026-11-30", "17:00"), ("", "需核实；原文未写；2026-10-29发布"),
                               ("", "软截止 2026-10-19；开始审阅，招满为止；另见邮件")])
        self.assertEqual(self.run_cli("validate", "--strict"), 0)
        # 「招满为止」单独出现不是软截止：日期保留，已过期的仍被 add 拦截
        self.assertEqual(self.run_cli("add", "--unit", "戊", "--title", "岗5", "--link", "https://x/5",
                                      "--deadline", "2026-10-19（招满为止）", *common), 1)
        self.assertEqual(self.run_cli("add", "--unit", "戊", "--title", "岗5", "--link", "https://x/5",
                                      "--deadline", "2027-08-29（长期有效，招满为止）", *common), 0)
        _, rows = jt.read_jobs(self.csv)
        self.assertEqual((rows[-1]["截止日期"], rows[-1]["截止说明"]), ("2027-08-29", "长期有效，招满为止"))

    def test_common_options_before_or_after_subcommand(self):
        jt.write_jobs([row(截止日期="2026-11-06")], self.csv)
        with open(self.csv, "rb") as f:
            before = f.read()
        buf = io.StringIO()
        with redirect_stdout(buf):
            # 全部放在子命令后
            self.assertEqual(jt.main(["status", "--dry-run", "--csv", self.csv, "--dismissed", self.dismissed,
                                      "--today", "2026-11-04"]), 0)
            # 子命令前后混用：子命令后的值覆盖子命令前的值
            self.assertEqual(jt.main(["--today", "2026-01-01", "--csv", self.csv, "status", "--dry-run",
                                      "--today", "2026-11-04", "--dismissed", self.dismissed]), 0)
            self.assertEqual(jt.main(["validate", "--csv", self.csv]), 0)
            self.assertEqual(jt.main(["report", "--csv", self.csv, "--today", "2026-11-04", "--out",
                                      os.path.join(self.dir, "out")]), 0)
        out = buf.getvalue()
        self.assertEqual(out.count("今天：2026-11-04"), 2)
        self.assertNotIn("今天：2026-01-01", out)
        with open(self.csv, "rb") as f:
            self.assertEqual(before, f.read())  # dry-run 未写
        # 子命令后省略时沿用子命令前的值（不会被 None 覆盖）
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(jt.main(["--csv", self.csv, "--today", "2026-11-04", "status", "--dry-run"]), 0)
        self.assertIn("今天：2026-11-04", buf.getvalue())


class SetCommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "jobs.csv")
        self.rows = [
            row(岗位ID="https://a.example/1", 链接="https://a.example/1", 单位="甲单位", 岗位="岗位A", 截止日期="2026-11-20"),
            row(岗位ID="https://a.example/2", 链接="https://a.example/2", 单位="乙单位", 岗位="岗位B", 状态="归档",
                归档日期="2026-10-20", 归档原因="太远", 我的标记="不要：太远", 截止日期="2026-12-01"),
            row(岗位ID="https://a.example/3", 链接="https://a.example/3", 单位="丙单位", 岗位="岗位C", 状态="归档",
                归档日期="2026-10-20", 归档原因="已过期", 截止日期="2026-10-01"),
            row(岗位ID="https://a.example/4", 链接="https://a.example/4", 单位="丁单位", 岗位="岗位D", 状态="归档",
                归档日期="2026-10-20", 归档原因="", 截止日期="", 备注="旧备注"),
        ]
        jt.write_jobs(self.rows, self.csv)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_set(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = jt.main(["--csv", self.csv, "--today", "2026-11-04", "set", *args])
        return code, buf.getvalue()

    def read(self, i):
        return jt.read_jobs(self.csv)[1][i]

    def test_requires_target_and_edit(self):
        code, out = self.run_set("https://a.example/1")
        self.assertEqual(code, 1)
        self.assertIn("至少给出一个修改项", out)
        code, out = self.run_set("--mark", "关注")
        self.assertEqual(code, 1)
        self.assertIn("--unit", out)
        code, out = self.run_set("https://nope.example/x", "--mark", "关注")
        self.assertEqual(code, 1)
        self.assertIn("找不到该岗位", out)
        code, out = self.run_set("--unit", "没有", "--title", "岗位A", "--mark", "关注")
        self.assertEqual(code, 1)
        code, out = self.run_set("https://a.example/1", "--archive", "x", "--restore")
        self.assertEqual(code, 1)
        self.assertIn("不能同时使用", out)
        code, out = self.run_set("https://a.example/1", "--apply-state", "面试")
        self.assertEqual(code, 1)
        self.assertIn("--apply-state", out)
        code, out = self.run_set("https://a.example/1", "--apply-date", "11/04")
        self.assertEqual(code, 1)
        self.assertEqual(self.run_set("https://a.example/1", "--mark", "=1+1")[0], 1)
        self.assertEqual(self.read(0)["我的标记"], "")

    def test_mark_apply_state_and_note(self):
        code, out = self.run_set("https://www.a.example/1/", "--mark", "已投递", "--apply-state", "已投递",
                                 "--append-note", "邮件投递")
        self.assertEqual(code, 0)
        self.assertIn("已更新：甲单位｜岗位A：我的标记 「」→「已投递」；投递状态 「」→「已投递」；投递日期 「」→「2026-11-04」；"
                      "备注 「」→「邮件投递」", out)
        r = self.read(0)
        self.assertEqual((r["我的标记"], r["投递状态"], r["投递日期"], r["备注"]), ("已投递", "已投递", "2026-11-04", "邮件投递"))
        self.assertEqual(r["状态"], "持续")  # 不改状态
        # 再改投递状态：投递日期已有值，不再覆盖；备注用「；」追加
        code, out = self.run_set("--unit", "甲单位", "--title", "岗位A", "--apply-state", "进展中", "--append-note", "笔试通过")
        self.assertEqual(code, 0)
        r = self.read(0)
        self.assertEqual((r["投递状态"], r["投递日期"], r["备注"]), ("进展中", "2026-11-04", "邮件投递；笔试通过"))
        # 显式 --apply-date 覆盖
        self.assertEqual(self.run_set("https://a.example/1", "--apply-date", "2026-11-01")[0], 0)
        self.assertEqual(self.read(0)["投递日期"], "2026-11-01")
        # 无变更
        code, out = self.run_set("https://a.example/1", "--apply-date", "2026-11-01")
        self.assertEqual(code, 0)
        self.assertIn("无变更", out)
        header, rows, info = jt.read_jobs_detailed(self.csv)
        errors, warnings = jt.validate(header, rows, info)
        self.assertEqual(errors, [])
        self.assertFalse(any("岗位A" in w for w in warnings))

    def test_archive_and_dry_run(self):
        with open(self.csv, "rb") as f:
            before = f.read()
        code, out = self.run_set("https://a.example/1", "--archive", "放弃：方向不符", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("已更新：甲单位｜岗位A：状态 「持续」→「归档」；归档日期 「」→「2026-11-04」；归档原因 「」→「放弃：方向不符」", out)
        self.assertIn("--dry-run", out)
        with open(self.csv, "rb") as f:
            self.assertEqual(before, f.read())
        code, out = self.run_set("https://a.example/1", "--archive", "放弃：方向不符")
        self.assertEqual(code, 0)
        r = self.read(0)
        self.assertEqual((r["状态"], r["归档日期"], r["归档原因"], r["状态更新日期"]), ("归档", "2026-11-04", "放弃：方向不符", "2026-11-04"))
        self.assertEqual(self.run_set("https://a.example/1", "--archive", " ")[0], 1)

    def test_restore(self):
        # 未归档 → 拒绝
        code, out = self.run_set("https://a.example/1", "--restore")
        self.assertEqual(code, 1)
        self.assertIn("而非归档", out)
        # 已过期 → 拒绝
        code, out = self.run_set("https://a.example/3", "--restore")
        self.assertEqual(code, 1)
        self.assertIn("已过", out)
        self.assertEqual(self.read(2)["状态"], "归档")
        # 归档且截止未过 → 恢复，标记一并改掉
        code, out = self.run_set("https://a.example/2", "--restore", "--mark", "再看看")
        self.assertEqual(code, 0)
        self.assertIn("已更新：乙单位｜岗位B：状态 「归档」→「持续」；归档日期 「2026-10-20」→「」；归档原因 「太远」→「」；"
                      "备注 「」→「2026-11-04 从归档恢复（原因：太远）」；我的标记 「不要：太远」→「再看看」；"
                      "状态更新日期 「」→「2026-11-04」", out)
        self.assertIn("运行 status", out)
        r = self.read(1)
        self.assertEqual((r["状态"], r["归档日期"], r["归档原因"], r["我的标记"]), ("持续", "", "", "再看看"))
        self.assertEqual(r["备注"], "2026-11-04 从归档恢复（原因：太远）")
        # 恢复后 status 不再归档它
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(jt.main(["--csv", self.csv, "--today", "2026-11-04", "status"]), 0)
        self.assertEqual(self.read(1)["状态"], "持续")
        # 截止不详的归档岗位也可恢复；无归档原因时记为「未记录」；备注追加
        code, out = self.run_set("https://a.example/4", "--restore")
        self.assertEqual(code, 0)
        self.assertEqual(self.read(3)["备注"], "旧备注；2026-11-04 从归档恢复（原因：未记录）")
        # 标记仍是「不要」时恢复会提示 status 将再次归档
        jt.write_jobs(self.rows, self.csv)
        code, out = self.run_set("https://a.example/2", "--restore")
        self.assertEqual(code, 0)
        self.assertIn("再次归档", out)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(jt.main(["--csv", self.csv, "validate"]), 0)

    def test_report_writes_compact_when_large(self):
        rows = [row(岗位ID=str(i), 链接=f"https://x/{i}", 状态="持续", 截止日期="2026-12-01",
                    单位="单位" * 10, 岗位="岗位" * 10, 城市="城市" * 5) for i in range(400)]
        jt.write_jobs(rows, self.csv)
        out_dir = os.path.join(self.dir, "out")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(jt.main(["--csv", self.csv, "--today", "2026-11-04", "report", "--out", out_dir]), 0)
        out = buf.getvalue()
        self.assertRegex(out, r"summary\.html 约 \d+ KB，超过 90 KB 安全阈值（Gmail 约 102 KB 起裁剪正文），请用 .*summary_compact\.html 作为 htmlBody")
        compact = os.path.join(out_dir, "summary_compact.html")
        self.assertTrue(os.path.exists(compact))
        self.assertLess(os.path.getsize(compact), 60_000)
        with open(compact, encoding="utf-8") as f:
            self.assertIn("持续 400 条未在邮件中逐条列出", f.read())
        # 数据变少后不再超限：不提示，并清理过时的精简版
        jt.write_jobs(rows[:5], self.csv)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(jt.main(["--csv", self.csv, "--today", "2026-11-04", "report", "--out", out_dir]), 0)
        self.assertNotIn("裁剪阈值", buf.getvalue())
        self.assertFalse(os.path.exists(compact))


class RepoDataTest(unittest.TestCase):
    """仓库中的真实 jobs.csv 必须能通过结构校验（容忍旧版 16 列表头的警告）。"""

    def test_repo_jobs_csv_valid(self):
        if not os.path.exists(jt.JOBS_CSV):
            self.skipTest("no jobs.csv")
        header, rows, info = jt.read_jobs_detailed()
        self.assertEqual(header, jt.COLUMNS)
        errors, warnings = jt.validate(header, rows, info)
        self.assertEqual(errors, [])
        if info.needs_migration:
            self.assertTrue(any("migrate" in w for w in warnings))
        # 升级后的行：截止日期只能是 ISO 或空
        for r in rows:
            self.assertTrue(r["截止日期"] == "" or jt.is_iso_date(r["截止日期"]), r["截止日期"])

    def test_repo_soft_deadlines_are_real_soft_deadlines(self):
        """回归：「软截止 」开头的说明余文必须含 优先审理|开始审阅|开始审理|review，防止再把真实截止日清空。"""
        if not os.path.exists(jt.JOBS_CSV):
            self.skipTest("no jobs.csv")
        _, rows = jt.read_jobs()
        soft_re = re.compile(r"优先审理|开始审阅|开始审理|review", re.I)
        for r in rows:
            note = r["截止说明"]
            if note.startswith("软截止 "):
                self.assertTrue(r["截止日期"] == "", (r["单位"], r["岗位"]))
                rest = note[len("软截止 "):].split("；", 1)
                self.assertTrue(len(rest) == 2 and soft_re.search(rest[1]), f"{r['单位']}｜{r['岗位']}：{note}")
        self.assertIsNone(jt.SOFT_DEADLINE_RE.search("长期有效，招满为止"))


if __name__ == "__main__":
    unittest.main()
