from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    import concurrent.futures

    import pathspec
    from tree_sitter import Node


__version__ = "0.6.0"
SCHEMA_VERSION = 5

# Extraction-rule revisions: one entry per language whose *persisted* symbols or
# references changed in this release. Values are code constants, not program
# versions, and languages that did not change stay absent so they are never
# re-parsed for rules. A nullable ``files.extractor_revision`` column records the
# value each indexed file was written with.
EXTRACTOR_REVISIONS: dict[str, str] = {
    "c": "c:1",
    "cpp": "cpp:2",
    "javascript": "javascript:1",
    "kotlin": "kotlin:2",
    "python": "python:2",
    "rust": "rust:1",
    "swift": "swift:1",
    "tsx": "tsx:1",
    "typescript": "typescript:1",
}
# C/C++ header files are the one extension whose language is ambiguous. The
# default stays C (unchanged behaviour); ``c_header_language`` in the meta table
# records the language future writes use, and every read uses the language the
# file's own row was written with.
HEADER_LANGUAGE_META = "c_header_language"
HEADER_LANGUAGES: tuple[str, ...] = ("c", "cpp")
DEFAULT_HEADER_LANGUAGE = "c"
HEADER_EXTENSION = ".h"
DEFAULT_INDEX_DIR = ".code-symbol-index"
DEFAULT_INDEX_DB = "index.sqlite"
TEXT_SAMPLE_BYTES = 8192
MAX_WORKERS = max((os.cpu_count() or 2) - 1, 1)
QUERY_PARSE_MAX_TREES = 4
QUERY_PARSE_MAX_SOURCE_CHARS = 256 * 1024
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
UPDATE_CHECK_INTERVAL_NS = 7 * 24 * 60 * 60 * 1_000_000_000
UPDATE_CHECK_TIMEOUT_S = 2.0
UPDATE_CHECK_JOIN_TIMEOUT_S = 1.0
PYPI_JSON_URL = "https://pypi.org/pypi/code-symbol-index/json"
UPDATE_CHECK_ENV_DISABLE = "CODE_SYMBOL_INDEX_NO_UPDATE_CHECK"
UPDATE_CHECK_CACHE_ENV = "CODE_SYMBOL_INDEX_UPDATE_CACHE"
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

- Line numbers are 1-based and ranges include both ends, matching `grep -n`,
  editors, tracebacks, and diffs — a line number can be carried between them
  unchanged. Edit anchors (`line:hash`) use the same numbering.
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

# Default field names holding a call node's callee. Grammars that name it
# differently override this per language.
_CALLEE_FIELD_NAMES = ("function", "constructor")

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
CALLER_SCAN_BATCH_SIZE = 32
NATIVE_DEFINITION_MAX_SYMBOLS = 64
GIT_PACKED_REFS_MAX_BYTES = 256 * 1024
NAME_SUMMARY_MAX_SOURCE_BYTES = 1024 * 1024
NAME_SUMMARY_MAX_QUERY_BYTES = 16 * 1024 * 1024
NAME_SUMMARY_SCAN_PREFIX = 32
NAME_SUMMARY_MIN_AGE_NS = 1_000_000_000
NAME_SUMMARY_SIZES = (64, 128, 256, 512, 1024, 2048, 4096)
SERIAL_PARSE_MAX_FILES = 16
SERIAL_PARSE_MAX_BYTES = 64 * 1024
# Symbol kinds a call edge can resolve to. Restricting callee resolution to
# these drops false matches against variables/constants/dict keys.
CALLEE_KINDS = ("class", "function", "method", "constructor", "struct")

CONTAINER_KINDS = {
    "class",
    "enum",
    "extension",
    "function",
    "impl",
    "interface",
    "method",
    "module",
    "namespace",
    "struct",
    "trait",
}

# Symbol kinds that introduce a local scope: definitions nested inside one of
# these are locals, not declarations.
FUNCTION_KINDS = {"function", "method", "constructor"}

IMPLEMENTATION_KINDS = {
    "class",
    "extension",
    "impl",
    "interface",
    "method",
    "struct",
    "trait",
}

# Symbol kinds that can own a call reference. Callables own calls directly;
# containers own what their member initialisers and class-level statements call.
# Variables, fields, constants and type aliases are deliberately excluded: an
# initialiser call is not a call from the field, and letting those win would
# hide the enclosing class behind a tighter range.
CALL_OWNER_KINDS = frozenset(FUNCTION_KINDS | CONTAINER_KINDS)

# Node types whose body bounds call ownership, per language. Only languages
# listed here get body-based depth-1 edges; others keep the older
# definition-range containment. Anonymous callable nodes are included because
# their bodies must still fence off the enclosing function.
_CALLABLE_BODY_NODE_TYPES: dict[str, tuple[str, ...]] = {
    "c": ("function_definition", "lambda_expression"),
    "cpp": ("function_definition", "lambda_expression"),
    "csharp": ("method_declaration", "constructor_declaration", "local_function_statement", "lambda_expression", "anonymous_method_expression"),
    "go": ("function_declaration", "method_declaration", "func_literal"),
    "java": ("method_declaration", "constructor_declaration", "lambda_expression"),
    "javascript": (
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
    ),
    "kotlin": ("function_declaration", "lambda_literal", "anonymous_function"),
    "php": ("function_definition", "method_declaration", "anonymous_function", "arrow_function"),
    "python": ("function_definition", "lambda"),
    "ruby": ("method", "singleton_method", "lambda", "block"),
    "rust": ("function_item", "closure_expression"),
    "swift": (
        "function_declaration",
        "init_declaration",
        "deinit_declaration",
        "subscript_declaration",
        "lambda_literal",
        "computed_property",
    ),
    "tsx": (
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
    ),
    "typescript": (
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
    ),
}

# Field names that hold a callable's body when the grammar names the field.
_BODY_FIELD_NAMES = ("body", "computed_value")

# Body node types used when the grammar leaves the body child unnamed
# (Kotlin/Swift ``function_body``, bare ``statements`` blocks).
_BODY_NODE_TYPES = frozenset(
    {
        "block",
        "body_statement",
        "compound_statement",
        "function_body",
        "statement_block",
        "statements",
    }
)


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
    """One excerpt line. Unlike `Position`, which stays 0-based because it feeds slicing and
    containment checks, `line` is already the 1-based number callers see — it is built for output
    and is what the edit anchor `line:hash` carries."""

    line: int
    hash: str
    text: str


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    """Excerpt bounds in the same 1-based, inclusive form as `HashLine.line`."""

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
    children: tuple[CallNode, ...] = ()


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
    git_freshness: str | None = None


@dataclass(frozen=True, slots=True)
class _IndexedFile:
    path: Path
    language: str
    mtime_ns: int
    size: int
    symbols: tuple[Symbol, ...]
    references: tuple[Reference, ...]
    name_summary: bytes | None = None
    # Transient parse metadata; never persisted. Empty when only references were
    # extracted (``reference_name`` path) or when the grammar has no known
    # function body nodes.
    bodies: tuple[_CallableBody, ...] = ()
    # Extraction-rule revision this file was parsed with; ``None`` for languages
    # without registered rules and for rows written before the column existed.
    revision: str | None = None

    def __reduce__(self):
        # Process-pool results contain thousands of nested frozen dataclasses.
        # Send plain rows instead, sharing this file's path/language on restore.
        return _restore_indexed_file, (
            self.path, self.language, self.mtime_ns, self.size,
            tuple(_symbol_row(symbol) for symbol in self.symbols),
            self.references, self.name_summary, self.bodies, self.revision,
        )


def _restore_indexed_file(path, language, mtime_ns, size, rows, references, name_summary, bodies, revision):
    symbols = tuple(
        Symbol(
            id=row[0], name=row[1], kind=row[2], language=row[3], path=path,
            range=Range(Position(row[5], row[6]), Position(row[7], row[8]), row[9], row[10]),
            signature=row[11], container=row[12],
        )
        for row in rows
    )
    return _IndexedFile(path, language, mtime_ns, size, symbols, references, name_summary, bodies, revision)


@dataclass(frozen=True, slots=True)
class _CallableBody:
    """Body span of one callable in the file that produced it.

    Depth-1 call ownership needs the body, not the whole declaration: a call
    inside a nested callable belongs to that nested callable. ``name`` is empty
    for anonymous callables (lambdas, closures), which still act as ownership
    boundaries even though no symbol is published for them.

    ``name_start_byte`` locates the defining name so a stored symbol can be
    matched to its own body without re-deriving the name.
    """

    name: str
    name_start_byte: int
    start_byte: int
    end_byte: int


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


