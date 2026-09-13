# Changelog

## Unreleased

### Added

- `index --header-language c|cpp` chooses how `.h` files are parsed. The value is
  stored in the index (`c_header_language`), reused by later `index`/`update` calls,
  and only an unfiltered `index` may change it. Reads keep using the language each
  file's own row was written with, so an interrupted or partial switch never mixes
  parsers inside one file, and a run that could not convert every header says so
  instead of reporting a completed conversion.
- Per-file extraction-rule revisions (`files.extractor_revision`, schema stays 5). An
  explicit `index` or `index --sync` re-extracts unchanged files whose rules moved,
  reports how many files it upgraded, and never rebuilds the whole database. Files
  already at the current revision are not parsed again; a file that fails keeps its
  previous rows and is retried by the next run; a filtered refresh only ever deletes
  rows of the languages it scanned.
- `update --json` reports gone or no-longer-indexable paths as `updated` and keeps
  failed paths separate, so a failed file is never reported as updated.

### Changed

- C/C++ extraction now follows declarator structure: pointer, array, function-pointer
  and multi-declarator forms, class/namespace scope, constructors versus destructors,
  qualified definitions, base classes as inheritance, unions, aliases, macros, fields
  and enumerators are all indexed, with the declaration used as the preview range.
- Depth-1 call edges come from real byte ranges plus function bodies. Two methods on
  one line no longer share a caller, and a nested function body (Python `def`,
  lambda, closure, arrow function) no longer contributes calls to the function that
  declares it. Anonymous bodies still fence off their enclosing function.
- When a declaration and a definition share a name, `inspect`, `refs`, `callers` and
  `callees` prefer the one with a body, including across scopes (a Rust trait method
  and its impl, a Swift protocol member and its conformance), and the text, JSON and
  object output of `inspect` all answer with that same target. Re-reading a header
  honors the language its own row was stored with, so a C++ header is not re-parsed
  as C when a body decides. Two definitions stay an ambiguity error for
  `inspect`/`callers`/`callees`, while `refs`/`impls` keep their existing order
  between equally valid matches.
- Other languages, per the confirmed gaps in the validation record: Python indexes
  multi-target, chained and class-body bindings; Rust indexes trait and extern
  function declarations; TypeScript/TSX index interface members
  (`method_signature`, `property_signature`), ambient function declarations,
  abstract classes and destructuring bindings; Swift indexes every name of a
  multi-property declaration; Kotlin indexes destructuring bindings and no longer
  treats a plain primary-constructor parameter as a property.
- Review fixes preserve existing rows when files or directories are unreadable,
  keep filtered refreshes from acknowledging a repository-wide Git checkout, and
  preserve headers excluded by a language filter during partial conversion.
  Full status checks honor the saved header language. Decorated Python classes
  retain their fields, and multi-binding previews cover the complete declaration.
- `inspect` no longer counts a plain read of a function value as a call relation, so
  a function passed as an argument is not reported as calling or being called.
- Index writers omit query-only body metadata, send flattened symbol rows between
  workers, and avoid unnecessary traversal work. C/C++ definition preference uses
  bounded ancestor lookups instead of re-extracting whole candidate files. A rule
  upgrade compares old and new symbols in batches and skips rewriting identical
  symbol/FTS rows. Index writes print a percentage while writing on a terminal.

### Notes

- Upgrading an existing index needs an explicit `index` (or `index --sync`): reading
  never migrates and never re-parses for rules. Symbol IDs, kinds and result contents
  are expected to differ from 0.5.5, and query results may contain more matches.
- Independent acceptance findings, the full measurement matrix and the untested
  boundaries are recorded in `benchmarks/REVIEW-language-support.md`, with every raw
  sample in `benchmarks/language-support-review.json`. The original fresh-index
  benchmark actually timed an already-populated index; that has been corrected.
- Fresh indexing of C/C++-heavy sources is up to 8% slower than 0.5.5 because the
  corrected rules publish up to 36% more symbols; per symbol it is about 19%
  cheaper. The effect does not scale: 1,001- and 10,001-file C corpora index 29%
  and 11% faster. Queries are unchanged or faster (large C `refs` 0.74x, `inspect`
  0.82x). Measured warm-cache, on local storage, in independent processes; cold
  cache, p95 and shared mounts are untested.
