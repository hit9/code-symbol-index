# code-symbol-index

基于 Tree-sitter 的符号索引与代码导航工具，为需要快速、可控、
对 LLM 友好的本地代码库查询而设计。

它提供一个小巧的 Python API 和一个简单的 CLI 命令：

```bash
code-symbol-index
```

默认 CLI 输出为可读文本。在查询命令中添加 `--json` 可在需要
机器可读响应时使用。

## 特性

- 基于 SQLite 的磁盘索引，位于 `.code-symbol-index/index.sqlite`
- 基于 `mtime_ns + size` 的增量索引
- 感知 `.gitignore` 的文件发现
- UTF-8 文本文件过滤
- 通过 `tree-sitter-language-pack` 支持主流语言解析
- 符号搜索、查看、引用、实现者、文件大纲及索引状态
- 针对编程 LLM 上下文窗口优化的有界输出

这是语法级的代码导航，而非语言服务器。它不提供类型感知的重命名安全性
或完整的语义调用图准确性。

本轮查询优化保持 schema 5 的符号索引不变，无需重建。引用仍读取当前源码，
但只提取所查询的名称，并跳过无关 AST 子树；不会把引用写入磁盘索引。
大仓库仍有文件扫描和候选文件解析成本。查询收益与 index/update 成本分别记录在
[性能实测报告](benchmarks/REPORT.md) 中。

### 文件名称摘要

引用和调用图查询利用紧凑的名称摘要排除未变化的无关文件；可能命中的文件仍扫描、
解析当前源码。前 32 次文件检查沿用原路径，避免少量早期结果也要加载摘要。
单次请求的摘要内存有界；没有摘要、特殊名称、超过 1 MiB 的文件均回退原路径。

下次写入时，schema 5 的 `files` 表会增加一个可空的 `name_summary` 字段；
读取旧库不会迁移。正常 `index` 会为未变化文件补摘要，**不重新构建 AST**，
交互终端显示简短阶段提示；后续只补齐缺失或 stat 失效的摘要，包括权限变化和
同大小同 mtime 文件替换。指定路径的 `update` 只维护所选文件。
符号、引用的存储格式不变，以少量空间和索引时间换取更少的查询文件读取。
最近一秒内变化的文件暂不生成摘要，后续刷新再补齐，避免时间戳精度导致漏掉编辑。
非 TTY／被 Codex、Claude 等工具捕获时，index/update 不输出进度，减少 token。
交互终端每跨过 10% 在同一行刷新文件计数和百分比，完成后保留最终一行，不擦除。stdout 结果格式保持兼容，必要提示在 stderr。

只有设备、inode、大小、mtime、ctime 均一致时，才信任摘要的否定结果。
检查只在单次请求内复用，多次调用同一 Repository 也会重新检查；不承诺对并发
源码编辑提供原子快照。Windows 因 ctime 语义不同，保持原扫描路径。
不保存全量引用或源码上下文。

## 安装

将 CLI 安装为 uv 工具：

```bash
uv tool install code-symbol-index
```

或者从本地代码库安装：

```bash
uv tool install .
```

本地开发（可编辑导入与测试）：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

然后：

```bash
code-symbol-index --version
```

## 快速开始

构建或刷新索引：

```bash
code-symbol-index index --root /path/to/repo
```

检查已索引的工具是否可用：

```bash
code-symbol-index status --root /path/to/repo
code-symbol-index status --root /path/to/repo --check
```

搜索符号：

```bash
code-symbol-index search Tool --root /path/to/repo --limit 20
code-symbol-index search Tool Agent Runner --root /path/to/repo
code-symbol-index search Tool --root /path/to/repo --kind class,function --path src --exact-only
```

查看一个符号：

```bash
code-symbol-index inspect Tool --root /path/to/repo
code-symbol-index inspect Tool.method_name --root /path/to/repo
code-symbol-index inspect Tool --root /path/to/repo --anchors
```

文件大纲：

```bash
code-symbol-index outline src/app.py --root /path/to/repo
code-symbol-index outline src/app.py --root /path/to/repo --symbol Tool
```

## 代理技能（Codex / Claude）

安装技能，使 LLM 编程代理能够自动发现并使用 `code-symbol-index`。同一份
`SKILL.md` 同时适用于 Codex 和 Claude Code，用 `--target` 选择代理：

```bash
code-symbol-index install-skill                  # Codex（默认）
code-symbol-index install-skill --target claude  # Claude Code
```

安装位置：

