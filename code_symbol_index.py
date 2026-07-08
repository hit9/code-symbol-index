from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pathspec
from tree_sitter import Node
from tree_sitter_language_pack import get_parser


__version__ = "0.3.5"
SCHEMA_VERSION = 5
DEFAULT_INDEX_DIR = ".code-symbol-index"
DEFAULT_INDEX_DB = "index.sqlite"
TEXT_SAMPLE_BYTES = 8192
MAX_WORKERS = max((os.cpu_count() or 2) - 1, 1)
SQLITE_BATCH_SIZE = 1000
SQLITE_FILE_BATCH_SIZE = 100
FILE_SCAN_CHUNK_SIZE = 1024 * 1024
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 80
DEFAULT_PAGE_LIMIT = 20
DEFAULT_MAX_SOURCE_CHARS = 12000
DEFAULT_MAX_TOTAL_CHARS = 20000
DEFAULT_MAX_MEMBERS = 80
DEFAULT_MAX_CALLERS = 50
DEFAULT_MAX_CALLEES = 50
DEFAULT_MAX_REFERENCES = 50
DEFAULT_MAX_IMPLEMENTORS = 50
DEFAULT_MAX_IMPORTS = 40
DEFAULT_MAX_OUTLINE_SYMBOLS = 200
DEFAULT_MAX_PENDING_FILES = 50
HASHLINE_HASH_CHARS = 8
MAX_INSPECT_CANDIDATES = 20
SYMBOL_QUERY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
API_FORMATS = ("object", "text", "json")
ANCHOR_FORMATS = ("legacy", "explicit")
_DEFAULT_PROGRESS = object()
CODEX_SKILL_NAME = "code-symbol-index"

CODEX_SKILL = """---
name: code-symbol-index
description: Reach for this the moment a code-navigation question is structural rather than textual — "who calls this", "what does this call" (transitive callers/callees with entry-point grouping), "where is this used and how" (references classified as call/read/write/inherit/type), "where is this defined", "what's in this file", or "who implements this interface". It answers these precisely over an index, without the false positives and whole-file reads that grep forces. Prefer grep only for plain string/text search; use this whenever call graphs, reference kinds, or exact symbol resolution matter, especially in large repos. Commands: search, inspect, refs, callers, callees, impls, outline, status, index, update.
---

# Code Symbol Index

Use `code-symbol-index` for bounded, indexed code navigation over a local repository. Reach for it whenever a question is about *structure* — call graphs, references, definitions, implementations — not plain text. It resolves symbols precisely over an index, avoiding grep's false positives and whole-file reads.

## When to use this instead of grep

- "Who calls X / what does X call" -> `callers` / `callees` (transitive, grouped by entry point). Grep cannot follow call chains.
- "Where is X used, and how" -> `refs` (each hit classified `call`/`read`/`write`/`inherit`/`type`). Grep can't tell a call from an assignment.
- "Where is X defined / what's in this file / who implements Y" -> `inspect` / `outline` / `impls`, precisely, without reading whole files.
- Plain string search with no structural intent -> just use grep.

## The fast path

Assume the index is usually `ready`. Just run the query you need (`search`, `inspect`, `refs`, `callers`, `callees`, `impls`, `outline`) — most commands print a clear hint if the index is missing or stale, so you rarely need a separate `status` check first. Only fall into the setup path below when a command reports the index is missing.

## Setup & freshness (only when needed)

1. Check index state with a cheap read-only status:
   `code-symbol-index status --root <repo>`
2. If status is `missing`, ask the user before initializing the index:
   `code-symbol-index index --root <repo>`
3. If status is `ready`, use the indexed tools directly.
4. If freshness matters, check staleness without refreshing:
   `code-symbol-index status --root <repo> --check`
   If status is `stale` with `reason: files changed after last index update`, keep using the indexed tools after syncing known changes.
   If `pending_files` are listed or you edited files in this turn, run incremental update for those exact paths:
   `code-symbol-index update src/app.py --root <repo>`
   Incremental update is expected to be fast even in large repositories. Do not ask for approval for incremental updates of known changed paths.
   If changed paths are unknown, ask before refreshing the whole index:
   `code-symbol-index index --root <repo>`

## Query reference

5. Search symbols by exact name or prefix:
   `code-symbol-index search Tool Agent --root <repo> --limit 20`
   Use filters when needed:
   `code-symbol-index search Tool --root <repo> --kind class,function --path src --exact-only`
6. Inspect a symbol:
   `code-symbol-index inspect Tool --root <repo>`
   Use source anchors before edits:
   `code-symbol-index inspect Tool --root <repo> --anchors`
7. Outline a file:
   `code-symbol-index outline src/app.py --root <repo>`
   For a local class/function outline:
   `code-symbol-index outline src/app.py --root <repo> --symbol Tool`
8. Find references or implementation candidates:
   `code-symbol-index refs Tool --root <repo>`
   `code-symbol-index impls Greeter --root <repo>`
   Each reference is classified by behavior (`kind`): `call`, `read`, `write`,
   `inherit`, `type`, `import`, `attribute`, or `usage`. By default `refs` and
   `inspect` hide the noisy `import` and `attribute` (same-named member access)
   kinds so you see the real behavioral dependency surface.
   Narrow to specific kinds, or show everything:
   `code-symbol-index refs Tool --root <repo> --ref-kind call,write`
   `code-symbol-index refs Tool --root <repo> --all-kinds`
   `inspect` reports a `reference_kinds` breakdown in its summary.
9. Trace transitive call chains to locate real execution paths:
   `code-symbol-index callers handle_job --root <repo> --depth 3`
   `code-symbol-index callees handle_job --root <repo> --depth 3`
   `callers` groups reachable entry points by type (http_route / worker /
   script / tool / test) with a call path back to the target. Disambiguate a
   common name with `--path`/`--kind`/`--exact-only`.

## Rules

- Queries are symbol names or prefixes, not natural language.
- Reference classification is syntactic (no type inference); treat `kind` as a
  strong hint, not a guarantee. Use `--all-kinds` if a reference seems missing.
- `callers`/`callees` are syntactic and name-based (`confidence: low`): indirect
  or dynamically dispatched calls may be missed, and same-named symbols can be
  conflated. Use them to narrow the search, then confirm with `inspect`.
- `callees` resolves each call to a callable, preferring the same file/package
  and dropping ambiguous cross-module matches on generic names (`get`, `add`,
  ...). Pass `--loose` to include those lower-precision matches.
- Use `outline` for file paths.
- Use `--json` only when structured data is needed; readable text is preferred for LLM context.
- Do not refresh the whole index automatically during ordinary status checks.
- After each round of edits, sync the index for the files you changed:
  `code-symbol-index update src/app.py src/lib.py --root <repo>`
  This is expected to be fast, including in large repositories, and keeps indexed tools usable after edits.
- Only ask before full-index refresh:
  `code-symbol-index index --root <repo>`
"""

DEFAULT_EXCLUDES = (
    ".git/**",
    ".code-symbol-index/**",
    ".hg/**",
    ".svn/**",
    ".direnv/**",
    ".eggs/**",
    ".nox/**",
    ".tox/**",
    ".venv/**",
    ".cache/**",
    ".gradle/**",
    ".mypy_cache/**",
    ".next/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".turbo/**",
    "__pycache__/**",
    "Pods/**",
    "DerivedData/**",
    "bazel-*/**",
    "coverage/**",
    "env/**",
    "generated/**",
    "node_modules/**",
    "target/**",
    "dist/**",
    "build/**",
    "out/**",
    "site-packages/**",
    "venv/**",
    "vendor/**",
)

IDENTIFIER_NODE_TYPES = (
    "identifier",
    "property_identifier",
    "field_identifier",
    "type_identifier",
    "constant",
    "constant_identifier",
    "scoped_identifier",
    "shorthand_property_identifier",
    "simple_identifier",
    "name",
    "variable_name",
)

# Identifier node types that denote member access (``obj.name``). Used to tell
# an attribute/property reference apart from a plain identifier read.
MEMBER_IDENTIFIER_NODE_TYPES = (
    "property_identifier",
    "field_identifier",
    "shorthand_property_identifier",
)

# Reference classification taxonomy. ``usage`` is the fallback when nothing more
# specific can be determined syntactically.
REFERENCE_KINDS = frozenset(
    {"call", "read", "write", "inherit", "type", "import", "attribute", "usage"}
)

# Kinds shown by default. We deny the two high-noise kinds rather than allow a
# fixed behavioral set, so misclassified or unknown (``usage``) references stay
# visible instead of being silently dropped.
DEFAULT_REFERENCE_NOISE_KINDS = frozenset({"import", "attribute"})
DEFAULT_REFERENCE_KINDS = frozenset(REFERENCE_KINDS - DEFAULT_REFERENCE_NOISE_KINDS)

# Sentinel for the friendly ``ref_kinds`` API argument: keep the behavioral
# default unless the caller asks for ``"all"`` or an explicit kind list.
_REF_KINDS_DEFAULT = "behavioral"

# Entry-point classification for call-chain queries. Detection is heuristic
# (path/name conventions + a decorator scan) and Python-first; treat it as a
# best-effort hint, not a guarantee.
ENTRY_TYPES = ("http_route", "worker", "tool", "script", "test")
DEFAULT_CALL_DEPTH = 3
MAX_CALL_DEPTH = 6
DEFAULT_CALL_FANOUT = 20
MAX_CALL_GRAPH_NODES = 200
# Symbol kinds a call edge can resolve to. Restricting callee resolution to
# these drops false matches against variables/constants/dict keys.
CALLEE_KINDS = ("class", "function", "method", "constructor", "struct")

CONTAINER_KINDS = {
    "class",
    "enum",
    "function",
    "impl",
    "interface",
    "method",
    "module",
    "namespace",
    "struct",
    "trait",
}

IMPLEMENTATION_KINDS = {
    "class",
    "impl",
    "interface",
    "method",
    "struct",
    "trait",
}


@dataclass(frozen=True, slots=True)
class Position:
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class Range:
    start: Position
    end: Position
    start_byte: int
    end_byte: int


@dataclass(frozen=True, slots=True)
class Symbol:
    id: str
    name: str
    kind: str
    language: str
    path: Path
    range: Range
    signature: str
    container: str | None = None


@dataclass(frozen=True, slots=True)
class Reference:
    symbol_id: str
    name: str
    language: str
    path: Path
    range: Range
    context: str
    reference_kind: str = "usage"


@dataclass(frozen=True, slots=True)
class ImportItem:
    path: Path
    range: Range
    statement: str


@dataclass(frozen=True, slots=True)
class HashLine:
    line: int
    hash: str
    text: str


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    path: Path
    start_line: int
    end_line: int
    start_anchor: str | None
    end_anchor: str | None
    lines: tuple[HashLine, ...]


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Any, ...]
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None = None

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def __getitem__(self, index):
        return self.items[index]


@dataclass(frozen=True, slots=True)
class CallNode:
    symbol: Symbol
    depth: int
    entry_type: str | None = None
    children: tuple["CallNode", ...] = ()


@dataclass(frozen=True, slots=True)
class EntryPoint:
    entry_type: str
    symbol: Symbol
    path: tuple[Symbol, ...]  # from the entry symbol down to the target


@dataclass(frozen=True, slots=True)
class CallGraph:
    target: Symbol
    direction: str  # "callers" | "callees"
    depth: int
    roots: tuple[CallNode, ...]
    entry_points: tuple[EntryPoint, ...] = ()
    truncated: bool = False
    confidence: str = "low"


@dataclass(frozen=True, slots=True)
class Inspection:
    definition: Symbol
    references: tuple[Reference, ...]
    implementations: tuple[Symbol, ...]
    imports: tuple[ImportItem, ...] = ()
    source_anchor: SourceAnchor | None = None
    doc: str | None = None
    source_preview: str | None = None
    confidence: str = "medium"
    references_has_more: bool = False
    references_next_offset: int | None = None
    implementations_has_more: bool = False
    implementations_next_offset: int | None = None


@dataclass(frozen=True, slots=True)
class InspectOptions:
    max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS
    max_members: int = DEFAULT_MAX_MEMBERS
    max_callers: int = DEFAULT_MAX_CALLERS
    max_callees: int = DEFAULT_MAX_CALLEES
    max_references: int = DEFAULT_MAX_REFERENCES
    max_implementors: int = DEFAULT_MAX_IMPLEMENTORS
    max_imports: int = DEFAULT_MAX_IMPORTS
    ref_kinds: str | tuple[str, ...] | None = _REF_KINDS_DEFAULT
    anchor_format: str = "legacy"


@dataclass(frozen=True, slots=True)
class IndexStatus:
    status: str
    root: Path
    files: int | None = None
    symbols: int | None = None
    languages: tuple[str, ...] = ()
    language_breakdown: tuple[dict[str, Any], ...] = ()
    updated_at: str | None = None
    pending_changes: int | str | None = None
    pending_files: tuple[str, ...] = ()
    reason: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class _IndexedFile:
    path: Path
    language: str
    mtime_ns: int
    size: int
    symbols: tuple[Symbol, ...]
    references: tuple[Reference, ...]


class CodeSymbolIndexError(Exception):
    """Base error for code-symbol-index."""


class UnsupportedLanguageError(CodeSymbolIndexError):
    """Raised when no tree-sitter parser is available for a language."""


class SymbolNotFoundError(CodeSymbolIndexError):
    """Raised when a symbol id is not present in the index."""


class IndexNotFoundError(CodeSymbolIndexError):
    """Raised when a disk index is required but missing."""


class BinaryFileError(CodeSymbolIndexError):
    """Raised when a file does not look like text."""


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]
    definitions: dict[str, str]
    identifier_node_types: tuple[str, ...] = IDENTIFIER_NODE_TYPES
    # Node-type hints used to classify references. Untuned languages keep the
    # shared defaults below and degrade gracefully to ``read``/``usage``.
    call_node_types: tuple[str, ...] = ("call", "call_expression")
    import_node_types: tuple[str, ...] = (
        "import_statement",
        "import_from_statement",
        "import_declaration",
        "import_spec",
        "use_declaration",
    )
    inherit_node_types: tuple[str, ...] = ()
    type_node_types: tuple[str, ...] = ("type_identifier",)
    assignment_node_types: tuple[str, ...] = ()
    member_node_types: tuple[str, ...] = ()


LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python",
        extensions=(".py", ".pyi"),
        definitions={
            "class_definition": "class",
            "function_definition": "function",
        },
        call_node_types=("call",),
        import_node_types=("import_statement", "import_from_statement", "future_import_statement"),
        inherit_node_types=(),  # class bases handled specially in _child_reference_context
        type_node_types=("type",),
        assignment_node_types=("assignment", "augmented_assignment"),
        member_node_types=("attribute",),
    ),
    LanguageSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        definitions={
            "class_declaration": "class",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "method_definition": "method",
            "variable_declarator": "variable",
        },
        call_node_types=("call_expression", "new_expression"),
        import_node_types=("import_statement", "import_clause", "import_specifier", "namespace_import"),
        inherit_node_types=("class_heritage", "extends_clause"),
        type_node_types=("type_identifier",),
        assignment_node_types=("assignment_expression", "augmented_assignment_expression", "variable_declarator"),
        member_node_types=("member_expression",),
    ),
    LanguageSpec(
        name="typescript",
        extensions=(".ts", ".mts", ".cts"),
        definitions={
            "class_declaration": "class",
            "enum_declaration": "enum",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "interface_declaration": "interface",
            "internal_module": "module",
            "method_definition": "method",
            "type_alias_declaration": "type",
            "variable_declarator": "variable",
        },
        call_node_types=("call_expression", "new_expression"),
        import_node_types=("import_statement", "import_clause", "import_specifier", "namespace_import"),
        inherit_node_types=("class_heritage", "extends_clause", "implements_clause", "extends_type_clause"),
        type_node_types=("type_annotation", "type_arguments", "type_identifier", "predefined_type"),
        assignment_node_types=("assignment_expression", "augmented_assignment_expression", "variable_declarator"),
        member_node_types=("member_expression",),
    ),
    LanguageSpec(
        name="tsx",
        extensions=(".tsx",),
        definitions={
            "class_declaration": "class",
            "enum_declaration": "enum",
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "interface_declaration": "interface",
            "internal_module": "module",
            "method_definition": "method",
            "type_alias_declaration": "type",
            "variable_declarator": "variable",
        },
        call_node_types=("call_expression", "new_expression"),
        import_node_types=("import_statement", "import_clause", "import_specifier", "namespace_import"),
        inherit_node_types=("class_heritage", "extends_clause", "implements_clause", "extends_type_clause"),
        type_node_types=("type_annotation", "type_arguments", "type_identifier", "predefined_type"),
        assignment_node_types=("assignment_expression", "augmented_assignment_expression", "variable_declarator"),
        member_node_types=("member_expression",),
    ),
    LanguageSpec(
        name="go",
        extensions=(".go",),
        definitions={
            "const_spec": "constant",
            "function_declaration": "function",
            "method_declaration": "method",
            "type_spec": "type",
            "var_spec": "variable",
        },
    ),
    LanguageSpec(
        name="rust",
        extensions=(".rs",),
        definitions={
            "const_item": "constant",
            "enum_item": "enum",
            "function_item": "function",
            "impl_item": "impl",
            "mod_item": "module",
            "static_item": "variable",
            "struct_item": "struct",
            "trait_item": "trait",
            "type_item": "type",
        },
    ),
    LanguageSpec(
        name="java",
        extensions=(".java",),
        definitions={
            "class_declaration": "class",
            "constructor_declaration": "method",
            "enum_declaration": "enum",
            "field_declaration": "field",
            "interface_declaration": "interface",
            "method_declaration": "method",
            "record_declaration": "class",
        },
    ),
    LanguageSpec(
        name="c",
        extensions=(".c", ".h"),
        definitions={
            "declaration": "variable",
            "enum_specifier": "enum",
            "function_definition": "function",
            "struct_specifier": "struct",
            "type_definition": "type",
        },
    ),
    LanguageSpec(
        name="cpp",
        extensions=(".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
        definitions={
            "class_specifier": "class",
            "declaration": "variable",
            "enum_specifier": "enum",
            "function_definition": "function",
            "namespace_definition": "namespace",
            "struct_specifier": "struct",
            "type_definition": "type",
        },
    ),
    LanguageSpec(
        name="csharp",
        extensions=(".cs",),
        definitions={
            "class_declaration": "class",
            "constructor_declaration": "method",
            "enum_declaration": "enum",
            "field_declaration": "field",
            "interface_declaration": "interface",
            "method_declaration": "method",
            "property_declaration": "property",
            "struct_declaration": "struct",
        },
    ),
    LanguageSpec(
        name="ruby",
        extensions=(".rb",),
        definitions={
            "class": "class",
            "method": "method",
            "module": "module",
            "singleton_method": "method",
        },
    ),
    LanguageSpec(
        name="php",
        extensions=(".php",),
        definitions={
            "class_declaration": "class",
            "function_definition": "function",
            "interface_declaration": "interface",
            "method_declaration": "method",
            "trait_declaration": "trait",
        },
    ),
)