class HeaderLanguageError(CodeSymbolIndexError):
    """Raised when a header-language change cannot be applied consistently."""


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
    # Field names holding a call's callee, tried in order.
    callee_field_names: tuple[str, ...] = _CALLEE_FIELD_NAMES
    # How to find the callee when the grammar gives it no field name at all.
    # ``"first_child"``  -- the callee expression leads the call node (Swift,
    #                       Kotlin); it may be a member access, not a bare name.
    # ``"first_name"``   -- a keyword leads the node, so take the first
    #                       identifier child instead (PHP's ``new Widget()``).
    callee_position: str | None = None
    # Wrapper nodes that carry no meaning for reference classification. They are
    # skipped when computing a child's parent/grandparent, so a nested
    # identifier still sees the node that actually decides its kind.
    transparent_node_types: tuple[str, ...] = ()
    # Definition kinds that only mean something at file or type scope. Grammars
    # that reuse one node type for both declarations and local bindings (Swift's
    # ``property_declaration``) list them here so locals stay out of the index --
    # otherwise a local would shadow its function as a reference's caller.
    non_local_kinds: tuple[str, ...] = ()
    # Grammars that mark an assignment's target by position instead of by a
    # named field (Kotlin's ``assignment``). The target is the first child.
    positional_assignment_target: bool = False
    # Subtrees to ignore when searching a definition node for its name. Kotlin
    # puts annotations, receivers, and type parameters ahead of the name, and a
    # plain left-to-right scan would return one of those identifiers instead.
    name_skip_node_types: tuple[str, ...] = ()
    # Take the signature from the line holding the name rather than the first
    # line of the definition. Kotlin keeps annotations inside the declaration
    # node, so ``@Deprecated("old")`` would otherwise be the whole signature --
    # which also breaks ``impls``, since it matches on signature text.
    signature_starts_at_name: bool = False


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
        # ``variable_declarator`` also matches function-body locals. Declarators
        # holding a function keep kind ``function`` and are indexed either way.
        non_local_kinds=("variable",),
    ),
    LanguageSpec(
        name="typescript",
        extensions=(".ts", ".mts", ".cts"),
        definitions={
            "abstract_class_declaration": "class",
            "abstract_method_signature": "method",
            "class_declaration": "class",
            "enum_declaration": "enum",
            "function_declaration": "function",
            "function_signature": "function",
            "generator_function_declaration": "function",
            "interface_declaration": "interface",
            "internal_module": "module",
            "method_definition": "method",
            "method_signature": "method",
            "module": "module",
            "property_signature": "field",
            "type_alias_declaration": "type",
            "variable_declarator": "variable",
        },
        call_node_types=("call_expression", "new_expression"),
        import_node_types=("import_statement", "import_clause", "import_specifier", "namespace_import"),
        inherit_node_types=("class_heritage", "extends_clause", "implements_clause", "extends_type_clause"),
        type_node_types=("type_annotation", "type_arguments", "type_identifier", "predefined_type"),
        assignment_node_types=("assignment_expression", "augmented_assignment_expression", "variable_declarator"),
        member_node_types=("member_expression",),
        # ``variable_declarator`` also matches function-body locals. Declarators
        # holding a function keep kind ``function`` and are indexed either way.
        non_local_kinds=("variable",),
    ),
    LanguageSpec(
        name="tsx",
        extensions=(".tsx",),
        definitions={
            "abstract_class_declaration": "class",
            "abstract_method_signature": "method",
            "class_declaration": "class",
            "enum_declaration": "enum",
            "function_declaration": "function",
            "function_signature": "function",
            "generator_function_declaration": "function",
            "interface_declaration": "interface",
            "internal_module": "module",
            "method_definition": "method",
            "method_signature": "method",
            "module": "module",
            "property_signature": "field",
            "type_alias_declaration": "type",
            "variable_declarator": "variable",
        },
        call_node_types=("call_expression", "new_expression"),
        import_node_types=("import_statement", "import_clause", "import_specifier", "namespace_import"),
        inherit_node_types=("class_heritage", "extends_clause", "implements_clause", "extends_type_clause"),
        type_node_types=("type_annotation", "type_arguments", "type_identifier", "predefined_type"),
        assignment_node_types=("assignment_expression", "augmented_assignment_expression", "variable_declarator"),
        member_node_types=("member_expression",),
        # ``variable_declarator`` also matches function-body locals. Declarators
        # holding a function keep kind ``function`` and are indexed either way.
        non_local_kinds=("variable",),
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
        # ``var``/``const`` specs also match function-body declarations.
        non_local_kinds=("variable", "constant"),
    ),
    LanguageSpec(
        name="rust",
        extensions=(".rs",),
        definitions={
            "const_item": "constant",
            "enum_item": "enum",
            "function_item": "function",
            # A trait or extern block declares a function without a body; the
            # declaration stays a symbol so the name is findable where it is
            # declared, and definition preference keeps the body the call target.
            "function_signature_item": "function",
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
        # Annotations/attributes live inside the declaration node.
        signature_starts_at_name=True,
    ),
    LanguageSpec(
        name="c",
        extensions=(".c", ".h"),
        definitions={
            "alias_declaration": "type",
            "declaration": "variable",
            "enum_specifier": "enum",
            "enumerator": "constant",
            "field_declaration": "field",
            "function_definition": "function",
            "preproc_def": "constant",
            "preproc_function_def": "constant",
            "struct_specifier": "struct",
            "type_definition": "type",
            "union_specifier": "struct",
        },
        # ``declaration`` also matches locals and for-loop initialisers.
        non_local_kinds=("variable",),
    ),
    LanguageSpec(
        name="cpp",
        extensions=(".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
        definitions={
            "alias_declaration": "type",
            "class_specifier": "class",
            "declaration": "variable",
            "enum_specifier": "enum",
            "enumerator": "constant",
            "field_declaration": "field",
            "function_definition": "function",
            "namespace_definition": "namespace",
            "preproc_def": "constant",
            "preproc_function_def": "constant",
            "struct_specifier": "struct",
            "type_definition": "type",
            "union_specifier": "struct",
        },
        # C++ names its base classes in a dedicated clause, which carries the
        # ``inherit`` context for reference classification and ``impls``.
        inherit_node_types=("base_class_clause",),
        # ``declaration`` also matches locals and for-loop initialisers.
        non_local_kinds=("variable",),
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
        # Annotations/attributes live inside the declaration node.
        signature_starts_at_name=True,
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
        # One ``call`` node covers ``f(x)``, ``obj.f(x)``, and bare ``obj.f``;
        # the callee is always the ``method`` field.
        call_node_types=("call",),
        callee_field_names=("method",),
        import_node_types=(),
        inherit_node_types=("superclass",),
        assignment_node_types=("assignment", "operator_assignment"),
    ),
    LanguageSpec(
        name="swift",
        extensions=(".swift",),
        definitions={
            # ``class_declaration`` covers class/struct/enum/actor/extension;
            # the concrete kind comes from the ``declaration_kind`` field.
            "class_declaration": "class",
            "protocol_declaration": "interface",
            "function_declaration": "function",
            "protocol_function_declaration": "function",
            "init_declaration": "constructor",
            "typealias_declaration": "type",
            "associatedtype_declaration": "type",
            "property_declaration": "property",
            "protocol_property_declaration": "property",
            "enum_entry": "enum_case",
        },
        call_node_types=("call_expression",),
        import_node_types=("import_declaration",),
        inherit_node_types=("inheritance_specifier",),
        type_node_types=("type_annotation", "type_identifier", "user_type", "type_arguments"),
        assignment_node_types=("assignment",),
        member_node_types=("navigation_expression",),
        callee_position="first_child",
        transparent_node_types=("navigation_suffix", "directly_assignable_expression"),
        non_local_kinds=("property",),
    ),
    LanguageSpec(
        name="kotlin",
        extensions=(".kt", ".kts"),
        definitions={
            # ``class_declaration`` also covers interfaces and enum classes.
            "class_declaration": "class",
            "object_declaration": "class",
            "function_declaration": "function",
            "property_declaration": "property",
            "class_parameter": "property",
            "type_alias": "type",
            "enum_entry": "enum_case",
        },
        call_node_types=("call_expression", "constructor_invocation"),
        import_node_types=("import_header",),
        inherit_node_types=("delegation_specifier",),
        type_node_types=("user_type", "type_identifier", "type_arguments"),
        assignment_node_types=("assignment",),
        member_node_types=("navigation_expression",),
        callee_position="first_child",
        positional_assignment_target=True,
        transparent_node_types=("navigation_suffix", "directly_assignable_expression"),
        non_local_kinds=("property",),
        name_skip_node_types=("modifiers", "receiver_type", "type_parameters"),
        signature_starts_at_name=True,
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
        # PHP splits calls across four node types. Three name the callee
        # (``function`` or ``name``); ``new Widget()`` names nothing and leads
        # with the ``new`` keyword.
        call_node_types=(
            "function_call_expression",
            "member_call_expression",
            "scoped_call_expression",
            "object_creation_expression",
        ),
        callee_field_names=("function", "name"),
        callee_position="first_name",
        import_node_types=("namespace_use_declaration",),
        inherit_node_types=("base_clause", "class_interface_clause"),
        assignment_node_types=("assignment_expression", "augmented_assignment_expression"),
        member_node_types=("member_access_expression",),
    ),
)

LANGUAGE_BY_NAME = {language.name: language for language in LANGUAGES}
LANGUAGE_BY_EXTENSION = {
    extension: language
    for language in LANGUAGES
    for extension in language.extensions
}


def _name_query(method: Any) -> Any:
    """Share checks inside one request, never across calls on a reused index."""
    @wraps(method)
    def query(self: CodeIndex, *args: Any, **kwargs: Any) -> Any:
        if getattr(self, "_name_filter", None) is not None:
            return method(self, *args, **kwargs)
        self._name_summary_unavailable = False
        self._name_filter = _NameFilter(self)
        self._language_cache = {}
        previous_trees = getattr(_PARSER_TLS, 'query_trees', None)
        _PARSER_TLS.query_trees = {}
        try:
            return method(self, *args, **kwargs)
        finally:
            self._name_filter = None
            self._language_cache = None
            _PARSER_TLS.query_trees = previous_trees
    return query


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
        header_language: str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.languages = _normalize_languages(languages)
        # Language used for ``.h`` files on the next write pass. ``None`` means the
        # default (C); a persisted ``Repository`` overrides it from meta.
        self.header_language = _validate_header_language(header_language)
        self._language_cache: dict[str, str | None] | None = None
        self.include = tuple(include or ())
        self.exclude = tuple(DEFAULT_EXCLUDES) + tuple(exclude or ())
        # A narrowed scan cannot speak for files it never walked, so a filter and a
        # header-language change are refused together instead of half-converted.
        self.filtered_scan = bool(languages) or bool(include) or bool(exclude)
        self._exclude_matcher = _compile_path_patterns(self.exclude)
        self.storage = _Storage(db_path, create=create_storage)
        # Gitignore specs in force for a directory, keyed by its posix prefix
        # ("" for the root, "src/pkg/" otherwise). Each entry is the parent's
        # stack plus that directory's own .gitignore, so a lookup costs one dict
        # hit and a match tests only the path's own ancestors.
        self._gitignore_specs: dict[str, tuple[tuple[str, str, pathspec.PathSpec], ...]] = {}

    def build(self, *, header_language: str | None = None) -> CodeIndex:
        if header_language is not None:
            self.header_language = _validate_header_language(header_language)
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

    @_name_query
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

    @_name_query
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

    @_name_query
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

    @_name_query
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

    @_name_query
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

    @_name_query
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

        # A declaration and its definition share name, kind and scope. ``refs`` and
        # ``impls`` resolve through this path, so the body wins here too instead of
        # whichever row the search happened to return first.
        matches = self.search_symbols(
            query, kind=kind, language=language, path=path, exact_only=exact_only, limit=MAX_INSPECT_CANDIDATES + 1
        )
        if not matches:
            raise SymbolNotFoundError(f"No symbol matched: {query}")
        if len(matches) > 1:
            preferred = _preferred_candidate(self, matches)
            if preferred is not None:
                return preferred
        return matches[0]

    def _index_files(self, paths: Iterable[Path]) -> None:
        for relative_path in paths:
            self._index_file(relative_path)

    def _index_file(self, relative_path: Path) -> None:
        indexed = _parse_file(
            self.root,
            relative_path,
            self.languages,
            header_language=self.write_header_language(),
            collect_bodies=False,
        )
        if indexed is None:
            return
        self.storage.insert_file_result(indexed)

    def write_header_language(self) -> str | None:
        """Language future writes use for ``.h`` files (``None`` = default C)."""
        return self.header_language

    def _write_spec(self, path: Path) -> LanguageSpec | None:
        """Language to parse ``path`` with on this write pass."""
        return _spec_for_path(path, self.languages, self.write_header_language())

    def _stored_language(self, relative_path: Path) -> str | None:
        """Language an already-indexed file was written with.

        Reads must use the file's own language, never the pending write setting,
        so a half-converted index still parses each file the way it was stored.
        Look-ups are cached for one request; misses are one indexed row each.
        """
        cache = self._language_cache
        if cache is None:
            return self.storage.file_languages([relative_path.as_posix()]).get(relative_path.as_posix())
        key = relative_path.as_posix()
        if key not in cache:
            stored = self.storage.file_languages([key])
            cache[key] = stored.get(key)
        return cache[key]

    def _stored_spec(self, relative_path: Path) -> LanguageSpec | None:
        stored = self._stored_language(relative_path)
        if stored is not None:
            spec = LANGUAGE_BY_NAME.get(stored)
            if spec is not None:
                try:
                    _parser_for_language(spec.name)
                except UnsupportedLanguageError:
                    spec = None
                if spec is not None:
                    return spec
        return self._write_spec(relative_path)


    def _iter_indexable_files(self) -> Iterable[Path]:
        # One walk, on posix strings, building a Path only for files actually
        # yielded. Gitignore specs are collected as the walk descends: a
        # separate pass to find .gitignore files could not prune ignored
        # directories (it had no specs yet), so it visited an order of magnitude
        # more directories than the scan itself needed.
        root_text = str(self.root)
        scan_errors: list[OSError] = []
        for dirpath, dirnames, filenames in os.walk(root_text, onerror=scan_errors.append):
            relative_dir = os.path.relpath(dirpath, root_text)
            prefix = "" if relative_dir == "." else relative_dir.replace(os.sep, "/") + "/"
            specs = self._gitignore_specs_for_dir(prefix, has_gitignore=".gitignore" in filenames)
            kept: list[str] = []
            for dirname in dirnames:
                child = prefix + dirname
                if self._is_excluded_text(child):
                    continue
                if self._matches_gitignore(specs, child, is_dir=True):
                    continue
                kept.append(dirname)
            dirnames[:] = kept
            for filename in filenames:
                path_text = prefix + filename
                if self._should_index_text(path_text, specs):
                    yield Path(path_text)
        if scan_errors:
            raise scan_errors[0]  # An unreadable subtree is not evidence that its indexed files were deleted.

    def _should_index(self, relative_path: Path) -> bool:
        return self._should_index_text(relative_path.as_posix())

    def _should_index_text(
        self,
        path_text: str,
        specs: tuple[tuple[str, str, pathspec.PathSpec], ...] | None = None,
    ) -> bool:
        # Extension first: it is the cheapest and most selective test, so most
        # files never reach the pattern and gitignore matching below.
        if _spec_for_extension(_extension_of(path_text), self.languages, self.write_header_language()) is None:
            return False
        if self.include and not any(fnmatch.fnmatch(path_text, pattern) for pattern in self.include):
            return False
        if self._is_excluded_text(path_text):
            return False
        return not self._is_gitignored_text(path_text, specs=specs)

    def _should_skip_dir(self, relative_path: Path) -> bool:
        return self._should_skip_dir_text(relative_path.as_posix())

    def _should_skip_dir_text(self, path_text: str) -> bool:
        if path_text == ".":
            return False
        return self._is_excluded_text(path_text) or self._is_gitignored_text(path_text, is_dir=True)

    def _is_excluded(self, relative_path: Path) -> bool:
        return self._is_excluded_text(relative_path.as_posix())

    def _is_excluded_text(self, path_text: str) -> bool:
        return self._exclude_matcher(os.path.normcase(path_text)) is not None

    def _relative_path(self, path: Path) -> Path:
        full_path = path if path.is_absolute() else self.root / path
        return full_path.resolve().relative_to(self.root)

    def _is_gitignored(self, relative_path: Path, *, is_dir: bool = False) -> bool:
        return self._is_gitignored_text(relative_path.as_posix(), is_dir=is_dir)

    def _is_gitignored_text(
        self,
        path_text: str,
        *,
        is_dir: bool = False,
        specs: tuple[tuple[str, str, pathspec.PathSpec], ...] | None = None,
    ) -> bool:
        if specs is None:
            cut = path_text.rfind("/")
            specs = self._gitignore_specs_for_dir(path_text[: cut + 1] if cut != -1 else "")
        return self._matches_gitignore(specs, path_text, is_dir=is_dir)

    @staticmethod
    def _matches_gitignore(
        specs: tuple[tuple[str, str, pathspec.PathSpec], ...],
        path_text: str,
        *,
        is_dir: bool = False,
    ) -> bool:
        for base_text, base_prefix, spec in specs:
            if not base_text:
                scoped_path = path_text
            elif path_text == base_text:
                scoped_path = "."
            elif path_text.startswith(base_prefix):
                scoped_path = path_text[len(base_prefix) :]
            else:
                continue
            if is_dir:
                scoped_path = f"{scoped_path}/"
            if spec.match_file(scoped_path):
                return True
        return False

    def _gitignore_specs_for_dir(
        self, prefix: str, *, has_gitignore: bool | None = None
    ) -> tuple[tuple[str, str, pathspec.PathSpec], ...]:
        """Gitignore specs in force for a directory: its ancestors', plus its own."""
        cached = self._gitignore_specs.get(prefix)
        if cached is not None:
            return cached

        parent = _parent_prefix(prefix)
        inherited = () if prefix == "" else self._gitignore_specs_for_dir(parent)
        specs = inherited
        if has_gitignore is not False:
            try:
                lines = (self.root / prefix / ".gitignore").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                lines = None
            if lines is not None:
                import pathspec

                base_text = prefix[:-1] if prefix else ""
                specs = (*inherited, (base_text, prefix, pathspec.PathSpec.from_lines("gitignore", lines)))

        self._gitignore_specs[prefix] = specs
        return specs


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
        # Outcome of the last ``update(paths)`` call, for command reporting: paths
        # whose new symbols were written, and paths whose previous rows were kept
        # because the file could not be read or parsed.
        self.last_update_updated: tuple[str, ...] = ()
        self.last_update_failed: tuple[str, ...] = ()
        # (header language, converted, pending) of the last refresh.
        self.last_header_language: tuple[str, int, int] = (DEFAULT_HEADER_LANGUAGE, 0, 0)

    def write_header_language(self) -> str | None:
        """Header language saved for future writes, defaulting to C."""
        if self.header_language is None:
            self.header_language = self.storage.meta_value(HEADER_LANGUAGE_META) or DEFAULT_HEADER_LANGUAGE
        return self.header_language

    def _write_revision(self, path_text: str) -> str | None:
        """Extraction-rule revision this scan would write for ``path``."""
        extension = _extension_of(path_text)
        spec = (LANGUAGE_BY_NAME[self.write_header_language() or DEFAULT_HEADER_LANGUAGE]
                if extension == HEADER_EXTENSION else LANGUAGE_BY_EXTENSION.get(extension))
        return EXTRACTOR_REVISIONS.get(spec.name) if spec is not None else None

    def _scan_covers_language(self, language: str) -> bool:
        """Whether this scan's language filter covers existing rows of ``language``.

        A language-filtered refresh knows nothing about files it never walked, so
        it must not turn its own narrow file set into whole-database deletions.
        """
        return self.languages is None or language in self.languages

    def refresh(self, *, progress: Any = _DEFAULT_PROGRESS, header_language: str | None = None) -> Repository:
        git_before = _git_state(self.root)
        self._gitignore_specs.clear()
        progress_callback = self.progress if progress is _DEFAULT_PROGRESS else progress
        if self.storage.schema_version() != SCHEMA_VERSION:
            self.storage.reset_schema()

        target = _validate_header_language(header_language)
        stored_header = self.storage.meta_value(HEADER_LANGUAGE_META)
        effective_header = target if target is not None else (stored_header or DEFAULT_HEADER_LANGUAGE)
        if target is not None and target != (stored_header or DEFAULT_HEADER_LANGUAGE):
            if self.filtered_scan:
                raise HeaderLanguageError(
                    "changing the header language requires an unfiltered refresh: rerun index "
                    "without --language/--include/--exclude so every header file is converted"
                )
            # Saved as the language of future writes, not as a completion marker:
            # files that fail keep their own language and are retried next time.
            self.storage.set_meta_value(HEADER_LANGUAGE_META, target)
        self.header_language = effective_header

        _emit_progress(progress_callback, "scan", done=0, total=0)
        current_files: dict[str, tuple[Path, os.stat_result]] = {}
        unavailable_paths: set[str] = set()
        paths = list(self._iter_indexable_files())
        for path in paths:
            try:
                stat = (self.root / path).stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                unavailable_paths.add(path.as_posix())
                continue
            current_files[path.as_posix()] = (path, stat)

        indexed_files = self.storage.files()
        # Only rows whose language this scan actually covers may be inferred
        # deleted: a filtered refresh says nothing about other languages.
        deleted = [
            Path(path)
            for path in indexed_files
            if path not in current_files and path not in unavailable_paths
            and self._scan_covers_language(indexed_files[path]["language"])
            and (path[-2:].lower() != HEADER_EXTENSION or self._scan_covers_language(effective_header))
        ]

        to_index: list[Path] = []
        to_summarize: list[tuple[Path, os.stat_result]] = []
        rule_upgrades = 0
        rule_upgrade_paths: set[str] = set()
        for path_text, (path, stat) in current_files.items():
            old = indexed_files.get(path_text)
            revision = self._write_revision(path_text)
            needs_rules = revision is not None and (old is None or old["extractor_revision"] != revision)
            metadata_changed = old is None or old["mtime_ns"] != stat.st_mtime_ns or old["size"] != stat.st_size
            if old is not None and needs_rules and not metadata_changed:
                # Unchanged file whose persisted extraction rules moved: one-time
                # re-parse, counted and reported separately from normal work.
                rule_upgrades += 1
                rule_upgrade_paths.add(path_text)
            if metadata_changed or needs_rules:
                to_index.append(path)
                continue
            if not _name_summary_current(old["summary_header"], stat):
                to_summarize.append((path, stat))

        if rule_upgrades:
            _emit_progress(progress_callback, "upgrade", done=0, total=rule_upgrades)

        summary_updates: list[tuple[bytes, str]] = []
        if to_summarize:
            _emit_progress(progress_callback, "summary", done=0, total=len(to_summarize))
        for done, (path, stat) in enumerate(to_summarize, 1):
            try:
                if stat.st_size > NAME_SUMMARY_MAX_SOURCE_BYTES:
                    summary = b"\x00"
                else:
                    with (self.root / path).open("rb") as stream:
                        source = stream.read(NAME_SUMMARY_MAX_SOURCE_BYTES + 1)
                    summary = _file_name_summary(source, stat)
                if summary is not None:
                    summary_updates.append((summary, path.as_posix()))
            except OSError:
                pass  # A disappearing file is handled by the next scan.
            if done % 100 == 0 or done == len(to_summarize):
                _emit_progress(progress_callback, "summary", done=done, total=len(to_summarize))

        total = len(to_index)
        _emit_progress(progress_callback, "start", done=0, total=total)
        # Start expensive files first using the scan's existing stat results.
        # No additional filesystem calls are needed for scheduling.
        to_index.sort(key=lambda path: current_files[path.as_posix()][1].st_size, reverse=True)
        indexed_results = self._parse_files(
            to_index, include_references=False, progress=progress_callback, header_language=effective_header
        )
        self.storage.replace_files(
            deleted_paths=deleted,
            summary_updates=summary_updates,
            indexed_files=indexed_results,
            progress=getattr(progress_callback, "_storage_progress", None),
            schema_version=SCHEMA_VERSION,
            git_baseline=None if self.filtered_scan else _git_baseline(self.root, git_before),
            rule_upgrade_paths=rule_upgrade_paths,
        )
        self._record_header_language_outcome(
            indexed_results, current_files, indexed_files, effective_header, unavailable_paths
        )
        _emit_progress(progress_callback, "finish", done=len(indexed_results), total=total + len(unavailable_paths))
        return self

    def _record_header_language_outcome(
        self,
        indexed_results: list[_IndexedFile],
        current_files: dict[str, tuple[Path, os.stat_result]],
        indexed_files: dict[str, sqlite3.Row],
        effective_header: str,
        unavailable_paths: set[str],
    ) -> None:
        """Note how many stored header files actually moved to the new language.

        The meta value already means "future writes", so a partial conversion must
        be reportable: callers compare converted with pending and say so instead
        of claiming the whole index was converted.
        """
        published = {indexed_file.path for indexed_file in indexed_results}
        pending = [
            path
            for path in indexed_files
            if path[-2:].lower() == HEADER_EXTENSION
            and (path in current_files or path in unavailable_paths)
            and indexed_files[path]["language"] != effective_header
        ]
        converted = sum(1 for path in pending if Path(path) in published)
        self.last_header_language = (effective_header, converted, len(pending))

    def build(self, *, progress: Any = _DEFAULT_PROGRESS) -> Repository:
        git_before = _git_state(self.root)
        self._gitignore_specs.clear()
        progress_callback = self.progress if progress is _DEFAULT_PROGRESS else progress
        self.storage.clear()
        paths = list(self._iter_indexable_files())
        total = len(paths)
        _emit_progress(progress_callback, "start", done=0, total=total)
        indexed_results = self._parse_files(
            paths, include_references=False, progress=progress_callback, header_language=self.write_header_language()
        )
        self.storage.replace_files(
            deleted_paths=(),
            indexed_files=indexed_results,
            progress=getattr(progress_callback, "_storage_progress", None),
            schema_version=SCHEMA_VERSION,
            git_baseline=_git_baseline(self.root, git_before),
        )
        _emit_progress(progress_callback, "finish", done=len(indexed_results), total=total)
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

        self._gitignore_specs.clear()
        self.header_language = None  # Another Repository may have changed the write setting.
        relative_paths = list(dict.fromkeys(self._relative_path(path) for path in _coerce_paths(paths)))
        from stat import S_ISREG

        to_index: list[Path] = []
        removed: list[Path] = []
        unavailable: list[Path] = []
        for path in relative_paths:
            if not self._should_index(path):
                removed.append(path)
                continue
            try:
                mode = (self.root / path).stat().st_mode
            except (FileNotFoundError, NotADirectoryError):
                removed.append(path)
            except OSError:
                unavailable.append(path)
            else:
                (to_index if S_ISREG(mode) else removed).append(path)
        total = len(to_index)
        _emit_progress(progress_callback, "start", done=0, total=total)
        indexed_results = self._parse_files(
            to_index,
            include_references=False,
            progress=progress_callback,
            header_language=self.write_header_language(),
        )
        published = {indexed_file.path for indexed_file in indexed_results}
        # Deletion candidates are only the requested paths that are gone or no
        # longer indexable. A file that still exists but failed to read or parse
        # keeps its previous rows and revision instead of losing them here.
        self.storage.replace_files(
            deleted_paths=removed,
            indexed_files=indexed_results,
            progress=getattr(progress_callback, "_storage_progress", None),
            schema_version=SCHEMA_VERSION,
        )
        self.last_update_updated = tuple(
            path.as_posix() for path in to_index if path in published
        )
        self.last_update_failed = tuple(path.as_posix() for path in [*to_index, *unavailable] if path not in published)
        _emit_progress(progress_callback, "finish", done=len(indexed_results), total=total + len(unavailable))
        return self

    def _parse_files(
        self,
        paths: list[Path],
        *,
        include_references: bool = True,
        progress: Any | None = None,
        header_language: str | None = None,
    ) -> list[_IndexedFile]:
        if not paths:
            return []
        serial = len(paths) == 1 or MAX_WORKERS <= 1
        if not serial and len(paths) <= SERIAL_PARSE_MAX_FILES:
            total_bytes = 0
            for path in paths:
                try:
                    total_bytes += (self.root / path).stat().st_size
                except OSError:
                    break
                if total_bytes > SERIAL_PARSE_MAX_BYTES:
                    break
            else:
                serial = True
        if serial:
            results = []
            for done, path in enumerate(paths, start=1):
                result = _parse_file(
                    self.root,
                    path,
                    self.languages,
                    include_references=include_references,
                    header_language=header_language,
                    collect_bodies=include_references,
                )
                if result is not None:
                    results.append(result)
                _emit_progress(progress, "file", done=done, total=len(paths), path=path.as_posix())
            return results

        import concurrent.futures

        results: list[_IndexedFile] = []
        workers = min(MAX_WORKERS, len(paths))
        # Keep several jobs per worker for load balancing, while amortizing IPC
        # for repositories with thousands of tiny files. Small batches stay 1:1.
        batch_size = min(16, max(1, len(paths) // (workers * 8)))
        batch_count = (len(paths) + batch_size - 1) // batch_size
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=workers)
        future_to_paths = {}
        try:
            future_to_paths = {
                executor.submit(
                    _parse_file_batch, self.root, batch, self.languages, include_references, header_language,
                ): batch
                # Stripe size-ordered paths across jobs so the largest files
                # do not accumulate in a single sequential worker batch.
                for batch in (paths[offset::batch_count] for offset in range(batch_count))
            }
            done = 0
            for future in concurrent.futures.as_completed(future_to_paths):
                batch = future_to_paths[future]
                try:
                    parsed = future.result()
                except Exception:
                    parsed = [None] * len(batch)
                for path, result in zip(batch, parsed, strict=True):
                    if result is not None:
                        results.append(result)
                    done += 1
                    _emit_progress(progress, "file", done=done, total=len(paths), path=path.as_posix())
        except KeyboardInterrupt:
            for future in future_to_paths:
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

    @_name_query
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

    @_name_query
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
            if not self._name_filter.may_contain(path, (needle,)) or not _file_contains_bytes(self.root / path, needle):
                continue
            indexed_file = _parse_file(
                self.root,
                path,
                self.languages,
                reference_name=symbol.name,
                language=self._stored_language(path),
            )
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
        """Stored file rows, tolerating any older column layout.

        Readers never migrate: the base four columns always exist, while
        ``name_summary`` and ``extractor_revision`` are optional additive
        capabilities. Each is selected independently, so a database missing one of
        them still reports the other instead of degrading to no capabilities.
        """
        columns = self._file_columns()
        summary_select = (
            "name_summary IS NOT NULL AS has_summary, substr(name_summary, 1, 41) AS summary_header"
            if "name_summary" in columns
            else "0 AS has_summary, NULL AS summary_header"
        )
        revision_select = "extractor_revision" if "extractor_revision" in columns else "NULL AS extractor_revision"
        try:
            rows = self.connection.execute(
                f"SELECT path, language, mtime_ns, size, {summary_select}, {revision_select} FROM files",
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {row["path"]: row for row in rows}

    def _file_columns(self) -> set[str]:
        return {row[1] for row in self.connection.execute("PRAGMA table_info(files)")}

    def file_languages(self, paths: Iterable[str]) -> dict[str, str]:
        """Stored language per path, for the paths that have a row."""
        wanted = list(dict.fromkeys(paths))
        languages: dict[str, str] = {}
        for chunk in _chunks(wanted, SQLITE_BATCH_SIZE):
            placeholders = ", ".join("?" * len(chunk))
            rows = self.connection.execute(
                f"SELECT path, language FROM files WHERE path IN ({placeholders})", chunk
            ).fetchall()
            languages.update({row["path"]: row["language"] for row in rows})
        return languages

    def meta_value(self, key: str) -> str | None:
        try:
            row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row["value"] if row is not None else None

    def set_meta_value(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )

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
        revision: str | None = None,
    ) -> None:
        symbols = list(symbols)
        self._ensure_file_columns()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO files(path, language, mtime_ns, size, extractor_revision)
                VALUES (?, ?, ?, ?, ?)
                """,
                (path.as_posix(), language, mtime_ns, size, revision),
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
            revision=indexed_file.revision,
        )

    def _ensure_file_columns(self) -> None:
        """Add the optional, nullable ``files`` columns when they are missing.

        Additive and on-demand: an old index is still readable without this, and
        no read path calls it. Older writers use explicit column lists and turn
        their values back into NULL, which is exactly what the revision check
        detects. Downgraded semantics are not guaranteed.
        """
        columns = self._file_columns()
        additions = (
            ("name_summary", "BLOB"),
            ("extractor_revision", "TEXT"),
        )
        with self.connection:
            for column, column_type in additions:
                if column not in columns:
                    self.connection.execute(f"ALTER TABLE files ADD COLUMN {column} {column_type}")

    def replace_files(
        self,
        *,
        deleted_paths: Iterable[Path],
        indexed_files: Iterable[_IndexedFile],
        schema_version: int,
        progress: Any | None = None,
        git_baseline: str | None = None,
        summary_updates: Iterable[tuple[bytes, str]] = (),
        rule_upgrade_paths: set[str] | None = None,
    ) -> None:
        # Nullable, additive schema-5 capabilities: no row rewrite or backfill.
        # Older writers use explicit columns and replace summaries with NULL.
        self._ensure_file_columns()
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
            unchanged = self._unchanged_upgrade_symbols(file_chunk, rule_upgrade_paths) if rule_upgrade_paths else set()
            changed_files = ([file for file in file_chunk if file.path.as_posix() not in unchanged]
                             if unchanged else file_chunk)
            file_rows = [
                (
                    indexed_file.path.as_posix(),
                    indexed_file.language,
                    indexed_file.mtime_ns,
                    indexed_file.size,
                    indexed_file.name_summary,
                    indexed_file.revision,
                )
                for indexed_file in file_chunk
            ]
            symbol_rows = [
                _symbol_row(symbol)
                for indexed_file in changed_files
                for symbol in indexed_file.symbols
            ]
            fts_rows = [
                _symbol_fts_row(symbol)
                for indexed_file in changed_files
                for symbol in indexed_file.symbols
            ] if self.has_symbol_fts else []
            with self.connection:
                _delete_paths_chunked(
                    self.connection,
                    [indexed_file.path for indexed_file in changed_files],
                    include_fts=self.has_symbol_fts,
                )
                if unchanged:
                    placeholders = ','.join('?' for _ in unchanged)
                    self.connection.execute(f'DELETE FROM refs WHERE path IN ({placeholders})', tuple(unchanged))
                    # The equality checks complete this work without rewriting
                    # identical symbol/FTS rows; metadata still advances below.
                    write_done += sum(len(file.symbols) for file in file_chunk
                                      if file.path.as_posix() in unchanged) * (2 if self.has_symbol_fts else 1)
                self.connection.executemany(
                    """
                    INSERT OR REPLACE INTO files(path, language, mtime_ns, size, name_summary, extractor_revision)
                    VALUES (?, ?, ?, ?, ?, ?)
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
            self.connection.executemany(
                "UPDATE files SET name_summary = ? WHERE path = ?", summary_updates,
            )
            if git_baseline is not None:
                self.connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('git_baseline', ?)",
                    (git_baseline,),
                )
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

    def _unchanged_upgrade_symbols(self, files: list[_IndexedFile], candidates: set[str]) -> set[str]:
        """Compare only rule upgrades, in one bounded read per file batch."""
        paths = [file.path.as_posix() for file in files if file.path.as_posix() in candidates]
        if not paths:
            return set()
        previous: dict[str, dict[str, tuple]] = {path: {} for path in paths}
        placeholders = ','.join('?' for _ in paths)
        for row in self.connection.execute(
            'SELECT id, name, kind, language, path, start_line, start_col, end_line, end_col, '
            f'start_byte, end_byte, signature, container FROM symbols WHERE path IN ({placeholders})', paths,
        ):
            previous[row[4]][row[0]] = tuple(row)
        return {
            file.path.as_posix() for file in files
            if file.path.as_posix() in previous
            and previous[file.path.as_posix()] == {symbol.id: _symbol_row(symbol) for symbol in file.symbols}
        }

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


def _spec_for_path(
    path: Path,
    languages: set[str] | None = None,
    header_language: str | None = None,
) -> LanguageSpec | None:
    return _spec_for_extension(path.suffix.lower(), languages, header_language)


def _spec_for_extension(
    extension: str,
    languages: set[str] | None = None,
    header_language: str | None = None,
) -> LanguageSpec | None:
    if header_language is not None and extension == HEADER_EXTENSION:
        spec = LANGUAGE_BY_NAME.get(header_language)
    else:
        spec = LANGUAGE_BY_EXTENSION.get(extension)
    if spec is None:
        return None
    if languages is not None and spec.name not in languages:
        return None
    try:
        _parser_for_language(spec.name)
    except UnsupportedLanguageError:
        return None
    return spec


def _validate_header_language(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in HEADER_LANGUAGES:
        raise ValueError(f"header language must be one of: {', '.join(HEADER_LANGUAGES)}")
    return value


def _file_spec(repo: CodeIndex, path: Path) -> LanguageSpec | None:
    """Language to parse an already-indexed file with.

    Reads follow the language the file's own row was written with, so a header
    conversion that is still in progress parses each file the way it was stored
    instead of with today's pending setting.
    """
    resolver = getattr(repo, "_stored_spec", None)
    if resolver is not None:
        return resolver(path)
    return _spec_for_path(path, getattr(repo, "languages", None))


def _file_language(repo: CodeIndex, path: Path) -> str | None:
    resolver = getattr(repo, "_stored_language", None)
    return resolver(path) if resolver is not None else None


def _name_summary_current(header: bytes | None, stat: os.stat_result) -> bool:
    if header == b"\x00":
        return True  # Intentionally unsupported; do not retry every refresh.
    if not isinstance(header, bytes) or len(header) != 41 or header[0] != 1:
        return False
    import struct

    return struct.unpack("<QQQqq", header[1:]) == (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    )


def _file_name_summary(source: bytes, stat: os.stat_result) -> bytes | None:
    """Lossy ASCII-name membership only; exact matches still parse live source."""
    if sys.platform == "win32" or len(source) > NAME_SUMMARY_MAX_SOURCE_BYTES:
        return b"\x00"
    # Coarse timestamp clocks can assign the same ctime to successive writes.
    # Only build summaries after the indexed timestamp's tick has passed.
    if time_ns() - stat.st_ctime_ns < NAME_SUMMARY_MIN_AGE_NS:
        return None
    import struct
    import zlib

    names = set(re.findall(rb"[A-Za-z_][A-Za-z_0-9]*", source))
    size = max(64, min(4096, 1 << max(0, (len(names) * 2 - 1).bit_length())))
    bits = bytearray(size)
    mask = size * 8 - 1
    for name in names:
        value = zlib.crc32(name)
        for position in (value & mask, (value >> 16) & mask):
            bits[position >> 3] |= 1 << (position & 7)
    # Capture the pre-read identity: edits/replacements during parsing cannot
    # make a summary of old bytes look valid for the resulting file.
    try:
        stamp = struct.pack("<QQQqq", stat.st_dev, stat.st_ino, stat.st_size,
                            stat.st_mtime_ns, stat.st_ctime_ns)
    except (struct.error, OverflowError):
        return b"\x00"  # Filesystems with wider identities keep normal scanning.
    return b"\x01" + stamp + bits


class _NameFilter:
    """Bounded per-request metadata, with stat checks only for negative hits."""
    def __init__(self, repo: CodeIndex) -> None:
        self.repo = repo
        self.rows: dict[str, bytes] | None = None
        self.remaining_prefix = NAME_SUMMARY_SCAN_PREFIX
        self.checked: dict[Path, bool] = {}
        self.positions: dict[tuple[bytes, ...], Any] = {}

    def may_contain(self, path: Path, needles: tuple[bytes, ...]) -> bool:
        # Windows ctime is birth time, not a reliable edit invalidator.
        if sys.platform == "win32":
            return True
        if self.rows is None and self.remaining_prefix > 0:
            self.remaining_prefix -= 1
            return True  # Early hits avoid loading the repository's summaries.
        import struct
        import zlib

        if self.rows is None:
            self.rows = {}
            budget = NAME_SUMMARY_MAX_QUERY_BYTES
            try:
                cursor = self.repo.storage.connection.execute(
                    "SELECT path, name_summary FROM files WHERE name_summary IS NOT NULL",
                )
            except sqlite3.OperationalError:
                self.repo._name_summary_unavailable = True
            else:
                seen_summary = False
                try:
                    for row in cursor:
                        seen_summary = True
                        data = row["name_summary"]
                        if not isinstance(data, bytes) or len(data) - 41 not in NAME_SUMMARY_SIZES or data[0] != 1:
                            continue
                        budget -= len(data) + len(row["path"]) + 128
                        if budget < 0:
                            break
                        self.rows[row["path"]] = data
                finally:
                    cursor.close()
                if not seen_summary:
                    self.repo._name_summary_unavailable = True
        data = self.rows.get(path.as_posix())
        if data is None:
            return True
        if needles not in self.positions:
            if any(re.fullmatch(rb"[A-Za-z_][A-Za-z_0-9]*", needle) is None for needle in needles):
                self.positions[needles] = None
            else:
                hashes = [zlib.crc32(needle) for needle in needles]
                self.positions[needles] = {
                    size: [((h & (size * 8 - 1)) >> 3, 1 << (h & 7),
                            ((h >> 16) & (size * 8 - 1)) >> 3, 1 << ((h >> 16) & 7))
                           for h in hashes]
                    for size in NAME_SUMMARY_SIZES
                }
        positions = self.positions[needles]
        if positions is None:
            return True  # Unicode/punctuation names retain the exact scanner.
        for byte1, bit1, byte2, bit2 in positions[len(data) - 41]:
            if data[41 + byte1] & bit1 and data[41 + byte2] & bit2:
                return True
        if path not in self.checked:
            try:
                stat = (self.repo.root / path).stat()
                current = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                self.checked[path] = current == struct.unpack("<QQQqq", data[1:41])
            except OSError:
                self.checked[path] = False
        return not self.checked[path]


def _parse_file(
    root: Path,
    relative_path: Path,
    languages: set[str] | None = None,
    include_references: bool = True,
    *,
    reference_name: str | frozenset[str] | None = None,
    language: str | None = None,
    header_language: str | None = None,
    collect_bodies: bool = True,
) -> _IndexedFile | None:
    full_path = root / relative_path
    spec = None
    if language is not None:
        candidate = LANGUAGE_BY_NAME.get(language)
        if candidate is not None:
            try:
                _parser_for_language(candidate.name)
            except UnsupportedLanguageError:
                candidate = None
            spec = candidate
    if spec is None:
        spec = _spec_for_path(relative_path, languages, header_language)
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
    bodies: tuple[_CallableBody, ...] = ()
    if reference_name is not None:
        symbols = []
        references = _extract_named_references(source_bytes, root_node, relative_path, spec, reference_name)
    else:
        symbols, references, extracted_bodies = _extract_symbols_and_references(
            source=source_bytes,
            root_node=root_node,
            path=relative_path,
            language=spec,
            include_references=include_references,
            collect_bodies=collect_bodies,
        )
        bodies = tuple(extracted_bodies)
    return _IndexedFile(
        path=relative_path,
        language=spec.name,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        symbols=tuple(symbols),
        references=tuple(references),
        bodies=tuple(bodies),
        name_summary=_file_name_summary(source_bytes, stat) if not include_references and not collect_bodies else None,
        revision=EXTRACTOR_REVISIONS.get(spec.name),
    )


def _parse_file_batch(root, paths, languages, include_references, header_language):
    results = []
    for path in paths:
        try:
            result = _parse_file(root, path, languages, include_references,
                                 header_language=header_language, collect_bodies=include_references)
        except Exception:
            result = None  # One failed file must not discard successful neighbours.
        results.append(result)
    return results


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


def _file_contains_pattern(path: Path, pattern: re.Pattern[bytes], overlap: int) -> bool:
    previous = b""
    try:
        with path.open("rb") as file:
            while chunk := file.read(FILE_SCAN_CHUNK_SIZE):
                window = previous + chunk
                if pattern.search(window) is not None:
                    return True
                previous = window[-overlap:] if overlap else b""
    except OSError:
        return False
    return False


_PARSER_TLS = threading.local()


def _parse_source(parser, source: str):
    # Share immutable trees only within a query. Content is part of the key, so
    # a live edit cannot return an old tree, even inside the same request.
    cache = getattr(_PARSER_TLS, 'query_trees', None)
    key = (id(parser), source) if cache is not None and len(source) <= QUERY_PARSE_MAX_SOURCE_CHARS else None
    if key is not None and key in cache:
        return cache[key]
    # tree-sitter's parse() signature varies across versions/builds: some accept str, others
    # require bytes (raising "source must be a bytestring or a callable, not str"). Try str first,
    # then fall back to encoded bytes so both bindings work. Byte offsets are identical either way.
    try:
        tree = parser.parse(source)
    except TypeError:
        tree = parser.parse(source.encode("utf-8"))
    if key is not None:
        if len(cache) >= QUERY_PARSE_MAX_TREES:
            del cache[next(iter(cache))]
        cache[key] = tree
    return tree


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
            from tree_sitter_language_pack import get_parser

            parser = get_parser(language)
        except Exception as exc:
            raise UnsupportedLanguageError(f"No parser available for language: {language}") from exc
        cache[language] = parser
    return parser


_MEMBER_NAME_FIELDS = ("attribute", "property", "field", "name", "suffix")


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


def _field_children(node: Node, field: str) -> list[Node]:
    """Every child in ``field``, in source order.

    ``child_by_field_name`` returns only the first one, which is why ``int a, b;``
    and Swift's ``let a = 1, b = 2`` used to lose the later names.
    """
    getter = getattr(node, "children_by_field_name", None)
    if getter is not None:
        try:
            return list(getter(field))
        except Exception:
            pass
    return [
        child
        for index, child in enumerate(_node_children(node))
        if _field_name_at(node, index) == field
    ]


def _member_name_node(member: Node | None, language: LanguageSpec) -> Node | None:
    """The name part of a member access node (``obj.NAME``), not the receiver."""
    field = _field_child(member, *_MEMBER_NAME_FIELDS)
    if field is not None:
        # The field may be a wrapper (Swift's ``navigation_suffix``) rather than
        # the identifier itself.
        if _node_kind(field) in language.identifier_node_types:
            return field
        return _first_identifier(field, language) or field
    # Fall back to the rightmost identifier, descending into wrappers (handles
    # grammars without a dedicated property field, such as Kotlin's
    # ``navigation_expression`` -> ``navigation_suffix`` pair).
    for child in reversed(_node_children(member) if member is not None else []):
        found = _first_identifier(child, language)
        if found is not None:
            return found
    return None


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


def _callable_body_node(node: Node, language: LanguageSpec) -> Node | None:
    """The body node of ``node`` when it is a callable that owns its calls."""
    node_types = _CALLABLE_BODY_NODE_TYPES.get(language.name)
    if node_types is None or _node_kind(node) not in node_types:
        return None
    field = _field_child(node, *_BODY_FIELD_NAMES)
    if field is not None:
        return field
    for child in _node_children(node):
        if _node_kind(child) in _BODY_NODE_TYPES:
            return child
    return None


# Containment is half-open; comparison is by byte offset so two callables on the
# same line stay distinguishable.
def _span_contains(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _callable_bodies(source: bytes, node: Node, language: LanguageSpec, symbol: Symbol | None) -> _CallableBody | None:
    body_node = _callable_body_node(node, language)
    if body_node is None:
        return None
    return _CallableBody(
        name=symbol.name if symbol is not None else "",
        name_start_byte=symbol.range.start_byte if symbol is not None else _node_start_byte(node),
        start_byte=_node_start_byte(body_node),
        end_byte=_node_end_byte(body_node),
    )


def _owning_body(bodies: Sequence[_CallableBody], symbol: Symbol) -> _CallableBody | None:
    """The body recorded for ``symbol``, or ``None`` when it has no body here."""
    exact = [
        body
        for body in bodies
        if body.name == symbol.name and body.name_start_byte == symbol.range.start_byte
    ]
    if len(exact) == 1:
        return exact[0]
    containing = [
        body
        for body in bodies
        if body.name == symbol.name and body.start_byte <= symbol.range.start_byte <= body.end_byte
    ]
    if containing:
        return min(containing, key=lambda body: (body.end_byte - body.start_byte, body.start_byte))
    return None


def _callable_is_named(source: bytes, language: LanguageSpec, node: Node) -> bool:
    """Whether a callable node is named by the declaration holding it.

    ``const f = () => x`` and ``auto fn = [] { x };`` name the callable outside
    the callable node, so a two-level look-up covers those grammars while staying
    far away from unrelated ancestors (a lambda inside a function is anonymous).
    """
    candidate = node.parent
    if _declares_call_owner(source, language, node):
        return True
    for _ in range(2):
        if candidate is None:
            return False
        if _declares_call_owner(source, language, candidate):
            name_node = _name_node(candidate, language)
            return name_node is not None and bool(_node_text(source, name_node))
        candidate = candidate.parent
    return False


def _declares_call_owner(source: bytes, language: LanguageSpec, node: Node) -> bool:
    """Whether ``node`` declares something that can own a call."""
    kind = _definition_kind(source, language, node)
    return kind is not None and kind in CALL_OWNER_KINDS


def _descendant_at(root_node: Node, start_byte: int, end_byte: int) -> Node | None:
    getter = getattr(root_node, "descendant_for_byte_range", None)
    if getter is None:
        return None
    try:
        return getter(start_byte, max(end_byte, start_byte + 1))
    except Exception:
        return None


def _call_owner_at(
    root_node: Node, source: bytes, language: LanguageSpec, start_byte: int, end_byte: int
) -> tuple[tuple[int, int], bool] | None:
    """Innermost callable body containing a position, and whether it is named.

    Anonymous bodies (lambdas, closures) are ownership boundaries but publish no
    symbol, so a call inside one belongs to nobody the index can name.
    """
    node = _descendant_at(root_node, start_byte, end_byte)
    span = (start_byte, end_byte)
    while node is not None:
        body = _callable_body_node(node, language)
        if body is not None:
            body_span = (_node_start_byte(body), _node_end_byte(body))
            if _span_contains(body_span, span):
                return body_span, _callable_is_named(source, language, node)
        node = node.parent
    return None


def _opens_with_bracket(node: Node) -> bool:
    """Whether ``node``'s leftmost delimiter is ``[`` rather than ``(``."""
    for child in _node_children(node):
        kind = _node_kind(child)
        if kind in ("[", "("):
            return kind == "["
        return _opens_with_bracket(child)
    return False


def _callee_node(call: Node, language: LanguageSpec) -> Node | None:
    """The callee expression of a call node."""
    field = _field_child(call, *language.callee_field_names)
    if field is not None:
        return field
    children = _node_children(call)
    if language.callee_position == "first_name":
        for child in children:
            if _node_kind(child) in language.identifier_node_types:
                return child
        return None
    if language.callee_position != "first_child" or len(children) < 2:
        return None
    # Swift parses subscripts (``a[i]``) with the same node as calls (``f(i)``);
    # only the argument delimiter tells them apart.
    if _opens_with_bracket(children[1]):
        return None
    return children[0]


def _is_call_callee(node: Node, parent: Node | None, grandparent: Node | None, language: LanguageSpec) -> bool:
    if parent is None:
        return False
    parent_kind = _node_kind(parent)
    if parent_kind in language.call_node_types:
        return _same_node(_callee_node(parent, language), node)
    # Qualified call: ``demo::helper()`` / ``Type::f()``. The callee is the
    # qualified name; its terminal part is the call, and the scope stays as
    # written in the source line that carries the reference.
    if (
        parent_kind in _QUALIFIED_NAME_NODE_TYPES
        and grandparent is not None
        and _node_kind(grandparent) in language.call_node_types
        and _same_node(_callee_node(grandparent, language), parent)
    ):
        return _same_node(_field_child(parent, "name"), node)
    # Method call: ``obj.method()`` — node is the member of a member-access node
    # that is itself the callee of the surrounding call.
    if (
        parent_kind in language.member_node_types
        and grandparent is not None
        and _node_kind(grandparent) in language.call_node_types
    ):
        callee = _callee_node(grandparent, language)
        return _same_node(callee, parent) and _same_node(_member_name_node(parent, language), node)
    return False


def _assignment_target_node(assignment: Node, language: LanguageSpec) -> Node | None:
    field = _field_child(assignment, "left", "name", "target")
    if field is not None:
        return field
    if not language.positional_assignment_target:
        return None
    for child in _node_children(assignment):
        return child
    return None


def _is_write_target(node: Node, parent: Node | None, language: LanguageSpec) -> bool:
    if parent is None or _node_kind(parent) not in language.assignment_node_types:
        return False
    return _same_node(_assignment_target_node(parent, language), node)


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


def _extract_named_references(
    source: bytes, root_node: Node, path: Path, language: LanguageSpec, name: str | frozenset[str],
) -> list[Reference]:
    """Extract requested names without building unrelated symbols or references.

    Prune only subtrees whose byte span cannot contain the name. Visit the
    remaining ancestors normally to preserve import/type/inheritance context
    and transparent-node handling from the full extractor.
    """
    names = frozenset({name}) if isinstance(name, str) else name
    if not names:
        return []
    needle = next(iter(names)).encode("utf-8") if len(names) == 1 else None
    pattern = re.compile(b"|".join(re.escape(value.encode("utf-8")) for value in sorted(names))) if needle is None else None
    line_starts = _line_starts(source)
    lines = source.decode("utf-8", errors="replace").splitlines()
    references: list[Reference] = []

    def walk(node: Node, parent: Node | None, grandparent: Node | None, ctx: frozenset[str]) -> None:
        start, end = _node_start_byte(node), _node_end_byte(node)
        if (source.find(needle, start, end) < 0 if needle is not None else pattern.search(source, start, end) is None):
            return
        kind = _node_kind(node)
        reference_name = _node_text(source, node) if kind in language.identifier_node_types else None
        if reference_name in names:
            references.append(Reference(
                symbol_id="", name=reference_name, language=language.name, path=path,
                range=_node_range(source, node, line_starts),
                context=_line_context(source, lines, node, line_starts),
                reference_kind=_classify_reference(node, parent, grandparent, ctx, language),
            ))
        child_ctx = _child_reference_context(node, parent, ctx, language)
        if kind in language.transparent_node_types:
            child_parent, child_grandparent = parent, grandparent
        else:
            child_parent, child_grandparent = node, parent
        for child in _node_children(node):
            walk(child, child_parent, child_grandparent, child_ctx)

    walk(root_node, None, None, frozenset())
    return references


def _extract_symbols_and_references(
    *,
    source: bytes,
    root_node: Node,
    path: Path,
    language: LanguageSpec,
    include_references: bool = True,
    collect_bodies: bool = True,
) -> tuple[list[Symbol], list[Reference], list[_CallableBody]]:
    symbols: list[Symbol] = []
    references: list[Reference] = []
    bodies: list[_CallableBody] = []
    line_starts = _line_starts(source)
    lines = source.decode("utf-8", errors="replace").splitlines() if include_references else []
    # Hoisted out of the walk: both are constant for the file, and the walk visits
    # every node, so a per-node dict look-up would be paid tens of thousands of times.
    body_node_types = _CALLABLE_BODY_NODE_TYPES.get(language.name) if collect_bodies else None
    has_multi_symbol_rules = language.name in _MULTI_SYMBOL_LANGUAGES

    def walk(
        node: Node,
        container: str | None,
        parent: Node | None,
        grandparent: Node | None,
        ctx: frozenset[str],
        in_function: bool = False,
        scope_kind: str | None = None,
    ) -> None:
        # Every node needs its kind: the declaration rule, the body rule, the
        # reference rule and the transparency rule all branch on it, so it is
        # resolved once instead of once per rule.
        node_kind = _node_kind(node)
        if c_state is not None:
            declared = (
                _c_node_symbols(source, path, language, node, container, scope_kind, line_starts, c_state)
                if node_kind in _C_DECLARATION_NODE_TYPES
                else []
            )
        else:
            declared = (
                _extra_node_symbols(source, path, language, node, container, line_starts)
                if has_multi_symbol_rules
                else None
            )
            if declared is None:
                single = (_symbol_from_node(source, path, language, node, container, line_starts)
                          if node_kind in language.definitions else None)
                declared = (
                    [(single, "container" if single.kind in CONTAINER_KINDS else None)]
                    if single is not None
                    else []
                )
        next_container = container
        next_scope_kind = scope_kind
        next_in_function = in_function
        primary: Symbol | None = None
        for symbol, opens in declared:
            if symbol.kind in FUNCTION_KINDS:
                next_in_function = True
            if in_function and symbol.kind in language.non_local_kinds:
                continue  # a local binding, not a declaration
            symbols.append(symbol)
            if primary is None:
                primary = symbol
            if opens is not None:
                # Only a class, namespace or function opens a scope; two variables
                # from one declaration must never become each other's container.
                next_container = symbol.name if container is None else f"{container}.{symbol.name}"
                if opens != "container":
                    next_scope_kind = opens

        if body_node_types is not None and node_kind in body_node_types:
            # The kind filter is the cheap part: only a plausible callable pays for
            # the body look-up, and the kind test is what ``_callable_body_node``
            # would have checked first anyway.
            body = _callable_bodies(source, node, language, primary)
            if body is not None:
                bodies.append(body)

        if include_references and node_kind in language.identifier_node_types:
            references.append(
                Reference(
                    symbol_id="",
                    name=_node_text(source, node),
                    language=language.name,
                    path=path,
                    range=_node_range(source, node, line_starts),
                    context=_line_context(source, lines, node, line_starts),
                    reference_kind=_classify_reference(node, parent, grandparent, ctx, language),
                )
            )

        child_ctx = _child_reference_context(node, parent, ctx, language) if include_references else ctx
        if node_kind in language.transparent_node_types:
            child_parent, child_grandparent = parent, grandparent
        else:
            child_parent, child_grandparent = node, parent
        # C/C++ declaration nodes are named. A symbol-only write need not visit
        # punctuation/keyword leaves; declaration handlers still see all children.
        children = getattr(node, "named_children", None) if c_state is not None and not include_references else None
        for child in children if children is not None else _node_children(node):
            if c_state is not None and not include_references and getattr(child, 'child_count', 1) == 0:
                continue  # C/C++ declarations own a name child; leaves cannot publish symbols.
            walk(child, next_container, child_parent, child_grandparent, child_ctx, next_in_function, next_scope_kind)

    c_state = _CFileState(source, root_node) if language.name in _C_LANGUAGE_NAMES else None
    walk(root_node, None, None, None, frozenset())
    if language.name == "python":
        symbols.extend(_python_symbols(source, path, language, root_node, line_starts))
    return symbols, references, bodies


def _python_symbols(
    source: bytes, path: Path, language: LanguageSpec, root_node: Node,
    line_starts: Sequence[int] | None = None,
) -> list[Symbol]:
    """Module-level bindings and class-body fields.

    The generic walk publishes nothing for an ``assignment``, so the module and
    class scopes are handled here: every name bound by an assignment target
    becomes a symbol, including chained (``a = b = 1``) and tuple/list targets.
    A class body's assignments are fields of that class; a function body's
    locals are not visited at all.
    """
    symbols: list[Symbol] = []
    for node in _node_children(root_node):
        if _node_kind(node) == "decorated_definition":
            node = node.child_by_field_name("definition")
            if node is None:
                continue
        node_kind = _node_kind(node)
        if node_kind == "assignment":
            symbols.extend(
                _python_assignment_symbols(
                    source, path, language, node, container=None, as_field=False, line_starts=line_starts
                )
            )
        elif node_kind == "class_definition":
            symbols.extend(
                _python_class_field_symbols(source, path, language, node, container=None, line_starts=line_starts)
            )
    return symbols


def _python_class_field_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    class_node: Node,
    container: str | None,
    line_starts: Sequence[int] | None = None,
) -> list[Symbol]:
    """Annotated and plain class-body assignments, plus nested classes."""
    name_node = class_node.child_by_field_name("name")
    class_name = _node_text(source, name_node) if name_node is not None else ""
    if not class_name:
        return []
    qualified = class_name if container is None else f"{container}.{class_name}"
    body = class_node.child_by_field_name("body")
    if body is None:
        return []
    symbols: list[Symbol] = []
    for child in _node_children(body):
        if _node_kind(child) == "decorated_definition":
            child = child.child_by_field_name("definition")
            if child is None:
                continue
        child_kind = _node_kind(child)
        if child_kind == "assignment":
            symbols.extend(
                _python_assignment_symbols(
                    source, path, language, child, container=qualified, as_field=True, line_starts=line_starts
                )
            )
        elif child_kind == "class_definition":
            symbols.extend(
                _python_class_field_symbols(
                    source, path, language, child, container=qualified, line_starts=line_starts
                )
            )
    return symbols


def _python_assignment_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    assignment: Node,
    *,
    container: str | None,
    as_field: bool,
    line_starts: Sequence[int] | None = None,
) -> list[Symbol]:
    """One symbol per name the right-hand chain of ``assignment`` binds."""
    symbols: list[Symbol] = []
    first_name: str | None = None
    for name_node in _python_assignment_bindings(language, assignment):
        name = _node_text(source, name_node)
        if not name or not _looks_like_symbol_name(name):
            continue
        kind = "field" if as_field else ("constant" if name.isupper() else "variable")
        range_ = _node_range(source, name_node, line_starts)
        symbols.append(
            Symbol(
                id=_symbol_id(language.name, path, kind, name, range_.start_byte),
                name=name,
                kind=kind,
                language=language.name,
                path=path,
                range=range_,
                signature=_signature(source, assignment),
                container=container,
            )
        )
        if first_name is None:
            first_name = name
    if first_name is not None and not as_field:
        # Dictionary keys keep their previous container and stay a single set of
        # symbols: a multi-target assignment must not duplicate them.
        symbols.extend(
            _python_dict_key_symbols(
                source, path, language, assignment, container=first_name, line_starts=line_starts
            )
        )
    return symbols


def _python_assignment_bindings(language: LanguageSpec, assignment: Node) -> list[Node]:
    """Name nodes an assignment chain binds, in target order.

    ``a = b = 1`` nests the second target in the first assignment's value field,
    so the chain is followed only while that value is itself an assignment.
    """
    bindings: list[Node] = []
    node: Node | None = assignment
    while node is not None and _node_kind(node) == "assignment":
        left = node.child_by_field_name("left")
        if left is not None:
            bindings.extend(_python_binding_targets(language, left))
        right = node.child_by_field_name("right")
        node = right if right is not None and _node_kind(right) == "assignment" else None
    return bindings


def _python_binding_targets(language: LanguageSpec, node: Node) -> list[Node]:
    """Identifier nodes bound by the target side of an assignment.

    Attribute and subscript targets name objects that already exist, so
    ``obj.value = 1`` defines neither ``obj`` nor ``value``; tuple, list and
    starred patterns contribute every name they bind.
    """
    if _node_kind(node) in language.identifier_node_types:
        return [node]
    if _node_kind(node) in _PYTHON_UNBINDABLE_TARGET_NODE_TYPES:
        return []
    found: list[Node] = []
    for child in _node_children(node):
        found.extend(_python_binding_targets(language, child))
    return found


# Assignment targets that name existing objects or values instead of declaring a
# binding. ``call`` and ``binary_operator`` are not valid Python targets, but a
# grammar error must not turn their contents into definitions.
_PYTHON_UNBINDABLE_TARGET_NODE_TYPES = frozenset(
    {"attribute", "binary_operator", "call", "subscript"}
)


def _python_dict_key_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    assignment: Node,
    *,
    container: str,
    line_starts: Sequence[int] | None = None,
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
        range_ = _node_range(source, key_node, line_starts)
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


_C_LANGUAGE_NAMES = ("c", "cpp")

# Declaration owners: the only C/C++ nodes allowed to publish symbols. Every other
# node contributes nothing, so the traversal never hunts for an arbitrary
# identifier inside an expression.
_C_DECLARATION_NODE_TYPES = frozenset(
    {
        "alias_declaration",
        "class_specifier",
        "declaration",
        "enum_specifier",
        "enumerator",
        "field_declaration",
        "function_definition",
        "namespace_definition",
        "preproc_def",
        "preproc_function_def",
        "struct_specifier",
        "type_definition",
        "union_specifier",
    }
)

_C_SCOPE_NODE_KINDS = {
    "class_specifier": "class",
    "enum_specifier": "enum",
    "namespace_definition": "namespace",
    "struct_specifier": "struct",
    "union_specifier": "struct",  # union reuses struct: no new kind this round
}

# Nodes that carry declarators (possibly several, as in ``int a, b, c;``).
_C_DECLARATOR_NODE_KINDS = ("declaration", "field_declaration", "function_definition", "type_definition")

_C_TYPE_SCOPE_KINDS = frozenset({"class", "struct", "enum"})

# Nodes whose text can be a declared name. ``destructor_name`` and
# ``operator_name`` stay whole so ``~Widget`` and ``operator+`` survive as names.
_C_NAME_NODE_TYPES = frozenset(
    {
        "destructor_name",
        "field_identifier",
        "identifier",
        "operator_name",
        "qualified_identifier",
        "type_identifier",
    }
)

# Fields that never hold the declared name: parameters, initialisers, bodies and
# the type specifier, whose identifiers belong to nested declarations.
_C_NAME_SKIP_FIELDS = frozenset(
    {"arguments", "body", "default_value", "parameters", "size", "type", "value"}
)

# Subtrees skipped while looking for a declared name.
_C_NAME_SKIP_NODE_TYPES = frozenset(
    {
        "argument_list",
        "compound_statement",
        "field_initializer_list",
        "initializer_list",
        "lambda_capture_specifier",
        "parameter_list",
        "parameters",
        "template_argument_list",
        "template_parameter_list",
    }
)

_C_DECLARATOR_OPERATIONS = {
    "array_declarator": "array",
    "function_declarator": "function",
    "pointer_declarator": "pointer",
    "reference_declarator": "reference",
}

# What a resolved declaration means structurally, before scope refinement.
_C_CONSTANT_NODE_KINDS = {
    "enumerator": "constant",
    "preproc_def": "constant",
    "preproc_function_def": "constant",
}


def _field_name_at(node: Node, index: int) -> str | None:
    getter = getattr(node, "field_name_for_child", None)
    if getter is None:
        return None
    try:
        return getter(index)
    except Exception:
        return None


def _declarator_field_children(node: Node) -> list[Node]:
    """Every ``declarator`` field child, in source order."""
    return _field_children(node, "declarator")


def _c_declarator_name_node(declarator: Node) -> Node | None:
    """The declared name inside a declarator, following declarator structure.

    Descends through pointer/reference/array/function/parenthesised/init
    declarators but never into parameters, initialisers or function bodies.
    """
    kind = _node_kind(declarator)
    if kind in _C_NAME_NODE_TYPES:
        return declarator
    if kind in _C_NAME_SKIP_NODE_TYPES:
        return None
    for index, child in enumerate(_node_children(declarator)):
        if _field_name_at(declarator, index) in _C_NAME_SKIP_FIELDS:
            continue
        found = _c_declarator_name_node(child)
        if found is not None:
            return found
    return None


def _c_declarator_operation(name_node: Node, stop: Node) -> str | None:
    """The first binding operation outward from the name, within ``stop``.

    ``int *f(int)`` reads function then pointer (a function); ``int (*fp)(int)``
    reads pointer then function (a variable). Parentheses are not operations.
    """
    node = name_node.parent
    while node is not None and not _same_node(node, stop):
        operation = _C_DECLARATOR_OPERATIONS.get(_node_kind(node))
        if operation is not None:
            return operation
        node = node.parent
    return None


def _c_qualified_name(source: bytes, node: Node) -> tuple[Node, str | None]:
    """Terminal name node and the scope written in the source, if any.

    ``demo::Widget::run`` yields the ``run`` node and ``demo.Widget``; the scope
    is read from ``qualified_identifier`` fields, never from the first identifier
    found in the subtree.
    """
    if _node_kind(node) != "qualified_identifier":
        return node, None
    scope_node = _field_child(node, "scope")
    name_node = _field_child(node, "name")
    if scope_node is None or name_node is None:
        return node, None
    terminal, inner_scope = _c_qualified_name(source, name_node)
    prefix = _node_text(source, scope_node)
    return terminal, prefix if inner_scope is None else f"{prefix}.{inner_scope}"


@dataclass(frozen=True, slots=True)
class _CDeclaration:
    """One declared entity inside a C/C++ declaration node.

    Temporary extraction record: ``definition_node`` is the whole declaration the
    preview must cover, ``name_node`` carries the name's byte range and identity.
    """

    name: str
    kind: str
    name_node: Node
    definition_node: Node
    explicit_scope: str | None = None


def _c_declarator_declarations(source: bytes, node: Node, node_kind: str) -> list[_CDeclaration]:
    records: list[_CDeclaration] = []
    for declarator in _declarator_field_children(node):
        name_node = _c_declarator_name_node(declarator)
        if name_node is None:
            continue
        if node_kind == "type_definition":
            kind = "type"  # typedef context wins: even a function pointer aliases a type
        elif node_kind == "field_declaration":
            kind = "method" if _c_declarator_operation(name_node, node) == "function" else "field"
        elif node_kind == "function_definition":
            kind = "function"
        else:
            kind = "function" if _c_declarator_operation(name_node, node) == "function" else "variable"
        terminal, explicit_scope = _c_qualified_name(source, name_node)
        records.append(
            _CDeclaration(
                name=_node_text(source, terminal),
                kind=kind,
                name_node=terminal,
                definition_node=node,
                explicit_scope=explicit_scope,
            )
        )
    return records


def _c_declarations(source: bytes, node: Node) -> list[_CDeclaration]:
    """Declarations owned by one node; empty for anything else."""
    node_kind = _node_kind(node)
    if node_kind not in _C_DECLARATION_NODE_TYPES:
        return []
    if node_kind in _C_DECLARATOR_NODE_KINDS:
        return _c_declarator_declarations(source, node, node_kind)
    name_node = _field_child(node, "name")
    if name_node is None:
        return []  # anonymous struct/union/enum: no symbol of its own
    scope_kind = _C_SCOPE_NODE_KINDS.get(node_kind)
    if scope_kind is not None:
        return [
            _CDeclaration(
                name=_node_text(source, name_node),
                kind=scope_kind,
                name_node=name_node,
                definition_node=node,
            )
        ]
    kind = _C_CONSTANT_NODE_KINDS.get(node_kind, "type")
    return [
        _CDeclaration(
            name=_node_text(source, name_node),
            kind=kind,
            name_node=name_node,
            definition_node=node,
        )
    ]


# Qualified name nodes (``demo::helper``, ``Type::f``) whose terminal part is the
# entity and whose leading parts are the explicitly written scope.
_QUALIFIED_NAME_NODE_TYPES = ("qualified_identifier", "scoped_identifier", "scoped_type_identifier")


class _CFileState:
    """Per-file scope facts shared by extraction and definition-range lookup."""

    __slots__ = ("root_node", "scanned", "source", "type_scopes")

    def __init__(self, source: bytes, root_node: Node) -> None:
        self.root_node = root_node
        self.source = source
        self.type_scopes: set[str] = set()
        self.scanned = False


def _c_last_segment(name: str | None) -> str | None:
    if not name:
        return None
    return name.rsplit(".", 1)[-1]


def _c_explicit_container(container: str | None, explicit_scope: str) -> str:
    if container and explicit_scope != container and not explicit_scope.startswith(container + "."):
        return f"{container}.{explicit_scope}"
    return explicit_scope


def _c_scope_opened_by(kind: str) -> str | None:
    """The scope kind a symbol introduces, or ``None`` when it introduces none."""
    if kind in _C_TYPE_SCOPE_KINDS:
        return "type"
    if kind == "namespace":
        return "namespace"
    if kind in FUNCTION_KINDS:
        return "function"
    return None


def _c_scan_type_scopes(state: _CFileState) -> None:
    """Collect every type-scope path in the file once, for late definitions."""
    state.scanned = True
    found = state.type_scopes

    def visit(node: Node, container: str | None) -> None:
        child_container = container
        for record in _c_declarations(state.source, node):
            if record.kind in _C_TYPE_SCOPE_KINDS or record.kind == "namespace":
                child_container = record.name if container is None else f"{container}.{record.name}"
                if record.kind in _C_TYPE_SCOPE_KINDS:
                    found.add(child_container)
                container = child_container
        for child in _node_children(node):
            visit(child, child_container)

    visit(state.root_node, None)


def _c_type_scope_known(name: str, state: _CFileState) -> bool:
    if name in state.type_scopes:
        return True
    if not state.scanned:
        _c_scan_type_scopes(state)
    return name in state.type_scopes


def _c_resolved_declarations(
    source: bytes,
    node: Node,
    container: str | None,
    scope_kind: str | None,
    state: _CFileState,
) -> list[tuple[_CDeclaration, str, str | None, str | None]]:
    """Declarations of one node with their final kind, container and scope."""
    resolved: list[tuple[_CDeclaration, str, str | None, str | None]] = []
    for record in _c_declarations(source, node):
        if not record.name or not _looks_like_symbol_name(record.name):
            continue
        kind = record.kind
        container_name = container
        if record.explicit_scope:
            # Only syntax that is explicit is restored; no using-directive or
            # type-inference lookup happens here.
            container_name = _c_explicit_container(container, record.explicit_scope)
            if kind == "function" and _c_type_scope_known(container_name, state):
                kind = "constructor" if record.name == _c_last_segment(container_name) else "method"
        elif kind == "function" and scope_kind == "type":
            kind = "constructor" if record.name == _c_last_segment(container) else "method"
        opens = _c_scope_opened_by(kind)
        if opens == "type":
            state.type_scopes.add(record.name if container is None else f"{container}.{record.name}")
        resolved.append((record, kind, container_name, opens))
    return resolved


def _c_node_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
    scope_kind: str | None,
    line_starts: Sequence[int] | None,
    state: _CFileState,
) -> list[tuple[Symbol, str | None]]:
    symbols: list[tuple[Symbol, str | None]] = []
    for record, kind, container_name, opens in _c_resolved_declarations(
        source, node, container, scope_kind, state
    ):
        range_ = _node_range(source, record.name_node, line_starts)
        symbols.append(
            (
                Symbol(
                    id=_symbol_id(language.name, path, kind, record.name, range_.start_byte),
                    name=record.name,
                    kind=kind,
                    language=language.name,
                    path=path,
                    range=range_,
                    signature=_signature(source, record.definition_node),
                    container=container_name,
                ),
                opens,
            )
        )
    return symbols