- The one-time rule upgrade costs about 5.1 s for 10,001 C files, after which
  `index` is back at steady state. Comparing it against 0.5.5's unchanged `index`
  compares against work 0.5.5 never does; against the previous upgrade
  implementation it is 0.58x where a file's symbols are unchanged and even
  otherwise.

## 0.5.5 - 2026-09-10

### Added

- CLI update checks: contact PyPI at most once every 7 days in a background thread
  and hint at most once per interval on stderr when a newer stable release exists.
  stdout stays clean; pre-release versions are ignored; failures are silent. Set
  `CODE_SYMBOL_INDEX_NO_UPDATE_CHECK=1` to disable, or
  `CODE_SYMBOL_INDEX_UPDATE_CACHE` to relocate the cache file.

## 0.5.4 - 2026-09-10

### Fixed

- Show terminal-only index-writing and transaction-commit stages after file
  parsing reaches 100%, without changing public progress callback events.

## 0.5.3 - 2026-09-10

### Fixed

- Refresh terminal index/update percentages on the same line and retain the
  final counts. Captured calls remain silent; warnings start on a separate line.

## 0.5.2 - 2026-09-10

### Changed

- Refill stat-invalidated name summaries on refresh without rebuilding unchanged
  ASTs; compare only stored headers against the existing scan's stat results.
- Keep Git baseline recording independent of skipped source files; report
  incomplete indexing separately, including in captured calls.
- Defer process-pool, Tree-sitter, hashing and argument-parser imports until
  needed; plain `version` bypasses parser construction. Captured index/update
  calls emit no progress. Terminals show file counts and percentages at 10% milestones without cursor control.
- Explain missing query summaries on stderr without migrating during reads;
  keep actionable freshness and upgrade hints separate from stdout results.
- Parse small batches directly (up to 16 files / 64 KiB); keep process workers
  for larger work. Avoid copying AST child lists that are already lists.
- Add compact per-file name summaries for live reference/caller prefiltering.
  Old schema-5 reads remain usable; writes add one nullable column, and normal
  index fills missing summaries without reparsing unchanged files. Missing or
  invalid summaries fall back to scanning. Query checks expire per request.

- Query commands warn on stderr after Git branch/HEAD changes. Default status
  exposes bounded Git freshness separately from full file checks. Schema 5 is
  unchanged: baseline metadata is recorded by the next full incremental refresh;
  partial updates do not clear Git suspicion. Unsupported Git layouts are unknown.

- Locate small result sets through native AST byte lookup during formatting;
  large outlines retain a single traversal. No schema or index/update changes.

- Load ignore-rule and parser dependencies when actually needed, reducing
  startup cost for status, version, empty results and other lightweight commands.
- Reuse a per-source line-offset table instead of repeatedly scanning source
  prefixes for positions. Symbol-only index/update skip reference context work.
- Repository reference queries extract only the requested name and prune
  unrelated AST subtrees. Results retain live-source and classification semantics;
  the schema-5 symbol database and stored rows are unchanged, with no migration.
- Batch same-file result ranges in search, inspect relation sections and impls
  formatting. Text outline reuses its query result; ordinary status avoids loading
  the complete file manifest. Explicit `status --check` still scans for changes.
- Reproducible before/after CLI and write-cost measurements are documented in
  `benchmarks/REPORT.md`. Small commands remain dominated by startup, and reference
  queries still scan files; these changes do not make every command equally fast.

## 0.5.1 - 2026-08-17

### Changed

