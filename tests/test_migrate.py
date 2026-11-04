"""jobs_tool 的迁移（16 列 → 17 列）、截止日期拆分、marks 合并、runlog/coverage 测试。"""
import csv
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

TODAY = "2026-11-04"


def legacy_row(**kw):
    r = {c: "" for c in jt.LEGACY_COLUMNS}
    r.update({"岗位ID": kw.get("链接", "https://a.example/1"), "链接": "https://a.example/1",
              "首次推送日期": "2026-10-20", "匹配等级": "可考虑", "单位": "某单位", "岗位": "某岗位",
              "城市": "南京", "截止日期": "需核实", "状态": "持续", "状态更新日期": "2026-11-03"})
    r.update(kw)
    return r


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def run_cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = jt.main(list(args))
    return code, buf.getvalue()


# 仓库 jobs.csv 中出现过的全部非 ISO「截止日期」值 → 期望的 (截止日期, 截止说明)
SPLIT_TABLE = [
    # (a) 真实日期 + 说明
    ("2026-07-24（聚合页：网申06-26至07-24，需核实）", "2026-07-24", "聚合页：网申06-26至07-24，需核实"),
    ("2026-10-02（聚合页：网申09-04至10-02，需核实）", "2026-10-02", "聚合页：网申09-04至10-02，需核实"),
    ("2026-10-07（明天截止）", "2026-10-07", "明天截止"),
    ("2026-10-08（需核实）", "2026-10-08", "需核实"),
    ("2026-10-10（个人申请；院系推荐10-31）", "2026-10-10", "个人申请；院系推荐10-31"),
    ("2026-10-14（聚合列表；原文页未写，需核实）", "2026-10-14", "聚合列表；原文页未写，需核实"),
    ("2026-10-16（需10-02前预注册）", "2026-10-16", "需10-02前预注册"),
    ("2026-10-20（第二批）", "2026-10-20", "第二批"),
    ("2026-10-24（另一来源；原文页显示09-30，需核实）", "2026-10-24", "另一来源；原文页显示09-30，需核实"),
    ("2026-10-30 17:00", "2026-10-30", "17:00"),
    ("2026-10-30（网申10-21至10-30；博士须1995-01-01后出生，单一来源需核实）", "2026-10-30",
     "网申10-21至10-30；博士须1995-01-01后出生，单一来源需核实"),
    ("2026-10-31（需核实）", "2026-10-31", "需核实"),
    ("2026-11-02 16:00", "2026-11-02", "16:00"),
    ("2026-11-02 24:00", "2026-11-02", "24:00"),
    ("2026-11-05（网申09-06至11-05；聚合页转载，需核实）", "2026-11-05", "网申09-06至11-05；聚合页转载，需核实"),
    ("2026-11-09（报名11-03至11-09；笔试12-06；第三方转载，需核实）", "2026-11-09", "报名11-03至11-09；笔试12-06；第三方转载，需核实"),
    ("2026-11-30（rolling）", "2026-11-30", "rolling"),
    ("2026-12-31（按季度受理）", "2026-12-31", "按季度受理"),  # 契约：视为真实截止，保留日期
    ("2026-10-30 17:00（现场确认）", "2026-10-30", "17:00；现场确认"),
    # (b) 软截止：日期只是优先审理日
    ("2026-10-18优先审理，之后rolling", "", "软截止 2026-10-18；优先审理，之后rolling"),
    ("2026-10-19（开始审阅，招满为止）", "", "软截止 2026-10-19；开始审阅，招满为止"),
    # 「招满为止」单独出现只是附注，日期是真实截止（否则 60 天规则会把在招岗位误归档）
    ("2027-08-29（长期有效，招满为止）", "2027-08-29", "长期有效，招满为止"),
    ("2026-12-15（招满为止）", "2026-12-15", "招满为止"),
    ("2026-12-01 (review starts, open until filled)", "", "软截止 2026-12-01；review starts, open until filled"),
    # (c) 非日期开头：受控前缀 + 「；」分隔
    ("2026-10下旬（需核实）", "", "需核实；2026-10下旬"),
    ("Rolling", "", "Rolling"),
    ("rolling", "", "Rolling"),
    ("滚动招聘", "", "Rolling；招聘"),
    ("Rolling（no closing deadline）", "", "Rolling；no closing deadline"),
    ("Rolling（open until filled）", "", "Rolling；open until filled"),
    ("Rolling（原文未写截止）", "", "Rolling；原文未写截止"),
    ("Rolling（原文：no application deadline，filled ASAP）", "", "Rolling；原文：no application deadline，filled ASAP"),
    ("Rolling（发布时间不详，需核实）", "", "Rolling；发布时间不详，需核实"),
    ("Rolling（无截止；岗位取决于每年4月/10月的经费决定）", "", "Rolling；无截止；岗位取决于每年4月/10月的经费决定"),
    ("原文未写截止（2026-04-28发布，距今约5.5个月，需核实是否仍开放）", "",
     "需核实；原文未写截止；2026-04-28发布，距今约5.5个月，需核实是否仍开放"),
    ("原文未写截止（2026-08-28发布）", "", "需核实；原文未写截止；2026-08-28发布"),
    ("原文未写（2026-06-16发布）", "", "需核实；原文未写；2026-06-16发布"),
    ("招完截止（2026-09-30发布）", "", "招满即止；2026-09-30发布"),
    ("招满即止", "", "招满即止"),
    ("招满即止（无固定截止；2026-09-19发布）", "", "招满即止；无固定截止；2026-09-19发布"),
    ("长期", "", "长期"),
    ("常年招聘", "", "长期；招聘"),
    ("长期有效（发布时间不一致：聚合页7-24，复旦人事处列表为2025-05-19，需核实）", "",
     "长期；发布时间不一致：聚合页7-24，复旦人事处列表为2025-05-19，需核实"),
    ("长期有效，招满即止（2026-09-19发布）", "", "长期；招满即止；2026-09-19发布"),
    ("长期（2026年度有效）", "", "长期；2026年度有效"),
    ("长期（全年招聘；2026-10-20发布）", "", "长期；全年招聘；2026-10-20发布"),
    ("长期（公告发布于2024-03需核实）", "", "长期；公告发布于2024-03需核实"),
    ("长期（常年招聘）", "", "长期；常年招聘"),
    ("长期（招满即止；2026-03-19发布，已超6个月）", "", "长期；招满即止；2026-03-19发布，已超6个月"),
    ("长期（未写截止）", "", "长期；未写截止"),
    ("长期（未写截止；4月13日发布）", "", "长期；未写截止；4月13日发布"),
    ("需核实", "", "需核实"),
    ("需核实（2026-08-11发布，未写截止）", "", "需核实；2026-08-11发布，未写截止"),
    ("需核实（2026-10-19发布）", "", "需核实；2026-10-19发布"),
    ("需核实（7-21发布，是否仍开放需核实）", "", "需核实；7-21发布，是否仍开放需核实"),
    ("需核实（9月22日发布，原文未见截止日）", "", "需核实；9月22日发布，原文未见截止日"),
    ("需核实（分三批）", "", "需核实；分三批"),
    ("需核实（原文503，取自检索摘要）", "", "需核实；原文503，取自检索摘要"),
    ("需核实（原文未写；2026-01-23发布）", "", "需核实；原文未写；2026-01-23发布"),
    ("需核实（原文未写；2026-01-23发布，已超6个月）", "", "需核实；原文未写；2026-01-23发布，已超6个月"),
    ("需核实（原文未写；流程：筛选至11月上旬，面试至11月中旬）", "", "需核实；原文未写；流程：筛选至11月上旬，面试至11月中旬"),
    ("需核实（原文未写）", "", "需核实；原文未写"),
    ("需核实（原文未写，页面仍开放；2026-08-03发布）", "", "需核实；原文未写，页面仍开放；2026-08-03发布"),
    ("需核实（原文未写；邮件投递 someone@example.org）", "", "需核实；原文未写；邮件投递 someone@example.org"),
    ("需核实（原文未列截止，页面标注10月13日）", "", "需核实；原文未列截止，页面标注10月13日"),
    ("需核实（见附表）", "", "需核实；见附表"),
    ("软截止 2026-10-01；招满为止", "", "软截止 2026-10-01；招满为止"),
    ("2026-13-01", "", "需核实；2026-13-01"),  # 不是有效日期
    ("", "", ""),
    ("2026-12-31", "2026-12-31", ""),
    (" 2026-12-31 ", "2026-12-31", ""),
]


