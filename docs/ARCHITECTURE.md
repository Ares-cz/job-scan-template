# job-scan 架构说明（给维护者）

本文解释项目的分层、每个组件的职责与实现方式、关键算法、数据格式、扩展点和发布流程。使用说明见 [README.zh.md](../README.zh.md)，英文版见 [README.md](../README.md)。

## 1. 一句话与设计原则

**一个由 LLM 定时任务（Routine）驱动、用 TOML 配置描述"我是谁、要找什么"的求职信息助理。**

设计原则只有一条：**LLM 只做判断，不做计算。**

| 角色 | 负责 | 为什么这样分 |
|---|---|---|
| 人 | 维护 `config/profile.toml`、`config/sources.toml`；在在线表格「我的标记」列写「不要：原因 / 已投递 / 考虑投递 …」 | 偏好与结果只有本人知道 |
| 脚本（`tools/`，Python ≥ 3.11 标准库） | 计划与关键词轮换、列表页抓取、状态与剩余天数、去重、标记合并、邮件表格与 Sheets 数据、检索覆盖统计 | 确定性、可测试、不费额度 |
| Routine（LLM） | 按计划搜索、打开原文核实硬门槛、写匹配理由、理解标记语义、发邮件写表格、提交 | 需要阅读理解 |

## 2. 每日数据流

```
                 ┌──────────────────────────────────────────────────────────┐
 config/*.toml ─▶│ plan_tool.py brief ──▶ out/brief.md  out/plan.json      │
 learned_rules ─▶│   （候选人状态、今日计划、负面清单、预算、补跑）        │
                 └──────────────────────────────────────────────────────────┘
                                       │ due 类别 → html_list 来源
                                       ▼
                 fetch_tool.py ──▶ out/fetched.md / fetched.json（候选 + 失败来源）
                                       │
 Google Sheets ──get_values──▶ out/sheet_values.json ──▶ jobs_tool.py marks ──┐
                                                                               ▼
                 jobs_tool.py status ───────────────────────────────────▶ jobs.csv
                                       ▲                                       │
 Routine 检索 + 核实原文 ──▶ jobs_tool.py check / add / set / runlog ───────────┘
                                       │
                 jobs_tool.py status && report
                   ──▶ out/summary.html (+ summary_compact.html)  邮件「岗位总表」
                   ──▶ out/sheets.json                              三个工作表的行/颜色/链接
                   ──▶ out/coverage.md                              「本次检索概况」
                                       │
                 Routine 写表格、发邮件 ──▶ git commit "scan YYYY-MM-DD" ──▶ push main
```

`PROMPT.md` 的「辅助脚本」一节就是这张图的步骤表（0–9）。`out/` 不提交。

## 3. 组件

### 3.1 配置层 `config/`

| 文件 | 内容 | 谁改 |
|---|---|---|
| `profile.toml` | **唯一含个人信息的文件**：身份、学位列表、届别/年龄门槛、研究主题与目标方向（多语言词表）、岗位类型开关、硬门槛、分级口径、城市/国家/专项区域、简历路径、标记前缀 | 本人 |
| `sources.toml` | 扫描范围：`[schedule]` 预算、`[[category]]` 类别（频率、关键词模板、固定词、点名单位、直接访问页、特殊规则）、`[[source]]` 具体页面（可抓取 / 需人工打开 / 不可达） | 本人（无身份信息，但按画像写） |
| `profile.example.toml` | 虚构人物的完整示例，字段齐全带注释；CI 用它校验 | — |

`tools/config.py` 负责读取（`tomllib`）、补默认值（`apply_*_defaults`）、校验（`validate_profile` / `validate_sources` 返回 `(errors, warnings)`），并提供公共函数：`terms_for(obj, lang)`（按类别语言取词，缺则回退 `en`，再回退任一）、`category_active(cat, profile)`（`enabled` ∧ roles 有交集 ∧ special_zone 已定义）、`normalize_url`、`highest_education`、`derive_cohort_year`。`python3 tools/config.py --check` 进 CI。

为什么用 TOML：人要改、要注释（JSON 不行）；`tomllib` 是标准库（PyYAML 不是）；原生日期类型省去解析。

### 3.2 计划层 `plan_tool.py` / `profile_tool.py`

**调度（纯函数，可复现）**

```
day        = (today − schedule.epoch).days
every_eff  = season.every   若当月 ∈ season.months
           = 7              若当月 ∉ active_months
           = every          否则
due        = category_active ∧ (day + offset) % every_eff == 0 ∧ (weekdays 省略 ∨ isoweekday ∈ weekdays)
周末        = ceil(queries_per_turn × weekend_factor)
预算裁剪    = 总数（含 pinned）> max_queries_per_day 时逐个减 1：weight 最低 → 组数最多 → 配置靠后；pinned 永不砍；每类至少 1 组
补跑        = jobs.csv 最大「状态更新日期」为 L；today − L ≥ 2 时并入 (L, today) 每天 due 的类别，lookback 延长
```

**关键词展开（对角轮询，不做笛卡尔积）**

