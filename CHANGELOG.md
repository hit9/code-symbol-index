# Changelog

## 0.4.0 - 2026-08-09

### Changed

- **Breaking:** report every line number as 1-based, with ranges inclusive on
  both ends. Output previously used 0-based numbering with an exclusive end,
  which made it the only source of line numbers in an agent's context that did
  not agree with `grep -n`, editors, tracebacks, and diffs. A symbol printed as
  `21:250` is now `22:250`: the same span, counted the way every other tool
  counts it. This covers text output, CLI `--json`, the Python API's
  `format="json"`, and the `line` part of an edit anchor (`line:hash`).
- `format="object"` is unchanged and still exposes 0-based `Position.line`. It
  returns the library's internal dataclasses, where the line number is meant to
  index into `source.splitlines()` directly.
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