def _symbol_from_node(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
    line_starts: Sequence[int] | None = None,
) -> Symbol | None:
    kind = _definition_kind(source, language, node)
    if kind is None:
        return None
    name_node = _name_node(node, language)
    if name_node is None:
        return None
    name = _node_text(source, name_node)
    if not name or not _looks_like_symbol_name(name):
        return None
    range_ = _node_range(source, name_node, line_starts)
    signature = (
        _signature_at_name(source, node, name_node)
        if language.signature_starts_at_name
        else _signature(source, node)
    )
    return Symbol(
        id=_symbol_id(language.name, path, kind, name, range_.start_byte),
        name=name,
        kind=kind,
        language=language.name,
        path=path,
        range=range_,
        signature=signature,
        container=container,
    )


_SWIFT_DECLARATION_KINDS = {
    "class": "class",
    "struct": "struct",
    "enum": "enum",
    "actor": "class",
    "extension": "extension",
}


# Node types a ``variable_declarator`` can hold that make it a function in all
# but name. Keeping these as callable symbols matters for the call graph.
_JS_FUNCTION_VALUE_NODE_TYPES = (
    "arrow_function",
    "function",
    "function_expression",
    "generator_function",
)
_JS_LANGUAGE_NAMES = ("javascript", "typescript", "tsx")


