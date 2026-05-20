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
```

Inspect one symbol:

```bash
code-symbol-index inspect Tool --root /path/to/repo
code-symbol-index inspect Tool.method_name --root /path/to/repo
```

Outline a file:

```bash
code-symbol-index outline src/app.py --root /path/to/repo
```

## CLI

```bash
code-symbol-index languages
code-symbol-index index --root /path/to/repo
code-symbol-index status --root /path/to/repo
code-symbol-index status --root /path/to/repo --check
code-symbol-index search Tool --root /path/to/repo
code-symbol-index search Tool Agent Runner --root /path/to/repo
code-symbol-index inspect Tool --root /path/to/repo
code-symbol-index outline src/app.py --root /path/to/repo
code-symbol-index refs Tool --root /path/to/repo --limit 20 --offset 0
code-symbol-index impls Greeter --root /path/to/repo --kind trait --limit 20 --offset 0
code-symbol-index clean --root /path/to/repo
```

JSON is available for structured consumers:

```bash
code-symbol-index search Tool --root /path/to/repo --json
code-symbol-index inspect Tool --root /path/to/repo --json
code-symbol-index outline src/app.py --root /path/to/repo --json
code-symbol-index refs Tool --root /path/to/repo --json
code-symbol-index impls Tool --root /path/to/repo --json
code-symbol-index status --root /path/to/repo --json
```

## Output Formats

Search returns candidates only, never source:

```text
query: Tool
count: 2

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
source:
  status: full
  range: 120:123
  shown_range: 120:123
  total_lines: 3

  120 |def foo():
  121 |    if ok:
  122 |        return 1
```

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
  reason: files changed after last index update
```

## Query Rules

`inspect` accepts only symbol-like input:

- `ClassName`
- `function_name`
- `ClassName.method_name`
- `symbol_prefix`

It rejects natural language, file paths, and directory paths. Use `outline` for
file paths.

All line ranges are `start:end`, 0-based, with `end` exclusive.

## Python API

```python
import code_symbol_index as csi

csi.index("/path/to/repo")
csi.update(["src/app.py", "src/lib.py"], root="/path/to/repo")

print(csi.status_text("/path/to/repo"))
print(csi.search_text("Tool", root="/path/to/repo"))
print(csi.inspect_text("Tool", root="/path/to/repo"))
print(csi.outline_text("src/app.py", root="/path/to/repo"))

symbols = csi.search("Tool", root="/path/to/repo", format="object")
symbols = csi.search(["Tool", "Agent", "Runner"], root="/path/to/repo")
search_payload = csi.search("Tool", root="/path/to/repo", format="json")
search_text = csi.search("Tool", root="/path/to/repo", format="text")
inspection = csi.inspect("Tool", root="/path/to/repo")
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

`search` accepts one query or a list of symbol names/prefixes. Multiple queries
are OR-ed, are not regexes, and share one total `limit`.

## Development

```bash
make install
make check
make smoke
make clean
```

## Python API List

Index lifecycle:

- `index(root=".", *, language=None) -> Repository`
- `update(paths, *, root=".", language=None) -> Repository`
- `clean(root=".") -> None`
- `status(root=".", *, language=None, db_path=None, check=False, format="object") -> IndexStatus | str | dict`
- `status_text(root=".", *, language=None, db_path=None, check=False) -> str`

Queries:

- `search(query: str | list[str], *, root=".", kind=None, language=None, limit=20, sync=False, format="object") -> list[Symbol] | str | list[dict]`
- `search_text(query: str | list[str], *, root=".", kind=None, language=None, limit=20, sync=False) -> str`
- `inspect(query, *, root=".", kind=None, language=None, limit=20, sync=False, format="object", ...) -> Inspection | str | dict`
- `inspect_text(query, *, root=".", kind=None, language=None, sync=False, ...) -> str`
- `refs(query, *, root=".", kind=None, language=None, limit=20, offset=0, sync=False, format="object") -> Page | str | dict`
- `impls(query, *, root=".", kind=None, language=None, limit=20, offset=0, sync=False, format="object") -> Page | str | dict`
- `outline(path, *, root=".", max_symbols=200, sync=False, format="object") -> Page | str | dict`
- `outline_text(path, *, root=".", max_symbols=200, sync=False) -> str`

Repository handle:

- `Repository(root=".", *, languages=None, include=None, exclude=None, db_path=None)`
- `Repository.refresh() -> Repository`
- `Repository.update(paths=None) -> Repository`
- `Repository.search(...)`, `search_text(...)`
- `Repository.inspect(...)`, `inspect_text(...)`
- `Repository.refs(...)`, `impls(...)`
- `Repository.outline(...)`, `outline_text(...)`
- `Repository.clean() -> None`

Data classes:

- `Symbol`
- `Reference`
- `Page`
- `Inspection`
- `InspectOptions`
- `IndexStatus`
