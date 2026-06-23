# code-symbol-index

Tree-sitter backed symbol index and code navigation for tools that need fast,
bounded, LLM-friendly answers over a local codebase.

It provides a small Python API and a single CLI command:

```bash
code-symbol-index
```

The default CLI output is readable text. Add `--json` on query commands when a
machine-readable response is better.

## Features

- Disk-backed SQLite index at `.code-symbol-index/index.sqlite`
- Incremental indexing by `mtime_ns + size`
- `.gitignore` aware file discovery
- UTF-8 text file filtering
- Mainstream language parsing through `tree-sitter-language-pack`
- Symbol search, inspect, references, implementors, file outline, and index status
- Bounded outputs designed for coding LLM context windows

This is syntactic code navigation, not a language server. It does not provide
type-aware rename safety or full semantic call graph accuracy.

## Install

Install the CLI as a uv tool:

```bash
uv tool install code-symbol-index
```

Or install from a local checkout:

```bash
uv tool install .
```

For local development with editable imports and tests:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

Then:

```bash
code-symbol-index --version
```

## Quick Start

Build or refresh the index:

```bash
code-symbol-index index --root /path/to/repo
```

Check whether indexed tools are available:

```bash
code-symbol-index status --root /path/to/repo
code-symbol-index status --root /path/to/repo --check
```

Search symbols:

```bash
code-symbol-index search Tool --root /path/to/repo --limit 20
code-symbol-index search Tool Agent Runner --root /path/to/repo
code-symbol-index search Tool --root /path/to/repo --kind class,function --path src --exact-only
```

Inspect one symbol:

```bash
code-symbol-index inspect Tool --root /path/to/repo
code-symbol-index inspect Tool.method_name --root /path/to/repo
code-symbol-index inspect Tool --root /path/to/repo --anchors
```

Outline a file:

```bash
code-symbol-index outline src/app.py --root /path/to/repo
code-symbol-index outline src/app.py --root /path/to/repo --symbol Tool
```


## Agent Skill (Codex / Claude)

Install the skill so LLM coding agents can discover and use
`code-symbol-index` automatically. The same `SKILL.md` works for both Codex and
Claude Code; choose the agent with `--target`:

```bash
code-symbol-index install-skill                  # Codex (default)
code-symbol-index install-skill --target claude  # Claude Code
```

Install locations:

- **Codex** → `$CODEX_HOME/skills/code-symbol-index/`, or
  `~/.codex/skills/code-symbol-index/` when `CODEX_HOME` is not set. Override with
  `--codex-home`.
- **Claude** → `$CLAUDE_CONFIG_DIR/skills/code-symbol-index/`, or
  `~/.claude/skills/code-symbol-index/` when `CLAUDE_CONFIG_DIR` is not set.
  Override with `--claude-dir`.

Use `--force` to overwrite an existing skill:

```bash
code-symbol-index install-skill --target claude --claude-dir ~/.claude --force
```

Once installed, the agent will know the skill rules for symbol search,
inspection, references, call chains, file outlines, incremental updates, and
index status checks.

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

JSON is available for structured consumers:

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

## Output Formats

Search returns candidates only, never source:

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

For multiple search queries:

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

Inspect returns bounded source with stable 0-based line ranges:

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

Use `inspect --anchors` or `inspect_text(..., anchors=True)` to emit hashline
source anchors from the current file contents. The default text format is
legacy:

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

Pass `--anchor-format explicit` (or `anchor_format="explicit"`) for
self-describing anchors:

```text
anchor=120:a1b2c3d4 | def foo():
anchor=121:d4e5f6a7 |     if ok:
anchor=122:f6a7b8c9 |         return 1
```

JSON inspect with `anchors=True` includes `source_anchor` with `path`,
`start_line`, `end_line`, `start_anchor`, `end_anchor`, and
`lines[{line, hash, text}]`. Hashes are computed from current file contents at
output time.

Outline returns file structure without source or ids:

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

Status is fast by default and does not scan the directory:

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

Use `--check` to scan the directory and compute staleness:

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

`pending_files` is bounded by `--max-pending-files` and is only computed with
`--check`.

## Query Rules

`inspect` accepts only symbol-like input:

- `ClassName`
- `function_name`
- `ClassName.method_name`
- `symbol_prefix`

It rejects natural language, file paths, and directory paths. Use `outline` for
file paths.