def _kotlin_class_kind(node: Node, default: str) -> str:
    """Kotlin's ``class_declaration`` also covers interfaces and enum classes."""
    for child in _node_children(node):
        child_kind = _node_kind(child)
        if child_kind == "interface":
            return "interface"
        if child_kind == "enum":
            return "enum"
        if child_kind == "class":
            break
    return default


def _js_declarator_kind(node: Node, default: str) -> str:
    """``const f = () => {}`` declares a function, not a variable."""
    value = _field_child(node, "value")
    if value is not None and _node_kind(value) in _JS_FUNCTION_VALUE_NODE_TYPES:
        return "function"
    return default


# Nodes whose extraction is language specific and can yield several symbols.
# The walk consults this table by (language, node kind) and falls back to the
# ordinary single-symbol path on a miss, so languages without an entry never
# build intermediate lists.
_MULTI_SYMBOL_RESOLVERS: dict[tuple[str, str], Any] = {}


def _extra_node_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
    line_starts: Sequence[int] | None,
) -> list[tuple[Symbol, str | None]] | None:
    """Symbols a language-specific resolver owns for ``node``, or ``None``.

    ``None`` means "no special case, use the default rule"; an empty list means
    "this node declares nothing", which is how a resolver drops a node the
    generic rule would misread.
    """
    resolver = _MULTI_SYMBOL_RESOLVERS.get((language.name, _node_kind(node)))
    if resolver is None:
        return None
    return resolver(source, path, language, node, container, line_starts)


