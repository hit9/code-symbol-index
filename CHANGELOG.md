# Changelog

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
