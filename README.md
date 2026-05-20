# code-symbol-index

Tree-sitter backed symbol search and inspection for Python projects that need to index another codebase.

```python
import code_symbol_index as cs

cs.index()

for symbol in cs.search("Handler"):
    print(symbol.name, symbol.path, symbol.range.start.line)

print(cs.inspect_text("Handler"))

inspection = cs.inspect("Handler")  # JSON/object-style API
```

For repeated queries in one process:

```python
cs.index()
repo = cs.Repository(".")
repo.search("Handler")
repo.refs("Handler", limit=20, offset=0)
```

Queries require an existing `.code-symbol-index/index.sqlite`; run `code-symbol-index index` or `code_symbol_index.index()` first. Queries read the existing index by default and do not sync automatically. Use `code-symbol-index index`, `--sync`, `code_symbol_index.index()`, `repo.refresh()`, or `sync=True` to refresh explicitly. Unchanged files are skipped by `mtime_ns + size`. Deleted files are removed from the index, and added or changed files are parsed in parallel and written to SQLite in committed file batches. The disk index persists files and symbols; references are computed on demand for `refs` and `inspect`.

File discovery respects `.gitignore` files, skips common dependency/build/cache directories, and only parses files that look like UTF-8 text.
Press `Ctrl-C` to interrupt indexing; the CLI exits with status 130.

The first version provides syntactic definitions, lexical references, and basic implementation lookup. It is not a replacement for a language server and does not promise type-aware rename safety.

## CLI

```bash
code-symbol-index languages
code-symbol-index index
code-symbol-index Handler
code-symbol-index Handler --sync
code-symbol-index search Handler --root /path/to/repo --language python
code-symbol-index search Handler --root /path/to/repo --language python --json
code-symbol-index outline src/app.py --root /path/to/repo
code-symbol-index outline src/app.py --root /path/to/repo --json
code-symbol-index status --root /path/to/repo
code-symbol-index status --root /path/to/repo --json
code-symbol-index inspect helper --root /path/to/repo --language python
code-symbol-index inspect helper --root /path/to/repo --language python --json
code-symbol-index refs helper --root /path/to/repo --language python --limit 20 --offset 0
code-symbol-index refs helper --root /path/to/repo --language python --limit 20 --offset 0 --json
code-symbol-index impls Greeter --root /path/to/repo --language rust --kind trait --limit 20 --offset 0
code-symbol-index impls Greeter --root /path/to/repo --language rust --kind trait --limit 20 --offset 0 --json
code-symbol-index clean
```