def _js_binding_nodes(node: Node) -> list[Node]:
    """Identifier nodes a JS/TS binding pattern declares, in source order.

    Only the binding side is visited: a ``pair_pattern`` binds its value and an
    ``assignment_pattern`` its left side, so ``{first, second: renamed}`` yields
    ``first`` and ``renamed`` -- never the property key ``second`` -- and a
    default value expression contributes no binding.
    """
    kind = _node_kind(node)
    if kind in ("identifier", "shorthand_property_identifier_pattern"):
        return [node]
    if kind in ("object_pattern", "array_pattern", "rest_pattern"):
        found: list[Node] = []
        for child in _node_children(node):
            found.extend(_js_binding_nodes(child))
        return found
    if kind == "pair_pattern":
        value = _field_child(node, "value")
        return [] if value is None else _js_binding_nodes(value)
    if kind in ("object_assignment_pattern", "assignment_pattern"):
        left = _field_child(node, "left")
        return [] if left is None else _js_binding_nodes(left)
    return []


def _js_declarator_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
    line_starts: Sequence[int] | None,
) -> list[tuple[Symbol, str | None]] | None:
    """One symbol per name bound by a destructuring declarator."""
    name_node = node.child_by_field_name("name")
    if name_node is None or _node_kind(name_node) not in ("object_pattern", "array_pattern"):
        return None
    return _binding_symbols(source, path, language, node, container, _js_binding_nodes(name_node), "variable", line_starts)