- **Codex** → `$CODEX_HOME/skills/code-symbol-index/`，未设置 `CODEX_HOME` 时为
  `~/.codex/skills/code-symbol-index/`。可用 `--codex-home` 覆盖。
- **Claude** → `$CLAUDE_CONFIG_DIR/skills/code-symbol-index/`，未设置
  `CLAUDE_CONFIG_DIR` 时为 `~/.claude/skills/code-symbol-index/`。可用
  `--claude-dir` 覆盖。

用 `--force` 覆盖已有技能：

```bash
code-symbol-index install-skill --target claude --claude-dir ~/.claude --force
```

安装后，代理将了解符号搜索、查看、引用、调用链、文件大纲、增量更新及索引状态检查等技能规则。

## CLI

```bash
code-symbol-index languages
code-symbol-index --version
code-symbol-index version
code-symbol-index index --root /path/to/repo
code-symbol-index update src/app.py src/lib.py --root /path/to/repo
code-symbol-index status --root /path/to/repo
code-symbol-index status --root /path/to/repo --check
code-symbol-index status --root /path/to/repo --check --max-pending-files 20
code-symbol-index search Tool --root /path/to/repo
code-symbol-index search Tool Agent Runner --root /path/to/repo
code-symbol-index search Tool --root /path/to/repo --kind class,function --path src --exact-only
code-symbol-index inspect Tool --root /path/to/repo
code-symbol-index inspect Tool --root /path/to/repo --path src --exact-only
code-symbol-index inspect Tool --root /path/to/repo --anchors
code-symbol-index outline src/app.py --root /path/to/repo
code-symbol-index outline src/app.py --root /path/to/repo --symbol Tool
code-symbol-index refs Tool --root /path/to/repo --limit 20 --offset 0
code-symbol-index refs Tool --root /path/to/repo --ref-kind call,write
code-symbol-index refs Tool --root /path/to/repo --all-kinds
code-symbol-index impls Greeter --root /path/to/repo --kind trait --limit 20 --offset 0
code-symbol-index callers handle_job --root /path/to/repo --depth 3
code-symbol-index callees handle_job --root /path/to/repo --depth 3
code-symbol-index clean --root /path/to/repo
code-symbol-index install-skill
code-symbol-index install-skill --target claude
```

还支持 JSON 输出：

```bash
code-symbol-index search Tool --root /path/to/repo --json
code-symbol-index inspect Tool --root /path/to/repo --json
code-symbol-index inspect Tool --root /path/to/repo --anchors --json
code-symbol-index outline src/app.py --root /path/to/repo --json
code-symbol-index refs Tool --root /path/to/repo --json
code-symbol-index impls Tool --root /path/to/repo --json
code-symbol-index callers handle_job --root /path/to/repo --json
code-symbol-index status --root /path/to/repo --json
```

## 输出格式

搜索返回候选列表，不返回源码：

```text
query: Tool
count: 2
limit: 20
has_more: false

symbols:
  - id: python:class:Tool:nanocode.py:1284:1330
    name: Tool
    kind: class
    file: nanocode.py
    range: 1284:1330
    signature: class Tool:
    score: exact
    language: python
```

多查询搜索：

```text
queries:
  - Tool
  - Agent
count: 2
limit: 20
has_more: false

symbols:
  - id: python:class:Tool:nanocode.py:1284:1330
    name: Tool
    kind: class
    file: nanocode.py
    range: 1284:1330
    signature: class Tool:
    score: exact
    matched_query: Tool
```

查看返回带稳定 1 基行号的受限源码：

```text
symbol:
  id: python:function:foo:src/app.py:120:123
  name: foo
  kind: function
  file: src/app.py
  range: 120:123
  signature: def foo():
summary:
  imports: 2
  members: 0
  callers: 1
  callees: 1
  references: 3
  reference_kinds: call=2, read=1
  implementors: 0
imports:
  - range: 0:1
    statement: import os
source:
  status: full
  range: 120:123
  shown_range: 120:123
  total_lines: 3

  120 |def foo():
  121 |    if ok:
  122 |        return 1
```

使用 `inspect --anchors` 或 `inspect_text(..., anchors=True)` 输出当前文件内容中的 hash 行锚。默认文本格式为 legacy：

```text
source:
  status: full
  range: 120:123
  shown_range: 120:123
  total_lines: 3
  note: Use line:hash as edit anchor; code starts after |

120:a1b2c3d4|def foo():
121:d4e5f6a7|    if ok:
122:f6a7b8c9|        return 1
```