LANGUAGE_BY_NAME = {language.name: language for language in LANGUAGES}
LANGUAGE_BY_EXTENSION = {
    extension: language
    for language in LANGUAGES
    for extension in language.extensions
}


class CodeIndex:
    def __init__(
        self,
        root: str | Path = ".",
        *,
        languages: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        db_path: str | Path | None = None,
        create_storage: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.languages = _normalize_languages(languages)
        self.include = tuple(include or ())
        self.exclude = tuple(DEFAULT_EXCLUDES) + tuple(exclude or ())
        self.storage = _Storage(db_path, create=create_storage)
        self._gitignore_specs: tuple[tuple[Path, pathspec.PathSpec], ...] | None = None

    def build(self) -> CodeIndex:
        self.storage.clear()
        self._index_files(self._iter_indexable_files())
        return self

    def update(self, paths: str | Path | Iterable[str | Path] | None = None) -> CodeIndex:
        if paths is None:
            return self.build()
        relative_paths = [self._relative_path(path) for path in _coerce_paths(paths)]
        self.storage.remove_files(relative_paths)
        self._index_files(path for path in relative_paths if self._should_index(path))
        return self

    def search_symbols(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        return self.storage.search_symbols(
            query,
            kind=kind,
            language=language,
            path=path,
            exact_only=exact_only,
            limit=limit,
        )

    def search(
        self,
        query: str | Iterable[str],
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        return list(self.search_page(query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit).items)

    def search_page(
        self,
        query: str | Iterable[str],
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> Page:
        return _search_page(self, query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit)

    def best_symbol(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
    ) -> Symbol:
        return self._resolve_symbol(query, kind=kind, language=language, path=path, exact_only=exact_only)

    def inspect(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        anchors: bool = False,
        max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
        ref_kinds: str | Iterable[str] | None = _REF_KINDS_DEFAULT,
    ) -> Inspection:
        return self._inspect(
            _resolve_inspect_symbol(self, query, kind=kind, language=language, path=path, exact_only=exact_only),
            limit=limit,
            anchors=anchors,
            max_source_chars=max_source_chars,
            ref_kinds=_resolve_ref_kinds(ref_kinds),
        )

    def refs(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ref_kinds: str | Iterable[str] | None = _REF_KINDS_DEFAULT,
    ) -> Page:
        return self.find_references(
            query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit, offset=offset, ref_kinds=ref_kinds
        )

    def impls(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        return self.find_implementations(query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit, offset=offset)

    def callers(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        depth: int = DEFAULT_CALL_DEPTH,
        limit: int = DEFAULT_CALL_FANOUT,
    ) -> CallGraph:
        symbol = _resolve_inspect_symbol(self, query, kind=kind, language=language, path=path, exact_only=exact_only)
        return _build_call_graph(self, symbol, direction="callers", depth=_clamp_depth(depth), limit=limit)

    def callees(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        depth: int = DEFAULT_CALL_DEPTH,
        limit: int = DEFAULT_CALL_FANOUT,
        loose: bool = False,
    ) -> CallGraph:
        symbol = _resolve_inspect_symbol(self, query, kind=kind, language=language, path=path, exact_only=exact_only)
        return _build_call_graph(self, symbol, direction="callees", depth=_clamp_depth(depth), limit=limit, loose=loose)

    def inspect_symbol(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        anchors: bool = False,
        max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
    ) -> Inspection:
        return self._inspect(
            _resolve_inspect_symbol(self, query, kind=kind, language=language, path=path, exact_only=exact_only),
            limit=limit,
            anchors=anchors,
            max_source_chars=max_source_chars,
        )

    def inspect_text(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        options: InspectOptions | None = None,
        anchors: bool = False,
    ) -> str:
        return _inspect_text(
            self,
            query,
            kind=kind,
            language=language,
            path=path,
            exact_only=exact_only,
            options=options or InspectOptions(),
            anchors=anchors,
        )

    def search_text(
        self,
        query: str | Iterable[str],
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> str:
        queries = _coerce_queries(query)
        return _format_search_text(
            self,
            queries,
            self.search_page(queries, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit),
        )

    def outline(
        self,
        path: str | Path,
        *,
        symbol: str | None = None,
        max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    ) -> Page:
        relative_path = self._relative_path(Path(path))
        symbols = self.storage.symbols_in_file(relative_path)
        if symbol is not None:
            symbols = _local_outline_symbols(self, symbols, symbol)
        return _page_from_extra(symbols[: max_symbols + 1], limit=max_symbols, offset=0)

    def outline_text(
        self,
        path: str | Path,
        *,
        symbol: str | None = None,
        max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    ) -> str:
        relative_path = self._relative_path(Path(path))
        return _format_outline_text(self, relative_path, self.outline(relative_path, symbol=symbol, max_symbols=max_symbols), symbol=symbol)

    def find_references(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ref_kinds: str | Iterable[str] | None = _REF_KINDS_DEFAULT,
    ) -> Page:
        symbol = self._resolve_symbol(query, kind=kind, language=language, path=path, exact_only=exact_only)
        return self.storage.references_for(symbol, limit=limit, offset=offset, ref_kinds=_resolve_ref_kinds(ref_kinds))

    def find_implementations(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        symbol = self._resolve_symbol(query, kind=kind, language=language, path=path, exact_only=exact_only)
        return self.storage.implementation_candidates(symbol, limit=limit, offset=offset)

    def _inspect(
        self,
        symbol: Symbol,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        anchors: bool = False,
        max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
        ref_kinds: frozenset[str] | None = DEFAULT_REFERENCE_KINDS,
    ) -> Inspection:
        references = self.storage.references_for(symbol, limit=limit, offset=0, ref_kinds=ref_kinds)
        implementations = self.storage.implementation_candidates(symbol, limit=limit, offset=0)
        source = self.storage.file_source(self.root, symbol.path)
        source_range = _definition_range(self, symbol) or symbol.range
        preview = _source_preview(source, symbol.range) if source is not None else None
        return Inspection(
            definition=symbol,
            references=references.items,
            implementations=implementations.items,
            imports=_imports_for_file(self, symbol.path, limit=DEFAULT_MAX_IMPORTS),
            source_anchor=_source_anchor(symbol.path, source, source_range, max_source_chars) if anchors and source is not None else None,
            source_preview=preview,
            confidence="medium" if implementations else "low",
            references_has_more=references.has_more,
            references_next_offset=references.next_offset,
            implementations_has_more=implementations.has_more,
            implementations_next_offset=implementations.next_offset,
        )

    def _resolve_symbol(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
    ) -> Symbol:
        symbol = self.storage.get_symbol(query)
        if symbol is not None:
            return symbol

        matches = self.search_symbols(query, kind=kind, language=language, path=path, exact_only=exact_only, limit=1)
        if matches:
            return matches[0]
        raise SymbolNotFoundError(f"No symbol matched: {query}")

    def _index_files(self, paths: Iterable[Path]) -> None:
        for relative_path in paths:
            self._index_file(relative_path)

    def _index_file(self, relative_path: Path) -> None:
        indexed = _parse_file(self.root, relative_path, self.languages)
        if indexed is None:
            return
        self.storage.insert_file_result(indexed)


    def _iter_indexable_files(self) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            current_dir = Path(dirpath)
            relative_dir = current_dir.relative_to(self.root)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not self._should_skip_dir(relative_dir / dirname)
            ]
            for filename in filenames:
                relative_path = relative_dir / filename
                if self._should_index(relative_path):
                    yield relative_path

    def _should_index(self, relative_path: Path) -> bool:
        path_text = relative_path.as_posix()
        if self.include and not any(fnmatch.fnmatch(path_text, pattern) for pattern in self.include):
            return False
        if self._is_excluded(relative_path):
            return False
        if self._is_gitignored(relative_path):
            return False
        return _spec_for_path(relative_path, self.languages) is not None

    def _should_skip_dir(self, relative_path: Path) -> bool:
        if relative_path == Path("."):
            return False
        path_text = relative_path.as_posix()
        return self._is_excluded(relative_path) or self._is_gitignored(relative_path, is_dir=True)

    def _is_excluded(self, relative_path: Path) -> bool:
        path_text = relative_path.as_posix()
        return any(_matches_path_pattern(path_text, pattern) for pattern in self.exclude)

    def _relative_path(self, path: Path) -> Path:
        full_path = path if path.is_absolute() else self.root / path
        return full_path.resolve().relative_to(self.root)

    def _is_gitignored(self, relative_path: Path, *, is_dir: bool = False) -> bool:
        path_text = relative_path.as_posix()
        if is_dir:
            path_text = f"{path_text}/"
        for base, spec in self._gitignore_specs_for_root():
            try:
                scoped_path = relative_path.relative_to(base).as_posix()
            except ValueError:
                continue
            if is_dir:
                scoped_path = f"{scoped_path}/"
            if spec.match_file(scoped_path):
                return True
        return False

    def _gitignore_specs_for_root(self) -> tuple[tuple[Path, pathspec.PathSpec], ...]:
        if self._gitignore_specs is not None:
            return self._gitignore_specs

        specs: list[tuple[Path, pathspec.PathSpec]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            current_dir = Path(dirpath)
            relative_dir = current_dir.relative_to(self.root)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not self._is_excluded(relative_dir / dirname)
            ]
            if ".gitignore" not in filenames:
                continue
            try:
                lines = (current_dir / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            specs.append((relative_dir, pathspec.PathSpec.from_lines("gitignore", lines)))

        self._gitignore_specs = tuple(specs)
        return self._gitignore_specs


class Repository(CodeIndex):
    def __init__(
        self,
        root: str | Path = ".",
        *,
        languages: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        db_path: str | Path | None = None,
        progress: Any | None = None,
        create_index: bool = False,
    ) -> None:
        resolved_root = Path(root).resolve()
        if db_path is None:
            db_path = resolved_root / DEFAULT_INDEX_DIR / DEFAULT_INDEX_DB
        super().__init__(
            resolved_root,
            languages=languages,
            include=include,
            exclude=exclude,
            db_path=db_path,
            create_storage=create_index,
        )
        self.progress = progress

    def refresh(self, *, progress: Any = _DEFAULT_PROGRESS) -> Repository:
        progress_callback = self.progress if progress is _DEFAULT_PROGRESS else progress
        if self.storage.schema_version() != SCHEMA_VERSION:
            self.storage.reset_schema()

        _emit_progress(progress_callback, "scan", done=0, total=0)
        current_files: dict[str, tuple[Path, int, int]] = {}
        paths = list(self._iter_indexable_files())
        for path in paths:
            try:
                stat = (self.root / path).stat()
            except OSError:
                continue
            current_files[path.as_posix()] = (path, stat.st_mtime_ns, stat.st_size)

        indexed_files = self.storage.files()
        deleted = [Path(path) for path in indexed_files if path not in current_files]

        to_index: list[Path] = []
        for path_text, (path, mtime_ns, size) in current_files.items():
            old = indexed_files.get(path_text)
            if old is not None and old["mtime_ns"] == mtime_ns and old["size"] == size:
                continue
            to_index.append(path)

        total = len(to_index)
        _emit_progress(progress_callback, "start", done=0, total=total)
        indexed_results = self._parse_files(to_index, include_references=False, progress=progress_callback)
        self.storage.replace_files(
            deleted_paths=deleted,
            indexed_files=indexed_results,
            schema_version=SCHEMA_VERSION,
        )
        _emit_progress(progress_callback, "finish", done=total, total=total)
        return self

    def build(self, *, progress: Any = _DEFAULT_PROGRESS) -> Repository:
        progress_callback = self.progress if progress is _DEFAULT_PROGRESS else progress
        self.storage.clear()
        paths = list(self._iter_indexable_files())
        total = len(paths)
        _emit_progress(progress_callback, "start", done=0, total=total)
        indexed_results = self._parse_files(paths, include_references=False, progress=progress_callback)
        self.storage.replace_files(
            deleted_paths=(),
            indexed_files=indexed_results,
            schema_version=SCHEMA_VERSION,
        )
        _emit_progress(progress_callback, "finish", done=total, total=total)
        return self

    def update(
        self,
        paths: str | Path | Iterable[str | Path] | None = None,
        *,
        progress: Any = _DEFAULT_PROGRESS,
    ) -> Repository:
        progress_callback = self.progress if progress is _DEFAULT_PROGRESS else progress
        if paths is None:
            return self.refresh(progress=progress_callback)
        if self.storage.schema_version() != SCHEMA_VERSION:
            return self.refresh(progress=progress_callback)

        relative_paths = list(dict.fromkeys(self._relative_path(path) for path in _coerce_paths(paths)))
        to_index = [
            path
            for path in relative_paths
            if (self.root / path).is_file() and self._should_index(path)
        ]
        total = len(to_index)
        _emit_progress(progress_callback, "start", done=0, total=total)
        indexed_results = self._parse_files(to_index, include_references=False, progress=progress_callback)
        self.storage.replace_files(
            deleted_paths=relative_paths,
            indexed_files=indexed_results,
            schema_version=SCHEMA_VERSION,
        )
        _emit_progress(progress_callback, "finish", done=total, total=total)
        return self

    def _parse_files(
        self,
        paths: list[Path],
        *,
        include_references: bool = True,
        progress: Any | None = None,
    ) -> list[_IndexedFile]:
        if not paths:
            return []
        if len(paths) == 1 or MAX_WORKERS <= 1:
            results = []
            for done, path in enumerate(paths, start=1):
                result = _parse_file(self.root, path, self.languages, include_references=include_references)
                if result is not None:
                    results.append(result)
                _emit_progress(progress, "file", done=done, total=len(paths), path=path.as_posix())
            return results

        results: list[_IndexedFile] = []
        workers = min(MAX_WORKERS, len(paths))
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=workers)
        try:
            future_to_path = {
                executor.submit(_parse_file, self.root, path, self.languages, include_references): path
                for path in paths
            }
            for done, future in enumerate(concurrent.futures.as_completed(future_to_path), start=1):
                try:
                    result = future.result()
                except Exception:
                    result = None
                if result is not None:
                    results.append(result)
                path = future_to_path[future]
                _emit_progress(progress, "file", done=done, total=len(paths), path=path.as_posix())
        except KeyboardInterrupt:
            for future in future_to_path:
                future.cancel()
            _terminate_executor(executor)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=False)
        return results

    def search_symbols(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        return super().search_symbols(query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit)

    def find_references(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ref_kinds: str | Iterable[str] | None = _REF_KINDS_DEFAULT,
    ) -> Page:
        symbol = self._resolve_symbol(query, kind=kind, language=language, path=path, exact_only=exact_only)
        return self._references_for_symbol(symbol, limit=limit, offset=offset, ref_kinds=_resolve_ref_kinds(ref_kinds))

    def _inspect(
        self,
        symbol: Symbol,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        anchors: bool = False,
        max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
        ref_kinds: frozenset[str] | None = DEFAULT_REFERENCE_KINDS,
    ) -> Inspection:
        references = self._references_for_symbol(symbol, limit=limit, offset=0, ref_kinds=ref_kinds)
        implementations = self.storage.implementation_candidates(symbol, limit=limit, offset=0)
        source = self.storage.file_source(self.root, symbol.path)
        source_range = _definition_range(self, symbol) or symbol.range
        preview = _source_preview(source, symbol.range) if source is not None else None
        return Inspection(
            definition=symbol,
            references=references.items,
            implementations=implementations.items,
            imports=_imports_for_file(self, symbol.path, limit=DEFAULT_MAX_IMPORTS),
            source_anchor=_source_anchor(symbol.path, source, source_range, max_source_chars) if anchors and source is not None else None,
            source_preview=preview,
            confidence="medium" if implementations else "low",
            references_has_more=references.has_more,
            references_next_offset=references.next_offset,
            implementations_has_more=implementations.has_more,
            implementations_next_offset=implementations.next_offset,
        )

    def _references_for_symbol(
        self,
        symbol: Symbol,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ref_kinds: frozenset[str] | None = None,
    ) -> Page:
        _validate_pagination(limit=limit, offset=offset)
        paths = self.storage.file_paths(language=symbol.language)
        needle = symbol.name.encode("utf-8")
        references: list[Reference] = []
        skipped = 0
        for path in paths:
            if not _file_contains_bytes(self.root / path, needle):
                continue
            indexed_file = _parse_file(self.root, path, self.languages)
            if indexed_file is None:
                continue
            for reference in indexed_file.references:
                if reference.name != symbol.name:
                    continue
                if ref_kinds is not None and reference.reference_kind not in ref_kinds:
                    continue
                if (
                    reference.path == symbol.path
                    and reference.range.start_byte == symbol.range.start_byte
                    and reference.range.end_byte == symbol.range.end_byte
                ):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                references.append(
                    Reference(
                        symbol_id=symbol.id,
                        name=reference.name,
                        language=reference.language,
                        path=reference.path,
                        range=reference.range,
                        context=reference.context,
                        reference_kind=reference.reference_kind,
                    )
                )
                if len(references) > limit:
                    return _page_from_extra(references, limit=limit, offset=offset)
        return _page_from_extra(references, limit=limit, offset=offset)

    def clean(self) -> None:
        if self.storage.db_path != ":memory:":
            self.storage.connection.close()
        shutil.rmtree(self.root / DEFAULT_INDEX_DIR, ignore_errors=True)


def search(
    query: str | Iterable[str],
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    limit: int = DEFAULT_SEARCH_LIMIT,
    sync: bool = False,
    format: str = "object",
) -> Any:
    output_format = _validate_api_format(format)
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    queries = _coerce_queries(query)
    page = repo.search_page(queries, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit)
    if output_format == "object":
        return list(page.items)
    if output_format == "text":
        return _format_search_text(repo, queries, page)
    return _search_jsonable(page)


def search_text(
    query: str | Iterable[str],
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    limit: int = DEFAULT_SEARCH_LIMIT,
    sync: bool = False,
) -> str:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.search_text(query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit)


def outline(
    path: str | Path,
    *,
    root: str | Path = ".",
    symbol: str | None = None,
    max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    sync: bool = False,
    format: str = "object",
) -> Any:
    output_format = _validate_api_format(format)
    repo = Repository(root)
    if sync:
        repo.refresh()
    page = repo.outline(path, symbol=symbol, max_symbols=max_symbols)
    if output_format == "object":
        return page
    if output_format == "text":
        relative_path = repo._relative_path(Path(path))
        return _format_outline_text(repo, relative_path, page, symbol=symbol)
    return _to_jsonable(page)


def outline_text(
    path: str | Path,
    *,
    root: str | Path = ".",
    symbol: str | None = None,
    max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    sync: bool = False,
) -> str:
    repo = Repository(root)
    if sync:
        repo.refresh()
    return repo.outline_text(path, symbol=symbol, max_symbols=max_symbols)


def best_symbol(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    sync: bool = False,
) -> Symbol:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.best_symbol(query, kind=kind, language=language, path=path, exact_only=exact_only)


def inspect(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    limit: int = DEFAULT_PAGE_LIMIT,
    sync: bool = False,
    format: str = "object",
    max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_callers: int = DEFAULT_MAX_CALLERS,
    max_callees: int = DEFAULT_MAX_CALLEES,
    max_references: int = DEFAULT_MAX_REFERENCES,
    max_implementors: int = DEFAULT_MAX_IMPLEMENTORS,
    max_imports: int = DEFAULT_MAX_IMPORTS,
    anchors: bool = False,
    anchor_format: str = "legacy",
    ref_kinds: str | Iterable[str] | None = _REF_KINDS_DEFAULT,
) -> Any:
    output_format = _validate_api_format(format)
    anchor_format = _validate_anchor_format(anchor_format)
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    if output_format == "text":
        return repo.inspect_text(
            query,
            kind=kind,
            language=language,
            path=path,
            exact_only=exact_only,
            options=InspectOptions(
                max_source_chars=max_source_chars,
                max_total_chars=max_total_chars,
                max_members=max_members,
                max_callers=max_callers,
                max_callees=max_callees,
                max_references=max_references,
                max_implementors=max_implementors,
                max_imports=max_imports,
                ref_kinds=_ref_kinds_option(ref_kinds),
                anchor_format=anchor_format,
            ),
            anchors=anchors,
        )
    inspection = repo.inspect(
        query,
        kind=kind,
        language=language,
        path=path,
        exact_only=exact_only,
        limit=limit,
        anchors=anchors,
        max_source_chars=max_source_chars,
        ref_kinds=ref_kinds,
    )
    if output_format == "object":
        return inspection
    return _to_jsonable(inspection)


def inspect_text(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_callers: int = DEFAULT_MAX_CALLERS,
    max_callees: int = DEFAULT_MAX_CALLEES,
    max_references: int = DEFAULT_MAX_REFERENCES,
    max_implementors: int = DEFAULT_MAX_IMPLEMENTORS,
    max_imports: int = DEFAULT_MAX_IMPORTS,
    anchors: bool = False,
    anchor_format: str = "legacy",
    sync: bool = False,
    ref_kinds: str | Iterable[str] | None = _REF_KINDS_DEFAULT,
) -> str:
    anchor_format = _validate_anchor_format(anchor_format)
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.inspect_text(
        query,
        kind=kind,
        language=language,
        path=path,
        exact_only=exact_only,
        options=InspectOptions(
            max_source_chars=max_source_chars,
            max_total_chars=max_total_chars,
            max_members=max_members,
            max_callers=max_callers,
            max_callees=max_callees,
            max_references=max_references,
            max_implementors=max_implementors,
            max_imports=max_imports,
            ref_kinds=_ref_kinds_option(ref_kinds),
            anchor_format=anchor_format,
        ),
        anchors=anchors,
    )


def refs(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sync: bool = False,
    format: str = "object",
    ref_kinds: str | Iterable[str] | None = _REF_KINDS_DEFAULT,
) -> Any:
    output_format = _validate_api_format(format)
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    page = repo.refs(
        query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit, offset=offset, ref_kinds=ref_kinds
    )
    if output_format == "object":
        return page
    if output_format == "text":
        return _format_page_text(repo, "references", page)
    return _to_jsonable(page)


def impls(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sync: bool = False,
    format: str = "object",
) -> Any:
    output_format = _validate_api_format(format)
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    page = repo.impls(query, kind=kind, language=language, path=path, exact_only=exact_only, limit=limit, offset=offset)
    if output_format == "object":
        return page
    if output_format == "text":
        return _format_page_text(repo, "implementors", page)
    return _to_jsonable(page)


def callers(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    depth: int = DEFAULT_CALL_DEPTH,
    limit: int = DEFAULT_CALL_FANOUT,
    sync: bool = False,
    format: str = "object",
) -> Any:
    return _call_chain(
        "callers", query, root=root, kind=kind, language=language, path=path,
        exact_only=exact_only, depth=depth, limit=limit, sync=sync, format=format,
    )


def callees(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | Iterable[str] | None = None,
    language: str | None = None,
    path: str | Path | Iterable[str | Path] | None = None,
    exact_only: bool = False,
    depth: int = DEFAULT_CALL_DEPTH,
    limit: int = DEFAULT_CALL_FANOUT,
    sync: bool = False,
    format: str = "object",
    loose: bool = False,
) -> Any:
    return _call_chain(
        "callees", query, root=root, kind=kind, language=language, path=path,
        exact_only=exact_only, depth=depth, limit=limit, sync=sync, format=format, loose=loose,
    )


def _call_chain(
    direction: str,
    query: str,
    *,
    root: str | Path,
    kind: str | Iterable[str] | None,
    language: str | None,
    path: str | Path | Iterable[str | Path] | None,
    exact_only: bool,
    depth: int,
    limit: int,
    sync: bool,
    format: str,
    loose: bool = False,
) -> Any:
    output_format = _validate_api_format(format)
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    extra = {"loose": loose} if direction == "callees" else {}
    method = repo.callers if direction == "callers" else repo.callees
    graph = method(query, kind=kind, language=language, path=path, exact_only=exact_only, depth=depth, limit=limit, **extra)
    if output_format == "object":
        return graph
    if output_format == "text":
        return _format_call_graph_text(repo, graph)
    return _to_jsonable(graph)


def status(
    root: str | Path = ".",
    *,
    language: str | None = None,
    db_path: str | Path | None = None,
    check: bool = False,
    max_pending_files: int = DEFAULT_MAX_PENDING_FILES,
    format: str = "object",
) -> Any:
    output_format = _validate_api_format(format)
    index_status = _index_status(
        root=Path(root).resolve(),
        languages=_languages_filter(language),
        include=(),
        exclude=(),
        db_path=Path(db_path) if db_path is not None else None,
        check=check,
        max_pending_files=max_pending_files,
    )
    if output_format == "object":
        return index_status
    if output_format == "text":
        return _format_status_text(index_status)
    return _to_jsonable(index_status)


def status_text(
    root: str | Path = ".",
    *,
    language: str | None = None,
    db_path: str | Path | None = None,
    check: bool = False,
    max_pending_files: int = DEFAULT_MAX_PENDING_FILES,
) -> str:
    return _format_status_text(
        status(root, language=language, db_path=db_path, check=check, max_pending_files=max_pending_files)
    )


def index(
    root: str | Path = ".",
    *,
    language: str | None = None,
    progress: Any | None = None,
) -> Repository:
    repo = Repository(root, languages=_languages_filter(language), progress=progress, create_index=True)
    return repo.refresh()


def update(
    paths: str | Path | Iterable[str | Path],
    *,
    root: str | Path = ".",
    language: str | None = None,
    progress: Any | None = None,
) -> Repository:
    repo = Repository(root, languages=_languages_filter(language), progress=progress)
    return repo.update(paths)


def refresh_async(
    root: str | Path = ".",
    *,
    language: str | None = None,
    db_path: str | Path | None = None,
    progress: Any | None = None,
    daemon: bool = True,
) -> threading.Thread:
    def run() -> None:
        repo = Repository(
            root,
            languages=_languages_filter(language),
            db_path=db_path,
            progress=progress,
            create_index=True,
        )
        repo.refresh()

    thread = threading.Thread(target=run, daemon=daemon)
    thread.start()
    return thread


def install_skill(
    *,
    target: str = "codex",
    codex_home: str | Path | None = None,
    claude_dir: str | Path | None = None,
    force: bool = False,
) -> Path:
    if target == "codex":
        base = Path(codex_home) if codex_home is not None else Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    elif target == "claude":
        base = Path(claude_dir) if claude_dir is not None else Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    else:
        raise ValueError(f"unsupported skill target: {target}; expected codex or claude")
    return _write_skill(base, force=force)


def _write_skill(base: Path, *, force: bool) -> Path:
    skill_dir = base / "skills" / CODEX_SKILL_NAME
    skill_file = skill_dir / "SKILL.md"
    content = CODEX_SKILL.rstrip() + "\n"
    if skill_file.exists():
        existing = skill_file.read_text(encoding="utf-8")
        if existing == content:
            return skill_file
        if not force:
            raise FileExistsError(f"skill already exists: {skill_file}")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def clean(root: str | Path = ".") -> None:
    shutil.rmtree(Path(root).resolve() / DEFAULT_INDEX_DIR, ignore_errors=True)


class _Storage:
    def __init__(self, db_path: Path | str | None = None, *, create: bool = True) -> None:
        self.db_path = str(db_path) if db_path is not None else ":memory:"
        self.has_symbol_fts = False
        if self.db_path != ":memory:":
            db_file = Path(self.db_path)
            if not create and not db_file.exists():
                raise IndexNotFoundError(f"Index not found: {db_file}")
            if create:
                db_file.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self._configure_connection()
        self._ensure_schema()
        if not create and self.schema_version() != SCHEMA_VERSION:
            self.has_symbol_fts = False

    def _configure_connection(self) -> None:
        with self.connection:
            if self.db_path != ":memory:":
                self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA temp_store=MEMORY")

    def reset_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                DROP TABLE IF EXISTS symbol_fts;
                DROP TABLE IF EXISTS refs;
                DROP TABLE IF EXISTS symbols;
                DROP TABLE IF EXISTS files;
                DROP TABLE IF EXISTS meta;
                """
            )
        self._ensure_schema()

    def clear(self) -> None:
        with self.connection:
            if self.has_symbol_fts:
                self.connection.execute("DELETE FROM symbol_fts")
            self.connection.execute("DELETE FROM refs")
            self.connection.execute("DELETE FROM symbols")
            self.connection.execute("DELETE FROM files")

    def files(self) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT path, language, mtime_ns, size FROM files",
        ).fetchall()
        return {row["path"]: row for row in rows}

    def schema_version(self) -> int | None:
        try:
            row = self.connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'",
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row["value"]) if row is not None else None

    def set_schema_version(self, version: int) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO meta(key, value)
                VALUES ('schema_version', ?)
                """,
                (str(version),),
            )

    def remove_files(self, paths: Iterable[Path]) -> None:
        with self.connection:
            for path in paths:
                path_text = path.as_posix()
                if self.has_symbol_fts:
                    self.connection.execute("DELETE FROM symbol_fts WHERE path = ?", (path_text,))
                self.connection.execute("DELETE FROM refs WHERE path = ?", (path_text,))
                self.connection.execute("DELETE FROM symbols WHERE path = ?", (path_text,))
                self.connection.execute("DELETE FROM files WHERE path = ?", (path_text,))

    def insert_file(
        self,
        *,
        path: Path,
        language: str,
        mtime_ns: int,
        size: int,
        symbols: Iterable[Symbol],
        references: Iterable[Reference],
    ) -> None:
        symbols = list(symbols)
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO files(path, language, mtime_ns, size)
                VALUES (?, ?, ?, ?)
                """,
                (path.as_posix(), language, mtime_ns, size),
            )
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO symbols(
                    id, name, kind, language, path,
                    start_line, start_col, end_line, end_col,
                    start_byte, end_byte, signature, container
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_symbol_row(symbol) for symbol in symbols],
            )
            if self.has_symbol_fts:
                self.connection.executemany(
                    """
                    INSERT INTO symbol_fts(
                        id, name, kind, language, path, signature, container
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [_symbol_fts_row(symbol) for symbol in symbols],
                )
            self.connection.executemany(
                """
                INSERT INTO refs(
                    name, language, path,
                    start_line, start_col, end_line, end_col,
                    start_byte, end_byte, context, reference_kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_reference_row(reference) for reference in references],
            )

    def insert_file_result(self, indexed_file: _IndexedFile) -> None:
        self.insert_file(
            path=indexed_file.path,
            language=indexed_file.language,
            mtime_ns=indexed_file.mtime_ns,
            size=indexed_file.size,
            symbols=indexed_file.symbols,
            references=indexed_file.references,
        )

    def replace_files(
        self,
        *,
        deleted_paths: Iterable[Path],
        indexed_files: Iterable[_IndexedFile],
        schema_version: int,
        progress: Any | None = None,
    ) -> None:
        indexed_files = list(indexed_files)
        deleted_paths = list(deleted_paths)
        symbol_count = sum(len(indexed_file.symbols) for indexed_file in indexed_files)
        fts_count = symbol_count if self.has_symbol_fts else 0
        write_total = len(indexed_files) + symbol_count + fts_count + 1
        write_done = 0

        if progress is not None and deleted_paths:
            progress("delete_start", done=0, total=len(deleted_paths))
        for deleted_chunk in _chunks(deleted_paths, SQLITE_FILE_BATCH_SIZE):
            with self.connection:
                _delete_paths_chunked(self.connection, deleted_chunk, include_fts=self.has_symbol_fts)
        if progress is not None and deleted_paths:
            progress("delete_finish", done=len(deleted_paths), total=len(deleted_paths))
        if progress is not None:
            progress("write_start", done=0, total=write_total)

        for file_chunk in _chunks(indexed_files, SQLITE_FILE_BATCH_SIZE):
            file_rows = [
                (
                    indexed_file.path.as_posix(),
                    indexed_file.language,
                    indexed_file.mtime_ns,
                    indexed_file.size,
                )
                for indexed_file in file_chunk
            ]
            symbol_rows = [
                _symbol_row(symbol)
                for indexed_file in file_chunk
                for symbol in indexed_file.symbols
            ]
            fts_rows = [
                _symbol_fts_row(symbol)
                for indexed_file in file_chunk
                for symbol in indexed_file.symbols
            ] if self.has_symbol_fts else []
            with self.connection:
                _delete_paths_chunked(
                    self.connection,
                    [indexed_file.path for indexed_file in file_chunk],
                    include_fts=self.has_symbol_fts,
                )
                self.connection.executemany(
                    """
                    INSERT OR REPLACE INTO files(path, language, mtime_ns, size)
                    VALUES (?, ?, ?, ?)
                    """,
                    file_rows,
                )
                write_done += len(file_rows)
                if progress is not None:
                    progress("write_tick", done=write_done, total=write_total)
                for row_chunk in _chunks(symbol_rows, SQLITE_BATCH_SIZE):
                    self.connection.executemany(
                        """
                        INSERT OR REPLACE INTO symbols(
                            id, name, kind, language, path,
                            start_line, start_col, end_line, end_col,
                            start_byte, end_byte, signature, container
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        row_chunk,
                    )
                    write_done += len(row_chunk)
                    if progress is not None:
                        progress("write_tick", done=write_done, total=write_total)
                for row_chunk in _chunks(fts_rows, SQLITE_BATCH_SIZE):
                    self.connection.executemany(
                        """
                        INSERT INTO symbol_fts(
                            id, name, kind, language, path, signature, container
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        row_chunk,
                    )
                    write_done += len(row_chunk)
                    if progress is not None:
                        progress("write_tick", done=write_done, total=write_total)

            if progress is not None:
                progress("commit_batch", done=write_done, total=write_total)

        with self.connection:
            updated_at = _utc_now()
            self.connection.execute(
                """
                INSERT OR REPLACE INTO meta(key, value)
                VALUES ('schema_version', ?)
                """,
                (str(schema_version),),
            )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO meta(key, value)
                VALUES ('updated_at', ?)
                """,
                (updated_at,),
            )
            write_done += 1
            if progress is not None:
                progress("write_tick", done=write_done, total=write_total)
                progress("finalize", done=write_done, total=write_total)

    def search_symbols(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None = None,
        language: str | None = None,
        path: str | Path | Iterable[str | Path] | None = None,
        exact_only: bool = False,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        if limit <= 0:
            return []

        normalized_query = query.strip()
        if normalized_query and not exact_only and self.has_symbol_fts and len(normalized_query) >= 3:
            try:
                rows = self._search_symbols_fts(
                    normalized_query,
                    kind=kind,
                    language=language,
                    path=path,
                    limit=limit,
                )
                return [_symbol_from_row(row) for row in rows]
            except sqlite3.OperationalError:
                self.has_symbol_fts = False

        return self._search_symbols_like(
            normalized_query,
            kind=kind,
            language=language,
            path=path,
            exact_only=exact_only,
            limit=limit,
        )

    def _search_symbols_fts(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None,
        language: str | None,
        path: str | Path | Iterable[str | Path] | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        clauses = []
        values: list[object] = [f"name : {_escape_fts_query(query)}"]
        kind_clause, kind_values = _sql_in_clause("symbols.kind", _coerce_filter_values(kind))
        if kind_clause:
            clauses.append(kind_clause)
            values.extend(kind_values)
        if language is not None:
            clauses.append("symbols.language = ?")
            values.append(language)
        path_clause, path_values = _path_filter_clause("symbols.path", path)
        if path_clause:
            clauses.append(path_clause)
            values.extend(path_values)

        filters = " AND ".join(["symbol_fts MATCH ?", *clauses])
        values.extend([query, f"{_escape_like(query)}%", limit])
        return self.connection.execute(
            f"""
            SELECT symbols.*
            FROM symbol_fts
            JOIN symbols ON symbols.id = symbol_fts.id
            WHERE {filters}
            ORDER BY
                CASE
                    WHEN symbols.name COLLATE NOCASE = ? THEN 0
                    WHEN symbols.name COLLATE NOCASE LIKE ? ESCAPE '\\' THEN 1
                    ELSE 2
                END,
                bm25(symbol_fts), length(symbols.name), symbols.path, symbols.start_byte
            LIMIT ?
            """,
            values,
        ).fetchall()

    def _search_symbols_like(
        self,
        query: str,
        *,
        kind: str | Iterable[str] | None,
        language: str | None,
        path: str | Path | Iterable[str | Path] | None,
        exact_only: bool,
        limit: int,
    ) -> list[Symbol]:
        clauses = []
        values: list[object] = []
        if query:
            if exact_only:
                clauses.append("name COLLATE NOCASE = ?")
                values.append(query)
            else:
                clauses.append("name COLLATE NOCASE LIKE ? ESCAPE '\\'")
                values.append(f"%{_escape_like(query)}%")
        kind_clause, kind_values = _sql_in_clause("kind", _coerce_filter_values(kind))
        if kind_clause:
            clauses.append(kind_clause)
            values.extend(kind_values)
        if language is not None:
            clauses.append("language = ?")
            values.append(language)
        path_clause, path_values = _path_filter_clause("path", path)
        if path_clause:
            clauses.append(path_clause)
            values.extend(path_values)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_values: list[object] = []
        if query and not exact_only:
            order_sql = """
                CASE
                    WHEN name COLLATE NOCASE = ? THEN 0
                    WHEN name COLLATE NOCASE LIKE ? ESCAPE '\\' THEN 1
                    ELSE 2
                END,
                length(name), path, start_byte
            """
            order_values = [query, f"{_escape_like(query)}%"]
        else:
            order_sql = "path, start_byte"

        rows = self.connection.execute(
            f"""
            SELECT * FROM symbols
            {where}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            (*values, *order_values, limit),
        ).fetchall()
        return [_symbol_from_row(row) for row in rows]

    def implementation_candidates(
        self,
        symbol: Symbol,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        _validate_pagination(limit=limit, offset=offset)
        if symbol.kind not in IMPLEMENTATION_KINDS:
            return Page(items=(), limit=limit, offset=offset, has_more=False)

        placeholders = ",".join("?" for _ in IMPLEMENTATION_KINDS)
        rows = self.connection.execute(
            f"""
            SELECT * FROM symbols
            WHERE language = ?
              AND kind IN ({placeholders})
              AND id != ?
              AND (
                name = ?
                OR signature LIKE ? ESCAPE '\\'
              )
            ORDER BY
              CASE WHEN name = ? THEN 0 ELSE 1 END,
              length(name), path, start_byte
            LIMIT ?
            OFFSET ?
            """,
            (
                symbol.language,
                *IMPLEMENTATION_KINDS,
                symbol.id,
                symbol.name,
                f"%{_escape_like(symbol.name)}%",
                symbol.name,
                limit + 1,
                offset,
            ),
        ).fetchall()
        return _page_from_extra([_symbol_from_row(row) for row in rows], limit=limit, offset=offset)

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        row = self.connection.execute(
            "SELECT * FROM symbols WHERE id = ?",
            (symbol_id,),
        ).fetchone()
        return _symbol_from_row(row) if row is not None else None

    def symbols(self) -> list[Symbol]:
        rows = self.connection.execute(
            "SELECT * FROM symbols ORDER BY path, start_byte",
        ).fetchall()
        return [_symbol_from_row(row) for row in rows]

    def symbols_in_file(self, path: Path) -> list[Symbol]:
        rows = self.connection.execute(
            "SELECT * FROM symbols WHERE path = ? ORDER BY start_byte",
            (path.as_posix(),),
        ).fetchall()
        return [_symbol_from_row(row) for row in rows]

    def symbol_names_by_language(self) -> dict[str, set[str]]:
        rows = self.connection.execute(
            "SELECT DISTINCT language, name FROM symbols",
        ).fetchall()
        names: dict[str, set[str]] = {}
        for row in rows:
            names.setdefault(row["language"], set()).add(row["name"])
        return names

    def file_paths(self, *, language: str | None = None) -> list[Path]:
        if language is None:
            rows = self.connection.execute(
                "SELECT path FROM files ORDER BY path",
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT path FROM files WHERE language = ? ORDER BY path",
                (language,),
            ).fetchall()
        return [Path(row["path"]) for row in rows]

    def references_for(
        self,
        symbol: Symbol,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        ref_kinds: frozenset[str] | None = None,
    ) -> Page:
        _validate_pagination(limit=limit, offset=offset)
        params: list[object] = [
            symbol.name,
            symbol.language,
            symbol.path.as_posix(),
            symbol.range.start_byte,
            symbol.range.end_byte,
        ]
        kind_clause, kind_params = _sql_in_clause("reference_kind", tuple(sorted(ref_kinds)) if ref_kinds is not None else ())
        kind_sql = f"\n              AND {kind_clause}" if kind_clause else ""
        params.extend(kind_params)
        params.extend([limit + 1, offset])
        rows = self.connection.execute(
            f"""
            SELECT * FROM refs
            WHERE name = ? AND language = ?
              AND NOT (path = ? AND start_byte = ? AND end_byte = ?){kind_sql}
            ORDER BY path, start_byte
            LIMIT ?
            OFFSET ?
            """,
            tuple(params),
        ).fetchall()
        return _page_from_extra([_reference_from_row(row, symbol.id) for row in rows], limit=limit, offset=offset)

    def file_source(self, root: Path, path: Path) -> str | None:
        full_path = root / path
        try:
            return _read_text_file(full_path)
        except (OSError, UnicodeDecodeError, BinaryFileError):
            return None

    def _ensure_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    language TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS symbols (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    language TEXT NOT NULL,
                    path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    start_col INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    end_col INTEGER NOT NULL,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    container TEXT
                );

                CREATE TABLE IF NOT EXISTS refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    language TEXT NOT NULL,
                    path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    start_col INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    end_col INTEGER NOT NULL,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER NOT NULL,
                    context TEXT NOT NULL,
                    reference_kind TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
                CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
                CREATE INDEX IF NOT EXISTS idx_symbols_language ON symbols(language);
                CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
                CREATE INDEX IF NOT EXISTS idx_files_language_path ON files(language, path);
                CREATE INDEX IF NOT EXISTS idx_symbols_name_nocase ON symbols(name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_symbols_language_kind ON symbols(language, kind);
                CREATE INDEX IF NOT EXISTS idx_refs_name_language ON refs(name, language);
                """
            )
            try:
                self.connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(
                        id UNINDEXED,
                        name,
                        kind UNINDEXED,
                        language UNINDEXED,
                        path UNINDEXED,
                        signature,
                        container,
                        tokenize='trigram'
                    )
                    """
                )
            except sqlite3.OperationalError:
                self.has_symbol_fts = False
            else:
                self.has_symbol_fts = True


def supported_languages() -> tuple[str, ...]:
    available = []
    for language in LANGUAGE_BY_NAME:
        try:
            _parser_for_language(language)
        except UnsupportedLanguageError:
            continue
        available.append(language)
    return tuple(available)


def _normalize_languages(languages: Iterable[str] | None) -> set[str] | None:
    if languages is None:
        return None
    normalized = set(languages)
    unknown = sorted(normalized - set(LANGUAGE_BY_NAME))
    if unknown:
        raise UnsupportedLanguageError(f"Unsupported language(s): {', '.join(unknown)}")
    return normalized


def _languages_filter(language: str | None) -> tuple[str, ...] | None:
    return (language,) if language is not None else None


def _validate_api_format(format: str) -> str:
    if format not in API_FORMATS:
        raise ValueError(f"format must be one of: {', '.join(API_FORMATS)}")
    return format


def _validate_anchor_format(format: str) -> str:
    if format not in ANCHOR_FORMATS:
        raise ValueError(f"anchor_format must be one of: {', '.join(ANCHOR_FORMATS)}")
    return format


def _emit_progress(
    progress: Any | None,
    event: str,
    *,
    done: int = 0,
    total: int = 0,
    path: str | None = None,
) -> None:
    if progress is None:
        return
    try:
        progress(event, done=done, total=total, path=path)
    except Exception:
        return


def _coerce_queries(query: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(query, str):
        queries = tuple(query.split("|"))
    else:
        queries = tuple(part for item in query for part in str(item).split("|"))
    return tuple(item for item in (value.strip() for value in queries) if item)


def _coerce_paths(paths: str | Path | Iterable[str | Path]) -> tuple[Path, ...]:
    if isinstance(paths, (str, Path)):
        return (Path(paths),)
    return tuple(Path(path) for path in paths)


def _coerce_filter_values(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = [part for item in value for part in str(item).split(",")]
    return tuple(item for item in (raw.strip() for raw in raw_values) if item)


def _ref_kinds_option(value: str | Iterable[str] | None) -> str | tuple[str, ...] | None:
    """Coerce a friendly ``ref_kinds`` value into a hashable form for InspectOptions."""
    if value is None or isinstance(value, str):
        return value
    return tuple(value)


def _resolve_ref_kinds(value: str | Iterable[str] | None) -> frozenset[str] | None:
    """Resolve a friendly ``ref_kinds`` value to an allow-set, or ``None`` for all.

    ``"behavioral"`` (the default) hides the high-noise ``import``/``attribute``
    kinds; ``"all"`` disables filtering; any other value is treated as an
    explicit comma-separated or iterable allow-list.
    """
    if value is None or value == _REF_KINDS_DEFAULT:
        return DEFAULT_REFERENCE_KINDS
    if isinstance(value, str) and value.strip().lower() == "all":
        return None
    kinds = _coerce_filter_values(value)
    if not kinds:
        return DEFAULT_REFERENCE_KINDS
    invalid = sorted(set(kinds) - REFERENCE_KINDS)
    if invalid:
        valid = ", ".join(sorted(REFERENCE_KINDS))
        raise ValueError(f"unknown reference kind(s): {', '.join(invalid)}; valid kinds: {valid}")
    return frozenset(kinds)


def _sql_in_clause(column: str, values: tuple[str, ...]) -> tuple[str, list[object]]:
    if not values:
        return "", []
    if len(values) == 1:
        return f"{column} = ?", [values[0]]
    placeholders = ",".join("?" for _ in values)
    return f"{column} IN ({placeholders})", list(values)


def _path_filter_values(path: str | Path | Iterable[str | Path] | None) -> tuple[str, ...]:
    if path is None:
        return ()
    if isinstance(path, (str, Path)):
        raw_values = str(path).split(",")
    else:
        raw_values = [part for item in path for part in str(item).split(",")]
    normalized = (_normalize_filter_path(item) for item in raw_values if item.strip())
    return tuple(item for item in normalized if item)


def _normalize_filter_path(path: str) -> str:
    normalized = Path(path.strip()).as_posix().strip("/")
    return "" if normalized == "." else normalized


def _path_filter_clause(column: str, path: str | Path | Iterable[str | Path] | None) -> tuple[str, list[object]]:
    values = _path_filter_values(path)
    if not values:
        return "", []

    clauses: list[str] = []
    params: list[object] = []
    for value in values:
        if any(character in value for character in "*?[]"):
            clauses.append(f"{column} GLOB ?")
            params.append(value)
            continue
        clauses.append(f"({column} = ? OR {column} LIKE ? ESCAPE '\\')")
        params.extend([value, f"{_escape_like(value)}/%"])
    return "(" + " OR ".join(clauses) + ")", params


def _spec_for_path(path: Path, languages: set[str] | None = None) -> LanguageSpec | None:
    spec = LANGUAGE_BY_EXTENSION.get(path.suffix.lower())
    if spec is None:
        return None
    if languages is not None and spec.name not in languages:
        return None
    try:
        _parser_for_language(spec.name)
    except UnsupportedLanguageError:
        return None
    return spec


def _parse_file(
    root: Path,
    relative_path: Path,
    languages: set[str] | None = None,
    include_references: bool = True,
) -> _IndexedFile | None:
    full_path = root / relative_path
    spec = _spec_for_path(relative_path, languages)
    if spec is None:
        return None

    try:
        stat = full_path.stat()
        source_text = _read_text_file(full_path)
    except (OSError, UnicodeDecodeError, BinaryFileError):
        return None

    tree = _parse_source(_parser_for_language(spec.name), source_text)
    root_node = tree.root_node() if callable(tree.root_node) else tree.root_node
    source_bytes = source_text.encode("utf-8")
    symbols, references = _extract_symbols_and_references(
        source=source_bytes,
        root_node=root_node,
        path=relative_path,
        language=spec,
        include_references=include_references,
    )
    return _IndexedFile(
        path=relative_path,
        language=spec.name,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        symbols=tuple(symbols),
        references=tuple(references),
    )


def _terminate_executor(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    terminate_workers = getattr(executor, "terminate_workers", None)
    if terminate_workers is not None:
        terminate_workers()
        return

    processes = getattr(executor, "_processes", None)
    if processes:
        for process in processes.values():
            process.terminate()
    executor.shutdown(wait=False, cancel_futures=True)


def _read_text_file(path: Path) -> str:
    with path.open("rb") as file:
        sample = file.read(TEXT_SAMPLE_BYTES)
        if b"\x00" in sample:
            raise BinaryFileError(f"not a text file: {path}")
        remainder = file.read()
    return (sample + remainder).decode("utf-8")


def _file_contains_bytes(path: Path, needle: bytes) -> bool:
    if not needle:
        return True

    overlap = len(needle) - 1
    previous = b""
    try:
        with path.open("rb") as file:
            while chunk := file.read(FILE_SCAN_CHUNK_SIZE):
                window = previous + chunk
                if needle in window:
                    return True
                previous = window[-overlap:] if overlap else b""
    except OSError:
        return False
    return False


_PARSER_TLS = threading.local()


def _parse_source(parser, source: str):
    # tree-sitter's parse() signature varies across versions/builds: some accept str, others
    # require bytes (raising "source must be a bytestring or a callable, not str"). Try str first,
    # then fall back to encoded bytes so both bindings work. Byte offsets are identical either way.
    try:
        return parser.parse(source)
    except TypeError:
        return parser.parse(source.encode("utf-8"))


def _parser_for_language(language: str):
    if language not in LANGUAGE_BY_NAME:
        raise UnsupportedLanguageError(f"Unsupported language: {language}")
    # Cache parsers per thread: tree-sitter parsers are not safe to share across
    # threads, and thread-local storage is released when a worker thread ends so
    # we do not retain parsers across background refreshes.
    cache = getattr(_PARSER_TLS, "parsers", None)
    if cache is None:
        cache = {}
        _PARSER_TLS.parsers = cache
    parser = cache.get(language)
    if parser is None:
        try:
            parser = get_parser(language)
        except Exception as exc:
            raise UnsupportedLanguageError(f"No parser available for language: {language}") from exc
        cache[language] = parser
    return parser


_CALLEE_FIELD_NAMES = ("function", "constructor")
_MEMBER_NAME_FIELDS = ("attribute", "property", "field", "name")


def _same_node(a: Node | None, b: Node | None) -> bool:
    if a is None or b is None:
        return False
    return _node_start_byte(a) == _node_start_byte(b) and _node_end_byte(a) == _node_end_byte(b)


def _field_child(node: Node | None, *names: str) -> Node | None:
    if node is None:
        return None
    getter = getattr(node, "child_by_field_name", None)
    if getter is None:
        return None
    for name in names:
        child = getter(name)
        if child is not None:
            return child
    return None


def _member_name_node(member: Node | None, language: LanguageSpec) -> Node | None:
    """The name part of a member access node (``obj.NAME``), not the receiver."""
    field = _field_child(member, *_MEMBER_NAME_FIELDS)
    if field is not None:
        return field
    # Fall back to the last identifier-like child (handles grammars without
    # a dedicated property field).
    name: Node | None = None
    for child in _node_children(member) if member is not None else []:
        if _node_kind(child) in language.identifier_node_types:
            name = child
    return name


def _child_reference_context(
    node: Node, parent: Node | None, ctx: frozenset[str], language: LanguageSpec
) -> frozenset[str]:
    """Context flags that ``node``'s subtree inherits (import / type / inherit)."""
    added: set[str] = set()
    node_kind = _node_kind(node)
    if node_kind in language.import_node_types:
        added.add("import")
    if node_kind in language.type_node_types:
        added.add("type")
    if node_kind in language.inherit_node_types:
        added.add("inherit")
    elif (
        language.name == "python"
        and node_kind == "argument_list"
        and parent is not None
        and _node_kind(parent) == "class_definition"
    ):
        # Python encodes base classes as the class definition's argument list.
        added.add("inherit")
    if not added:
        return ctx
    return ctx | added


def _is_call_callee(node: Node, parent: Node | None, grandparent: Node | None, language: LanguageSpec) -> bool:
    if parent is None:
        return False
    parent_kind = _node_kind(parent)
    if parent_kind in language.call_node_types:
        return _same_node(_field_child(parent, *_CALLEE_FIELD_NAMES), node)
    # Method call: ``obj.method()`` — node is the member of a member-access node
    # that is itself the callee of the surrounding call.
    if (
        parent_kind in language.member_node_types
        and grandparent is not None
        and _node_kind(grandparent) in language.call_node_types
    ):
        callee = _field_child(grandparent, *_CALLEE_FIELD_NAMES)
        return _same_node(callee, parent) and _same_node(_member_name_node(parent, language), node)
    return False


def _is_write_target(node: Node, parent: Node | None, language: LanguageSpec) -> bool:
    if parent is None or _node_kind(parent) not in language.assignment_node_types:
        return False
    return _same_node(_field_child(parent, "left", "name"), node)


def _is_attribute_ref(node: Node, parent: Node | None, language: LanguageSpec) -> bool:
    if _node_kind(node) in MEMBER_IDENTIFIER_NODE_TYPES:
        return True
    if parent is not None and _node_kind(parent) in language.member_node_types:
        return _same_node(_member_name_node(parent, language), node)
    return False


def _classify_reference(
    node: Node,
    parent: Node | None,
    grandparent: Node | None,
    ctx: frozenset[str],
    language: LanguageSpec,
) -> str:
    if "import" in ctx:
        return "import"
    if "inherit" in ctx:
        return "inherit"
    if _is_call_callee(node, parent, grandparent, language):
        return "call"
    if _is_write_target(node, parent, language):
        return "write"
    if "type" in ctx or _node_kind(node) == "type_identifier":
        return "type"
    if _is_attribute_ref(node, parent, language):
        return "attribute"
    if parent is None:
        return "usage"
    return "read"


def _extract_symbols_and_references(
    *,
    source: bytes,
    root_node: Node,
    path: Path,
    language: LanguageSpec,
    include_references: bool = True,
) -> tuple[list[Symbol], list[Reference]]:
    symbols: list[Symbol] = []
    references: list[Reference] = []
    text = source.decode("utf-8", errors="replace")
    lines = text.splitlines()

    def walk(
        node: Node,
        container: str | None,
        parent: Node | None,
        grandparent: Node | None,
        ctx: frozenset[str],
    ) -> None:
        symbol = _symbol_from_node(source, path, language, node, container)
        next_container = container
        if symbol is not None:
            symbols.append(symbol)
            if symbol.kind in CONTAINER_KINDS:
                next_container = symbol.name if container is None else f"{container}.{symbol.name}"

        if include_references and _node_kind(node) in language.identifier_node_types:
            references.append(
                Reference(
                    symbol_id="",
                    name=_node_text(source, node),
                    language=language.name,
                    path=path,
                    range=_node_range(source, node),
                    context=_line_context(source, lines, node),
                    reference_kind=_classify_reference(node, parent, grandparent, ctx, language),
                )
            )

        child_ctx = _child_reference_context(node, parent, ctx, language)
        for child in _node_children(node):
            walk(child, next_container, node, parent, child_ctx)

    walk(root_node, None, None, None, frozenset())
    if language.name == "python":
        symbols.extend(_python_top_level_symbols(source, path, language, root_node))
    return symbols, references


def _python_top_level_symbols(source: bytes, path: Path, language: LanguageSpec, root_node: Node) -> list[Symbol]:
    symbols: list[Symbol] = []
    for node in _node_children(root_node):
        if _node_kind(node) != "assignment":
            continue
        left = node.child_by_field_name("left")
        if left is None:
            continue
        name_node = _first_identifier(left, language)
        if name_node is None:
            continue
        name = _node_text(source, name_node)
        if not name or not _looks_like_symbol_name(name):
            continue
        kind = "constant" if name.isupper() else "variable"
        range_ = _node_range(source, name_node)
        symbols.append(
            Symbol(
                id=_symbol_id(language.name, path, kind, name, range_.start_byte),
                name=name,
                kind=kind,
                language=language.name,
                path=path,
                range=range_,
                signature=_signature(source, node),
                container=None,
            )
        )
        symbols.extend(_python_dict_key_symbols(source, path, language, node, container=name))
    return symbols


def _python_dict_key_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    assignment: Node,
    *,
    container: str,
) -> list[Symbol]:
    value = assignment.child_by_field_name("right")
    if value is None or _node_kind(value) != "dictionary":
        return []

    symbols: list[Symbol] = []
    for child in _node_children(value):
        if _node_kind(child) != "pair":
            continue
        key = child.child_by_field_name("key")
        if key is None:
            continue
        key_name, key_node = _python_dict_key_name(source, language, key)
        if not key_name or not _looks_like_symbol_name(key_name):
            continue
        range_ = _node_range(source, key_node)
        symbols.append(
            Symbol(
                id=_symbol_id(language.name, path, "dict_key", key_name, range_.start_byte),
                name=key_name,
                kind="dict_key",
                language=language.name,
                path=path,
                range=range_,
                signature=_signature(source, child),
                container=container,
            )
        )
    return symbols


def _python_dict_key_name(source: bytes, language: LanguageSpec, node: Node) -> tuple[str | None, Node]:
    if _node_kind(node) == "string":
        for child in _node_children(node):
            if _node_kind(child) == "string_content":
                return _node_text(source, child), child
        return _node_text(source, node).strip("\"'"), node
    if _node_kind(node) in language.identifier_node_types:
        return _node_text(source, node), node
    return None, node


def _symbol_from_node(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
) -> Symbol | None:
    kind = language.definitions.get(_node_kind(node))
    if kind is None:
        return None
    name_node = _name_node(node, language)
    if name_node is None:
        return None
    name = _node_text(source, name_node)
    if not name or not _looks_like_symbol_name(name):
        return None
    range_ = _node_range(source, name_node)
    return Symbol(
        id=_symbol_id(language.name, path, kind, name, range_.start_byte),
        name=name,
        kind=kind,
        language=language.name,
        path=path,
        range=range_,
        signature=_signature(source, node),
        container=container,
    )


def _name_node(node: Node, language: LanguageSpec) -> Node | None:
    direct = node.child_by_field_name("name")
    if direct is not None:
        leaf = _first_identifier(direct, language)
        return leaf if leaf is not None else direct

    for field in ("declarator", "declaration", "type", "path", "left", "pattern"):
        child = node.child_by_field_name(field)
        if child is not None:
            found = _first_identifier(child, language)
            if found is not None:
                return found

    return _first_identifier(node, language)


def _first_identifier(node: Node, language: LanguageSpec) -> Node | None:
    if _node_kind(node) in language.identifier_node_types:
        return node
    for child in _node_children(node):
        found = _first_identifier(child, language)
        if found is not None:
            return found
    return None


def _node_text(source: bytes, node: Node) -> str:
    return source[_node_start_byte(node) : _node_end_byte(node)].decode("utf-8", errors="replace")


def _signature(source: bytes, node: Node) -> str:
    text = _node_text(source, node).strip()
    first_line = text.splitlines()[0] if text else ""
    return first_line[:240]


def _node_range(source: bytes, node: Node) -> Range:
    start_byte = _node_start_byte(node)
    end_byte = _node_end_byte(node)
    start_line, start_column = _byte_position(source, start_byte)
    end_line, end_column = _byte_position(source, end_byte)
    return Range(
        start=Position(line=start_line, column=start_column),
        end=Position(line=end_line, column=end_column),
        start_byte=start_byte,
        end_byte=end_byte,
    )


def _byte_position(source: bytes, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    line = prefix.count(b"\n")
    line_start = prefix.rfind(b"\n") + 1
    return line, len(prefix) - line_start


def _line_context(source: bytes, lines: list[str], node: Node) -> str:
    line, _ = _byte_position(source, _node_start_byte(node))
    if 0 <= line < len(lines):
        return lines[line].strip()
    return ""


def _node_kind(node: Node) -> str:
    kind = getattr(node, "type", None)
    if kind is not None:
        return kind
    return node.kind()


def _node_children(node: Node) -> list[Node]:
    children = getattr(node, "children", None)
    if children is not None:
        return list(children)
    child_count = _node_value(node, "child_count")
    return [node.child(index) for index in range(child_count)]


def _node_start_byte(node: Node) -> int:
    return _node_value(node, "start_byte")


def _node_end_byte(node: Node) -> int:
    return _node_value(node, "end_byte")


def _node_start_point(node: Node) -> Any:
    return _node_value(node, "start_point", "start_position")


def _node_end_point(node: Node) -> Any:
    return _node_value(node, "end_point", "end_position")


def _node_value(node: Node, *names: str) -> Any:
    for name in names:
        value = getattr(node, name, None)
        if value is not None:
            return value() if callable(value) else value
    raise AttributeError(f"node has none of: {', '.join(names)}")


def _source_preview(source: str, range_: Range, radius: int = 2) -> str:
    lines = source.splitlines()
    start = max(range_.start.line - radius, 0)
    end = min(range_.end.line + radius + 1, len(lines))
    return "\n".join(lines[start:end])


def _imports_for_file(repo: CodeIndex, path: Path, *, limit: int) -> tuple[ImportItem, ...]:
    if limit <= 0:
        return ()
    source = repo.storage.file_source(repo.root, path)
    if source is None:
        return ()
    imports: list[ImportItem] = []
    for line_number, line in enumerate(source.splitlines()):
        stripped = line.strip()
        if not _looks_like_import_statement(stripped):
            continue
        range_ = Range(
            start=Position(line_number, 0),
            end=Position(line_number, len(line)),
            start_byte=0,
            end_byte=0,
        )
        imports.append(ImportItem(path=path, range=range_, statement=stripped[:240]))
        if len(imports) >= limit:
            break
    return tuple(imports)


def _looks_like_import_statement(stripped: str) -> bool:
    return (
        stripped.startswith("import ")
        or stripped.startswith("from ")
        or stripped.startswith("use ")
        or stripped.startswith("#include")
        or stripped.startswith("require ")
    )


def _symbol_row(symbol: Symbol) -> tuple[object, ...]:
    return (
        symbol.id,
        symbol.name,
        symbol.kind,
        symbol.language,
        symbol.path.as_posix(),
        symbol.range.start.line,
        symbol.range.start.column,
        symbol.range.end.line,
        symbol.range.end.column,
        symbol.range.start_byte,
        symbol.range.end_byte,
        symbol.signature,
        symbol.container,
    )


def _symbol_fts_row(symbol: Symbol) -> tuple[object, ...]:
    return (
        symbol.id,
        symbol.name,
        symbol.kind,
        symbol.language,
        symbol.path.as_posix(),
        symbol.signature,
        symbol.container or "",
    )


def _reference_row(reference: Reference) -> tuple[object, ...]:
    return (
        reference.name,
        reference.language,
        reference.path.as_posix(),
        reference.range.start.line,
        reference.range.start.column,
        reference.range.end.line,
        reference.range.end.column,
        reference.range.start_byte,
        reference.range.end_byte,
        reference.context,
        reference.reference_kind,
    )


def _symbol_from_row(row: sqlite3.Row) -> Symbol:
    return Symbol(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        language=row["language"],
        path=Path(row["path"]),
        range=Range(
            start=Position(row["start_line"], row["start_col"]),
            end=Position(row["end_line"], row["end_col"]),
            start_byte=row["start_byte"],
            end_byte=row["end_byte"],
        ),
        signature=row["signature"],
        container=row["container"],
    )


def _reference_from_row(row: sqlite3.Row, symbol_id: str) -> Reference:
    return Reference(
        symbol_id=symbol_id,
        name=row["name"],
        language=row["language"],
        path=Path(row["path"]),
        range=Range(
            start=Position(row["start_line"], row["start_col"]),
            end=Position(row["end_line"], row["end_col"]),
            start_byte=row["start_byte"],
            end_byte=row["end_byte"],
        ),
        context=row["context"],
        reference_kind=row["reference_kind"],
    )


def _inspect_text(
    repo: CodeIndex,
    query: str,
    *,
    kind: str | Iterable[str] | None,
    language: str | None,
    path: str | Path | Iterable[str | Path] | None,
    exact_only: bool,
    options: InspectOptions,
    anchors: bool,
) -> str:
    anchor_format = _validate_anchor_format(options.anchor_format)
    invalid_reason = _invalid_symbol_query_reason(query, repo.root)
    if invalid_reason is not None:
        return _bounded_text(f"invalid_input:\n  reason: {invalid_reason}\n", options.max_total_chars)

    candidates = _inspect_candidates(repo, query, kind=kind, language=language, path=path, exact_only=exact_only)
    if not candidates:
        return _bounded_text(f"not_found:\n  query: {query}\n", options.max_total_chars)
    if len(candidates) > 1:
        lines = ["ambiguous:", "  candidates:"]
        for candidate in candidates[:MAX_INSPECT_CANDIDATES]:
            lines.extend(_format_relation_item(repo, candidate, indent=4))
        return _bounded_text("\n".join(lines) + "\n", options.max_total_chars)

    symbol = candidates[0]
    source = repo.storage.file_source(repo.root, symbol.path) or ""
    source_range = _definition_range(repo, symbol) or symbol.range
    imports = _imports_for_file(repo, symbol.path, limit=options.max_imports)
    members = _members_for_symbol(repo, symbol, limit=options.max_members)
    references = _references_for_inspect(
        repo, symbol, limit=options.max_references, ref_kinds=_resolve_ref_kinds(options.ref_kinds)
    )
    implementors = (
        tuple(repo.storage.implementation_candidates(symbol, limit=options.max_implementors, offset=0).items)
        if options.max_implementors > 0
        else ()
    )
    callers = _callers_for_symbol(repo, symbol, references, limit=options.max_callers)
    callees = _callees_for_symbol(repo, symbol, source_range, limit=options.max_callees)

    lines = ["symbol:"]
    lines.extend(_format_symbol_fields(symbol, indent=2, range_=source_range))
    lines.extend(_format_summary_section(imports, members, callers, callees, references, implementors))
    lines.extend(_format_import_section(imports))
    lines.extend(_format_source_block(source, source_range, options.max_source_chars, anchors=anchors, anchor_format=anchor_format))
    lines.extend(_format_relation_section(repo, "members", members, options.max_members))
    lines.extend(_format_relation_section(repo, "callers", callers, options.max_callers))
    lines.extend(_format_relation_section(repo, "callees", callees, options.max_callees))
    lines.extend(_format_relation_section(repo, "references", references, options.max_references))
    lines.extend(_format_relation_section(repo, "implementors", implementors, options.max_implementors))
    return _bounded_text("\n".join(lines) + "\n", options.max_total_chars)


def _invalid_symbol_query_reason(query: str, root: Path) -> str | None:
    stripped = query.strip()
    if not stripped:
        return "empty query"
    if "\n" in stripped:
        return "query must be a symbol name, not natural language"
    if "/" in stripped or "\\" in stripped:
        return "file and directory paths are not supported"
    if " " in stripped or "\t" in stripped:
        return "natural language queries are not supported"
    path = root / stripped
    if path.exists():
        return "file and directory paths are not supported"
    if not SYMBOL_QUERY_PATTERN.match(stripped):
        return "query must be ClassName, function_name, ClassName.method_name, or symbol_prefix"
    return None


def _inspect_candidates(
    repo: CodeIndex,
    query: str,
    *,
    kind: str | Iterable[str] | None,
    language: str | None,
    path: str | Path | Iterable[str | Path] | None,
    exact_only: bool,
) -> list[Symbol]:
    if "." in query:
        container, name = query.rsplit(".", 1)
        matches = repo.search_symbols(
            name,
            kind=kind,
            language=language,
            path=path,
            exact_only=exact_only,
            limit=MAX_INSPECT_CANDIDATES + 1,
        )
        matches = [
            symbol
            for symbol in matches
            if symbol.name == name and symbol.container is not None and symbol.container.split(".")[-1] == container
        ]
    else:
        matches = repo.search_symbols(
            query,
            kind=kind,
            language=language,
            path=path,
            exact_only=exact_only,
            limit=MAX_INSPECT_CANDIDATES + 1,
        )
        exact = [symbol for symbol in matches if symbol.name == query]
        prefix = [] if exact_only else [symbol for symbol in matches if symbol.name.startswith(query)]
        matches = exact or prefix
    return matches


def _resolve_inspect_symbol(
    repo: CodeIndex,
    query: str,
    *,
    kind: str | Iterable[str] | None,
    language: str | None,
    path: str | Path | Iterable[str | Path] | None,
    exact_only: bool,
) -> Symbol:
    invalid_reason = _invalid_symbol_query_reason(query, repo.root)
    if invalid_reason is not None:
        raise SymbolNotFoundError(invalid_reason)
    candidates = _inspect_candidates(repo, query, kind=kind, language=language, path=path, exact_only=exact_only)
    if not candidates:
        raise SymbolNotFoundError(f"No symbol matched: {query}")
    if len(candidates) > 1:
        raise SymbolNotFoundError(f"Ambiguous symbol: {query}")
    return candidates[0]


def _members_for_symbol(repo: CodeIndex, symbol: Symbol, *, limit: int) -> tuple[Symbol, ...]:
    if limit <= 0:
        return ()
    if symbol.kind not in CONTAINER_KINDS:
        return ()
    members = [
        candidate
        for candidate in repo.storage.symbols_in_file(symbol.path)
        if candidate.id != symbol.id
        and candidate.container is not None
        and (candidate.container == symbol.name or candidate.container.startswith(f"{symbol.name}."))
    ]
    return tuple(members[:limit])


def _local_outline_symbols(repo: CodeIndex, symbols: list[Symbol], query: str) -> list[Symbol]:
    stripped = query.strip()
    if not stripped:
        return []

    candidates = _local_outline_candidates(symbols, stripped)
    if not candidates:
        return []
    ranges = _definition_ranges_for_symbols(repo, symbols[0].path, symbols) if symbols else {}
    selected = sorted(candidates, key=lambda symbol: (_local_outline_rank(symbol, stripped), symbol.range.start_byte))[0]
    selected_range = ranges.get(selected.id) or selected.range

    result: list[Symbol] = []
    for symbol in symbols:
        range_ = ranges.get(symbol.id) or symbol.range
        if symbol.id == selected.id or _range_within(range_, selected_range):
            result.append(symbol)
    return result


def _local_outline_candidates(symbols: list[Symbol], query: str) -> list[Symbol]:
    if "." in query:
        container, name = query.rsplit(".", 1)
        return [
            symbol
            for symbol in symbols
            if symbol.name == name and symbol.container is not None and symbol.container.split(".")[-1] == container
        ]
    exact = [symbol for symbol in symbols if symbol.name == query]
    if exact:
        return exact
    return [symbol for symbol in symbols if symbol.name.startswith(query)]


def _local_outline_rank(symbol: Symbol, query: str) -> int:
    if "." in query:
        return 0
    if symbol.name == query:
        return 0
    if symbol.name.startswith(query):
        return 1
    return 2


def _range_within(candidate: Range, container: Range) -> bool:
    return (
        container.start.line <= candidate.start.line
        and candidate.start.line < container.end.line + 1
        and candidate.end.line <= container.end.line
    )


def _references_for_inspect(
    repo: CodeIndex, symbol: Symbol, *, limit: int, ref_kinds: frozenset[str] | None = None
) -> tuple[Reference, ...]:
    if limit <= 0:
        return ()
    if isinstance(repo, Repository):
        return tuple(repo._references_for_symbol(symbol, limit=limit, offset=0, ref_kinds=ref_kinds).items)
    return tuple(repo.storage.references_for(symbol, limit=limit, offset=0, ref_kinds=ref_kinds).items)


_HTTP_ROUTE_DECORATOR_RE = re.compile(
    r"(?:^|\.)(?:route|get|post|put|patch|delete|head|options|websocket|api_route|endpoint)\b"
    r"|app\.route|router\.|blueprint|\bapi\.(?:get|post|put|patch|delete|route)",
    re.IGNORECASE,
)
_WORKER_DECORATOR_RE = re.compile(
    r"(?:^|\.)(?:shared_task|periodic_task|task|cron|scheduled|on_message|consumer|subscriber|subscribe)\b"
    r"|celery|\brq\b",
    re.IGNORECASE,
)
_TOOL_DECORATOR_RE = re.compile(
    r"(?:^|\.)(?:tool|register_tool|function_tool|command)\b",
    re.IGNORECASE,
)
_TEST_FILE_RE = re.compile(r"(?:^test_|_test$|\.test$|\.spec$)", re.IGNORECASE)
_MAIN_GUARD_RE = re.compile(r"""__name__\s*==\s*['"]__main__['"]""")
_SCRIPT_DIR_PARTS = frozenset({"bin", "scripts", "cmd"})
_TEST_DIR_PARTS = frozenset({"test", "tests", "__tests__", "spec", "specs"})


def _decorators_above(repo: CodeIndex, symbol: Symbol, source_cache: dict[Path, str]) -> list[str]:
    """Return decorator text (without the leading @) on the lines above a def."""
    source = source_cache.get(symbol.path)
    if source is None:
        source = repo.storage.file_source(repo.root, symbol.path) or ""
        source_cache[symbol.path] = source
    lines = source.splitlines()
    decorators: list[str] = []
    index = symbol.range.start.line - 1
    while index >= 0 and index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index -= 1
            continue
        if stripped.startswith("@"):
            decorators.append(stripped[1:])
            index -= 1
            continue
        break
    return decorators


def _is_test_symbol(symbol: Symbol) -> bool:
    if any(part.lower() in _TEST_DIR_PARTS for part in symbol.path.parts):
        return True
    if _TEST_FILE_RE.search(symbol.path.stem):
        return True
    return symbol.name.startswith("test_") or symbol.name.lower().startswith("test")


def _is_script_symbol(repo: CodeIndex, symbol: Symbol, source_cache: dict[Path, str]) -> bool:
    if any(part.lower() in _SCRIPT_DIR_PARTS for part in symbol.path.parts):
        return True
    if symbol.container is None and symbol.name == "main":
        source = source_cache.get(symbol.path)
        if source is None:
            source = repo.storage.file_source(repo.root, symbol.path) or ""
            source_cache[symbol.path] = source
        if _MAIN_GUARD_RE.search(source):
            return True
    return False


def _entry_type(repo: CodeIndex, symbol: Symbol, *, source_cache: dict[Path, str]) -> str | None:
    if _is_test_symbol(symbol):
        return "test"
    decorators = _decorators_above(repo, symbol, source_cache)
    if any(_HTTP_ROUTE_DECORATOR_RE.search(dec) for dec in decorators):
        return "http_route"
    if any(_WORKER_DECORATOR_RE.search(dec) for dec in decorators):
        return "worker"
    if any(_TOOL_DECORATOR_RE.search(dec) for dec in decorators):
        return "tool"
    if _is_script_symbol(repo, symbol, source_cache):
        return "script"
    return None


def _direct_callers(repo: CodeIndex, symbol: Symbol, *, limit: int) -> tuple[Symbol, ...]:
    references = _references_for_inspect(repo, symbol, limit=limit, ref_kinds=frozenset({"call"}))
    return _callers_for_symbol(repo, symbol, references, limit=limit)


def _direct_callees(repo: CodeIndex, symbol: Symbol, *, limit: int, loose: bool = False) -> tuple[Symbol, ...]:
    source_range = _definition_range(repo, symbol) or symbol.range
    return _callees_for_symbol(repo, symbol, source_range, limit=limit, ref_kinds=frozenset({"call"}), loose=loose)


def _clamp_depth(depth: int) -> int:
    if depth < 1:
        return 1
    return min(depth, MAX_CALL_DEPTH)


def _build_call_graph(
    repo: CodeIndex,
    symbol: Symbol,
    *,
    direction: str,
    depth: int,
    limit: int,
    loose: bool = False,
    max_nodes: int = MAX_CALL_GRAPH_NODES,
) -> CallGraph:
    def step(current: Symbol) -> tuple[Symbol, ...]:
        if direction == "callers":
            return _direct_callers(repo, current, limit=limit)
        return _direct_callees(repo, current, limit=limit, loose=loose)

    source_cache: dict[Path, str] = {}

    visited: dict[str, int] = {symbol.id: 0}
    parent: dict[str, Symbol] = {}
    symbol_by_id: dict[str, Symbol] = {symbol.id: symbol}
    children_ids: dict[str, list[str]] = {}
    truncated = False

    frontier = [symbol]
    for level in range(1, depth + 1):
        next_frontier: list[Symbol] = []
        for current in frontier:
            for neighbour in step(current):
                if neighbour.id == symbol.id:
                    continue
                if neighbour.id not in visited:
                    if len(visited) > max_nodes:
                        truncated = True
                        break
                    visited[neighbour.id] = level
                    parent[neighbour.id] = current
                    symbol_by_id[neighbour.id] = neighbour
                    next_frontier.append(neighbour)
                    children_ids.setdefault(current.id, []).append(neighbour.id)
                elif neighbour.id != current.id and neighbour.id not in children_ids.get(current.id, ()):
                    # Edge to an already-seen node (cross-link / shallower depth).
                    children_ids.setdefault(current.id, []).append(neighbour.id)
            if truncated:
                break
        if truncated or not next_frontier:
            break
        frontier = next_frontier

    entry_cache: dict[str, str | None] = {}

    def entry_of(node_symbol: Symbol) -> str | None:
        if node_symbol.id not in entry_cache:
            entry_cache[node_symbol.id] = _entry_type(repo, node_symbol, source_cache=source_cache)
        return entry_cache[node_symbol.id]

    def build_node(node_id: str, building: frozenset[str]) -> CallNode:
        node_symbol = symbol_by_id[node_id]
        child_nodes: list[CallNode] = []
        for child_id in children_ids.get(node_id, ()):
            # Only nest children first discovered through this node, and break
            # cycles in the rendered tree.
            discovery_parent = parent.get(child_id)
            if discovery_parent is not None and discovery_parent.id == node_id and child_id not in building:
                child_nodes.append(build_node(child_id, building | {node_id}))
        return CallNode(
            symbol=node_symbol,
            depth=visited[node_id],
            entry_type=entry_of(node_symbol),
            children=tuple(child_nodes),
        )

    roots = tuple(build_node(cid, frozenset({symbol.id})) for cid in children_ids.get(symbol.id, ()))

    entry_points: list[EntryPoint] = []
    if direction == "callers":
        for node_id, node_symbol in symbol_by_id.items():
            if node_id == symbol.id:
                continue
            kind = entry_of(node_symbol)
            if kind is None:
                continue
            entry_points.append(
                EntryPoint(entry_type=kind, symbol=node_symbol, path=_reconstruct_path(node_id, symbol, parent, symbol_by_id))
            )
        entry_points.sort(key=lambda item: (ENTRY_TYPES.index(item.entry_type) if item.entry_type in ENTRY_TYPES else 99, item.symbol.path.as_posix(), item.symbol.range.start.line))

    return CallGraph(
        target=symbol,
        direction=direction,
        depth=depth,
        roots=roots,
        entry_points=tuple(entry_points),
        truncated=truncated,
    )


def _reconstruct_path(
    node_id: str, target: Symbol, parent: dict[str, Symbol], symbol_by_id: dict[str, Symbol]
) -> tuple[Symbol, ...]:
    """Path from the entry symbol down to the target (entry first)."""
    chain = [symbol_by_id[node_id]]
    current_id = node_id
    while current_id in parent:
        parent_symbol = parent[current_id]
        chain.append(parent_symbol)
        current_id = parent_symbol.id
        if len(chain) > MAX_CALL_DEPTH + 1:
            break
    return tuple(chain)


def _callers_for_symbol(
    repo: CodeIndex,
    symbol: Symbol,
    references: tuple[Reference, ...],
    *,
    limit: int,
) -> tuple[Symbol, ...]:
    if limit <= 0:
        return ()
    file_symbols_cache: dict[Path, list[Symbol]] = {}
    def_ranges_cache: dict[Path, dict[str, Range]] = {}
    callers: list[Symbol] = []
    seen: set[str] = set()
    for reference in references:
        path = reference.path
        if path not in file_symbols_cache:
            file_symbols = repo.storage.symbols_in_file(path)
            file_symbols_cache[path] = file_symbols
            def_ranges_cache[path] = _definition_ranges_for_symbols(repo, path, file_symbols)
        caller = _enclosing_symbol(file_symbols_cache[path], def_ranges_cache[path], reference.range, exclude_id=symbol.id)
        if caller is None or caller.id in seen:
            continue
        callers.append(caller)
        seen.add(caller.id)
        if len(callers) >= limit:
            break
    return tuple(callers)


def _callees_for_symbol(
    repo: CodeIndex,
    symbol: Symbol,
    range_: Range,
    *,
    limit: int,
    ref_kinds: frozenset[str] | None = None,
    loose: bool = False,
) -> tuple[Symbol, ...]:
    if limit <= 0:
        return ()
    indexed = _parse_file(repo.root, symbol.path, repo.languages)
    if indexed is None:
        return ()
    known = repo.storage.symbol_names_by_language().get(symbol.language, set())
    definition_spans = {(candidate.range.start_byte, candidate.range.end_byte) for candidate in indexed.symbols}
    names: list[str] = []
    seen_names: set[str] = set()
    for reference in indexed.references:
        if reference.range.start.line < range_.start.line or reference.range.start.line >= range_.end.line + 1:
            continue
        if ref_kinds is not None and reference.reference_kind not in ref_kinds:
            continue
        if (reference.range.start_byte, reference.range.end_byte) in definition_spans:
            continue
        if reference.name == symbol.name or reference.name not in known or reference.name in seen_names:
            continue
        names.append(reference.name)
        seen_names.add(reference.name)
        if len(names) >= limit:
            break
    callees: list[Symbol] = []
    seen_ids: set[str] = set()
    for name in names:
        candidate = _resolve_callee(repo, name, symbol, loose=loose)
        if candidate is not None and candidate.id not in seen_ids:
            callees.append(candidate)
            seen_ids.add(candidate.id)
    return tuple(callees)


def _resolve_callee(repo: CodeIndex, name: str, symbol: Symbol, *, loose: bool) -> Symbol | None:
    """Resolve a called name to a callable symbol, preferring locality.

    Prefers a unique callable in the same file, then the same package
    (directory), then a unique match anywhere. Cross-module ambiguous names are
    dropped unless ``loose`` is set, in which case the first global match wins.
    """
    language = symbol.language

    def exact(path: str | Path | None, fetch: int) -> list[Symbol]:
        matches = repo.search_symbols(
            name, language=language, path=path, kind=CALLEE_KINDS, exact_only=True, limit=fetch
        )
        return [candidate for candidate in matches if candidate.id != symbol.id]

    same_file = exact(symbol.path, 3)
    if same_file:
        return same_file[0]

    package = symbol.path.parent
    if package != Path("."):
        same_package = [c for c in exact(package, 5) if c.path.parent == package]
        if len(same_package) == 1:
            return same_package[0]

    global_matches = exact(None, 2)
    if len(global_matches) == 1:
        return global_matches[0]
    if loose and global_matches:
        return global_matches[0]
    return None


def _enclosing_symbol(
    symbols: list[Symbol], def_ranges: dict[str, Range], range_: Range, *, exclude_id: str
) -> Symbol | None:
    candidates: list[tuple[Symbol, Range]] = []
    for symbol in symbols:
        if symbol.id == exclude_id:
            continue
        body = def_ranges.get(symbol.id, symbol.range)
        if body.start.line <= range_.start.line <= body.end.line:
            candidates.append((symbol, body))
    if not candidates:
        return None
    # Innermost enclosing definition: deepest start, then tightest end.
    return max(candidates, key=lambda item: (item[1].start.line, -item[1].end.line))[0]


def _search_page(
    repo: CodeIndex,
    query: str | Iterable[str],
    *,
    kind: str | Iterable[str] | None,
    language: str | None,
    path: str | Path | Iterable[str | Path] | None,
    exact_only: bool,
    limit: int,
) -> Page:
    queries = _coerce_queries(query)
    if not queries or limit <= 0:
        return Page(items=(), limit=limit, offset=0, has_more=False)

    seen: dict[str, tuple[Symbol, tuple[int, int, int, str, int]]] = {}
    for query_index, item in enumerate(queries):
        candidates = repo.search_symbols(
            item,
            kind=kind,
            language=language,
            path=path,
            exact_only=exact_only,
            limit=limit + 1,
        )
        for result_index, symbol in enumerate(candidates):
            score_rank = {"exact": 0, "prefix": 1}.get(_match_score(item, symbol), 2)
            rank = (score_rank, query_index, len(symbol.name), symbol.path.as_posix(), result_index)
            existing = seen.get(symbol.id)
            if existing is None or rank < existing[1]:
                seen[symbol.id] = (symbol, rank)

    ranked = sorted(seen.values(), key=lambda item: item[1])
    return _page_from_extra([symbol for symbol, _ in ranked], limit=limit, offset=0)


def _definition_range(repo: CodeIndex, symbol: Symbol) -> Range | None:
    return _definition_ranges_for_symbols(repo, symbol.path, (symbol,)).get(symbol.id)


def _definition_ranges_for_symbols(
    repo: CodeIndex,
    path: Path,
    symbols: Iterable[Symbol],
    *,
    source: str | None = None,
) -> dict[str, Range]:
    symbols = tuple(symbols)
    if not symbols:
        return {}
    if source is None:
        source = repo.storage.file_source(repo.root, path)
    if source is None:
        return {}
    spec = _spec_for_path(path, repo.languages)
    if spec is None:
        return {}
    source_bytes = source.encode("utf-8")

    wanted: dict[tuple[str, int], Symbol] = {
        (symbol.kind, symbol.range.start_byte): symbol
        for symbol in symbols
    }
    ranges: dict[str, Range] = {}
    tree = _parse_source(_parser_for_language(spec.name), source)
    root_node = tree.root_node() if callable(tree.root_node) else tree.root_node

    def walk(node: Node) -> None:
        if len(ranges) >= len(wanted):
            return
        kind = spec.definitions.get(_node_kind(node))
        if kind is not None:
            name_node = _name_node(node, spec)
            if name_node is not None:
                symbol = wanted.get((kind, _node_start_byte(name_node)))
                if symbol is not None:
                    ranges[symbol.id] = _node_range(source_bytes, node)
        for child in _node_children(node):
            walk(child)

    walk(root_node)
    return ranges


def _format_symbol_fields(symbol: Symbol, *, indent: int, range_: Range | None = None) -> list[str]:
    display_range = range_ or symbol.range
    prefix = " " * indent
    return [
        f"{prefix}id: {_text_symbol_id(symbol, display_range)}",
        f"{prefix}name: {symbol.name}",
        f"{prefix}kind: {symbol.kind}",
        f"{prefix}file: {symbol.path.as_posix()}",
        f"{prefix}range: {_line_range(display_range)}",
        f"{prefix}signature: {symbol.signature}",
    ]


def _format_relation_item(repo: CodeIndex, symbol: Symbol, *, indent: int) -> list[str]:
    range_ = _definition_range(repo, symbol) or symbol.range
    prefix = " " * indent
    lines = [f"{prefix}- id: {_text_symbol_id(symbol, range_)}"]
    lines.extend(_format_symbol_fields(symbol, indent=indent + 2, range_=range_)[1:])
    return lines


def _format_reference_item(reference: Reference, *, indent: int) -> list[str]:
    prefix = " " * indent
    return [
        f"{prefix}- id: {_text_reference_id(reference)}",
        f"{prefix}  file: {reference.path.as_posix()}",
        f"{prefix}  range: {_line_range(reference.range)}",
        f"{prefix}  kind: {reference.reference_kind}",
        f"{prefix}  context: {reference.context}",
    ]


def _reference_kind_counts(references: tuple[Reference, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reference in references:
        counts[reference.reference_kind] = counts.get(reference.reference_kind, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _reference_kind_breakdown(references: tuple[Reference, ...]) -> str:
    counts = _reference_kind_counts(references)
    if not counts:
        return "{}"
    return ", ".join(f"{kind}={count}" for kind, count in counts.items())


def _format_relation_section(repo: CodeIndex, name: str, items: tuple[Any, ...], limit: int) -> list[str]:
    lines = [f"{name}:"]
    if not items:
        lines.append("  []")
        return lines
    for item in items[:limit]:
        if isinstance(item, Reference):
            lines.extend(_format_reference_item(item, indent=2))
        else:
            lines.extend(_format_relation_item(repo, item, indent=2))
    return lines


def _format_summary_section(
    imports: tuple[ImportItem, ...],
    members: tuple[Symbol, ...],
    callers: tuple[Symbol, ...],
    callees: tuple[Symbol, ...],
    references: tuple[Reference, ...],
    implementors: tuple[Symbol, ...],
) -> list[str]:
    return [
        "summary:",
        f"  imports: {len(imports)}",
        f"  members: {len(members)}",
        f"  callers: {len(callers)}",
        f"  callees: {len(callees)}",
        f"  references: {len(references)}",
        f"  reference_kinds: {_reference_kind_breakdown(references)}",
        f"  implementors: {len(implementors)}",
    ]


def _format_import_section(imports: tuple[ImportItem, ...]) -> list[str]:
    lines = ["imports:"]
    if not imports:
        lines.append("  []")
        return lines
    for import_item in imports:
        lines.append(f"  - range: {_line_range(import_item.range)}")
        lines.append(f"    statement: {import_item.statement}")
    return lines


def _format_page_text(repo: CodeIndex, name: str, page: Page) -> str:
    lines = [f"{name}:", f"  limit: {page.limit}", f"  offset: {page.offset}", f"  has_more: {_text_bool(page.has_more)}"]
    if page.next_offset is not None:
        lines.append(f"  next_offset: {page.next_offset}")
    lines.append("  items:")
    if not page.items:
        lines.append("    []")
    else:
        for item in page.items:
            if isinstance(item, Reference):
                lines.extend(_format_reference_item(item, indent=4))
            else:
                lines.extend(_format_relation_item(repo, item, indent=4))
    return "\n".join(lines) + "\n"


def _symbol_location(symbol: Symbol) -> str:
    return f"{symbol.name}  {symbol.path.as_posix()}:{symbol.range.start.line}"


def _format_call_graph_text(repo: CodeIndex, graph: CallGraph) -> str:
    target_range = _definition_range(repo, graph.target) or graph.target.range
    lines = [
        "target:",
        f"  id: {_text_symbol_id(graph.target, target_range)}",
        f"  name: {graph.target.name}",
        f"  kind: {graph.target.kind}",
        f"  file: {graph.target.path.as_posix()}",
        f"  range: {_line_range(target_range)}",
        f"direction: {graph.direction}",
        f"depth: {graph.depth}",
        f"confidence: {graph.confidence}",
        f"truncated: {_text_bool(graph.truncated)}",
    ]

    if graph.direction == "callers":
        lines.append("entry_points:")
        if not graph.entry_points:
            lines.append("  []")
        else:
            grouped: dict[str, list[EntryPoint]] = {}
            for entry in graph.entry_points:
                grouped.setdefault(entry.entry_type, []).append(entry)
            for entry_type in ENTRY_TYPES:
                bucket = grouped.get(entry_type)
                if not bucket:
                    continue
                lines.append(f"  {entry_type}:")
                for entry in bucket:
                    lines.append(f"    - {_symbol_location(entry.symbol)}")
                    path_text = " -> ".join(item.name for item in entry.path)
                    lines.append(f"        path: {path_text}")

    lines.append(f"{graph.direction}:")
    if not graph.roots:
        lines.append("  []")
    else:
        for node in graph.roots:
            _append_call_node_lines(node, lines, indent=2)
    return "\n".join(lines) + "\n"


def _append_call_node_lines(node: CallNode, lines: list[str], *, indent: int) -> None:
    prefix = " " * indent
    tag = f"  [{node.entry_type}]" if node.entry_type else ""
    lines.append(f"{prefix}- {_symbol_location(node.symbol)}{tag}")
    for child in node.children:
        _append_call_node_lines(child, lines, indent=indent + 4)


def _format_search_text(repo: CodeIndex, query: str | Iterable[str], page: Page) -> str:
    queries = _coerce_queries(query)
    if len(queries) == 1:
        lines = [f"query: {queries[0]}"]
    else:
        lines = ["queries:"]
        lines.extend(f"  - {item}" for item in queries)
    symbols = tuple(item for item in page.items if isinstance(item, Symbol))
    lines.extend([
        f"count: {len(symbols)}",
        f"limit: {page.limit}",
        f"has_more: {_text_bool(page.has_more)}",
        "",
        "symbols:",
    ])
    if not symbols:
        lines.append("  []")
        return "\n".join(lines) + "\n"
    for symbol in symbols:
        range_ = _definition_range(repo, symbol) or symbol.range
        lines.append(f"  - id: {_text_symbol_id(symbol, range_)}")
        lines.append(f"    name: {symbol.name}")
        lines.append(f"    kind: {symbol.kind}")
        lines.append(f"    file: {symbol.path.as_posix()}")
        lines.append(f"    range: {_line_range(range_)}")
        lines.append(f"    signature: {symbol.signature}")
        matched_query = _matched_query(queries, symbol)
        lines.append(f"    score: {_match_score(matched_query, symbol)}")
        if len(queries) > 1:
            lines.append(f"    matched_query: {matched_query}")
        if symbol.language:
            lines.append(f"    language: {symbol.language}")
        if symbol.container:
            lines.append(f"    container: {symbol.container}")
    return "\n".join(lines) + "\n"


def _format_outline_text(repo: CodeIndex, path: Path, page: Page, *, symbol: str | None = None) -> str:
    source = repo.storage.file_source(repo.root, path)
    total_lines = len(source.splitlines()) if source is not None else 0
    symbols = tuple(item for item in page.items if isinstance(item, Symbol))
    lines = [
        f"file: {path.as_posix()}",
        f"range: 0:{total_lines}",
        f"count: {len(symbols)}",
    ]
    if symbol is not None:
        lines.append(f"symbol: {symbol}")
    if page.has_more:
        lines.append(f"has_more: true")
        lines.append(f"limit: {page.limit}")
    lines.extend(["", "outline:"])
    if not symbols:
        lines.append("  []")
        return "\n".join(lines) + "\n"

    outline_items = []
    definition_ranges = _definition_ranges_for_symbols(repo, path, symbols, source=source)
    for symbol in symbols:
        definition_range = definition_ranges.get(symbol.id)
        outline_items.append((symbol, definition_range or symbol.range, definition_range))
    range_width = max(len(_line_range(range_)) for _, range_, _ in outline_items)
    for symbol, range_, definition_range in outline_items:
        line_range = _line_range(range_)
        lines.append(f"{line_range:<{range_width}} | {_outline_signature(symbol, definition_range)}")
    return "\n".join(lines) + "\n"


def _outline_signature(symbol: Symbol, definition_range: Range | None) -> str:
    signature = symbol.signature.lstrip()
    if definition_range is None:
        return signature[:240].rstrip()
    return f"{' ' * definition_range.start.column}{signature}"[:240].rstrip()


def _index_status(
    *,
    root: Path,
    languages: Iterable[str] | None,
    include: Iterable[str],
    exclude: Iterable[str],
    db_path: Path | None,
    check: bool,
    max_pending_files: int,
) -> IndexStatus:
    index_path = db_path or root / DEFAULT_INDEX_DIR / DEFAULT_INDEX_DB
    if not index_path.exists():
        return IndexStatus(
            status="missing",
            root=root,
            reason="index not initialized",
        )

    try:
        data = _read_index_metadata(index_path)
        pending_files: tuple[str, ...] = ()
        if check:
            pending_changes, pending_files = _pending_index_changes(
                root=root,
                languages=languages,
                include=include,
                exclude=exclude,
                indexed_files=data["indexed_files"],
                max_files=max_pending_files,
            )
        else:
            pending_changes = "unknown"
    except Exception as exc:
        return IndexStatus(
            status="error",
            root=root,
            message=str(exc),
        )

    schema_version = data["schema_version"]
    is_schema_stale = schema_version != SCHEMA_VERSION
    is_stale = is_schema_stale or isinstance(pending_changes, int) and pending_changes > 0
    if is_schema_stale:
        reason = "index schema is out of date"
    elif isinstance(pending_changes, int) and pending_changes > 0:
        reason = "files changed after last index update"
    else:
        reason = None
    return IndexStatus(
        status="stale" if is_stale else "ready",
        root=root,
        files=data["files"],
        symbols=data["symbols"],
        languages=data["languages"],
        language_breakdown=data["language_breakdown"],
        updated_at=data["updated_at"],
        pending_changes=pending_changes,
        pending_files=pending_files,
        reason=reason,
    )


def _read_index_metadata(db_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        schema_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()
        updated_at_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'updated_at'",
        ).fetchone()
        files = connection.execute("SELECT count(*) FROM files").fetchone()[0]
        symbols = connection.execute("SELECT count(*) FROM symbols").fetchone()[0]
        languages = tuple(
            row["language"]
            for row in connection.execute(
                "SELECT DISTINCT language FROM files ORDER BY language",
            ).fetchall()
        )
        language_counts = connection.execute(
            """
            SELECT language, count(*) AS files
            FROM files
            GROUP BY language
            ORDER BY language
            """
        ).fetchall()
        indexed_rows = connection.execute(
            "SELECT path, language, mtime_ns, size FROM files",
        ).fetchall()
    finally:
        connection.close()

    return {
        "schema_version": int(schema_row["value"]) if schema_row is not None else None,
        "updated_at": updated_at_row["value"] if updated_at_row is not None else None,
        "files": files,
        "symbols": symbols,
        "languages": languages,
        "language_breakdown": _language_breakdown(language_counts, files),
        "indexed_files": {row["path"]: (row["language"], row["mtime_ns"], row["size"]) for row in indexed_rows},
    }


def _pending_index_changes(
    *,
    root: Path,
    languages: Iterable[str] | None,
    include: Iterable[str],
    exclude: Iterable[str],
    indexed_files: dict[str, tuple[str, int, int]],
    max_files: int,
) -> tuple[int, tuple[str, ...]]:
    language_filter = set(languages) if languages is not None else None
    filtered_indexed_files = {
        path: (mtime_ns, size)
        for path, (language, mtime_ns, size) in indexed_files.items()
        if language_filter is None or language in language_filter
    }
    scanner = CodeIndex(
        root,
        languages=languages,
        include=include,
        exclude=exclude,
        db_path=":memory:",
    )
    current_files: dict[str, tuple[int, int]] = {}
    for path in scanner._iter_indexable_files():
        try:
            stat = (root / path).stat()
        except OSError:
            continue
        current_files[path.as_posix()] = (stat.st_mtime_ns, stat.st_size)

    pending = 0
    pending_files: list[str] = []
    for path_text, stat_info in current_files.items():
        if filtered_indexed_files.get(path_text) != stat_info:
            pending += 1
            if len(pending_files) < max_files:
                pending_files.append(path_text)
    for path_text in filtered_indexed_files:
        if path_text not in current_files:
            pending += 1
            if len(pending_files) < max_files:
                pending_files.append(path_text)
    return pending, tuple(pending_files)


def _language_breakdown(rows: list[sqlite3.Row], total_files: int) -> tuple[dict[str, Any], ...]:
    if total_files <= 0:
        return ()
    return tuple(
        {
            "language": row["language"],
            "files": row["files"],
            "percent": round(row["files"] * 100 / total_files, 1),
        }
        for row in rows
    )


def _format_status_text(index_status: IndexStatus) -> str:
    lines = ["index:", f"  status: {index_status.status}", f"  root: {index_status.root}"]
    if index_status.files is not None:
        lines.append(f"  files: {index_status.files}")
    if index_status.symbols is not None:
        lines.append(f"  symbols: {index_status.symbols}")
    if index_status.languages:
        lines.append(f"  languages: {', '.join(index_status.languages)}")
    if index_status.language_breakdown:
        lines.append("  language_breakdown:")
        for item in index_status.language_breakdown:
            lines.append(f"    - {item['language']}: {item['files']} files ({item['percent']}%)")
    if index_status.updated_at is not None:
        lines.append(f"  updated_at: {index_status.updated_at}")
    if index_status.pending_changes is not None:
        lines.append(f"  pending_changes: {index_status.pending_changes}")
    if index_status.pending_files:
        lines.append("  pending_files:")
        for path in index_status.pending_files:
            lines.append(f"    - {path}")
    if index_status.reason:
        lines.append(f"  reason: {index_status.reason}")
    if index_status.message:
        lines.append(f"  message: {index_status.message}")
    return "\n".join(lines) + "\n"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _match_score(query: str, symbol: Symbol) -> str:
    normalized_query = query.lower()
    normalized_name = symbol.name.lower()
    if normalized_name == normalized_query:
        return "exact"
    if normalized_name.startswith(normalized_query):
        return "prefix"
    return "fuzzy"


def _matched_query(queries: tuple[str, ...], symbol: Symbol) -> str:
    if not queries:
        return ""
    return min(
        queries,
        key=lambda item: (
            {"exact": 0, "prefix": 1}.get(_match_score(item, symbol), 2),
            queries.index(item),
        ),
    )


def _format_source_block(source: str, range_: Range, max_source_chars: int, *, anchors: bool = False, anchor_format: str = "legacy") -> list[str]:
    anchor_format = _validate_anchor_format(anchor_format)
    start, end, shown_end, total_lines, shown_lines, status = _source_excerpt(source, range_, max_source_chars)
    lines = [
        "source:",
        f"  status: {status}",
        f"  range: {start}:{end}",
        f"  shown_range: {start}:{shown_end}",
        f"  total_lines: {total_lines}",
    ]
    if anchors:
        note = (
            "Use anchor=line:hash as edit anchor; hash = hash(line_content)."
            if anchor_format == "explicit"
            else "Use line:hash as edit anchor; code starts after |"
        )
        lines.append(f"  note: {note}")
    lines.append("")

    for line_number, line in enumerate(shown_lines, start=start):
        if anchors:
            anchor = f"{line_number}:{_hash_line(line)}"
            lines.append(f"anchor={anchor} | {line}" if anchor_format == "explicit" else f"{anchor}|{line}")
        else:
            lines.append(f"  {line_number} |{line}")
    if status == "truncated":
        lines.extend(_format_chunks(start, end, shown_end))
    return lines


def _source_anchor(path: Path, source: str, range_: Range, max_source_chars: int) -> SourceAnchor:
    start, _end, shown_end, _total_lines, shown_lines, _status = _source_excerpt(source, range_, max_source_chars)
    hash_lines = tuple(
        HashLine(line=line_number, hash=_hash_line(line), text=line)
        for line_number, line in enumerate(shown_lines, start=start)
    )
    return SourceAnchor(
        path=path,
        start_line=start,
        end_line=shown_end,
        start_anchor=_anchor_for_line(hash_lines[0]) if hash_lines else None,
        end_anchor=_anchor_for_line(hash_lines[-1]) if hash_lines else None,
        lines=hash_lines,
    )


def _source_excerpt(source: str, range_: Range, max_source_chars: int) -> tuple[int, int, int, int, list[str], str]:
    all_lines = source.splitlines()
    start = range_.start.line
    end = min(range_.end.line + 1, len(all_lines))
    symbol_lines = all_lines[start:end]
    total_lines = len(symbol_lines)
    shown_lines: list[str] = []
    used = 0
    for line in symbol_lines:
        line_cost = len(line) + 16
        if used + line_cost > max_source_chars:
            break
        shown_lines.append(line)
        used += line_cost
    shown_end = start + len(shown_lines)
    status = "full" if len(shown_lines) == total_lines else "truncated"
    return start, end, shown_end, total_lines, shown_lines, status


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:HASHLINE_HASH_CHARS]


def _anchor_for_line(line: HashLine) -> str:
    return f"{line.line}:{line.hash}"


def _format_chunks(start: int, end: int, shown_end: int) -> list[str]:
    labels = ("setup", "validation", "main loop", "error handling", "formatting")
    remaining_start = max(shown_end, start)
    if remaining_start >= end:
        return []
    span = max(end - remaining_start, 1)
    chunk_count = min(len(labels), span)
    chunk_size = max((span + chunk_count - 1) // chunk_count, 1)
    lines = ["", "  chunks:"]
    cursor = remaining_start
    for index in range(chunk_count):
        chunk_start = cursor
        chunk_end = min(end, chunk_start + chunk_size)
        if chunk_start >= chunk_end:
            break
        lines.append(f"    - range: {chunk_start}:{chunk_end}")
        lines.append(f"      label: {labels[index]}")
        cursor = chunk_end
    return lines


def _line_range(range_: Range) -> str:
    return f"{range_.start.line}:{range_.end.line + 1}"


def _text_symbol_id(symbol: Symbol, range_: Range) -> str:
    return f"{symbol.language}:{symbol.kind}:{symbol.name}:{symbol.path.as_posix()}:{_line_range(range_)}"


def _text_reference_id(reference: Reference) -> str:
    return f"{reference.language}:reference:{reference.path.as_posix()}:{_line_range(reference.range)}"


def _bounded_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\ntruncated:\n  reason: max_total_chars\n"
    return text[: max(max_chars - len(suffix), 0)].rstrip() + suffix


def _text_bool(value: bool) -> str:
    return "true" if value else "false"


def _looks_like_symbol_name(name: str) -> bool:
    return "\n" not in name and len(name) <= 256


def _symbol_id(language: str, path: Path, kind: str, name: str, start_byte: int) -> str:
    return f"{language}:{path.as_posix()}:{kind}:{name}:{start_byte}"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _escape_fts_query(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _validate_pagination(*, limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if offset < 0:
        raise ValueError("offset must be >= 0")


def _page_from_extra(items: list[Any], *, limit: int, offset: int) -> Page:
    has_more = len(items) > limit
    return Page(
        items=tuple(items[:limit]),
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
    )


def _chunks(rows: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _delete_paths_chunked(
    connection: sqlite3.Connection,
    paths: list[Path],
    *,
    include_fts: bool,
) -> None:
    path_rows = [(path.as_posix(),) for path in paths]
    for chunk in _chunks(path_rows, SQLITE_BATCH_SIZE):
        placeholders = ",".join("?" for _ in chunk)
        values = [row[0] for row in chunk]
        if include_fts:
            connection.execute(f"DELETE FROM symbol_fts WHERE path IN ({placeholders})", values)
        connection.execute(f"DELETE FROM refs WHERE path IN ({placeholders})", values)
        connection.execute(f"DELETE FROM symbols WHERE path IN ({placeholders})", values)
        connection.execute(f"DELETE FROM files WHERE path IN ({placeholders})", values)


def _matches_path_pattern(path_text: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path_text, pattern):
        return True
    if pattern.endswith("/**"):
        directory = pattern[:-3].rstrip("/")
        return path_text == directory or path_text.startswith(f"{directory}/")
    return False


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple):
        return list(value)
    return value


def _to_jsonable(value: Any) -> Any:
    if isinstance(
        value,
        (Symbol, Reference, ImportItem, HashLine, SourceAnchor, Inspection, IndexStatus, Page, Position, Range, CallGraph, CallNode, EntryPoint),
    ):
        return _to_jsonable(asdict(value))
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(_to_jsonable(value), default=_json_default, ensure_ascii=False, indent=2))


def _print_cli_json(value: Any) -> None:
    print(json.dumps(_to_cli_jsonable(value), ensure_ascii=False, indent=2))


def _search_jsonable(page: Page, *, cli: bool = False) -> dict[str, Any]:
    convert = _to_cli_jsonable if cli else _to_jsonable
    return {
        "symbols": [convert(item) for item in page.items],
        "count": len(page.items),
        "limit": page.limit,
        "has_more": page.has_more,
    }


def _to_cli_jsonable(value: Any) -> Any:
    if isinstance(value, Page):
        return _readable_page(value)
    if isinstance(value, Symbol):
        return _readable_symbol(value)
    if isinstance(value, Reference):
        return _readable_reference(value)
    if isinstance(value, ImportItem):
        return _readable_import(value)
    if isinstance(value, HashLine):
        return _readable_hash_line(value)
    if isinstance(value, SourceAnchor):
        return _readable_source_anchor(value)
    if isinstance(value, Inspection):
        return _readable_inspection(value)
    if isinstance(value, CallGraph):
        return _readable_call_graph(value)
    if isinstance(value, list):
        return [_to_cli_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_cli_jsonable(item) for item in value]
    return _to_jsonable(value)


def _readable_page(page: Page) -> dict[str, Any]:
    return {
        "items": [_to_cli_jsonable(item) for item in page.items],
        "limit": page.limit,
        "offset": page.offset,
        "has_more": page.has_more,
        "next_offset": page.next_offset,
    }


def _readable_symbol(symbol: Symbol) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": symbol.name,
        "kind": symbol.kind,
        "language": symbol.language,
        "path": symbol.path.as_posix(),
        "line": symbol.range.start.line + 1,
        "column": symbol.range.start.column + 1,
    }
    if symbol.container:
        result["container"] = symbol.container
    if symbol.signature:
        result["signature"] = symbol.signature
    return result


def _readable_reference(reference: Reference) -> dict[str, Any]:
    return {
        "name": reference.name,
        "path": reference.path.as_posix(),
        "line": reference.range.start.line + 1,
        "column": reference.range.start.column + 1,
        "kind": reference.reference_kind,
        "context": reference.context,
    }


def _readable_import(import_item: ImportItem) -> dict[str, Any]:
    return {
        "path": import_item.path.as_posix(),
        "range": _line_range(import_item.range),
        "statement": import_item.statement,
    }


def _readable_hash_line(hash_line: HashLine) -> dict[str, Any]:
    return {
        "line": hash_line.line,
        "hash": hash_line.hash,
        "text": hash_line.text,
    }


def _readable_source_anchor(source_anchor: SourceAnchor) -> dict[str, Any]:
    return {
        "path": source_anchor.path.as_posix(),
        "start_line": source_anchor.start_line,
        "end_line": source_anchor.end_line,
        "start_anchor": source_anchor.start_anchor,
        "end_anchor": source_anchor.end_anchor,
        "lines": [_readable_hash_line(line) for line in source_anchor.lines],
    }


def _readable_inspection(inspection: Inspection) -> dict[str, Any]:
    result: dict[str, Any] = {
        "definition": _readable_symbol(inspection.definition),
    }
    if inspection.source_preview:
        result["source"] = inspection.source_preview
    if inspection.source_anchor is not None:
        result["source_anchor"] = _readable_source_anchor(inspection.source_anchor)
    result["imports"] = [_readable_import(import_item) for import_item in inspection.imports]
    result["references"] = [_readable_reference(reference) for reference in inspection.references]
    result["reference_kinds"] = _reference_kind_counts(inspection.references)
    result["references_has_more"] = inspection.references_has_more
    if inspection.references_next_offset is not None:
        result["references_next_offset"] = inspection.references_next_offset
    result["implementations"] = [_readable_symbol(symbol) for symbol in inspection.implementations]
    result["implementations_has_more"] = inspection.implementations_has_more
    if inspection.implementations_next_offset is not None:
        result["implementations_next_offset"] = inspection.implementations_next_offset
    return result


def _readable_call_node(node: CallNode) -> dict[str, Any]:
    result: dict[str, Any] = _readable_symbol(node.symbol)
    result["depth"] = node.depth
    result["entry_type"] = node.entry_type
    result["children"] = [_readable_call_node(child) for child in node.children]
    return result


def _readable_entry_point(entry: EntryPoint) -> dict[str, Any]:
    return {
        "entry_type": entry.entry_type,
        "symbol": _readable_symbol(entry.symbol),
        "path": [item.name for item in entry.path],
    }


def _readable_call_graph(graph: CallGraph) -> dict[str, Any]:
    return {
        "target": _readable_symbol(graph.target),
        "direction": graph.direction,
        "depth": graph.depth,
        "confidence": graph.confidence,
        "truncated": graph.truncated,
        "entry_points": [_readable_entry_point(entry) for entry in graph.entry_points],
        graph.direction: [_readable_call_node(node) for node in graph.roots],
    }


class _CliProgress:
    def __init__(self, stream: Any | None = None) -> None:
        self.stream = stream
        self.visible = False
        self.last_total = 0
        target = stream if stream is not None else sys.stderr
        isatty = getattr(target, "isatty", None)
        self.interactive = bool(isatty()) if callable(isatty) else False

    def __call__(
        self,
        event: str,
        *,
        done: int = 0,
        total: int = 0,
        path: str | None = None,
    ) -> None:
        if total:
            self.last_total = total
        stream = self.stream if self.stream is not None else sys.stderr

        # When stderr is captured (non-TTY), the live `\r` bar does not collapse
        # and floods the output, so suppress per-file updates and emit a single
        # summary line on finish instead.
        if not self.interactive:
            if event == "finish" and self.last_total > 0:
                stream.write(f"indexed {self.last_total} files\n")
                stream.flush()
            return

        if event == "scan":
            stream.write("\rscanning files...")
            stream.flush()
            self.visible = True
            return
        if event == "start":
            stream.write("\r" + _progress_line(done, total, label="indexing", unit="files"))
            stream.flush()
            self.visible = True
            return
        if event == "finish":
            if self.visible:
                stream.write("\n")
                stream.flush()
            self.visible = False
            return
        if event == "file":
            self.visible = True
            stream.write("\r" + _progress_line(done, total, label="indexing", unit="files"))
            stream.flush()


def _progress_line(done: int, total: int, *, label: str, unit: str) -> str:
    width = 24
    if total <= 0:
        return "index up to date"
    filled = round(width * done / total)
    bar = "#" * filled + "-" * (width - filled)
    percent = round(100 * done / total)
    return f"{label} [{bar}] {done}/{total} {unit} {percent}%"


def _add_index_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Codebase root. Defaults to the current directory.")
    parser.add_argument("--language", action="append", dest="languages", help="Language to include. Repeatable.")
    parser.add_argument("--include", action="append", default=(), help="Glob include pattern. Repeatable.")
    parser.add_argument("--exclude", action="append", default=(), help="Glob exclude pattern. Repeatable.")
    parser.add_argument("--db", help="SQLite index path. Defaults to .code-symbol-index/index.sqlite.")


def _add_match_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", help="Filter by symbol kind. Use comma-separated values for multiple kinds.")
    parser.add_argument("--path", action="append", help="Filter to a file or directory path. Repeatable.")
    parser.add_argument("--exact-only", action="store_true", help="Only return exact symbol-name matches.")
    parser.add_argument("--sync", action="store_true", help="Refresh the index before querying.")


def _add_page_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=_positive_int, default=DEFAULT_PAGE_LIMIT)
    parser.add_argument("--offset", type=_non_negative_int, default=0)


def _ref_kind_value(value: str) -> str:
    unknown = sorted(set(_coerce_filter_values(value)) - REFERENCE_KINDS)
    if unknown:
        valid = ", ".join(sorted(REFERENCE_KINDS))
        raise argparse.ArgumentTypeError(f"unknown reference kind(s): {', '.join(unknown)}; valid kinds: {valid}")
    return value


def _add_ref_kind_options(parser: argparse.ArgumentParser) -> None:
    valid = ", ".join(sorted(REFERENCE_KINDS))
    parser.add_argument(
        "--ref-kind",
        dest="ref_kind",
        type=_ref_kind_value,
        help=(
            "Filter references by behavior. Comma-separated subset of: "
            f"{valid}. Defaults to hiding import/attribute noise."
        ),
    )
    parser.add_argument(
        "--all-kinds",
        action="store_true",
        help="Show every reference kind, including imports and member-access noise.",
    )


def _ref_kinds_arg(args: argparse.Namespace) -> str | tuple[str, ...] | None:
    if getattr(args, "all_kinds", False):
        return "all"
    ref_kind = getattr(args, "ref_kind", None)
    if ref_kind:
        return ref_kind
    return _REF_KINDS_DEFAULT


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _search_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_SEARCH_LIMIT:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_SEARCH_LIMIT}")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _depth(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > MAX_CALL_DEPTH:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_CALL_DEPTH}")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-symbol-index")
    parser.add_argument("--version", action="version", version=f"code-symbol-index {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search symbols in a codebase.")
    _add_index_options(search)
    search.add_argument("query", nargs="+")
    _add_match_options(search)
    search.add_argument("--limit", type=_search_limit, default=DEFAULT_SEARCH_LIMIT)
    search.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    inspect = subparsers.add_parser("inspect", help="Inspect the best symbol match for a keyword.")
    _add_index_options(inspect)
    inspect.add_argument("query")
    _add_match_options(inspect)
    inspect.add_argument("--limit", type=_positive_int, default=DEFAULT_PAGE_LIMIT)
    inspect.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")
    inspect.add_argument("--anchors", action="store_true", help="Emit current-file line hashes for source snippets.")
    inspect.add_argument("--anchor-format", choices=ANCHOR_FORMATS, default="legacy", help="Format for --anchors source lines.")
    inspect.add_argument("--max-source-chars", type=_positive_int, default=DEFAULT_MAX_SOURCE_CHARS)
    inspect.add_argument("--max-total-chars", type=_positive_int, default=DEFAULT_MAX_TOTAL_CHARS)
    inspect.add_argument("--max-members", type=_non_negative_int, default=DEFAULT_MAX_MEMBERS)
    inspect.add_argument("--max-callers", type=_non_negative_int, default=DEFAULT_MAX_CALLERS)
    inspect.add_argument("--max-callees", type=_non_negative_int, default=DEFAULT_MAX_CALLEES)
    inspect.add_argument("--max-references", type=_non_negative_int, default=DEFAULT_MAX_REFERENCES)
    inspect.add_argument("--max-implementors", type=_non_negative_int, default=DEFAULT_MAX_IMPLEMENTORS)
    inspect.add_argument("--max-imports", type=_non_negative_int, default=DEFAULT_MAX_IMPORTS)
    _add_ref_kind_options(inspect)

    refs = subparsers.add_parser("refs", help="Find references for the best symbol match.")
    _add_index_options(refs)
    refs.add_argument("query")
    _add_match_options(refs)
    _add_page_options(refs)
    _add_ref_kind_options(refs)
    refs.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    impls = subparsers.add_parser("impls", help="Find implementation candidates for the best symbol match.")
    _add_index_options(impls)
    impls.add_argument("query")
    _add_match_options(impls)
    _add_page_options(impls)
    impls.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    for direction, help_text in (
        ("callers", "Walk the transitive callers of a symbol, grouped by entry type."),
        ("callees", "Walk the transitive callees of a symbol."),
    ):
        chain_parser = subparsers.add_parser(direction, help=help_text)
        _add_index_options(chain_parser)
        chain_parser.add_argument("query")
        _add_match_options(chain_parser)
        chain_parser.add_argument("--depth", type=_depth, default=DEFAULT_CALL_DEPTH, help=f"Traversal depth (1-{MAX_CALL_DEPTH}).")
        chain_parser.add_argument("--limit", type=_positive_int, default=DEFAULT_CALL_FANOUT, help="Max neighbours expanded per node.")
        if direction == "callees":
            chain_parser.add_argument(
                "--loose",
                action="store_true",
                help="Include ambiguous cross-module callee matches (lower precision).",
            )
        chain_parser.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    outline_parser = subparsers.add_parser("outline", help="Print an indexed file outline.")
    _add_index_options(outline_parser)
    outline_parser.add_argument("path")
    outline_parser.add_argument("--sync", action="store_true", help="Refresh the index before querying.")
    outline_parser.add_argument("--symbol", help="Show only the local outline for one class, function, or prefix.")
    outline_parser.add_argument("--max-symbols", type=_positive_int, default=DEFAULT_MAX_OUTLINE_SYMBOLS)
    outline_parser.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    status_parser = subparsers.add_parser("status", help="Print index status.")
    _add_index_options(status_parser)
    status_parser.add_argument("--check", action="store_true", help="Scan files to compute stale state and pending changes.")
    status_parser.add_argument("--max-pending-files", type=_non_negative_int, default=DEFAULT_MAX_PENDING_FILES)
    status_parser.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    index_parser = subparsers.add_parser("index", help="Refresh the on-disk code-symbol-index index.")
    _add_index_options(index_parser)

    update_parser = subparsers.add_parser("update", help="Incrementally update indexed files.")
    _add_index_options(update_parser)
    update_parser.add_argument("paths", nargs="+", help="File paths to refresh in the index.")

    clean_parser = subparsers.add_parser("clean", help="Delete the on-disk code-symbol-index index.")
    clean_parser.add_argument("--root", default=".", help="Codebase root. Defaults to the current directory.")

    install_skill_parser = subparsers.add_parser("install-skill", help="Install the code-symbol-index agent skill (Codex or Claude).")
    install_skill_parser.add_argument("--target", default="codex", choices=("codex", "claude"), help="Skill target agent. Defaults to codex.")
    install_skill_parser.add_argument("--codex-home", help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex.")
    install_skill_parser.add_argument("--claude-dir", help="Claude config directory. Defaults to $CLAUDE_CONFIG_DIR or ~/.claude.")
    install_skill_parser.add_argument("--force", action="store_true", help="Overwrite an existing skill.")

    languages = subparsers.add_parser("languages", help="Print configured languages with available parsers.")
    languages.set_defaults(command="languages")
    version = subparsers.add_parser("version", help="Print the code-symbol-index version.")
    version.set_defaults(command="version")
    return parser


def _inspect_options_from_args(args: argparse.Namespace) -> InspectOptions:
    return InspectOptions(
        max_source_chars=args.max_source_chars,
        max_total_chars=args.max_total_chars,
        max_members=args.max_members,
        max_callers=args.max_callers,
        max_callees=args.max_callees,
        max_references=args.max_references,
        max_implementors=args.max_implementors,
        max_imports=args.max_imports,
        ref_kinds=_ref_kinds_arg(args),
        anchor_format=args.anchor_format,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        commands = {
            "search",
            "inspect",
            "refs",
            "impls",
            "callers",
            "callees",
            "outline",
            "status",
            "index",
            "update",
            "clean",
            "install-skill",
            "languages",
            "version",
        }
        if raw_args and raw_args[0] not in commands and not raw_args[0].startswith("-"):
            raw_args.insert(0, "search")

        parser = build_arg_parser()
        args = parser.parse_args(raw_args)
        if args.command == "version":
            print(f"code-symbol-index {__version__}")
            return 0
        if args.command == "languages":
            _print_json(list(supported_languages()))
            return 0
        if args.command == "clean":
            clean(args.root)
            return 0
        if args.command == "install-skill":
            path = install_skill(target=args.target, codex_home=args.codex_home, claude_dir=args.claude_dir, force=args.force)
            print(f"installed {args.target} skill: {path}")
            return 0
        if args.command == "status":
            payload = _index_status(
                root=Path(args.root).resolve(),
                languages=args.languages,
                include=args.include,
                exclude=args.exclude,
                db_path=Path(args.db) if args.db is not None else None,
                check=args.check,
                max_pending_files=args.max_pending_files,
            )
            if args.json:
                _print_json(payload)
            else:
                print(_format_status_text(payload), end="")
            return 0

        repo = Repository(
            args.root,
            languages=args.languages,
            include=args.include,
            exclude=args.exclude,
            db_path=args.db,
            progress=_CliProgress(),
            create_index=args.command == "index",
        )
        language = args.languages[0] if args.languages and len(args.languages) == 1 else None
        if getattr(args, "sync", False):
            repo.refresh()

        if args.command == "index":
            repo.refresh()
            _print_json({"index": str(Path(repo.storage.db_path)), "root": str(repo.root)})
        elif args.command == "update":
            repo.update(args.paths)
            _print_json(
                {
                    "index": str(Path(repo.storage.db_path)),
                    "root": str(repo.root),
                    "updated": [Path(path).as_posix() for path in args.paths],
                }
            )
        elif args.command == "search":
            page = repo.search_page(
                args.query,
                kind=args.kind,
                language=language,
                path=args.path,
                exact_only=args.exact_only,
                limit=args.limit,
            )
            if args.json:
                _print_json(_search_jsonable(page, cli=True))
            else:
                print(_format_search_text(repo, args.query, page), end="")
        elif args.command == "inspect":
            if args.json:
                _print_cli_json(
                    repo.inspect(
                        args.query,
                        kind=args.kind,
                        language=language,
                        path=args.path,
                        exact_only=args.exact_only,
                        limit=args.limit,
                        anchors=args.anchors,
                        max_source_chars=args.max_source_chars,
                        ref_kinds=_ref_kinds_arg(args),
                    )
                )
            else:
                print(
                    repo.inspect_text(
                        args.query,
                        kind=args.kind,
                        language=language,
                        path=args.path,
                        exact_only=args.exact_only,
                        options=_inspect_options_from_args(args),
                        anchors=args.anchors,
                    ),
                    end="",
                )
        elif args.command == "refs":
            page = repo.refs(
                args.query,
                kind=args.kind,
                language=language,
                path=args.path,
                exact_only=args.exact_only,
                limit=args.limit,
                offset=args.offset,
                ref_kinds=_ref_kinds_arg(args),
            )
            if args.json:
                _print_cli_json(page)
            else:
                print(_format_page_text(repo, "references", page), end="")
        elif args.command == "impls":
            page = repo.impls(
                args.query,
                kind=args.kind,
                language=language,
                path=args.path,
                exact_only=args.exact_only,
                limit=args.limit,
                offset=args.offset,
            )
            if args.json:
                _print_cli_json(page)
            else:
                print(_format_page_text(repo, "implementors", page), end="")
        elif args.command in ("callers", "callees"):
            method = repo.callers if args.command == "callers" else repo.callees
            extra = {"loose": args.loose} if args.command == "callees" else {}
            graph = method(
                args.query,
                kind=args.kind,
                language=language,
                path=args.path,
                exact_only=args.exact_only,
                depth=args.depth,
                limit=args.limit,
                **extra,
            )
            if args.json:
                _print_cli_json(graph)
            else:
                print(_format_call_graph_text(repo, graph), end="")
        elif args.command == "outline":
            page = repo.outline(args.path, symbol=args.symbol, max_symbols=args.max_symbols)
            if args.json:
                _print_cli_json(page)
            else:
                print(repo.outline_text(args.path, symbol=args.symbol, max_symbols=args.max_symbols), end="")
        else:
            parser.error(f"unknown command: {args.command}")
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
    except FileExistsError as exc:
        sys.stderr.write(f"{exc}; use --force to overwrite\n")
        return 2
    except IndexNotFoundError:
        sys.stderr.write("index not found; run `code-symbol-index index` first\n")
        return 2
    except SymbolNotFoundError as exc:
        sys.stderr.write(f"{exc}; narrow with --path/--kind/--exact-only\n")
        return 2


__all__ = [
    "BinaryFileError",
    "CallGraph",
    "CallNode",
    "CodeIndex",
    "CodeSymbolIndexError",
    "EntryPoint",
    "IndexNotFoundError",
    "IndexStatus",
    "Inspection",
    "InspectOptions",
    "HashLine",
    "ImportItem",
    "Page",
    "Position",
    "Range",
    "Reference",
    "Repository",
    "SourceAnchor",
    "Symbol",
    "SymbolNotFoundError",
    "UnsupportedLanguageError",
    "__version__",
    "best_symbol",
    "build_arg_parser",
    "callees",
    "callers",
    "clean",
    "impls",
    "index",
    "install_skill",
    "inspect",
    "inspect_text",
    "main",
    "outline",
    "outline_text",
    "refresh_async",
    "refs",
    "search",
    "search_text",
    "status",
    "status_text",
    "supported_languages",
    "update",
]


if __name__ == "__main__":
    raise SystemExit(main())