`search` accepts `A|B|C` as a non-regex OR shorthand. `--kind` accepts one kind
or comma-separated kinds, `--path` filters to a file or directory, and
`--exact-only` disables prefix/fuzzy matches. The same filters are available in
the Python API as `kind=`, `path=`, and `exact_only=True`.

Python indexes top-level constants, top-level variables, and top-level
dictionary keys as symbols. Dictionary keys use `kind=dict_key` and the parent
assignment as `container`.

All line ranges are `start:end`, 0-based, with `end` exclusive.

## Reference Kinds

`refs` classifies every reference by how it uses the symbol, so you can tell a
real behavioral dependency apart from incidental noise. Each item carries a
`kind`:

| kind | meaning |
| --- | --- |
| `call` | the symbol is invoked (`f(...)`, method call) |
| `read` | the value is read in an expression |
| `write` | the symbol is an assignment / mutation target |
| `inherit` | a base class / `extends` / `implements` / trait bound |
| `type` | used in a type annotation position |
| `import` | named in an import / `use` statement |
| `attribute` | `obj.name` member access (can't be bound syntactically) |
| `usage` | fallback when nothing more specific applies |

By default `refs` and `inspect` hide the high-noise `import` and `attribute`
kinds. Use `--ref-kind` to request an explicit comma-separated subset, or
`--all-kinds` to show everything:

```bash
code-symbol-index refs Tool --root /path/to/repo --ref-kind call,write
code-symbol-index refs Tool --root /path/to/repo --all-kinds
```

The Python API mirrors this with `ref_kinds=` on `refs(...)` / `inspect(...)`:
pass an iterable or comma-separated string of kinds, or `"all"` to disable the
filter. `inspect` reports a `reference_kinds` count breakdown in its summary.

Classification is syntactic (tree-sitter, no type inference). Python,
JavaScript, and TypeScript/TSX have tuned rules; other languages get a
best-effort subset and otherwise fall back to `read`/`usage`. Treat `kind` as a
strong hint, not a guarantee.

## Call Chains

`callers` and `callees` walk the transitive call graph from a symbol up to
`--depth` (default 3, max 6), following actual `call` edges. They make it fast
to find the real execution paths into or out of a function in a large codebase.

```bash
code-symbol-index callers handle_agent_job_run --root /path/to/repo --depth 3
code-symbol-index callees handle_agent_job_run --root /path/to/repo --depth 3
```

`callers` groups the reachable **entry points** by type — `http_route`,
`worker`, `tool`, `script`, `test` — and shows a representative call path back
to the target:

```text
target:
  name: handle_agent_job_run
  ...
direction: callers
depth: 3
confidence: low
truncated: false
entry_points:
  http_route:
    - run_agent_endpoint  app/api/agents.py:45
        path: run_agent_endpoint -> dispatch_job -> handle_agent_job_run
  worker:
    - process_queue  app/workers/queue.py:88
        path: process_queue -> handle_agent_job_run
callers:
  - dispatch_job  app/jobs.py:200
      - run_agent_endpoint  app/api/agents.py:45  [http_route]
  - process_queue  app/workers/queue.py:88  [worker]
```

Entry types are detected heuristically (path/name conventions + a decorator
scan) and are Python-first. The traversal is **syntactic and name-based**
(`confidence: low`): indirect/dynamic dispatch may be missed and same-named
symbols can be conflated, so use it to narrow down, then confirm with `inspect`.
`--limit` caps the fan-out expanded per node; `truncated: true` marks a capped
result. Disambiguate a common name with `--path` / `--kind` / `--exact-only`.

`callees` resolves each call to a callable symbol, preferring the same file,
then the same package, then a unique match anywhere; ambiguous cross-module
matches on generic names (`get`, `add`, ...) are dropped for precision. Pass
`--loose` (`loose=True` in the API) to include those lower-precision matches.

The Python API mirrors this: `callers(query, *, depth=3, limit=20, ...)` and
`callees(query, *, depth=3, limit=20, loose=False, ...)` return a `CallGraph`
(or text/JSON via `format=`).

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

For repeated queries, reuse a repository handle:

```python
repo = csi.Repository("/path/to/repo")
repo.update(["src/app.py"])
print(repo.search_text("Tool"))
print(repo.inspect_text("Tool"))
print(repo.outline_text("src/app.py"))
```

Refresh and update accept an optional progress callback:

```python
def on_progress(event, *, done=0, total=0, path=None):
    print(event, done, total, path)

repo = csi.Repository("/path/to/repo", progress=on_progress)
repo.refresh()
repo.update(["src/app.py"], progress=on_progress)
```

Stable progress events are `scan`, `start`, `file`, and `finish`.

The CLI shows a live progress bar only when stderr is an interactive terminal.
When stderr is captured (piped, or read by an agent), per-file updates are
suppressed and a single `indexed N files` summary line is printed on completion,
so a `--sync` query keeps its result output clean.

To refresh the index during application startup without blocking startup:

```python
thread = csi.refresh_async("/path/to/repo", progress=on_progress)
```

`refresh_async` creates its own `Repository` inside the background thread.
Do not share a `Repository` instance across threads.

Queries require an existing index. Run `code-symbol-index index` or
`code_symbol_index.index()` first. Queries do not sync automatically unless
called with `--sync` or `sync=True`. After external file edits, call
`code_symbol_index.update(paths, root=...)` or `Repository.update(paths)` to
refresh only those files; deleted or newly ignored paths are removed from the
index.


Top-level query APIs accept `format="object" | "text" | "json"`:

- `object` returns Python dataclasses/lists and is the default.
- `text` returns the same readable format as the `*_text` helpers.
- `json` returns JSON-safe Python dict/list data.

`search` accepts one query, `A|B|C`, or a list of symbol names/prefixes.
Multiple queries are OR-ed, are not regexes, and share one total `limit`.
Search text and JSON formats include `has_more` when more matches exist beyond
`limit`.

## Development

```bash
make install
make check
make smoke
make clean
```

## Python API List

Index lifecycle:

- `index(root=".", *, language=None, progress=None) -> Repository`
- `update(paths, *, root=".", language=None, progress=None) -> Repository`
  CLI: `code-symbol-index update <paths...> --root <repo>`
- `refresh_async(root=".", *, language=None, db_path=None, progress=None, daemon=True) -> threading.Thread`
- `install_skill(*, target="codex", codex_home=None, claude_dir=None, force=False) -> Path`
- `clean(root=".") -> None`
- `status(root=".", *, language=None, db_path=None, check=False, max_pending_files=50, format="object") -> IndexStatus | str | dict`
- `status_text(root=".", *, language=None, db_path=None, check=False, max_pending_files=50) -> str`

Queries:

- `search(query: str | list[str], *, root=".", kind=None, language=None, path=None, exact_only=False, limit=20, sync=False, format="object") -> list[Symbol] | str | dict`
- `search_text(query: str | list[str], *, root=".", kind=None, language=None, path=None, exact_only=False, limit=20, sync=False) -> str`
- `inspect(query, *, root=".", kind=None, language=None, path=None, exact_only=False, limit=20, anchors=False, anchor_format="legacy", sync=False, format="object", ...) -> Inspection | str | dict`
- `inspect_text(query, *, root=".", kind=None, language=None, path=None, exact_only=False, anchors=False, anchor_format="legacy", sync=False, ...) -> str`
- `refs(query, *, root=".", kind=None, language=None, path=None, exact_only=False, limit=20, offset=0, sync=False, format="object", ref_kinds="behavioral") -> Page | str | dict`
- `impls(query, *, root=".", kind=None, language=None, path=None, exact_only=False, limit=20, offset=0, sync=False, format="object") -> Page | str | dict`
- `callers(query, *, root=".", kind=None, language=None, path=None, exact_only=False, depth=3, limit=20, sync=False, format="object") -> CallGraph | str | dict`
- `callees(query, *, root=".", kind=None, language=None, path=None, exact_only=False, depth=3, limit=20, sync=False, format="object") -> CallGraph | str | dict`
- `outline(path, *, root=".", symbol=None, max_symbols=200, sync=False, format="object") -> Page | str | dict`
- `outline_text(path, *, root=".", symbol=None, max_symbols=200, sync=False) -> str`

Repository handle:

- `Repository(root=".", *, languages=None, include=None, exclude=None, db_path=None)`
- `Repository.refresh(*, progress=None) -> Repository`
- `Repository.update(paths=None, *, progress=None) -> Repository`
- `Repository.search(...)`, `search_text(...)`
- `Repository.inspect(...)`, `inspect_text(...)`
- `Repository.refs(...)`, `impls(...)`
- `Repository.callers(...)`, `callees(...)`
- `Repository.outline(...)`, `outline_text(...)`
- `Repository.clean() -> None`

Data classes:

- `Symbol`
- `Reference`
- `Page`
- `Inspection`
- `InspectOptions`
- `IndexStatus`
- `CallGraph`
- `CallNode`
- `EntryPoint`