```
turn   = day // every_eff
模板起点 = (turn × queries_per_turn) % len(templates)，取 queries_per_turn 个模板
槽位取值 = 候选列表（按 weight 降序）[(day + 模板序号) % len(候选)]
{unit}  = units 窗口滚动 [(turn × q) % n, +q)
pinned  = 永远在最前
```

允许的槽位：`{topic} {domain} {role} {city} {province} {country} {unit} {zone} {year} {year_next} {cohort} {degree}`。槽位无候选的模板整条跳过并计数；`cohort_system = none` 时含 `{cohort}` 的模板跳过。

**候选人状态**（`profile_tool status`）：年龄按出生月 1 日保守计算，每个 `age_thresholds` 给宽/严两种口径的合格截止日；学位阶段按 `defense_date / conferral_date / diploma_received` 与今天比较得出四段措辞；候选人届别 vs 当前校招季届别（8 月切换）；应届/留学归国身份有效期；「获学位 N 年内」期限。`age-ok --born-after D` 判断按出生日期划线的公告，退出码 0/1/2（2 = 无法判断，含划线日落在出生当月）。

**负面清单**：解析 `learned_rules.md` 中「依据 N 条」且 N ≥ 3 的条目 + `profile.target.avoid`。

产物：`out/brief.md`（节次固定：候选人当前状态 / 今日计划 / 直接抓取结果 / 未轮到的类别 / 负面清单 / 硬门槛 / 分级口径 / 预算 / 补跑）与 `out/plan.json`（`categories[]` 含 `queries, pinned, units, browse, browse_notes, sources_detail, lookback_days, turn…`，`not_due[]` 含 `next_due, reason`）。

### 3.3 抓取层 `fetch_tool.py`

一个通用适配器，站点差异全部在 `[[source]]` 配置里：

1. **线性化**：`HTMLParser` 把页面变成「文本流 + 锚点表（href、锚文本、在文本流中的位置）+ `<time datetime>` 表」。
2. **选锚点**：`link_pattern` 对 href 原文或拼成的绝对 URL 做 `re.search`。
3. **取上下文**：有 `block_pattern` 时按 class/id 切块；否则取锚点前后 300 字（不越过相邻锚点）。
4. **取日期**：`time_attr` → `date_pattern + date_format`（无年份：截止类取就近未来但 ≤14 天前视为刚截止；发布类取就近过去；均标 `inferred`）→ `date_from_url`（命名组 y m d）；`date_kind = range` 取右端。
5. **过滤**：`closed_markers`（块内出现即跳过）→ `title_exclude`（排噪音，正则）→ `title_filter`（`profile` = 按类别语言取 topics/domains/role_terms，多词需各词出现）→ 截止已过 / `max_age_days` → 与 `jobs.csv`、`dismissed.csv` 去重（`normalize_url`）→ 每源上限。
6. **健壮性**：每源超时 + 整轮 `fetch_total_sec`（`deadline` 传入 socket 并分块读）、压缩前/解压后都限 8 MiB、只请求配置声明的 URL、跨域重定向拦截、charset 按 header → meta → utf-8 → gbk 回退、失败分类（超时 / HTTP / TLS 或代理 / 解析 0 条）、**永远 exit 0**。
7. **输出**：`out/fetched.md`（按类别分组的候选，标题经 `safe_field` 转义截断，末尾「抓取失败的来源」）与 `fetched.json`；`--source ID` 合并进当天结果而非覆盖。

`fetch = "browse"` 的来源不抓，只把 URL 与 `access_note` 放进 brief 的「直接访问页」并标注「需人工打开」；`fetch = "none"` 仅作记录。

### 3.4 数据层 `jobs_tool.py` / `jobs.csv`

`jobs.csv`：UTF-8、CRLF、17 列

```
岗位ID, 首次推送日期, 匹配等级, 单位, 岗位, 城市, 链接, 截止日期, 截止说明, 状态, 状态更新日期,
我的标记, 归档日期, 归档原因, 备注, 投递状态, 投递日期
```

- `截止日期` 只允许 `YYYY-MM-DD` 或空；`截止说明` 受控前缀 `需核实 / 长期 / Rolling / 招满即止 / 软截止 YYYY-MM-DD` + 「；」说明。`split_deadline()` 把旧写法拆成两列；软截止关键词：优先审理 / 开始审阅 / 开始审理 / review（「招满为止」只是附注，不改变日期性质）。
- `read_jobs()` 识别旧 16 列表头并在内存升级，`write_jobs()` 恒写 17 列；`migrate` 幂等。因此结构变更**不需要**在 PR 里改 jobs.csv（Routine 每天重写 ~2/3 的行，PR 改它必冲突）。

状态规则（`judge()`，与 PROMPT「状态判定」一一对应）：

