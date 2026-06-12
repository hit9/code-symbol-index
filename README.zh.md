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

查看返回带稳定 0 基行号的受限源码：

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

使用 `inspect --anchors` 或 `inspect_text(..., anchors=True)` 输出当前文件内容中的 hash 行锚：

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

所有行区间均为 0 基数的 `start:end`，`end` 不包含。

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

分类是语法级的（tree-sitter，无类型推断）。Python、JavaScript、TypeScript/TSX
有调优规则，其它语言为尽力而为并回退到 `read`/`usage`。请把 `kind` 当作强提示而非保证。

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

仅当 stderr 为交互式终端时，CLI 才显示实时进度条。当 stderr 被捕获（管道，或被
agent 读取）时，会抑制逐文件刷新，并在完成时只打印一行 `indexed N files` 摘要，
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
| `inspect_text(symbol, root?, ...)` | `str` | 文本格式的查看结果 |
| `outline(path, root?, symbol?)` | `dict` | 对象格式的文件大纲 |
| `outline_text(path, root?, symbol?)` | `str` | 文本格式的文件大纲 |
| `refs(symbol, root?, limit?, offset?)` | `list` | 对象格式的引用列表 |
| `impls(symbol, root?, kind?, limit?, offset?)` | `list` | 对象格式的实现候选列表 |
| `install_skill(target?, codex_home?, claude_dir?, force?)` | `Path` | 安装代理技能（Codex / Claude） |
| `languages()` | `list` | 支持的语言列表 |
| `version_text()` | `str` | 版本字符串 |