def _binding_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
    name_nodes: Sequence[Node],
    kind: str,
    line_starts: Sequence[int] | None,
) -> list[tuple[Symbol, str | None]]:
    """One symbol per bound name, all sharing ``node``'s declaration preview."""
    symbols: list[tuple[Symbol, str | None]] = []
    for name_node in name_nodes:
        name = _node_text(source, name_node)
        if not name or name == "_" or not _looks_like_symbol_name(name):
            continue
        range_ = _node_range(source, name_node, line_starts)
        signature = (
            _signature_at_name(source, node, name_node)
            if language.signature_starts_at_name
            else _signature(source, node)
        )
        symbols.append(
            (
                Symbol(
                    id=_symbol_id(language.name, path, kind, name, range_.start_byte),
                    name=name,
                    kind=kind,
                    language=language.name,
                    path=path,
                    range=range_,
                    signature=signature,
                    container=container,
                ),
                None,
            )
        )
    return symbols


def _swift_binding_identifiers(node: Node) -> list[Node]:
    """Identifier nodes a Swift pattern binds.

    ``let a = 1, b = 2`` names one pattern per binding and ``let (a, b) = pair``
    nests plain patterns, so the pattern tree is walked rather than the node's
    first identifier taken.
    """
    if _node_kind(node) in ("simple_identifier", "identifier"):
        return [node]
    bound = _field_child(node, "bound_identifier")
    if bound is not None:
        return _swift_binding_identifiers(bound)
    if _node_kind(node) not in ("pattern", "tuple_pattern"):
        return []
    found: list[Node] = []
    for child in _node_children(node):
        found.extend(_swift_binding_identifiers(child))
    return found


def _swift_property_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
    line_starts: Sequence[int] | None,
) -> list[tuple[Symbol, str | None]] | None:
    """``let a = 1, b = 2`` declares two properties."""
    names: list[Node] = []
    for pattern in _field_children(node, "name"):
        names.extend(_swift_binding_identifiers(pattern))
    if len(names) < 2:
        # A single binding keeps the ordinary path, and so do declarations whose
        # initialiser holds the only name found.
        return None
    return _binding_symbols(source, path, language, node, container, names, "property", line_starts)


def _kotlin_property_symbols(
    source: bytes,
    path: Path,
    language: LanguageSpec,
    node: Node,
    container: str | None,
    line_starts: Sequence[int] | None,
) -> list[tuple[Symbol, str | None]] | None:
    """``val (a, b) = pair`` declares one property per bound name."""
    names: list[Node] = []
    for child in _node_children(node):
        if _node_kind(child) != "multi_variable_declaration":
            continue
        for declaration in _node_children(child):
            if _node_kind(declaration) != "variable_declaration":
                continue
            for identifier in _node_children(declaration):
                if _node_kind(identifier) in language.identifier_node_types:
                    names.append(identifier)
    if not names:
        return None
    return _binding_symbols(source, path, language, node, container, names, "property", line_starts)


_MULTI_SYMBOL_RESOLVERS.update(
    {(name, "variable_declarator"): _js_declarator_symbols for name in _JS_LANGUAGE_NAMES}
)
_MULTI_SYMBOL_RESOLVERS[("swift", "property_declaration")] = _swift_property_symbols
_MULTI_SYMBOL_RESOLVERS[("kotlin", "property_declaration")] = _kotlin_property_symbols
# Languages with at least one resolver, checked before building the (language,
# kind) key for a node: most languages own no special case at all.
_MULTI_SYMBOL_LANGUAGES = frozenset(name for name, _kind in _MULTI_SYMBOL_RESOLVERS)


def _definition_kind(source: bytes, language: LanguageSpec, node: Node) -> str | None:
    """The symbol kind ``node`` defines, or ``None`` if it defines nothing."""
    node_kind = _node_kind(node)
    kind = language.definitions.get(node_kind)
    if kind is None:
        return None
    if language.name == "swift" and node_kind == "class_declaration":
        # Swift folds class/struct/enum/actor/extension into one node type.
        marker = _field_child(node, "declaration_kind")
        if marker is not None:
            return _SWIFT_DECLARATION_KINDS.get(_node_text(source, marker), kind)
    if language.name == "kotlin" and node_kind == "class_declaration":
        return _kotlin_class_kind(node, kind)
    if language.name == "kotlin" and node_kind == "class_parameter":
        # A primary-constructor parameter is only a property when it owns its
        # own ``val``/``var`` keyword; ``class Box(input: Int)`` declares nothing.
        if not any(_node_kind(child) == "binding_pattern_kind" for child in _node_children(node)):
            return None
        return kind
    if language.name in _JS_LANGUAGE_NAMES and node_kind == "variable_declarator":
        return _js_declarator_kind(node, kind)
    return kind


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
        if _node_kind(child) in language.name_skip_node_types:
            continue
        found = _first_identifier(child, language)
        if found is not None:
            return found
    return None


def _node_text(source: bytes, node: Node) -> str:
    return source[_node_start_byte(node) : _node_end_byte(node)].decode("utf-8", errors="replace")


def _signature(source: bytes, node: Node) -> str:
    text = _node_text(source, node).strip()
    return text[:240].splitlines()[0] if text else ""


def _signature_at_name(source: bytes, node: Node, name_node: Node) -> str:
    """The signature line anchored at the name, skipping leading annotations."""
    start = _node_start_byte(node)
    name_start = _node_start_byte(name_node)
    line_start = source.rfind(b"\n", start, name_start) + 1
    if line_start <= start:
        line_start = start
    line_end = source.find(b"\n", name_start)
    if line_end == -1:
        line_end = len(source)
    return source[line_start:line_end].decode("utf-8", errors="replace").strip()[:240]


def _node_range(source: bytes, node: Node, line_starts: Sequence[int] | None = None) -> Range:
    start_byte = _node_start_byte(node)
    end_byte = _node_end_byte(node)
    start_line, start_column = _byte_position(source, start_byte, line_starts)
    end_line, end_column = _byte_position(source, end_byte, line_starts)
    return Range(
        start=Position(line=start_line, column=start_column),
        end=Position(line=end_line, column=end_column),
        start_byte=start_byte,
        end_byte=end_byte,
    )


def _line_starts(source: bytes) -> list[int]:
    return [0, *(match.end() for match in re.finditer(b"\n", source))]


def _byte_position(source: bytes, offset: int, line_starts: Sequence[int] | None = None) -> tuple[int, int]:
    if line_starts is not None:
        line = bisect_right(line_starts, offset) - 1
        return line, offset - line_starts[line]
    prefix = source[:offset]
    line = prefix.count(b"\n")
    line_start = prefix.rfind(b"\n") + 1
    return line, len(prefix) - line_start


def _line_context(source: bytes, lines: list[str], node: Node, line_starts: Sequence[int] | None = None) -> str:
    line, _ = _byte_position(source, _node_start_byte(node), line_starts)
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
        return children if isinstance(children, list) else list(children)
    child_count = _node_value(node, "child_count")
    return [node.child(index) for index in range(child_count)]


def _node_start_byte(node: Node) -> int:
    return _node_value(node, "start_byte")


def _node_end_byte(node: Node) -> int:
    return _node_value(node, "end_byte")


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
        # Text and JSON must resolve the same target: a declaration and its
        # definition are not an ambiguity, the definition is the answer.
        preferred = _preferred_candidate(repo, candidates)
        if preferred is not None:
            candidates = [preferred]
        else:
            lines = ["ambiguous:", "  candidates:"]
            ranges = _result_definition_ranges(repo, candidates[:MAX_INSPECT_CANDIDATES])
            for candidate in candidates[:MAX_INSPECT_CANDIDATES]:
                lines.extend(
                    _format_relation_item(repo, candidate, indent=4, range_=ranges.get(candidate.id, candidate.range))
                )
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


def _defined_symbol_ids(repo: CodeIndex, candidates: Sequence[Symbol]) -> set[str]:
    """Candidate ids that have a body in the current source.

    A declaration and a definition share name, kind and scope, so the body is
    what tells them apart. Files are parsed once for the whole candidate set
    (candidates are already bounded by ``MAX_INSPECT_CANDIDATES``), and the parse
    result is shared within the request instead of re-parsing per candidate.
    """
    by_path: dict[Path, list[Symbol]] = {}
    for symbol in candidates:
        by_path.setdefault(symbol.path, []).append(symbol)
    defined: set[str] = set()
    for path, group in by_path.items():
        language = _file_language(repo, path)
        if language in _C_LANGUAGE_NAMES:
            source = repo.storage.file_source(repo.root, path)
            if source is None:
                continue
            source_bytes = source.encode('utf-8')
            tree = _parse_source(_parser_for_language(language), source)
            root = tree.root_node() if callable(tree.root_node) else tree.root_node
            if hasattr(root, 'descendant_for_byte_range'):
                # Only these bounded candidates matter. Re-extracting every
                # symbol in a large file just to test for a body is unnecessary.
                for symbol in group:
                    start = symbol.range.start_byte
                    if not 0 <= start < len(source_bytes):
                        continue
                    node = root.descendant_for_byte_range(start, start + 1)
                    while node is not None:
                        if any(record.name == symbol.name and _node_start_byte(record.name_node) == start
                               for record in _c_declarations(source_bytes, node)):
                            if _callable_body_node(node, LANGUAGE_BY_NAME[language]) is not None:
                                defined.add(symbol.id)
                            break
                        node = node.parent
                continue
        # The file's own stored language decides the parser, exactly as it does for
        # every other read: parsing a C++ header as C yields an error tree whose
        # bodies would mark the declaration as the owner and hide the definition.
        indexed = _parse_file(
            repo.root, path, repo.languages, include_references=False, language=language
        )
        if indexed is None:
            continue
        for symbol in group:
            if _owning_body(indexed.bodies, symbol) is not None:
                defined.add(symbol.id)
    return defined


def _preferred_candidate(repo: CodeIndex, candidates: Sequence[Symbol]) -> Symbol | None:
    """The one candidate that has a body, when exactly one does.

    A declaration and its definition share name, kind and scope, so the body is the
    navigation target. Scopes are deliberately not compared here, because a
    declaration and its definition legitimately live in different scopes (a Rust
    trait method versus its impl, a Swift protocol member versus its conformance),
    and indexing the declaration must not hide a target that used to resolve.
    Anything less clear-cut stays ambiguous rather than guessing.
    """
    if (len(candidates) > MAX_INSPECT_CANDIDATES
            or len({(candidate.name, candidate.language) for candidate in candidates}) != 1
            or any(candidate.kind not in FUNCTION_KINDS for candidate in candidates)):
        return None
    defined = _defined_symbol_ids(repo, candidates)
    preferred = [candidate for candidate in candidates if candidate.id in defined]
    return preferred[0] if len(preferred) == 1 else None


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
        preferred = _preferred_candidate(repo, candidates)
        if preferred is None:
            raise SymbolNotFoundError(f"Ambiguous symbol: {query}")
        return preferred
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