class SplitDeadlineTest(unittest.TestCase):
    def test_table(self):
        for text, date, note in SPLIT_TABLE:
            with self.subTest(text=text):
                self.assertEqual(jt.split_deadline(text), (date, note))

    def test_idempotent_on_output(self):
        """拆分结果再放回「截止日期」列拆一次，日期不变、说明不变。"""
        for text, date, note in SPLIT_TABLE:
            if date:
                self.assertEqual(jt.split_deadline(date), (date, ""))
            elif note:
                self.assertEqual(jt.split_deadline(note)[0], "")

    def test_repo_values(self):
        """仓库 jobs.csv 现有全部「截止日期」值都能拆成 ISO/空 + 非空说明。"""
        if not os.path.exists(jt.JOBS_CSV):
            self.skipTest("no jobs.csv")
        with open(jt.JOBS_CSV, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            values = {r["截止日期"] for r in reader}
        table = {t: (d, n) for t, d, n in SPLIT_TABLE}
        for v in sorted(values):
            with self.subTest(value=v):
                date, note = jt.split_deadline(v)
                self.assertTrue(date == "" or jt.is_iso_date(date))
                self.assertTrue(date or note or not v.strip())
                self.assertNotIn("（", note)
                self.assertFalse(note.startswith("；") or note.endswith("；"))
                if v in table:
                    self.assertEqual((date, note), table[v])


class ReadLegacyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "jobs.csv")
        self.dismissed = os.path.join(self.dir, "dismissed.csv")
        with open(self.dismissed, "w", encoding="utf-8") as f:
            f.write("链接\n")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_legacy_header_upgraded_in_memory(self):
        rows = [legacy_row(), legacy_row(岗位ID="2", 链接="https://a.example/2", 截止日期="2026-12-31"),
                legacy_row(岗位ID="3", 链接="https://a.example/3", 截止日期="2026-10-19（开始审阅，招满为止）")]
        write_csv(self.csv, jt.LEGACY_COLUMNS, [[r[c] for c in jt.LEGACY_COLUMNS] for r in rows])
        header, got, info = jt.read_jobs_detailed(self.csv)
        self.assertEqual(header, jt.COLUMNS)
        self.assertTrue(info.legacy_header)
        self.assertTrue(info.needs_migration)
        self.assertEqual(info.added_rows, 3)
        self.assertEqual(info.split_rows, 2)
        self.assertEqual(len(info.soft), 1)
        self.assertEqual([(r["截止日期"], r["截止说明"]) for r in got],
                         [("", "需核实"), ("2026-12-31", ""), ("", "软截止 2026-10-19；开始审阅，招满为止")])
        errors, warnings = jt.validate(header, got, info)
        self.assertEqual(errors, [])
        self.assertIn(jt.LEGACY_WARNING, warnings)
        # 兼容的二元组接口
        header2, rows2 = jt.read_jobs(self.csv)
        self.assertEqual((header2, rows2), (header, got))

    def test_mixed_rows_under_new_header(self):
        old = legacy_row(岗位ID="old", 链接="https://a.example/old", 截止日期="长期（2026年度有效）")
        new_ok = {c: "" for c in jt.COLUMNS}
        new_ok.update(legacy_row(岗位ID="new", 链接="https://a.example/new", 截止日期="2026-12-01"))
        new_ok["截止说明"] = "17:00"
        new_raw = dict(new_ok, 岗位ID="raw", 链接="https://a.example/raw", 截止日期="2026-11-30（rolling）", 截止说明="已有说明")
        write_csv(self.csv, jt.COLUMNS, [
            [old[c] for c in jt.LEGACY_COLUMNS],            # 16 列旧行
            [new_ok[c] for c in jt.COLUMNS],                 # 17 列正常行
            [new_raw[c] for c in jt.COLUMNS],                # 17 列但截止日期未拆分
        ])
        header, rows, info = jt.read_jobs_detailed(self.csv)
        self.assertEqual(header, jt.COLUMNS)
        self.assertFalse(info.legacy_header)
        self.assertEqual(info.added_rows, 1)
        self.assertEqual(info.split_rows, 2)
        self.assertEqual([(r["截止日期"], r["截止说明"]) for r in rows],
                         [("", "长期；2026年度有效"), ("2026-12-01", "17:00"), ("2026-11-30", "rolling；已有说明")])
        errors, _ = jt.validate(header, rows, info)
        self.assertEqual(errors, [])

    def test_malformed_row_is_error(self):
        write_csv(self.csv, jt.COLUMNS, [["only", "three", "cols"], [""] * 20])
        header, rows, info = jt.read_jobs_detailed(self.csv)
        errors, _ = jt.validate(header, rows, info)
        self.assertEqual(len([e for e in errors if "列数与表头不一致" in e]), 2)
        # 未知表头原样返回并报错
        write_csv(self.csv, ["a", "b"], [["1", "2"]])
        header, rows, info = jt.read_jobs_detailed(self.csv)
        self.assertEqual(header, ["a", "b"])
        errors, _ = jt.validate(header, rows, info)
        self.assertTrue(any("表头与约定不一致" in e for e in errors))
        self.assertEqual(run_cli("--csv", self.csv, "migrate", "--dry-run")[0], 1)

    def test_empty_file(self):
        open(self.csv, "w").close()
        header, rows, info = jt.read_jobs_detailed(self.csv)
        self.assertEqual((header, rows), ([], []))
        self.assertFalse(info.needs_migration)

    def test_validate_cli_legacy_warning_and_strict(self):
        write_csv(self.csv, jt.LEGACY_COLUMNS, [[legacy_row()[c] for c in jt.LEGACY_COLUMNS]])
        code, out = run_cli("--csv", self.csv, "validate")
        self.assertEqual(code, 0)
        self.assertIn("警告：旧版 16 列表头", out)
        self.assertEqual(run_cli("--csv", self.csv, "validate", "--strict")[0], 1)
        with open(self.csv, "rb") as f:
            self.assertTrue(f.read().startswith(",".join(jt.LEGACY_COLUMNS).encode()))  # validate 不写文件

    def test_status_write_upgrades(self):
        write_csv(self.csv, jt.LEGACY_COLUMNS, [[legacy_row()[c] for c in jt.LEGACY_COLUMNS]])
        code, out = run_cli("--csv", self.csv, "--dismissed", self.dismissed, "--today", TODAY, "status")
        self.assertEqual(code, 0)
        self.assertIn("升级为 17 列", out)
        with open(self.csv, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(",".join(jt.COLUMNS).encode() + b"\r\n"))
        header, rows, info = jt.read_jobs_detailed(self.csv)
        self.assertFalse(info.needs_migration)
        self.assertEqual(rows[0]["截止说明"], "需核实")
        self.assertEqual(rows[0]["状态更新日期"], TODAY)


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "jobs.csv")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def read_bytes(self):
        with open(self.csv, "rb") as f:
            return f.read()

    def test_migrate_idempotent_synthetic(self):
        rows = [legacy_row(), legacy_row(岗位ID="2", 链接="https://a.example/2", 截止日期="2026-10-18优先审理，之后rolling"),
                legacy_row(岗位ID="3", 链接="https://a.example/3", 截止日期="2026-10-30 17:00", 备注="含,逗号")]
        write_csv(self.csv, jt.LEGACY_COLUMNS, [[r[c] for c in jt.LEGACY_COLUMNS] for r in rows])
        before = self.read_bytes()
        code, out = run_cli("--csv", self.csv, "migrate", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("迁移 3 行：补列 3，拆分 3；软截止 1 条如下：", out)
        self.assertIn("软截止 2026-10-18；优先审理，之后rolling", out)
        self.assertEqual(before, self.read_bytes())  # dry-run 不写
        code, out = run_cli("--csv", self.csv, "migrate")
        self.assertEqual(code, 0)
        once = self.read_bytes()
        self.assertNotEqual(before, once)
        self.assertTrue(once.startswith(",".join(jt.COLUMNS).encode() + b"\r\n"))
        code, out = run_cli("--csv", self.csv, "migrate")
        self.assertEqual(code, 0)
        self.assertIn("无需迁移", out)
        self.assertEqual(once, self.read_bytes())  # migrate(migrate(x)) == migrate(x)
        header, rows2, info = jt.read_jobs_detailed(self.csv)
        self.assertFalse(info.needs_migration)
        self.assertEqual([(r["截止日期"], r["截止说明"]) for r in rows2],
                         [("", "需核实"), ("", "软截止 2026-10-18；优先审理，之后rolling"), ("2026-10-30", "17:00")])
        self.assertEqual(rows2[2]["备注"], "含,逗号")
        self.assertEqual(run_cli("--csv", self.csv, "validate", "--strict")[0], 0)

    def test_migrate_repo_copy_idempotent(self):
        """对仓库 jobs.csv 的副本迁移：幂等，且除「截止日期/截止说明」外其他列逐行不变。"""
        if not os.path.exists(jt.JOBS_CSV):
            self.skipTest("no jobs.csv")
        shutil.copy(jt.JOBS_CSV, self.csv)
        with open(self.csv, encoding="utf-8-sig", newline="") as f:
            original = list(csv.DictReader(f))
        code, out = run_cli("--csv", self.csv, "migrate")
        self.assertEqual(code, 0)
        if "无需迁移" in out:
            self.skipTest("仓库 jobs.csv 已迁移")
        self.assertRegex(out, r"迁移 \d+ 行：补列 \d+，拆分 \d+；软截止 \d+ 条如下：")
        once = self.read_bytes()
        code, out = run_cli("--csv", self.csv, "migrate")
        self.assertIn("无需迁移", out)
        self.assertEqual(once, self.read_bytes())
        header, rows, info = jt.read_jobs_detailed(self.csv)
        self.assertEqual(len(rows), len(original))
        for a, b in zip(original, rows):
            for c in jt.LEGACY_COLUMNS:
                if c != "截止日期":
                    self.assertEqual(a[c], b[c])
            if jt.is_iso_date(a["截止日期"]):
                self.assertEqual((a["截止日期"], ""), (b["截止日期"], b["截止说明"]))
            else:
                self.assertEqual(jt.split_deadline(a["截止日期"]), (b["截止日期"], b["截止说明"]))
        errors, warnings = jt.validate(header, rows, info)
        self.assertEqual(errors, [])
        self.assertNotIn(jt.LEGACY_WARNING, warnings)
        # 写回不再改变字节
        jt.write_jobs(rows, self.csv)
        self.assertEqual(once, self.read_bytes())


class MarksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "jobs.csv")
        self.json = os.path.join(self.dir, "sheet_values.json")
        self.rows = [
            dict({c: "" for c in jt.COLUMNS}, **legacy_row(岗位ID="https://a.example/1", 链接="https://a.example/1",
                                                            单位="甲单位", 岗位="岗位A", 我的标记="")),
            dict({c: "" for c in jt.COLUMNS}, **legacy_row(岗位ID="https://a.example/2", 链接="https://a.example/2",
                                                            单位="乙单位", 岗位="岗位B", 我的标记="关注")),
            dict({c: "" for c in jt.COLUMNS}, **legacy_row(岗位ID="https://a.example/3", 链接="https://a.example/3",
                                                            单位="丙单位", 岗位="岗位C", 我的标记="已投递", 投递状态="已投递")),
        ]
        for r in self.rows:
            r["截止日期"], r["截止说明"] = "", "需核实"
        jt.write_jobs(self.rows, self.csv)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def sheets(self, total, applied, archived):
        h1 = ["状态", "等级", "单位", "岗位", "城市", "截止日期", "截止说明", "剩余天数", "首次推送日期", "链接", "我的标记", "备注"]
        h2 = ["投递状态", "我的标记", "单位", "岗位", "城市", "截止日期", "截止说明", "剩余天数", "岗位状态", "投递日期",
              "首次推送日期", "链接", "备注"]
        h3 = ["归档日期", "归档原因", "等级", "单位", "岗位", "城市", "截止日期", "截止说明", "首次推送日期", "链接", "我的标记", "备注"]

        def r1(unit, title, link, mark):
            return ["持续", "可考虑", unit, title, "南京", "", "需核实", "", "2026-10-20", link, mark, ""]

        def r2(unit, title, link, mark):
            return ["已投递", mark, unit, title, "南京", "", "需核实", "", "持续", "", "2026-10-20", link, ""]

        def r3(unit, title, link, mark):
            return ["2026-10-01", "x", "可考虑", unit, title, "南京", "", "需核实", "2026-10-20", link, mark, ""]
        return {"岗位总表": [h1] + [r1(*x) for x in total], "已投递": [h2] + [r2(*x) for x in applied],
                "归档": [h3] + [r3(*x) for x in archived]}

    def test_merge_rules(self):
        sheets = self.sheets(
            total=[("甲单位", "岗位A", "https://a.example/1", "不要：太远"),       # 新标记
                   ("乙单位", "岗位B", "https://a.example/2", "关注"),           # 与 csv 相同
                   ("丙单位", "岗位C", "https://a.example/3", "总表改的")],      # 与已投递冲突 → 取已投递
            applied=[("丙单位", "岗位C", "https://a.example/3", "面试")],
            archived=[("丙单位", "岗位C", "https://a.example/3", "归档改的"),
                      ("不存在", "岗位Z", "https://a.example/9", "x")])
        result = jt.merge_marks(self.rows, sheets)
        marks = {r["岗位"]: r["我的标记"] for r in self.rows}
        self.assertEqual(marks, {"岗位A": "不要：太远", "岗位B": "关注", "岗位C": "面试"})
        self.assertEqual(len(result["changes"]), 2)
        self.assertTrue(any("来自「已投递」；另有 2 个工作表取值不同" in c for c in result["changes"]))
        self.assertEqual(result["unmatched"], ["归档：不存在｜岗位Z"])
        self.assertFalse(result["link_without_url"])

    def test_priority_total_over_archive_and_rich_text_link(self):
        """链接列是「原文」富文本（无网址）时按单位+岗位匹配；岗位总表优先于归档。"""
        sheets = self.sheets(total=[("乙单位", "岗位B", "原文", "考虑投递")],
                             applied=[],
                             archived=[("乙单位", "岗位B", "原文", "不要")])
        result = jt.merge_marks(self.rows, sheets)
        self.assertEqual(self.rows[1]["我的标记"], "考虑投递")
        self.assertTrue(result["link_without_url"])
        # HYPERLINK 公式也能取出网址
        self.assertEqual(jt.extract_url('=HYPERLINK("https://a.example/2";"原文")'), "https://a.example/2")

    def test_empty_does_not_clear_unless_allowed(self):
        sheets = self.sheets(total=[("乙单位", "岗位B", "https://a.example/2", "")], applied=[], archived=[])
        result = jt.merge_marks(self.rows, sheets)
        self.assertEqual(self.rows[1]["我的标记"], "关注")
        self.assertEqual(len(result["skipped_clear"]), 1)
        self.assertEqual(result["restore"], [])
        jt.merge_marks(self.rows, sheets, allow_clear=True)
        self.assertEqual(self.rows[1]["我的标记"], "")

    def archived(self, link, unit, title, mark, reason="太远"):
        r = dict({c: "" for c in jt.COLUMNS}, **legacy_row(岗位ID=link, 链接=link, 单位=unit, 岗位=title, 我的标记=mark,
                                                            状态="归档", 归档日期="2026-10-01", 归档原因=reason))
        r["截止日期"], r["截止说明"] = "", "需核实"
        return r

    def test_restore_gesture_in_archive_sheet(self):
        """「归档」表中清空/改写「不要…」= 恢复请求：不需要 --allow-clear，写回并列入 restore；其他情况不变。"""
        self.rows += [self.archived("https://a.example/4", "丁单位", "岗位D", "不要：太远"),
                      self.archived("https://a.example/5", "戊单位", "岗位E", "不要，qs低", "qs低"),
                      self.archived("https://a.example/6", "己单位", "岗位F", "不要：限本科", "限本科"),
                      self.archived("https://a.example/7", "庚单位", "岗位G", "考虑投递", "已过期，未投递")]
        sheets = self.sheets(
            total=[("乙单位", "岗位B", "https://a.example/2", "")],                  # 非归档表为空 → 仍不清空
            applied=[],
            archived=[("丁单位", "岗位D", "https://a.example/4", ""),                # 清空「不要」→ 恢复
                      ("戊单位", "岗位E", "https://a.example/5", "再看看"),           # 改为非「不要」文字 → 恢复
                      ("己单位", "岗位F", "https://a.example/6", "不要：换了原因"),   # 仍是「不要」→ 普通变更
                      ("庚单位", "岗位G", "https://a.example/7", "")])               # 原本不是「不要」→ 仍不清空
        result = jt.merge_marks(self.rows, sheets)
        marks = {r["岗位"]: r["我的标记"] for r in self.rows}
        self.assertEqual(marks["岗位B"], "关注")
        self.assertEqual(marks["岗位D"], "")
        self.assertEqual(marks["岗位E"], "再看看")
        self.assertEqual(marks["岗位F"], "不要：换了原因")
        self.assertEqual(marks["岗位G"], "考虑投递")
        self.assertEqual(len(result["restore"]), 2)
        self.assertEqual(result["restore"][0], "丁单位｜岗位D：「不要：太远」→「」，未过期的请运行 set https://a.example/4 --restore")
        self.assertIn("戊单位｜岗位E：「不要，qs低」→「再看看」", result["restore"][1])
        self.assertEqual(len(result["changes"]), 3)
        self.assertEqual({s.split("：")[0] for s in result["skipped_clear"]}, {"归档", "岗位总表"})
        # 状态/归档字段由 set --restore 处理，marks 本身不改
        self.assertEqual([r["状态"] for r in self.rows[3:]], ["归档"] * 4)
        # 「已投递」表取值优先于「归档」表的恢复手势
        rows2 = [self.archived("https://a.example/8", "辛单位", "岗位H", "不要：太远")]
        sheets2 = self.sheets(total=[], applied=[("辛单位", "岗位H", "https://a.example/8", "不要：太远，已确认")],
                              archived=[("辛单位", "岗位H", "https://a.example/8", "")])
        result2 = jt.merge_marks(rows2, sheets2)
        self.assertEqual(rows2[0]["我的标记"], "不要：太远，已确认")
        self.assertEqual(result2["restore"], [])

    def test_clean_mark_rejects_formula_and_strips_control_chars(self):
        self.assertEqual(jt.clean_mark("  关注 "), ("关注", None))
        self.assertEqual(jt.clean_mark("不要：\r\n原因\x00"), ("不要： 原因", None))
        self.assertEqual(jt.clean_mark("a\tb"), ("a b", None))
        for bad in ('=HYPERLINK("http://x")', "@SUM(1,2)", "+1", "-1"):
            mark, why = jt.clean_mark(bad)
            self.assertTrue(jt.is_rejected_mark(why), bad)
        mark, why = jt.clean_mark("长" * 250)
        self.assertEqual(len(mark), jt.MARK_MAX_LEN)
        self.assertIn("已截断", why)
        self.assertFalse(jt.is_rejected_mark(why))
        self.assertEqual(jt.clean_mark(None), ("", None))

    def test_merge_marks_rejects_formula_cells(self):
        self.rows.append(self.archived("https://a.example/4", "丁单位", "岗位D", "不要：太远"))
        sheets = self.sheets(
            total=[("甲单位", "岗位A", "https://a.example/1", '=HYPERLINK("https://evil.example","点我")'),
                   ("乙单位", "岗位B", "https://a.example/2", "@SUM(1,2)"),
                   ("丙单位", "岗位C", "https://a.example/3", "不要：\r\n原因\x00"),
                   ("丁单位", "岗位D", "https://a.example/4", "长" * 230)],
            applied=[], archived=[])
        result = jt.merge_marks(self.rows, sheets)
        marks = {r["岗位"]: r["我的标记"] for r in self.rows}
        self.assertEqual(marks["岗位A"], "")
        self.assertEqual(marks["岗位B"], "关注")
        self.assertEqual(marks["岗位C"], "不要： 原因")
        self.assertEqual(marks["岗位D"], "长" * 200)
        self.assertEqual(len(result["rejected"]), 2)
        self.assertTrue(all("疑似表格公式" in s for s in result["rejected"]))
        self.assertTrue(result["rejected"][0].startswith("岗位总表：甲单位｜岗位A"))
        self.assertEqual(len(result["changes"]), 2)
        self.assertTrue(any("已截断" in c for c in result["changes"]))
        # CLI 输出中列出「已拒收」，dry-run 同样显示
        with open(self.json, "w", encoding="utf-8") as f:
            json.dump(sheets, f, ensure_ascii=False)
        code, out = run_cli("--csv", self.csv, "marks", "--from", self.json, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("已拒收（2 条，需人工核对）", out)
        # 写入后的 jobs.csv 没有公式前缀，validate 无相关警告
        jt.write_jobs(self.rows, self.csv)
        header, rows, info = jt.read_jobs_detailed(self.csv)
        errors, warnings = jt.validate(header, rows, info)
        self.assertEqual(errors, [])
        self.assertFalse(any("公式前缀" in w or "控制字符" in w for w in warnings))

    def test_cli_restore_hint(self):
        self.rows.append(self.archived("https://a.example/4", "丁单位", "岗位D", "不要：太远"))
        jt.write_jobs(self.rows, self.csv)
        sheets = self.sheets(total=[], applied=[], archived=[("丁单位", "岗位D", "https://a.example/4", "")])
        with open(self.json, "w", encoding="utf-8") as f:
            json.dump(sheets, f, ensure_ascii=False)
        code, out = run_cli("--csv", self.csv, "marks", "--from", self.json)
        self.assertEqual(code, 0)
        self.assertIn("待恢复（1 条）", out)
        self.assertIn("丁单位｜岗位D：「不要：太远」→「」，未过期的请运行 set https://a.example/4 --restore", out)
        self.assertNotIn("--allow-clear", out)
        self.assertEqual(jt.read_jobs(self.csv)[1][3]["我的标记"], "")
        # 旧提示文案（FORMULA）不再出现
        sheets = self.sheets(total=[("乙单位", "岗位B", "原文", "考虑投递")], applied=[], archived=[])
        with open(self.json, "w", encoding="utf-8") as f:
            json.dump(sheets, f, ensure_ascii=False)
        code, out = run_cli("--csv", self.csv, "marks", "--from", self.json, "--dry-run")
        self.assertIn("任何 valueRenderOption 都一样", out)
        self.assertNotIn("valueRenderOption=FORMULA", out)

    def test_api_shape_and_missing_mark_column(self):
        sheets = {"岗位总表": {"range": "岗位总表!A1:L3", "values": [["单位", "岗位", "链接", "我的标记"],
                                                                 ["甲单位", "岗位A", "https://a.example/1", "新标记"]]},
                  "已投递": [["单位", "岗位"]], "归档": []}
        result = jt.merge_marks(self.rows, sheets)
        self.assertEqual(self.rows[0]["我的标记"], "新标记")
        self.assertTrue(any("没有「我的标记」列" in n for n in result["notes"]))

    def test_cli_dry_run_and_write(self):
        sheets = self.sheets(total=[("甲单位", "岗位A", "https://a.example/1", "不要：太远")], applied=[], archived=[])
        with open(self.json, "w", encoding="utf-8") as f:
            json.dump(sheets, f, ensure_ascii=False)
        code, out = run_cli("--csv", self.csv, "marks", "--from", self.json, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("「」→「不要：太远」", out)
        self.assertIn("--dry-run", out)
        self.assertEqual(jt.read_jobs(self.csv)[1][0]["我的标记"], "")
        code, out = run_cli("--csv", self.csv, "marks", "--from", self.json)
        self.assertEqual(code, 0)
        self.assertEqual(jt.read_jobs(self.csv)[1][0]["我的标记"], "不要：太远")
        # 再跑一次：无变更
        code, out = run_cli("--csv", self.csv, "marks", "--from", self.json)
        self.assertIn("变更 0 条", out)
        # 坏文件
        with open(self.json, "w", encoding="utf-8") as f:
            f.write("[1,2]")
        self.assertEqual(run_cli("--csv", self.csv, "marks", "--from", self.json)[0], 1)
        self.assertEqual(run_cli("--csv", self.csv, "marks", "--from", self.json + ".missing")[0], 1)


class RunlogCoverageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "jobs.csv")
        self.out = os.path.join(self.dir, "out")
        jt.write_jobs([dict({c: "" for c in jt.COLUMNS}, **legacy_row(截止日期="2026-11-20"))], self.csv)
        self.plan = {
            "today": TODAY,
            "categories": [
                {"id": "overseas_postdoc", "name": "海外博后", "due": True, "queries": ["q1", "q2"], "pinned": ["p1"],
                 "next_due": None, "units": ["ETH"], "browse": []},
                {"id": "cn_postdoc", "name": "国内博后", "due": True, "queries": ["q1", "q2"],
                 "next_due": None, "units": [], "browse": [], "catch_up_from": "2026-11-02"},
                {"id": "civil_service", "name": "公务员", "due": False, "queries": [],
                 "next_due": "2026-11-08", "units": [], "browse": []},
            ],
            # plan_tool 实际把未轮到的类别放在 not_due（含 reason）
            "not_due": [{"id": "talent_program", "name": "人才计划", "next_due": "2026-11-10", "active": True,
                         "reason": "今天未轮到"}],
        }

    def tearDown(self):
        shutil.rmtree(self.dir)

    def runlog(self, *args):
        return run_cli("--today", TODAY, "runlog", "--out", self.out, *args)

    def test_runlog_append_and_coverage(self):
        code, out = self.runlog("--category", "overseas_postdoc", "--searches", "2", "--hits", "1",
                                "--failed", "https://x.example/list", "--note", "少跑一组")
        self.assertEqual(code, 0)
        self.assertIn("今天累计 1 条", out)
        self.runlog("--category", "overseas_postdoc", "--searches", "1", "--hits", "0")
        self.runlog("--category", "soe_campus", "--searches", "2", "--hits", "2", "--note", "计划外补查")
        with open(os.path.join(self.out, "runlog.json"), encoding="utf-8") as f:
            log = json.load(f)
        self.assertEqual(len(log), 3)
        self.assertEqual(log[0]["failed"], ["https://x.example/list"])
        self.assertEqual(log[0]["date"], TODAY)
        self.assertEqual(self.runlog("--category", "x", "--searches", "-1", "--hits", "0")[0], 1)

        # 无 plan.json：只按 runlog 汇总
        code, out = run_cli("--csv", self.csv, "--today", TODAY, "report", "--out", self.out)
        self.assertEqual(code, 0)
        with open(os.path.join(self.out, "coverage.md"), encoding="utf-8") as f:
            cov = f.read()
        self.assertIn("未找到 out/plan.json", cov)
        self.assertIn("| overseas_postdoc | 3 | 1 |", cov)
        self.assertIn("https://x.example/list", cov)

        # 有 plan.json：按计划逐类
        with open(os.path.join(self.out, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(self.plan, f, ensure_ascii=False)
        code, out = run_cli("--csv", self.csv, "--today", TODAY, "report", "--out", self.out)
        self.assertEqual(code, 0)
        with open(os.path.join(self.out, "coverage.md"), encoding="utf-8") as f:
            cov = f.read()
        self.assertIn("| 海外博后（overseas_postdoc） | 3 | 3 | 1 | 少跑一组 |", cov)
        self.assertIn("| 国内博后（cn_postdoc） | 2 | 0 | 0 | 未记录 runlog；补跑自 2026-11-02 |", cov)
        self.assertIn("- 人才计划（talent_program）：下次 2026-11-10，今天未轮到", cov)
        self.assertIn("soe_campus，计划外） | — | 2 | 2 | 计划外补查 |", cov)
        self.assertIn("- 公务员（civil_service）：下次 2026-11-08", cov)
        self.assertIn("合计：计划 5 组，实际 5 组，命中 3 条。", cov)
        self.assertIn("- https://x.example/list（海外博后）", cov)
        for name in ("summary.html", "summary.txt", "sheets.json"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)))

    def test_coverage_filters_by_date_and_marks_shortfall(self):
        runlog = [{"date": "2026-11-03", "category": "overseas_postdoc", "searches": 9, "hits": 9, "failed": []},
                  {"date": TODAY, "category": "overseas_postdoc", "searches": 1, "hits": 0, "failed": []}]
        cov = jt.render_coverage(self.plan, runlog, dt.date.fromisoformat(TODAY))
        self.assertIn("| 海外博后（overseas_postdoc） | 3 | 1 | 0 | 少于计划 2 组 |", cov)
        self.assertNotIn("| 9 |", cov)
        self.assertIn("- 无", cov)  # 未能访问的来源：无
        plan = dict(self.plan, today="2026-11-01")
        self.assertIn("与今天不同", jt.render_coverage(plan, [], dt.date.fromisoformat(TODAY)))
        self.assertIn("今天没有 runlog 记录", jt.render_coverage(None, [], dt.date.fromisoformat(TODAY)))

    def test_report_without_runlog_or_plan(self):
        code, out = run_cli("--csv", self.csv, "--today", TODAY, "report", "--out", self.out)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, "coverage.md")))

    def test_runlog_concurrent_appends_do_not_lose_records(self):
        """Routine 常并行记录 runlog：N 个进程同时追加，runlog.json 必须恰有 N 条且全部 rc=0。"""
        import subprocess
        n = 16
        tool = os.path.join(jt.ROOT, "tools", "jobs_tool.py")
        procs = [subprocess.Popen([sys.executable, tool, "--today", TODAY, "runlog", "--out", self.out,
                                   "--category", f"cat{i}", "--searches", "1", "--hits", "0"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(n)]
        results = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * n, [r[1].decode() for r in results if r[1]])
        with open(os.path.join(self.out, "runlog.json"), encoding="utf-8") as f:
            log = json.load(f)
        self.assertEqual(sorted(e["category"] for e in log), sorted(f"cat{i}" for i in range(n)))
        self.assertFalse([f for f in os.listdir(self.out) if f.endswith(".tmp")])  # 无残留临时文件

    def test_append_runlog_atomic_and_locked(self):
        path = os.path.join(self.out, "runlog.json")
        jt.append_runlog(path, {"date": TODAY, "category": "a"})
        jt.append_runlog(path, {"date": TODAY, "category": "b"})
        with open(path, encoding="utf-8") as f:
            self.assertEqual([e["category"] for e in json.load(f)], ["a", "b"])
        if jt.fcntl is not None:
            self.assertTrue(os.path.exists(path + ".lock"))
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        with self.assertRaises(ValueError):
            jt.append_runlog(path, {"date": TODAY})


class NoPersonalInfoTest(unittest.TestCase):
    def test_tool_and_tests_have_no_email(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for path in [os.path.join(jt.ROOT, "tools", "jobs_tool.py"), __file__, os.path.join(here, "test_jobs_tool.py")]:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for m in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
                self.assertTrue(m.endswith(("example.org", "example.com")), f"{path} 含邮箱 {m}")


if __name__ == "__main__":
    unittest.main()