- `index` no longer walks the tree twice. Collecting `.gitignore` files was a
  separate pass that ran before the scan, and it could not prune ignored
  directories because it was the pass building the ignore rules -- so it
  descended into every gitignored build and cache directory. Whenever such a
  directory is not also covered by the built-in exclude list, that pass
  dominated the runtime of an up-to-date `index`: on a tree whose ignored cache
  directories hold 10.8k files, it visited 3,724 directories where the scan
  itself needed 2. Ignore rules are now gathered as the single walk descends
  (each directory inherits its parent's stack and adds its own), so ignored
  directories are pruned on contact and never entered. Matching a path also
  tests only its own ancestors' rules instead of every `.gitignore` in the
  repo. That tree's scan went from 119ms to 2ms.
- The `index` scan stage is dramatically faster on large trees. It now walks on
  plain strings and builds a `Path` only for files it actually yields, tests the
  file extension first (the cheapest and most selective filter), matches the
  exclude list with one precompiled regex instead of an `fnmatch` call per
  pattern, and scopes each `.gitignore` by string prefix instead of
  `Path.relative_to` with exceptions for control flow. The old code tested every
  `.gitignore` against every candidate path, so cost grew with
  *files x nested-ignore-files*: a 18.6k-file tree with 200 nested `.gitignore`
  files scanned in 18.0s and now takes 47ms. Scanning was 37% of a full build on
  a mid-sized tree and is now under 15%. The set of indexed files is unchanged.
- `dir/**` exclude patterns whose directory half contains a glob now prune the
  directory during the walk. `bazel-*/**` matched `bazel-out/x.py` but not
  `bazel-out`, so the walk descended into the whole tree only to exclude every
  file in it one at a time. Indexed files are unchanged; the walk just stops
  earlier.

## 0.5.0 - 2026-08-17

### Added

- Swift support (`.swift`), with tuned reference classification. Swift folds
  class/struct/enum/actor/extension into one grammar node, marks call callees by
  position rather than by field, and reuses its call node for subscripts; each is
  handled so `callers`/`callees`, `impls`, and `refs --kind` behave the same way
  they do for the other tuned languages. Local `let`/`var` bindings inside a
  function body are deliberately not indexed, so a call's reported caller is the
  enclosing function rather than the local it was assigned to.
- Kotlin support (`.kt`, `.kts`), with tuned reference classification. Kotlin's
  grammar carries almost no field names, so callees and assignment targets are
  read positionally; `class_declaration` covers classes, interfaces, and enum
  classes alike; and annotations, receivers, and type parameters are skipped when
  resolving a declaration's name (`fun Point.scaled()` is `scaled`, not `Point`).
  Primary-constructor `val`/`var` parameters are indexed as properties.

### Changed

- **Breaking:** in JavaScript and TypeScript/TSX, a declarator holding a function
  (`const f = () => {}`, `const g = function () {}`) now has kind `function`
  rather than `variable`. It is a function in all but spelling, and the old kind
  kept such declarations from being treated as call-graph nodes. Saved queries
  that filter these with `--kind variable` need updating to `--kind function`.

### Fixed

- C, C++, and Go no longer index function-body locals as symbols. Their `var`,
  `const`, and `declaration` nodes match locals and file-scope declarations
  alike, so `int n = helper();` produced a `variable` symbol that sat inside the
  enclosing function's range. Caller attribution picks the innermost enclosing
  definition, so `callers helper` reported `n` instead of `caller`. File-scope
  and package-level declarations are still indexed.
- JavaScript and TypeScript/TSX no longer index function-body locals either.
  Declarators holding a function (`const inner = () => {}`) are now reported with
  kind `function` instead of `variable` and stay indexed at any scope, so local
  closures remain in the call graph while plain locals stop shadowing their
  enclosing function.
- Ruby and PHP now produce call edges at all. Neither grammar names its callee
  `function`, and PHP's call node types (`function_call_expression`,
  `member_call_expression`, `scoped_call_expression`,
  `object_creation_expression`) did not overlap the defaults, so `callers` and
  `callees` returned nothing for both languages. Ruby names the callee `method`;
  PHP's `new Widget()` names it nothing at all and is now read positionally.
  Inheritance, assignment, and import classification are tuned for both too.
- Java and C# no longer report an annotation as a declaration's whole signature.
  Annotations and attributes live inside the declaration node, so `@Deprecated`
  on its own line became the entire signature of the class below it. Because
  `impls` matches on signature text, `impls Greeter` silently returned nothing
  for annotated implementors.