传入 `--anchor-format explicit`（或 `anchor_format="explicit"`）可输出自解释格式：

```text
anchor=120:a1b2c3d4 | def foo():
anchor=121:d4e5f6a7 |     if ok:
anchor=122:f6a7b8c9 |         return 1
```

JSON 查看模式下使用 `anchors=True` 会包含 `source_anchor`，其中包含 `path`、
`start_line`、`end_line`、`start_anchor`、`end_anchor` 及
`lines[{line, hash, text}]`。哈希基于输出时的文件内容计算。

文件大纲返回文件结构，不包含源码或 ID：

```text
file: nanocode.py
range: 0:9060
count: 142

outline:
1284:1330 | class Tool:
1289:1292 |     def cli_args(cls, args):
1312:1325 |     def tool_schema(cls):
9023:9060 | def main(argv=None):
```

状态查询默认很快，不会扫描目录：

```text
index:
  status: ready
  root: /path/to/repo
  files: 128
  symbols: 4820
  languages: python, typescript
  language_breakdown:
    - python: 80 files (62.5%)
    - typescript: 48 files (37.5%)
  pending_changes: unknown
```

查询子命令（`search`、`inspect`、`refs`、`callers`、`callees`、`impls`、
`outline`）会比较上次完整刷新记录的 Git HEAD 和分支。发生变化就在 stderr
提示索引可能过期，不改变 stdout/JSON，也不自动刷新。检查只读取有界的 Git
元数据，不启动 Git 子进程，不扫描工作区。仅更新远程跟踪引用的 fetch 不报警。

`status` 新增 `git_freshness`：`unchanged`、`changed`、`unknown` 或
`not-applicable`。**Git 未变化不代表源码未变化。** 未提交的编辑、忽略规则变化、
嵌套仓库/子模块以及同大小同 mtime 替换不在 Git 提示的检测范围。
`--root` 指向子目录时监视最近的所属 Git 仓库，该仓库其他目录的提交也可能触发提示。
无法解析的文件不会清空 Git 基线；index/update 会在 stderr 单独报告失败数量，
包括非终端调用。Git 新鲜度不保证所有文件均成功索引。元数据缺失或
不可读、无 loose ref 的 reftable 布局、超过 256 KiB 的 packed refs 都降级为
`unknown`，不误报最新。

schema 仍是 5，旧库无需迁移。旧库没有 Git 基线时，查询会提示尚无法判断；
下次成功运行 `index` 或 `--sync` 时记录基线，这是正常增量刷新，不重解析未变化
文件。指定路径的 `update` 保留旧基线，因为其他文件可能仍需更新。
`status --check` 不写基线；文件一致时即使 Git 变化也可报告 ready。它按文件
大小和 mtime 检查，不校验内容哈希。

使用 `--check` 扫描目录并计算过期状态：

```text
index:
  status: stale
  root: /path/to/repo
  files: 128
  symbols: 4820
  pending_changes: 3
  pending_files:
    - src/app.py
    - src/new_feature.py
  reason: files changed after last index update
```

`pending_files` 受 `--max-pending-files` 限制，仅在 `--check` 时计算。

## 查询规则

`inspect` 仅接受类似符号的输入：

- `ClassName`
- `function_name`
- `ClassName.method_name`
- `symbol_prefix`

它拒绝自然语言、文件路径和目录路径。文件路径请使用 `outline`。

`search` 接受 `A|B|C` 作为非正则的 OR 简写。`--kind` 接受单个或逗号分隔的
种类。`--path` 过滤到文件或目录。`--exact-only` 禁用前缀/模糊匹配。
Python API 中对应的参数为 `kind=`、`path=` 和 `exact_only=True`。

Python 索引将顶层常量、顶层变量和顶层字典键作为符号索引。字典键使用
`kind=dict_key`，父级赋值作为 `container`。

## 行号

所有对外报告的行号均为 **1 基**，区间 `start:end` **两端都包含**——与 `grep -n`、
编辑器、traceback 和 diff 使用的编号一致，因此行号可以在它们之间直接传递而无需换算。
文本输出、CLI `--json` 以及 Python API 的 `format="json"` 都遵循此约定，编辑锚点
（`line:hash`）中的 `line` 部分同样如此。出于同样的理由，JSON 输出中的列号也是 1 基。

唯一的例外是 `format="object"`，它返回库内部的 dataclass：其中的 `Position.line`
和 `Position.column` 仍为 **0 基**，因为它们的用途是直接索引 `source.splitlines()`。

## 引用类型（Reference Kinds）