def _direct_callers_batch(repo: Repository, symbols: list[Symbol], *, limit: int) -> dict[str, tuple[Symbol, ...]]:
    """Share a source prefilter/parse pass among a bounded set of graph nodes."""
    if len(symbols) <= 1 or limit <= 0:
        return {symbol.id: _direct_callers(repo, symbol, limit=limit) for symbol in symbols}
    name_filter = getattr(repo, "_name_filter", None) or _NameFilter(repo)
    by_language: dict[str, list[Symbol]] = {}
    for symbol in symbols:
        by_language.setdefault(symbol.language, []).append(symbol)
    callers: dict[str, tuple[Symbol, ...]] = {}
    for language, targets in by_language.items():
        by_name: dict[str, list[Symbol]] = {}
        found: dict[str, list[Reference]] = {symbol.id: [] for symbol in targets}
        for symbol in targets:
            by_name.setdefault(symbol.name, []).append(symbol)
        names = frozenset(by_name)
        needles = tuple(name.encode("utf-8") for name in sorted(names))
        pattern = re.compile(b"|".join(re.escape(needle) for needle in needles))
        overlap = max(map(len, needles)) - 1
        for path in repo.storage.file_paths(language=language):
            if not name_filter.may_contain(path, needles) or not _file_contains_pattern(repo.root / path, pattern, overlap):
                continue
            indexed = _parse_file(
                repo.root,
                path,
                repo.languages,
                reference_name=names,
                language=repo._stored_language(path),
            )
            if indexed is None:
                continue
            for reference in indexed.references:
                if reference.reference_kind != "call":
                    continue
                for target in by_name[reference.name]:
                    references = found[target.id]
                    if len(references) >= limit:
                        continue
                    if (reference.path == target.path
                            and reference.range.start_byte == target.range.start_byte
                            and reference.range.end_byte == target.range.end_byte):
                        continue
                    references.append(reference)
            if all(len(references) >= limit for references in found.values()):
                break
        for target in targets:
            callers[target.id] = _callers_for_symbol(repo, target, tuple(found[target.id]), limit=limit)
    return callers


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
        batch: dict[str, tuple[Symbol, ...]] = {}
        for index, current in enumerate(frontier):
            if direction == "callers" and isinstance(repo, Repository) and len(frontier) > 1:
                if current.id not in batch:
                    # Avoid prefetching a wide batch when the graph is about to
                    # hit its node limit. Results are still consumed in BFS order.
                    width = min(CALLER_SCAN_BATCH_SIZE, max(1, max_nodes - len(visited) + 1))
                    batch = _direct_callers_batch(repo, frontier[index:index + width], limit=limit)
                neighbours = batch[current.id]
            else:
                neighbours = step(current)
            for neighbour in neighbours:
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
    trees_cache: dict[Path, tuple[Node | None, bytes]] = {}
    callers: list[Symbol] = []
    seen: set[str] = set()
    for reference in references:
        if reference.reference_kind != 'call':
            continue
        path = reference.path
        if path not in file_symbols_cache:
            file_symbols = repo.storage.symbols_in_file(path)
            file_symbols_cache[path] = file_symbols
            # One read and one parse per file: this request needs the definition
            # ranges and the per-reference owner from the same tree.
            source = repo.storage.file_source(repo.root, path)
            ranges, tree = _definition_ranges_and_tree(repo, path, file_symbols, source=source)
            def_ranges_cache[path] = ranges
            trees_cache[path] = (tree, source.encode("utf-8") if source is not None else b"")
        tree, source_bytes = trees_cache[path]
        owner = None
        body_known = False
        if tree is not None and source_bytes:
            spec = _file_spec(repo, path)
            body_known = spec is not None and spec.name in _CALLABLE_BODY_NODE_TYPES
            owner = (
                _call_owner_at(tree, source_bytes, spec, reference.range.start_byte, reference.range.end_byte)
                if spec is not None
                else None
            )
            if owner is not None and not owner[1]:
                # The call sits in an anonymous callable's body: that body is an
                # ownership boundary, and no public symbol stands for it.
                continue
        caller = _enclosing_symbol(
            file_symbols_cache[path], def_ranges_cache[path], reference.range, exclude_id=symbol.id,
            owner_span=owner[0] if owner is not None else None, body_known=body_known,
        )
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
    ref_kinds: frozenset[str] | None = frozenset({'call'}),
    loose: bool = False,
) -> tuple[Symbol, ...]:
    if limit <= 0:
        return ()
    indexed = _parse_file(repo.root, symbol.path, repo.languages, language=_file_language(repo, symbol.path))
    if indexed is None:
        return ()
    known = repo.storage.symbol_names_by_language().get(symbol.language, set())
    definition_spans = {(candidate.range.start_byte, candidate.range.end_byte) for candidate in indexed.symbols}
    owner = _owning_body(indexed.bodies, symbol)
    if owner is not None:
        owner_span = (owner.start_byte, owner.end_byte)
        # A call inside a nested callable belongs to that callable, not to the
        # callable being queried: depth-1 must not leak inner calls outwards.
        nested_spans = [
            (body.start_byte, body.end_byte)
            for body in indexed.bodies
            if body is not owner and _span_contains(owner_span, (body.start_byte, body.end_byte))
        ]
    else:
        # No body recorded for this symbol: keep the declaration span, and stay
        # conservative because nested boundaries cannot be identified here.
        owner_span = (range_.start_byte, range_.end_byte)
        nested_spans = []
    # A reference must not scan every nested function. Coalesce body intervals
    # once, then find the only possible containing interval by binary search.
    merged: list[tuple[int, int]] = []
    for start, end in sorted(nested_spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    nested_starts = [start for start, _ in merged]
    names: list[str] = []
    seen_names: set[str] = set()
    for reference in indexed.references:
        if not _span_contains(owner_span, (reference.range.start_byte, reference.range.end_byte)):
            continue
        nested_index = bisect_right(nested_starts, reference.range.start_byte) - 1
        if nested_index >= 0 and reference.range.end_byte <= merged[nested_index][1]:
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
        candidate = _resolve_callee(repo, name, symbol, loose=loose, bodies=indexed.bodies)
        if candidate is not None and candidate.id not in seen_ids:
            callees.append(candidate)
            seen_ids.add(candidate.id)
    return tuple(callees)


def _resolve_callee(
    repo: CodeIndex,
    name: str,
    symbol: Symbol,
    *,
    loose: bool,
    bodies: Sequence[_CallableBody] | None = None,
) -> Symbol | None:
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
        # A declaration and its definition can both live in this file; prefer the
        # one with a body so adding a declaration does not steal the target. The
        # caller passes the file's already-parsed bodies: no extra parse here.
        defined = [
            candidate
            for candidate in same_file
            if any(
                body.name == candidate.name and body.name_start_byte == candidate.range.start_byte
                for body in bodies or ()
            )
        ]
        if len(same_file) > 1 and len(defined) == 1:
            return defined[0]
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
    symbols: list[Symbol], def_ranges: dict[str, Range], range_: Range, *, exclude_id: str,
    owner_span: tuple[int, int] | None = None, body_known: bool = False,
) -> Symbol | None:
    """The innermost callable or container whose byte range contains ``range_``.

    Line-based containment cannot separate two definitions that share a line, so
    positions are compared by byte offset (half-open). A symbol with no known
    definition range is skipped instead of falling back to its name range: a
    name range would masquerade as a body and produce a plausible-looking but
    wrong owner.
    """
    span = owner_span if owner_span is not None else (range_.start_byte, range_.end_byte)
    candidates: list[tuple[Symbol, Range]] = []
    for symbol in symbols:
        if symbol.id == exclude_id or symbol.kind not in CALL_OWNER_KINDS:
            continue
        if body_known and owner_span is None and symbol.kind in FUNCTION_KINDS:
            continue  # A default argument outside every callable body is not called by that function.
        body = def_ranges.get(symbol.id)
        if body is None:
            continue
        if _span_contains((body.start_byte, body.end_byte), span):
            candidates.append((symbol, body))
    if not candidates:
        return None
    # Innermost definition first (deepest start, then tightest end), then
    # callables over containers, then a stable id so ties are not incidental.
    return max(
        candidates,
        key=lambda item: (
            item[1].start_byte,
            -item[1].end_byte,
            item[0].kind in FUNCTION_KINDS,
            item[0].id,
        ),
    )[0]


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


def _c_native_definition_ranges(
    source: bytes,
    root_node: Node,
    wanted: dict[tuple[str, int], Symbol],
    line_starts: Sequence[int] | None,
    state: _CFileState,
) -> dict[str, Range] | None:
    """Definition spans found by native descent, or ``None`` to traverse instead.

    A result page holds few symbols, so each name is located in native code and its
    ancestors are walked upwards. The enclosing scopes are rebuilt from those same
    ancestors, which keeps a class member's ``method``/``constructor`` kind. ``None``
    means the traversal has to decide: too many symbols at once, a stale byte
    position, or a name whose resolved kind the reconstruction does not confirm.
    """
    from tree_sitter import Node

    if not isinstance(root_node, Node) or len(wanted) > NATIVE_DEFINITION_MAX_SYMBOLS:
        return None
    ranges: dict[str, Range] = {}
    for (kind, start), symbol in wanted.items():
        if not 0 <= start < len(source):
            return None
        ancestors: list[Node] = []
        node = root_node.descendant_for_byte_range(start, start + 1)
        while node is not None:
            ancestors.append(node)
            node = node.parent
        located: int | None = None
        for index, ancestor in enumerate(ancestors):
            if any(_node_start_byte(record.name_node) == start for record in _c_declarations(source, ancestor)):
                located = index
                break
        if located is None:
            return None
        container: str | None = None
        scope_kind: str | None = None
        for ancestor in reversed(ancestors[located + 1 :]):
            for record, _kind, _name, opens in _c_resolved_declarations(
                source, ancestor, container, scope_kind, state
            ):
                if opens is not None:
                    container = record.name if container is None else f"{container}.{record.name}"
                    scope_kind = opens
        matched = False
        for record, resolved_kind, _name, _opens in _c_resolved_declarations(
            source, ancestors[located], container, scope_kind, state
        ):
            if _node_start_byte(record.name_node) != start:
                continue
            if resolved_kind != kind:
                return None
            ranges[symbol.id] = _node_range(source, record.definition_node, line_starts)
            matched = True
            break
        if not matched:
            return None
    return ranges


def _c_definition_ranges_for_symbols(
    source: bytes,
    root_node: Node,
    wanted: dict[tuple[str, int], Symbol],
    line_starts: Sequence[int] | None,
) -> dict[str, Range]:
    """Definition spans for C/C++ symbols using the extraction's own rules.

    Extraction and this lookup both resolve declarations through
    ``_c_resolved_declarations``, so a second declarator (``int a, b;``), a
    destructor name or a class-scoped definition preview all match, and a symbol
    whose description is not found keeps its name range instead of guessing.

    ``_c_native_definition_ranges`` answers a small lookup first, in native code;
    the traversal below stays the reference for what it cannot confirm.
    """
    ranges: dict[str, Range] = {}
    if not wanted:
        return ranges
    state = _CFileState(source, root_node)
    native = _c_native_definition_ranges(source, root_node, wanted, line_starts, state)
    if native is not None:
        return native

    def visit(node: Node, container: str | None, scope_kind: str | None) -> None:
        if len(ranges) >= len(wanted):
            return
        next_container = container
        next_scope_kind = scope_kind
        for record, kind, _container_name, opens in _c_resolved_declarations(
            source, node, container, scope_kind, state
        ):
            symbol = wanted.get((kind, _node_start_byte(record.name_node)))
            if symbol is not None and symbol.id not in ranges:
                ranges[symbol.id] = _node_range(source, record.definition_node, line_starts)
            if opens is not None:
                next_container = record.name if container is None else f"{container}.{record.name}"
                if opens != "container":
                    next_scope_kind = opens
        for child in _node_children(node):
            visit(child, next_container, next_scope_kind)

    visit(root_node, None, None)
    return ranges


def _definition_range(repo: CodeIndex, symbol: Symbol) -> Range | None:
    return _definition_ranges_for_symbols(repo, symbol.path, (symbol,)).get(symbol.id)


def _definition_ranges_for_symbols(
    repo: CodeIndex,
    path: Path,
    symbols: Iterable[Symbol],
    *,
    source: str | None = None,
) -> dict[str, Range]:
    return _definition_ranges_and_tree(repo, path, symbols, source=source)[0]


def _definition_name_keys(
    source: bytes, path: Path, spec: LanguageSpec, node: Node, line_starts: Sequence[int],
) -> tuple[tuple[str, int], ...]:
    extra = _extra_node_symbols(source, path, spec, node, None, line_starts)
    if extra is not None:
        return tuple((symbol.kind, symbol.range.start_byte) for symbol, _ in extra)
    if spec.name == 'python' and _node_kind(node) == 'assignment':
        parent = node.parent
        while parent is not None and _node_kind(parent) not in ('class_definition', 'function_definition'):
            parent = parent.parent
        if parent is not None and _node_kind(parent) == 'function_definition':
            return ()
        symbols = _python_assignment_symbols(
            source, path, spec, node, container=None, as_field=parent is not None, line_starts=line_starts,
        )
        return tuple((symbol.kind, symbol.range.start_byte) for symbol in symbols)
    kind = _definition_kind(source, spec, node)
    if kind is not None:
        name = _name_node(node, spec)
        if name is not None:
            return ((kind, _node_start_byte(name)),)
    return ()


def _definition_ranges_and_tree(
    repo: CodeIndex,
    path: Path,
    symbols: Iterable[Symbol],
    *,
    source: str | None = None,
) -> tuple[dict[str, Range], Node | None]:
    """Definition ranges plus the parse they came from.

    Callers that also need per-reference ownership reuse the same parse instead
    of paying a second one: every source file is read and parsed once per request.
    """
    symbols = tuple(symbols)
    if not symbols:
        return {}, None
    from tree_sitter import Node

    if source is None:
        source = repo.storage.file_source(repo.root, path)
    if source is None:
        return {}, None
    spec = _file_spec(repo, path)
    if spec is None:
        return {}, None
    source_bytes = source.encode("utf-8")

    wanted: dict[tuple[str, int], Symbol] = {
        (symbol.kind, symbol.range.start_byte): symbol
        for symbol in symbols
    }
    ranges: dict[str, Range] = {}
    line_starts = _line_starts(source_bytes)
    tree = _parse_source(_parser_for_language(spec.name), source)
    root_node = tree.root_node() if callable(tree.root_node) else tree.root_node

    # Locate a few names in native code instead of walking every AST node in
    # Python. Large outlines still benefit from one sequential traversal.
    if spec.name in _C_LANGUAGE_NAMES:
        return _c_definition_ranges_for_symbols(source_bytes, root_node, wanted, line_starts), root_node

    declaration_keys: dict[tuple[int, int, str], tuple[tuple[str, int], ...]] = {}

    def name_keys(node: Node) -> tuple[tuple[str, int], ...]:
        key = (_node_start_byte(node), _node_end_byte(node), _node_kind(node))
        if key not in declaration_keys:
            declaration_keys[key] = _definition_name_keys(source_bytes, path, spec, node, line_starts)
        return declaration_keys[key]

    if isinstance(root_node, Node) and len(wanted) <= NATIVE_DEFINITION_MAX_SYMBOLS:
        for symbol in wanted.values():
            start = symbol.range.start_byte
            if not 0 <= start < len(source_bytes):
                break  # Malformed/stale positions use the original traversal.
            # Only the start is part of the existing match contract: a live
            # edit can shorten the name while the indexed end is still old.
            node = root_node.descendant_for_byte_range(start, start + 1)
            match = None
            ambiguous = False
            while node is not None:
                if (symbol.kind, start) in name_keys(node):
                    if match is not None:
                        ambiguous = True
                        break
                    match = node
                node = node.parent
            if ambiguous:
                break  # Preserve traversal order for nested grammar wrappers.
            if match is not None:
                ranges[symbol.id] = _node_range(source_bytes, match, line_starts)
        else:
            return ranges, root_node
        ranges.clear()

    def walk(node: Node) -> None:
        if len(ranges) >= len(wanted):
            return
        for key in _definition_name_keys(source_bytes, path, spec, node, line_starts):
            symbol = wanted.get(key)
            if symbol is not None:
                ranges[symbol.id] = _node_range(source_bytes, node, line_starts)
        for child in _node_children(node):
            walk(child)

    walk(root_node)
    return ranges, root_node


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


def _result_definition_ranges(repo: CodeIndex, symbols: Iterable[Symbol]) -> dict[str, Range]:
    by_path: dict[Path, list[Symbol]] = {}
    for symbol in symbols:
        by_path.setdefault(symbol.path, []).append(symbol)
    return {
        symbol_id: range_
        for path, file_symbols in by_path.items()
        for symbol_id, range_ in _definition_ranges_for_symbols(repo, path, file_symbols).items()
    }


def _format_relation_item(repo: CodeIndex, symbol: Symbol, *, indent: int, range_: Range | None = None) -> list[str]:
    range_ = range_ or _definition_range(repo, symbol) or symbol.range
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
    ranges = _result_definition_ranges(repo, (item for item in items[:limit] if isinstance(item, Symbol)))
    for item in items[:limit]:
        if isinstance(item, Reference):
            lines.extend(_format_reference_item(item, indent=2))
        else:
            lines.extend(_format_relation_item(repo, item, indent=2, range_=ranges.get(item.id, item.range)))
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
        ranges = _result_definition_ranges(repo, (item for item in page.items if isinstance(item, Symbol)))
        for item in page.items:
            if isinstance(item, Reference):
                lines.extend(_format_reference_item(item, indent=4))
            else:
                lines.extend(_format_relation_item(repo, item, indent=4, range_=ranges.get(item.id, item.range)))
    return "\n".join(lines) + "\n"


def _symbol_location(symbol: Symbol) -> str:
    return f"{symbol.name}  {symbol.path.as_posix()}:{_display_line(symbol.range.start.line)}"


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
    ranges = _result_definition_ranges(repo, symbols)
    for symbol in symbols:
        range_ = ranges.get(symbol.id, symbol.range)
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
        f"range: 1:{total_lines}",  # the whole file, 1-based and inclusive
        f"count: {len(symbols)}",
    ]
    if symbol is not None:
        lines.append(f"symbol: {symbol}")
    if page.has_more:
        lines.append("has_more: true")
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


def _read_git_text(path: Path, limit: int = 4096) -> str:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Git metadata exceeds bounded check")
    return data.decode("utf-8").strip()


def _git_state(root: Path) -> str | None:
    """Bounded Git-files check, no subprocess or worktree traversal.

    None means unknown, never a clean bill of health. Include the symbolic HEAD
    to detect branch switches even when two branches point at the same commit.
    Remote-tracking refs and FETCH_HEAD are deliberately not part of the state.
    """
    try:
        for directory in (root, *root.parents):
            marker = directory / ".git"
            if marker.is_dir():
                git_dir = marker
                break
            try:
                text = _read_git_text(marker)
            except FileNotFoundError:
                continue
            if not text.startswith("gitdir: "):
                return None
            git_dir = (directory / text[8:]).resolve()
            break
        else:
            return "no-git"
        try:
            common_dir = (git_dir / _read_git_text(git_dir / "commondir")).resolve()
        except FileNotFoundError:
            common_dir = git_dir
        head = _read_git_text(git_dir / "HEAD")
        value = head
        for _ in range(5):
            if not value.startswith("ref: "):
                if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
                    return json.dumps([str(git_dir), head, value.lower()])
                return None
            ref = value[5:]
            if (not ref.startswith("refs/") or "\\" in ref
                    or any(part in {"", ".", ".."} for part in ref.split("/"))):
                return None
            ref_dir = git_dir if ref.startswith(("refs/bisect/", "refs/worktree/", "refs/rewritten/")) else common_dir
            try:
                value = _read_git_text(ref_dir / ref)
                continue
            except FileNotFoundError:
                pass
            # A missing loose ref is not evidence of an unborn reftable HEAD.
            if (common_dir / "reftable").exists():
                return None
            try:
                packed = _read_git_text(common_dir / "packed-refs", GIT_PACKED_REFS_MAX_BYTES)
            except FileNotFoundError:
                packed = ""
            for line in packed.splitlines():
                fields = line.split(" ", 1)
                if len(fields) == 2 and fields[1] == ref:
                    value = fields[0]
                    break
            else:
                return json.dumps([str(git_dir), head, "unborn"])
        return None
    except (OSError, ValueError):
        return None