## 0.4.0 - 2026-08-09

### Changed

- **Breaking:** report every line number as 1-based, with ranges inclusive on
  both ends. Output previously used 0-based numbering with an exclusive end,
  which made it the only source of line numbers in an agent's context that did
  not agree with `grep -n`, editors, tracebacks, and diffs. A symbol printed as
  `21:250` is now `22:250`: the same span, counted the way every other tool
  counts it. This covers text output, CLI `--json`, the Python API's
  `format="json"`, and the `line` part of an edit anchor (`line:hash`).
- **Breaking:** the Python API's `format="json"` now reports 1-based `column`
  too, so `range.start` reads `{"line": 5, "column": 9}` where it used to read
  `{"line": 4, "column": 8}`. A 1-based line beside a 0-based column is a trap,
  and the CLI's `--json` already reported columns 1-based; both JSON shapes now
  agree.
- `format="object"` is unchanged and still exposes 0-based `Position.line` and
  `Position.column`. It returns the library's internal dataclasses, where the
  line number is meant to index into `source.splitlines()` directly.
- Line numbers stay 0-based throughout the index internals (tree-sitter
  positions, SQLite rows, source slicing) and are converted only on the way out,
  so **no reindexing is required** — existing `.code-symbol-index` databases
  keep working.

### Fixed

- Report a single line-number base in JSON. `line` on symbols and references was
  1-based while `range`, `start_line`/`end_line`, and hash-line anchors in the
  same response were 0-based, contradicting each other and the documented
  contract.

### Migration

- Anything that pinned exact line numbers from text or JSON output — snapshot
  tests, cached symbol IDs (which embed a range), scripts adding `+1` to work
  around the old base — needs updating. Subtract the compensation rather than
  adding one.
- Agent sessions started before the upgrade may reuse an anchor captured under
  the old numbering. Anchors carry a content hash, so a stale one is either
  relocated to the correct line or rejected outright; it cannot silently apply
  to the wrong line. Re-read the file when one is rejected.

## 0.3.5 - 2026-07-08

### Changed

- Require the tree-sitter versions validated with the SIGSEGV fix
  (`tree-sitter>=0.26.0`, `tree-sitter-language-pack>=1.12.5`) without adding
  upper bounds, so downstream applications can still choose their own parser
  stack constraints.

## 0.3.4 - 2026-07-08

### Fixed

- Avoid tree-sitter point access when building source ranges and reference
  contexts. Some Python/tree-sitter builds can return unstable `Point` data for
  valid nodes, which could corrupt memory and crash with SIGSEGV during
  indexing. Ranges are now derived from source byte offsets instead.

## 0.3.3 - 2026-07-08

### Fixed