`refs` 会按“如何使用该符号”对每条引用分类，从而把真正的行为依赖与无关噪声区分开。
每个条目都带有 `kind`：

| kind | 含义 |
| --- | --- |
| `call` | 符号被调用（`f(...)`、方法调用） |
| `read` | 在表达式中读取其值 |
| `write` | 作为赋值/变更的目标 |
| `inherit` | 基类 / `extends` / `implements` / trait 约束 |
| `type` | 用于类型注解位置 |
| `import` | 出现在 import / `use` 语句中 |
| `attribute` | `obj.name` 成员访问（无法在语法层面绑定） |
| `usage` | 无法判定时的兜底 |

默认情况下 `refs` 与 `inspect` 会隐藏噪声较大的 `import` 与 `attribute` 类型。
使用 `--ref-kind` 指定逗号分隔的子集，或用 `--all-kinds` 显示全部：

```bash
code-symbol-index refs Tool --root /path/to/repo --ref-kind call,write
code-symbol-index refs Tool --root /path/to/repo --all-kinds
```

Python API 通过 `refs(...)` / `inspect(...)` 上的 `ref_kinds=` 提供同样的能力：
传入可迭代对象或逗号分隔字符串，或传 `"all"` 关闭过滤。`inspect` 的摘要中会给出
`reference_kinds` 计数明细。

分类是语法级的（tree-sitter，无类型推断）。Python、JavaScript、TypeScript/TSX、
Swift、Kotlin、Ruby、PHP 有调优规则，其它语言为尽力而为并回退到 `read`/`usage`。
请把 `kind` 当作强提示而非保证。

Ruby 有一处限制：无接收者且不带括号与参数的调用（`helper`）会被解析为普通标识符，
在无语义分析的前提下与局部变量读取无法区分，因此不会产生调用边。`helper()`、
`helper 1`、`self.helper`、`obj.helper` 均可正常解析。

## 调用链（Call Chains）

`callers` 与 `callees` 从一个符号出发，沿真实的 `call` 边遍历调用图，最多到
`--depth`（默认 3，最大 6）。在大型代码库中快速定位某个函数的真实执行路径非常有用。

```bash
code-symbol-index callers handle_agent_job_run --root /path/to/repo --depth 3
code-symbol-index callees handle_agent_job_run --root /path/to/repo --depth 3
```

`callers` 会把可达的**入口点**按类型分组——`http_route`、`worker`、`tool`、
`script`、`test`——并给出一条回到目标的代表性调用路径：

```text
direction: callers
depth: 3
confidence: low
entry_points:
  http_route:
    - run_agent_endpoint  app/api/agents.py:45
        path: run_agent_endpoint -> dispatch_job -> handle_agent_job_run
  worker:
    - process_queue  app/workers/queue.py:88
        path: process_queue -> handle_agent_job_run
```

入口类型为启发式判断（路径/命名约定 + 装饰器扫描），以 Python 为主。遍历是
**语法级、基于名字**的（`confidence: low`）：间接/动态分发可能被遗漏，同名符号可能被
混淆，因此用它来缩小范围，再用 `inspect` 确认。`--limit` 限制每个节点展开的扇出，
`truncated: true` 表示结果被截断。同名歧义可用 `--path` / `--kind` / `--exact-only` 消解。

`callees` 在解析每个调用时优先匹配同文件、其次同目录（包），最后取全局唯一匹配；
对通用名（`get`、`add` 等）的跨模块歧义匹配会被丢弃以保证精度。加 `--loose`
（API 中 `loose=True`）可纳入这些低精度匹配。

Python API 同样提供 `callers(query, *, depth=3, limit=20, ...)` 与
`callees(query, *, depth=3, limit=20, loose=False, ...)`，返回 `CallGraph`
（或通过 `format=` 返回文本/JSON）。

## Python API

```python
import code_symbol_index as csi

csi.index("/path/to/repo")
csi.update(["src/app.py", "src/lib.py"], root="/path/to/repo")

print(csi.status_text("/path/to/repo"))
print(csi.search_text("Tool", root="/path/to/repo"))
print(csi.search_text("Tool|Agent", root="/path/to/repo", kind="class,function", path="src"))
print(csi.inspect_text("Tool", root="/path/to/repo"))
print(csi.inspect_text("Tool", root="/path/to/repo", path="src", exact_only=True))
print(csi.inspect_text("Tool", root="/path/to/repo", anchors=True))
print(csi.outline_text("src/app.py", root="/path/to/repo"))
print(csi.outline_text("src/app.py", root="/path/to/repo", symbol="Tool"))

symbols = csi.search("Tool", root="/path/to/repo", format="object")
symbols = csi.search(["Tool", "Agent", "Runner"], root="/path/to/repo")
search_payload = csi.search("Tool", root="/path/to/repo", format="json")
search_text = csi.search("Tool", root="/path/to/repo", format="text")
inspection = csi.inspect("Tool", root="/path/to/repo")
anchored = csi.inspect("Tool", root="/path/to/repo", format="json", anchors=True)
references = csi.refs("Tool", root="/path/to/repo", limit=20, offset=0)
```

