# 待人工核实的新来源（收件箱）

由 Routine 通过 `python3 tools/sources_tool.py add URL --note "一句说明" [--category 类别id]` 追加；
不要手写「某日复查无新岗位」之类的流水。定期人工核实：有效且结构稳定的并入 `config/sources.toml`
（`[[source]]`，或某类别的 `browse` / `units`），无效的删除。

每行固定格式：`- URL ｜ 一句说明 ｜ 发现日期 ｜ 建议类别id`（`python3 tools/sources_tool.py lint` 检查）。