def _git_baseline(root: Path, before: str | None) -> str:
    # Record the checkout at scan start, never acknowledge a newer HEAD that
    # appeared while parsing/writing. The next query compares it with live Git.
    return json.dumps([str(root), before])


def _git_freshness(root: Path, baseline: str | None) -> str:
    current = _git_state(root)
    if baseline is not None:
        try:
            saved = json.loads(baseline)
            if isinstance(saved, list) and len(saved) == 2 and isinstance(saved[1], str):
                if saved[0] != str(root):
                    return "changed"
                if current is not None:
                    if saved[1] != current:
                        return "changed"
                    return "not-applicable" if current == "no-git" else "unchanged"
        except (ValueError, TypeError):
            pass
    return "not-applicable" if current == "no-git" else "unknown"


def _warn_git_freshness(repo: Repository) -> None:
    row = repo.storage.connection.execute("SELECT value FROM meta WHERE key = 'git_baseline'").fetchone()
    freshness = _git_freshness(repo.root, row[0] if row else None)
    if freshness == "changed":
        sys.stderr.write("warning: index may be stale: Git checkout changed; "
                         "run `code-symbol-index index` or repeat with --sync (incremental).\n")
    elif freshness == "unknown":
        sys.stderr.write("warning: Git freshness unknown: baseline missing or Git metadata unavailable; "
                         "use `status --check` to check files, or `index` to record a baseline (incremental).\n")


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
        data = _read_index_metadata(index_path, include_files=check)
        pending_files: tuple[str, ...] = ()
        if check:
            pending_changes, pending_files = _pending_index_changes(
                root=root,
                languages=languages,
                include=include,
                exclude=exclude,
                indexed_files=data["indexed_files"],
                max_files=max_pending_files,
                header_language=data["header_language"],
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
    git_freshness = _git_freshness(root, data["git_baseline"])
    is_stale = (is_schema_stale or (isinstance(pending_changes, int) and pending_changes > 0)
                or (not check and git_freshness == "changed"))
    if is_schema_stale:
        reason = "index schema is out of date"
    elif isinstance(pending_changes, int) and pending_changes > 0:
        reason = "files changed after last index update"
    elif not check and git_freshness == "changed":
        reason = "Git checkout changed; index may be stale"
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
        git_freshness=git_freshness,
    )


def _read_index_metadata(db_path: Path, *, include_files: bool = True) -> dict[str, Any]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        schema_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone()
        updated_at_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'updated_at'",
        ).fetchone()
        git_row = connection.execute("SELECT value FROM meta WHERE key = 'git_baseline'").fetchone()
        header_row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (HEADER_LANGUAGE_META,),
        ).fetchone() if include_files else None
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
        ).fetchall() if include_files else []
    finally:
        connection.close()

    return {
        "schema_version": int(schema_row["value"]) if schema_row is not None else None,
        "updated_at": updated_at_row["value"] if updated_at_row is not None else None,
        "git_baseline": git_row["value"] if git_row is not None else None,
        "header_language": header_row["value"] if header_row is not None else DEFAULT_HEADER_LANGUAGE,
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
    header_language: str | None = None,
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
        header_language=header_language,
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
    if index_status.git_freshness is not None:
        lines.append(f"  git_freshness: {index_status.git_freshness}")
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
        f"  range: {_display_line(start)}:{_display_line(end - 1)}",
        f"  shown_range: {_display_line(start)}:{_display_line(shown_end - 1)}",
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

    for line_number, line in enumerate(shown_lines, start=_display_line(start)):
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
        for line_number, line in enumerate(shown_lines, start=_display_line(start))
    )
    return SourceAnchor(
        path=path,
        start_line=_display_line(start),
        end_line=_display_line(shown_end - 1),
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
    import hashlib

    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:HASHLINE_HASH_CHARS]


def _display_line(index: int) -> int:
    """Convert an internal 0-based line index to the 1-based number shown to callers.

    Line numbers are 0-based everywhere inside the index (tree-sitter positions, SQLite rows,
    containment checks, source slicing) and 1-based only once they leave through text, JSON, or a
    result object, so they line up with grep, editors, tracebacks, and diffs. Ranges are inclusive
    on both ends after conversion: an exclusive 0-based `end` is the same number as the inclusive
    1-based last line, so pass `end - 1` when converting a half-open bound.
    """
    return index + 1


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
        lines.append(f"    - range: {_display_line(chunk_start)}:{_display_line(chunk_end - 1)}")
        lines.append(f"      label: {labels[index]}")
        cursor = chunk_end
    return lines


def _line_range(range_: Range) -> str:
    return f"{_display_line(range_.start.line)}:{_display_line(range_.end.line)}"


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


def _compile_path_patterns(patterns: Iterable[str]) -> Any:
    """One regex for a whole exclude list, matched instead of per-pattern fnmatch."""
    alternatives: list[str] = []
    for pattern in patterns:
        normalised = os.path.normcase(pattern)
        alternatives.append(fnmatch.translate(normalised))
        if normalised.endswith("/**"):
            # ``dir/**`` also covers ``dir`` itself, so the walk can prune the
            # directory instead of descending into it and excluding each file
            # separately. The directory half stays a glob, so ``bazel-*/**``
            # prunes ``bazel-out`` and not only the paths beneath it.
            directory = normalised[:-3].rstrip("/")
            if directory:
                alternatives.append(fnmatch.translate(directory))
    if not alternatives:
        return lambda _path_text: None
    return re.compile("|".join(alternatives)).match


def _parent_prefix(prefix: str) -> str:
    """``"a/b/"`` -> ``"a/"``; ``"a/"`` and ``""`` -> ``""``."""
    if not prefix:
        return ""
    cut = prefix.rfind("/", 0, len(prefix) - 1)
    return "" if cut == -1 else prefix[: cut + 1]


def _extension_of(path_text: str) -> str:
    """``Path(path_text).suffix.lower()`` without building a Path."""
    dot = path_text.rfind(".")
    # A dot leading the basename (``.gitignore``) is not a suffix.
    if dot <= path_text.rfind("/") + 1:
        return ""
    return path_text[dot:].lower()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple):
        return list(value)
    return value


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Position):
        # The only place a raw Position leaves the library. Positions stay 0-based in memory
        # because they index into source lines, and are converted here so that every serialized
        # line number -- text, CLI JSON, API JSON -- is 1-based.
        return {"line": _display_line(value.line), "column": _display_line(value.column)}
    if is_dataclass(value) and not isinstance(value, type):
        # Walked one level at a time rather than with asdict(), which would flatten nested
        # dataclasses before the Position branch above could see them.
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
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
        "line": _display_line(symbol.range.start.line),
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
        "line": _display_line(reference.range.start.line),
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
        target = stream if stream is not None else sys.stderr
        isatty = getattr(target, "isatty", None)
        self.interactive = bool(isatty()) if callable(isatty) else False
        self._last_bucket = -1
        self._last_write_percent = -1
        self._line_open = False
        # Private CLI hook: preserve the public Repository callback protocol.
        self._storage_progress = self._write_progress if self.interactive else None

    def _write_progress(self, event: str, *, done: int = 0, total: int = 0) -> None:
        if not self.interactive:
            return
        stream = self.stream if self.stream is not None else sys.stderr
        if event in {"write_start", "write_tick", "commit_batch"}:
            if event == "write_start":
                self._last_write_percent = -1
                if self._line_open:
                    stream.write("\n")
                    self._line_open = False
            # Rows are counted by the existing writer; this is work completed,
            # not a time estimate. Final transaction commit is a separate stage.
            percent = min(100, max(0, done * 100 // total)) if total else 0
            if percent <= self._last_write_percent:
                return
            self._last_write_percent = percent
            prefix = "\r" if self._line_open else ""
            stream.write(f"{prefix}writing index... {percent}%")
            self._line_open = True
            stream.flush()
            return
        messages = {
            "delete_start": "removing old index entries...",
            "finalize": "committing index...",
        }
        message = messages.get(event)
        if message is None:
            return
        if self._line_open:
            stream.write("\n")
            self._line_open = False
        stream.write(message + "\n")
        stream.flush()

    def __call__(
        self,
        event: str,
        *,
        done: int = 0,
        total: int = 0,
        path: str | None = None,
    ) -> None:
        # Agent/tool captures need results, not progress logs. In a terminal,
        # overwrite bounded percentage milestones, retaining the final line.
        stream = self.stream if self.stream is not None else sys.stderr
        new_stage = event == "start" or (event == "summary" and done == 0)
        if self.interactive and self._line_open and (new_stage or event == "finish"):
            stream.write("\n")
            stream.flush()
            self._line_open = False
        if event == "upgrade":
            # One-time cost of a new extraction rule; only a terminal shows it.
            if self.interactive:
                stream.write(f"re-extracting {total} files to apply updated extraction rules (one-time)\n")
                stream.flush()
            return
        if event == "finish" and done < total:
            stream.write(f"warning: {total - done}/{total} files could not be indexed; "
                         "existing index entries for them were kept. "
                         "check file readability/encoding or exclude unsupported files. "
                         "Git freshness only tracks the checkout.\n")
            stream.flush()
            return
        if not self.interactive or total <= 0:
            return
        if event not in {"start", "file", "summary"}:
            return
        if new_stage:
            self._last_bucket = -1
        percent = min(100, max(0, done * 100 // total))
        bucket = percent // 10
        if bucket <= self._last_bucket:
            return
        self._last_bucket = bucket
        stage = "query summaries" if event == "summary" else "indexing"
        prefix = "\r" if self._line_open else ""
        suffix = "\n" if done >= total else ""
        message = f"{prefix}{stage} {done}/{total} files ({percent}%){suffix}"
        self._line_open = not suffix
        stream.write(message)
        stream.flush()


def _add_index_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Codebase root. Defaults to the current directory.")
    parser.add_argument("--language", action="append", dest="languages", help="Language to include. Repeatable.")
    parser.add_argument("--include", action="append", default=(), help="Glob include pattern. Repeatable.")
    parser.add_argument("--exclude", action="append", default=(), help="Glob exclude pattern. Repeatable.")
    parser.add_argument("--db", help="SQLite index path. Defaults to .code-symbol-index/index.sqlite.")
    parser.add_argument(
        "--header-language",
        choices=HEADER_LANGUAGES,
        default=None,
        help="Language for .h files (default: c, unchanged). Saved for later writes; "
             "changing it requires an unfiltered index run.",
    )


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
        import argparse

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
        import argparse

        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _search_limit(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > MAX_SEARCH_LIMIT:
        import argparse

        raise argparse.ArgumentTypeError(f"must be <= {MAX_SEARCH_LIMIT}")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        import argparse

        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _depth(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > MAX_CALL_DEPTH:
        import argparse

        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_CALL_DEPTH}")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    import argparse

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


def _update_check_cache_path() -> Path:
    """Cache file for the update check, in the user's platform cache directory."""
    override = os.environ.get(UPDATE_CHECK_CACHE_ENV)
    if override:
        return Path(override)
    if sys.platform == "darwin":
        base = Path("~/Library/Caches").expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "~/.cache")).expanduser()
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    return base / "code-symbol-index" / "update-check.json"


def _read_update_check_cache(path: Path) -> dict:
    """Read the cached update-check state; missing or corrupt files count as empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_update_check_cache(path: Path, data: dict) -> None:
    """Best-effort atomic write of the update-check state; failures are ignored."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def _fetch_latest_version() -> str | None:
    """Fetch the latest stable version from PyPI, or None on any failure."""
    import urllib.request

    request = urllib.request.Request(PYPI_JSON_URL, headers={"User-Agent": f"code-symbol-index/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=UPDATE_CHECK_TIMEOUT_S) as response:
            payload = json.loads(response.read())
        version = payload["info"]["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return version if isinstance(version, str) and version else None


def _is_newer_release(latest: str, current: str) -> bool:
    """True when `latest` is a newer stable release than `current` (pre-releases ignored)."""
    def parse(version: str) -> tuple[int, ...] | None:
        parts = version.split(".")
        if len(parts) > 4 or not all(part.isdigit() for part in parts):
            return None  # pre-release or malformed; never suggest an upgrade to it
        return tuple(int(part) for part in parts)

    latest_parts = parse(latest)
    current_parts = parse(current)
    if latest_parts is None or current_parts is None:
        return False
    return latest_parts > current_parts


class _UpdateCheck:
    """Background PyPI check that hints once per interval on stderr."""

    def __init__(self, state: dict, thread: threading.Thread | None = None) -> None:
        self._state = state
        self._thread = thread

    @classmethod
    def start(cls) -> _UpdateCheck | None:
        """Start a check unless disabled or already done for this interval."""
        if os.environ.get(UPDATE_CHECK_ENV_DISABLE):
            return None
        state = _read_update_check_cache(_update_check_cache_path())
        if time_ns() - state.get("last_check_ns", 0) < UPDATE_CHECK_INTERVAL_NS:
            if state.get("hint_emitted"):
                return None
            return cls(state)  # hint is due but was never shown; no network needed
        check = cls(state)
        check._thread = threading.Thread(target=check._run, daemon=True, name="code-symbol-index-update")
        check._thread.start()
        return check

    def _run(self) -> None:
        self._state = {
            "last_check_ns": time_ns(),
            "latest_version": _fetch_latest_version(),
        }
        _write_update_check_cache(_update_check_cache_path(), self._state)

    def emit(self) -> None:
        """Wait briefly for the check and print the hint on stderr if due."""
        if self._thread is not None:
            self._thread.join(timeout=UPDATE_CHECK_JOIN_TIMEOUT_S)
        if self._thread is not None and self._thread.is_alive():
            return  # check unfinished; the next run retries within this interval
        version = self._state.get("latest_version")
        if not isinstance(version, str) or not _is_newer_release(version, __version__):
            return
        self._state["hint_emitted"] = True
        _write_update_check_cache(_update_check_cache_path(), self._state)
        sys.stderr.write(
            f"note: code-symbol-index {version} is available (installed {__version__}); "
            "upgrade: uv tool upgrade code-symbol-index\n"
        )


def main(argv: list[str] | None = None) -> int:
    update_check = _UpdateCheck.start()
    try:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        if raw_args == ["version"]:
            print(f"code-symbol-index {__version__}")
            return 0
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

        if args.command not in {"index", "update"}:
            _warn_git_freshness(repo)

        if args.command == "index":
            repo.refresh(header_language=getattr(args, "header_language", None))
            header_language, converted, pending = repo.last_header_language
            if pending and converted < pending:
                # The saved setting only means "future writes": never claim a
                # conversion that still has files on their previous language.
                sys.stderr.write(
                    f"warning: header language for future writes is {header_language}, but "
                    f"{pending - converted}/{pending} header files could not be converted and keep "
                    "their previous language; rerun index to retry them\n"
                )
            elif pending and converted == pending:
                sys.stderr.write(
                    f"header language for future writes is {header_language}: "
                    f"converted {converted} header files\n"
                )
            _print_json({"index": str(Path(repo.storage.db_path)), "root": str(repo.root)})
        elif args.command == "update":
            repo.update(args.paths)
            payload = {
                "index": str(Path(repo.storage.db_path)),
                "root": str(repo.root),
                "updated": list(repo.last_update_updated),
            }
            if repo.last_update_failed:
                # Never report a failed path as updated: its previous rows stand.
                payload["failed"] = list(repo.last_update_failed)
            _print_json(payload)
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
                print(_format_outline_text(repo, repo._relative_path(Path(args.path)), page, symbol=args.symbol), end="")
        else:
            parser.error(f"unknown command: {args.command}")
        if getattr(repo, "_name_summary_unavailable", False):
            sys.stderr.write("hint: query summaries are not ready; run `code-symbol-index index` "
                             "to prepare them without rebuilding unchanged ASTs.\n")
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
    except HeaderLanguageError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except SymbolNotFoundError as exc:
        sys.stderr.write(f"{exc}; narrow with --path/--kind/--exact-only\n")
        return 2
    finally:
        if update_check is not None:
            update_check.emit()


__all__ = [
    "BinaryFileError",
    "CallGraph",
    "CallNode",
    "CodeIndex",
    "CodeSymbolIndexError",
    "EntryPoint",
    "HashLine",
    "HeaderLanguageError",
    "ImportItem",
    "IndexNotFoundError",
    "IndexStatus",
    "InspectOptions",
    "Inspection",
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
    "inspect",
    "inspect_text",
    "install_skill",
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