对于重复查询，可复用仓库句柄：

```python
repo = csi.Repository("/path/to/repo")
repo.update(["src/app.py"])
print(repo.search_text("Tool"))
print(repo.inspect_text("Tool"))
print(repo.outline_text("src/app.py"))
```

刷新和更新接受可选的任务进度回调：

```python
def on_progress(event, *, done=0, total=0, path=None):
    print(event, done, total, path)

repo = csi.Repository("/path/to/repo", progress=on_progress)
repo.refresh()
repo.update(["src/app.py"], progress=on_progress)
```

稳定的进度事件为 `scan`、`start`、`file` 和 `finish`。
`finish` 的 `done` 是成功解析文件数，`total` 是尝试解析文件数。

仅当 stderr 为交互式终端时，CLI 才每跨过 10% 在同一行刷新文件计数和百分比，完成后保留最终一行。当 stderr 被捕获（管道，或被
agent 读取）时，不输出进度，
因此 `--sync` 查询的结果输出保持干净。

若要在应用启动时刷新索引而不阻塞启动：

```python
thread = csi.refresh_async("/path/to/repo", progress=on_progress)
```

`refresh_async` 会在后台线程中创建自己的 `Repository` 实例。
请勿跨线程共享 `Repository` 实例。

查询需要已存在的索引。请先运行 `code-symbol-index index` 或
`code_symbol_index.index()`。查询不会自动同步，除非使用 `--sync` 或
`sync=True` 调用。文件外部编辑后，调用
`code_symbol_index.update(paths, root=...)` 或 `Repository.update(paths)`
来仅刷新这些文件；已删除或新增忽略的文件会从索引中移除。

## 开发

```bash
# 克隆代码库后，安装所需依赖并运行测试
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
uv run pytest
```

## Python API 列表

| 函数 | 返回 | 作用 |
|---|---|---|
| `index(root, progress?)` | `int` | 对根目录下的所有文件构建索引 |
| `update(paths, root, progress?)` | `int` | 增量更新给定路径 |
| `clean(root)` | – | 删除磁盘索引 |
| `refresh(root, progress?)` | `int` | 刷新索引（扫描变更并更新） |
| `refresh_async(root, progress?)` | `Thread` | 在后台线程中刷新 |
| `Repository(root, progress?)` | `Repository` | 带缓存的持久化仓库句柄 |
| `Repository.refresh(progress?)` | `int` | 刷新实例索引 |
| `Repository.update(paths, progress?)` | `int` | 增量更新实例索引 |
| `Repository.close()` | – | 提交并关闭数据库 |
| `status(root, check?, max_pending_files?)` | `dict` | 对象格式的索引状态 |
| `status_text(root, check?, max_pending_files?)` | `str` | 文本格式的索引状态 |
| `search(queries, root?, kind?, path?, exact_only?, limit?, offset?, format?)` | `list`, `dict`, `str` | 搜索符号 |
| `search_text(queries, root?, ...)` | `str` | 文本格式的搜索结果 |
| `inspect(symbol, root?, path?, exact_only?, anchors?, format?)` | `dict`, `str` | 查看一个符号 |
| `inspect_text(symbol, root?, anchors?, anchor_format?, ...)` | `str` | 文本格式的查看结果 |
| `outline(path, root?, symbol?)` | `dict` | 对象格式的文件大纲 |
| `outline_text(path, root?, symbol?)` | `str` | 文本格式的文件大纲 |
| `refs(symbol, root?, limit?, offset?)` | `list` | 对象格式的引用列表 |
| `impls(symbol, root?, kind?, limit?, offset?)` | `list` | 对象格式的实现候选列表 |
| `install_skill(target?, codex_home?, claude_dir?, force?)` | `Path` | 安装代理技能（Codex / Claude） |
| `languages()` | `list` | 支持的语言列表 |
| `version_text()` | `str` | 版本字符串 |