| 条件 | 状态 / 归档原因 |
|---|---|
| 标记以 dismiss 前缀开头，或链接在 dismissed.csv | 归档 / 标记中的原因（默认「本人标记不要」） |
| 截止不详且首次推送 ≥ 60 天且无标记无投递状态 | 归档 / 长期无截止日期 |
| 过期 ≥ 7 天（投递状态 ≠ 进展中） | 归档 / 已过期 · 已过期，已投递 · 已过期，未投递 |
| 首次推送 = 今天 | 新增 |
| 剩余 > 7 天或截止不详 | 持续 |
| 0 ≤ 剩余 ≤ 7 | 预警 |
| 过期 < 7 天 | 过期 |

已归档行脚本不动；恢复走 `set --restore`。

子命令：`validate`、`migrate`、`status`、`check`、`add`、`set`（标记 / 投递状态 / 追加备注 / 归档 / 恢复）、`marks --from`（合并三张表：与现值不同者为新值，多个按 已投递 > 岗位总表 > 归档；归档表中清除「不要」视为恢复请求；公式前缀拒收；按表头名定位列，链接列取不到网址时按单位+岗位匹配）、`runlog`（带文件锁）、`report`（`summary.html` 超 90 KB 时另出 `summary_compact.html`；`sheets.json` 三表含 `mark_column / link_column / deadline_note_column`；`coverage.md` 合并 plan、runlog 与 fetched 失败）。

### 3.5 指令层 `PROMPT.md` / `learned_rules.md`

- `PROMPT.md`：流程、规则、红线、步骤表。**不含个人事实与类别清单**，一律引用 `out/brief.md` 与 `config/`。兜底句：脚本任一步失败则直接读两份 TOML 照做并在附注注明。Routine 不得改 `config/ tools/ tests/ .github/`；新来源只能 `sources_tool add`。
- `learned_rules.md`：Routine 从「不要：原因」归纳的规律（每条注明依据条数）+ 标记语义表；本人改过的以本人为准。

### 3.6 质量与 CI

- `tests/`：unittest，离线可跑。覆盖拆分表驱动、迁移幂等、judge 边界、marks 优先级、调度/预算/轮询、年龄与学位阶段、抓取 fixture（真实页面裁剪）、gzip 炸弹与整轮时限、公式注入清洗、隐私扫描（`git ls-files` 减白名单）。
- `.github/workflows/validate.yml`：config check（真实 + 示例）、jobs.csv validate、离线 brief（`--quiet` 不回显个人信息）、sources lint、全部测试。

## 4. 扩展点

| 需求 | 改动 | 代码 |
|---|---|---|
| 新类别 | `[[category]]` + `requires_roles`；profile `target.roles` 打开 | 否 |
| 新列表页 | `[[source]]`；`fetch_tool --source id --no-write` 验证；裁 fixture 加断言 | 否 |
| 新语言 | `terms = { xx = [...] }`，类别 `lang = "xx"` | 否 |
| 频率/预算 | `[schedule]`、`every/offset/season/weight`；`schedule --days 14` 预览 | 否 |
| 新槽位 | `config.ALLOWED_SLOTS` + `plan_tool` 候选表 | 两处 |
| 状态阈值 | `jobs_tool.py` 常量 `WARN_DAYS / EXPIRE_ARCHIVE_DAYS / NO_DEADLINE_ARCHIVE_DAYS` | 常量 |
| 新投递渠道 | PROMPT 第 8 步 + profile `[delivery]` 开关 | PROMPT |
| 新列（如要求要点/匹配理由） | `COLUMNS` + `add` 参数 + `migrate` 链式升级 + sheets_payload | 中等 |
| 表头/枚举英文化 | 加 UI 词表层（`render_*`、`sheets_payload`、工作表名） | 中等，见已知限制 |
| 换 LLM 平台 | 任何能读仓库、执行 `python3`、发邮件的 agent 照 PROMPT.md 都能跑 | 否 |

## 5. 已知限制

- 表格表头、状态/等级/投递状态枚举、邮件章节名固定中文；`identity.language` 只影响 brief 的候选人状态与 Routine 自撰文字。
- 可脚本抓取的来源偏学术门户；工业界大站多为登录/JS 页面，只能 `browse` + 搜索引擎转载。
- 无年份日期的年份是推断的（标 `inferred`），跨年时请核对。
- 单 Routine、单用户；多用户需各自 fork。

## 6. 发布为公开模板

仓库 git 历史里有简历、邮箱、投递记录，**不能直接设为 public**。流程：

1. 用 `scripts/make_template.sh`（或手动）把文件复制到一个全新目录（不带 `.git`）。
2. 脱敏：`profile.toml` ← `profile.example.toml`；删 `resume_*.md`、`sheet_id.txt`；`jobs.csv` 只留表头；`dismissed.csv` 只留「链接」；`sources_found.md` 只留说明段；`learned_rules.md` 只留标记语义表；`sources.toml` 顶部注明是按原作者画像写的模板。
3. `README.md` 用英文版，中文版改名 `README.zh.md`；加 `LICENSE`。
4. 在模板目录跑 `python3 tools/config.py --check` 与全部测试（隐私测试会用 profile 的值扫全仓库）。
5. `git init` 后推到新建的 public 仓库，并在 GitHub 设置中勾选 **Template repository**。
6. 私有仓库照常使用；模板的改进通过 cherry-pick 同步。