- Parse files across tree-sitter binding variants: some builds require the
  parse source as `bytes` (raising "source must be a bytestring or a callable,
  not str"), others require `str`. Try `str` first and fall back to encoded
  `bytes`, so indexing/search works on both without a hard tree-sitter pin.

## 0.3.2 - 2026-07-06

### Changed

- Rewrote the bundled agent skill (`SKILL.md`) to trigger on structural
  code-navigation questions (callers/callees, reference kinds, definitions,
  implementations) rather than a flat feature list, clarify when to prefer it
  over grep, and lead with the fast path instead of index setup.

## 0.3.1 - 2026-06-23

### Added

- Added explicit inspect source anchor formatting with
  `--anchor-format explicit` and Python `anchor_format="explicit"`, emitting
  `anchor=line:hash | code` while keeping the legacy `line:hash|code` format as
  the default.

## 0.3.0 - 2026-06-12

### Added

- `install-skill --target claude` installs the agent skill for Claude Code at
  `~/.claude/skills/code-symbol-index/` (honoring `$CLAUDE_CONFIG_DIR`, override
  with `--claude-dir`). The same `SKILL.md` serves Codex and Claude.

- `callers` and `callees` commands (and Python `callers()`/`callees()`) walk the
  transitive call graph up to `--depth` (default 3). `callers` groups reachable
  entry points by type (http_route / worker / tool / script / test) with a call
  path back to the target. Syntactic/name-based (`confidence: low`).
- `callees` resolution is locality-aware (same file/package preferred) and
  callable-kind filtered; ambiguous cross-module matches on generic names are
  dropped by default. `--loose` (`loose=True`) includes them.

- Classified references by behavior: each `refs` result now carries a `kind`
  (`call`, `read`, `write`, `inherit`, `type`, `import`, `attribute`, or
  `usage`). Tuned rules for Python/JavaScript/TypeScript, best-effort elsewhere.
- `refs` and `inspect` hide the noisy `import`/`attribute` kinds by default;
  `--ref-kind <kinds>` filters to an explicit subset and `--all-kinds` shows
  everything. The Python API exposes the same via `ref_kinds=`.
- `inspect` summary now reports a `reference_kinds` count breakdown.

### Changed

- Bumped index schema to version 5 (references are re-extracted with kinds).

## 0.1.13 - 2026-05-23

### Changed

- Updated installed Codex skill guidance to sync changed files after each round of edits.

## 0.1.12 - 2026-05-23

### Changed

- Relaxed installed Codex skill guidance for incremental index sync of known changed paths.
- Clarified that full index refresh still requires approval when changed paths are unknown.
- Updated installed Codex skill guidance to use the CLI for incremental index sync.

### Added

- Added `code-symbol-index update <paths...>` for incremental index sync from the CLI.

## 0.1.11 - 2026-05-23

### Changed

- Clarified installed Codex skill guidance for `files changed after last index update`.
- Recommended incremental index updates when changed paths are known.

## 0.1.10 - 2026-05-23

### Fixed

- Optimized outline text formatting to parse each file once instead of once per symbol.

## 0.1.9 - 2026-05-23

### Changed

- Updated the installed Codex skill guidance to ask before initializing or refreshing indexes.
- Clarified that ordinary index status checks should stay read-only and should not sync automatically.

## 0.1.8 - 2026-05-22

### Added

- Added optional hashline source anchors to inspect text and JSON output.

## 0.1.7 - 2026-05-21

### Added

- Added symbol search filters for kind, path, and exact-only matching in CLI and API.
- Added bounded pending file lists to checked index status.
- Added local file outlines with `outline --symbol` and `symbol=` in the API.
- Added import summaries to inspect output.
- Added Python top-level constants, variables, and dictionary keys to the symbol index.

### Changed

- Bumped the index schema so existing indexes are refreshed for the new Python symbols.

## 0.1.6 - 2026-05-21

### Added

- Added `refresh_async()` for startup-time background index refresh.

### Fixed

- Changed tree-sitter parser caching to thread-local storage to avoid cross-thread parser reuse.

## 0.1.5 - 2026-05-21

### Added

- Added `code-symbol-index install-skill` to install a Codex skill.

## 0.1.4 - 2026-05-20

### Added

- Added stable progress callbacks for `Repository.refresh()` and `Repository.update()`.

## 0.1.3 - 2026-05-20

### Added

- Added `code-symbol-index version` as a subcommand alias for `--version`.

## 0.1.2 - 2026-05-20

### Changed

- Search text and JSON output now report `limit` and `has_more` when results are truncated.

## 0.1.1 - 2026-05-20

### Added

- Added `LICENSE` with the MIT license.
- Added incremental index update APIs: `update(paths, root=...)` and `Repository.update(paths)`.
- Added `format="object" | "text" | "json"` to top-level query APIs.
- Added multi-query symbol search with `search(["A", "B"])` and `code-symbol-index search A B`.
- Added a README Python API list.

### Changed

- Compacted readable outline output to aligned `range | signature` lines.
- Preserved source indentation on outline signatures without repeating symbol names.
- Documented `uv tool install` usage.

### Fixed

- Avoided stale-index outline signature drift by using indexed signatures instead of current file lines.

## 0.1.0 - 2026-05-20

### Added

- Initial single-file Python module and CLI.
- Disk-backed SQLite symbol index with tree-sitter parsers.
- Symbol search, inspect, references, implementors, file outline, and index status.
