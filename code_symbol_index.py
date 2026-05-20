from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import pathspec
from tree_sitter import Node
from tree_sitter_language_pack import get_parser


SCHEMA_VERSION = 3
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
DEFAULT_MAX_OUTLINE_SYMBOLS = 200
MAX_INSPECT_CANDIDATES = 20
SYMBOL_QUERY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")

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
class Inspection:
    definition: Symbol
    references: tuple[Reference, ...]
    implementations: tuple[Symbol, ...]
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


LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python",
        extensions=(".py", ".pyi"),
        definitions={
            "class_definition": "class",
            "function_definition": "function",
        },
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

    def update(self, paths: Iterable[str | Path] | None = None) -> CodeIndex:
        if paths is None:
            return self.build()
        relative_paths = [self._relative_path(Path(path)) for path in paths]
        self.storage.remove_files(relative_paths)
        self._index_files(path for path in relative_paths if self._should_index(path))
        return self

    def search_symbols(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        return self.storage.search_symbols(
            query,
            kind=kind,
            language=language,
            limit=limit,
        )

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        return self.search_symbols(query, kind=kind, language=language, limit=limit)

    def best_symbol(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
    ) -> Symbol:
        return self._resolve_symbol(query, kind=kind, language=language)

    def inspect(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> Inspection:
        return self._inspect(_resolve_inspect_symbol(self, query, kind=kind, language=language), limit=limit)

    def refs(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        return self.find_references(query, kind=kind, language=language, limit=limit, offset=offset)

    def impls(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        return self.find_implementations(query, kind=kind, language=language, limit=limit, offset=offset)

    def inspect_symbol(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> Inspection:
        return self._inspect(_resolve_inspect_symbol(self, query, kind=kind, language=language), limit=limit)

    def inspect_text(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        options: InspectOptions | None = None,
    ) -> str:
        return _inspect_text(self, query, kind=kind, language=language, options=options or InspectOptions())

    def search_text(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> str:
        return _format_search_text(self, query, self.search(query, kind=kind, language=language, limit=limit))

    def outline(
        self,
        path: str | Path,
        *,
        max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    ) -> Page:
        relative_path = self._relative_path(Path(path))
        symbols = self.storage.symbols_in_file(relative_path)
        return _page_from_extra(symbols[: max_symbols + 1], limit=max_symbols, offset=0)

    def outline_text(
        self,
        path: str | Path,
        *,
        max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    ) -> str:
        relative_path = self._relative_path(Path(path))
        return _format_outline_text(self, relative_path, self.outline(relative_path, max_symbols=max_symbols))

    def find_references(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        symbol = self._resolve_symbol(query, kind=kind, language=language)
        return self.storage.references_for(symbol, limit=limit, offset=offset)

    def find_implementations(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        symbol = self._resolve_symbol(query, kind=kind, language=language)
        return self.storage.implementation_candidates(symbol, limit=limit, offset=offset)

    def _inspect(self, symbol: Symbol, *, limit: int = DEFAULT_PAGE_LIMIT) -> Inspection:
        references = self.storage.references_for(symbol, limit=limit, offset=0)
        implementations = self.storage.implementation_candidates(symbol, limit=limit, offset=0)
        source = self.storage.file_source(self.root, symbol.path)
        preview = _source_preview(source, symbol.range) if source is not None else None
        return Inspection(
            definition=symbol,
            references=references.items,
            implementations=implementations.items,
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
        kind: str | None = None,
        language: str | None = None,
    ) -> Symbol:
        symbol = self.storage.get_symbol(query)
        if symbol is not None:
            return symbol

        matches = self.search_symbols(query, kind=kind, language=language, limit=1)
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

    def refresh(self) -> Repository:
        if self.storage.schema_version() != SCHEMA_VERSION:
            self.storage.reset_schema()

        if self.progress is not None:
            self.progress("scan", done=0, total=0)
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
        if self.progress is not None:
            self.progress("start", done=0, total=total)
        indexed_results = self._parse_files(to_index, include_references=False)
        self.storage.replace_files(
            deleted_paths=deleted,
            indexed_files=indexed_results,
            schema_version=SCHEMA_VERSION,
            progress=self.progress,
        )
        if self.progress is not None:
            self.progress("finish", done=total, total=total)
        return self

    def build(self) -> Repository:
        self.storage.clear()
        paths = list(self._iter_indexable_files())
        total = len(paths)
        if self.progress is not None:
            self.progress("start", done=0, total=total)
        indexed_results = self._parse_files(paths, include_references=False)
        self.storage.replace_files(
            deleted_paths=(),
            indexed_files=indexed_results,
            schema_version=SCHEMA_VERSION,
            progress=self.progress,
        )
        if self.progress is not None:
            self.progress("finish", done=total, total=total)
        return self

    def _parse_files(self, paths: list[Path], *, include_references: bool = True) -> list[_IndexedFile]:
        if not paths:
            return []
        if len(paths) == 1 or MAX_WORKERS <= 1:
            results = []
            for done, path in enumerate(paths, start=1):
                result = _parse_file(self.root, path, self.languages, include_references=include_references)
                if result is not None:
                    results.append(result)
                if self.progress is not None:
                    self.progress("tick", done=done, total=len(paths))
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
                if self.progress is not None:
                    self.progress("tick", done=done, total=len(paths))
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
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        return super().search_symbols(query, kind=kind, language=language, limit=limit)

    def find_references(
        self,
        query: str,
        *,
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> Page:
        symbol = self._resolve_symbol(query, kind=kind, language=language)
        return self._references_for_symbol(symbol, limit=limit, offset=offset)

    def _inspect(self, symbol: Symbol, *, limit: int = DEFAULT_PAGE_LIMIT) -> Inspection:
        references = self._references_for_symbol(symbol, limit=limit, offset=0)
        implementations = self.storage.implementation_candidates(symbol, limit=limit, offset=0)
        source = self.storage.file_source(self.root, symbol.path)
        preview = _source_preview(source, symbol.range) if source is not None else None
        return Inspection(
            definition=symbol,
            references=references.items,
            implementations=implementations.items,
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
    query: str,
    *,
    root: str | Path = ".",
    kind: str | None = None,
    language: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    sync: bool = False,
) -> list[Symbol]:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.search(query, kind=kind, language=language, limit=limit)


def search_text(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | None = None,
    language: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    sync: bool = False,
) -> str:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.search_text(query, kind=kind, language=language, limit=limit)


def outline(
    path: str | Path,
    *,
    root: str | Path = ".",
    max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    sync: bool = False,
) -> Page:
    repo = Repository(root)
    if sync:
        repo.refresh()
    return repo.outline(path, max_symbols=max_symbols)


def outline_text(
    path: str | Path,
    *,
    root: str | Path = ".",
    max_symbols: int = DEFAULT_MAX_OUTLINE_SYMBOLS,
    sync: bool = False,
) -> str:
    repo = Repository(root)
    if sync:
        repo.refresh()
    return repo.outline_text(path, max_symbols=max_symbols)


def best_symbol(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | None = None,
    language: str | None = None,
    sync: bool = False,
) -> Symbol:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.best_symbol(query, kind=kind, language=language)


def inspect(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | None = None,
    language: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    sync: bool = False,
) -> Inspection:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.inspect(query, kind=kind, language=language, limit=limit)


def inspect_text(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | None = None,
    language: str | None = None,
    max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_callers: int = DEFAULT_MAX_CALLERS,
    max_callees: int = DEFAULT_MAX_CALLEES,
    max_references: int = DEFAULT_MAX_REFERENCES,
    max_implementors: int = DEFAULT_MAX_IMPLEMENTORS,
    sync: bool = False,
) -> str:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.inspect_text(
        query,
        kind=kind,
        language=language,
        options=InspectOptions(
            max_source_chars=max_source_chars,
            max_total_chars=max_total_chars,
            max_members=max_members,
            max_callers=max_callers,
            max_callees=max_callees,
            max_references=max_references,
            max_implementors=max_implementors,
        ),
    )


def refs(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | None = None,
    language: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sync: bool = False,
) -> Page:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.refs(query, kind=kind, language=language, limit=limit, offset=offset)


def impls(
    query: str,
    *,
    root: str | Path = ".",
    kind: str | None = None,
    language: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    sync: bool = False,
) -> Page:
    repo = Repository(root, languages=_languages_filter(language))
    if sync:
        repo.refresh()
    return repo.impls(query, kind=kind, language=language, limit=limit, offset=offset)


def status(
    root: str | Path = ".",
    *,
    language: str | None = None,
    db_path: str | Path | None = None,
    check: bool = False,
) -> IndexStatus:
    return _index_status(
        root=Path(root).resolve(),
        languages=_languages_filter(language),
        include=(),
        exclude=(),
        db_path=Path(db_path) if db_path is not None else None,
        check=check,
    )


def status_text(
    root: str | Path = ".",
    *,
    language: str | None = None,
    db_path: str | Path | None = None,
    check: bool = False,
) -> str:
    return _format_status_text(status(root, language=language, db_path=db_path, check=check))


def index(
    root: str | Path = ".",
    *,
    language: str | None = None,
) -> Repository:
    repo = Repository(root, languages=_languages_filter(language), create_index=True)
    return repo.refresh()


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
        kind: str | None = None,
        language: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[Symbol]:
        if limit <= 0:
            return []

        normalized_query = query.strip()
        if normalized_query and self.has_symbol_fts and len(normalized_query) >= 3:
            try:
                rows = self._search_symbols_fts(
                    normalized_query,
                    kind=kind,
                    language=language,
                    limit=limit,
                )
                return [_symbol_from_row(row) for row in rows]
            except sqlite3.OperationalError:
                self.has_symbol_fts = False

        return self._search_symbols_like(
            normalized_query,
            kind=kind,
            language=language,
            limit=limit,
        )

    def _search_symbols_fts(
        self,
        query: str,
        *,
        kind: str | None,
        language: str | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        clauses = []
        values: list[object] = [f"name : {_escape_fts_query(query)}"]
        if kind is not None:
            clauses.append("symbols.kind = ?")
            values.append(kind)
        if language is not None:
            clauses.append("symbols.language = ?")
            values.append(language)

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
        kind: str | None,
        language: str | None,
        limit: int,
    ) -> list[Symbol]:
        clauses = []
        values: list[object] = []
        if query:
            clauses.append("name COLLATE NOCASE LIKE ? ESCAPE '\\'")
            values.append(f"%{_escape_like(query)}%")
        if kind is not None:
            clauses.append("kind = ?")
            values.append(kind)
        if language is not None:
            clauses.append("language = ?")
            values.append(language)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_values: list[object] = []
        if query:
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
    ) -> Page:
        _validate_pagination(limit=limit, offset=offset)
        rows = self.connection.execute(
            """
            SELECT * FROM refs
            WHERE name = ? AND language = ?
              AND NOT (path = ? AND start_byte = ? AND end_byte = ?)
            ORDER BY path, start_byte
            LIMIT ?
            OFFSET ?
            """,
            (
                symbol.name,
                symbol.language,
                symbol.path.as_posix(),
                symbol.range.start_byte,
                symbol.range.end_byte,
                limit + 1,
                offset,
            ),
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

    tree = _parser_for_language(spec.name).parse(source_text)
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


@lru_cache(maxsize=None)
def _parser_for_language(language: str):
    if language not in LANGUAGE_BY_NAME:
        raise UnsupportedLanguageError(f"Unsupported language: {language}")
    try:
        return get_parser(language)
    except Exception as exc:
        raise UnsupportedLanguageError(f"No parser available for language: {language}") from exc


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

    def walk(node: Node, container: str | None) -> None:
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
                    range=_node_range(node),
                    context=_line_context(lines, node),
                )
            )

        for child in _node_children(node):
            walk(child, next_container)

    walk(root_node, None)
    return symbols, references


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
    range_ = _node_range(name_node)
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


def _node_range(node: Node) -> Range:
    start_line, start_column = _point(_node_start_point(node))
    end_line, end_column = _point(_node_end_point(node))
    return Range(
        start=Position(line=start_line, column=start_column),
        end=Position(line=end_line, column=end_column),
        start_byte=_node_start_byte(node),
        end_byte=_node_end_byte(node),
    )


def _point(point: Any) -> tuple[int, int]:
    if hasattr(point, "row"):
        return point.row, point.column
    return point[0], point[1]


def _line_context(lines: list[str], node: Node) -> str:
    line, _ = _point(_node_start_point(node))
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
    kind: str | None,
    language: str | None,
    options: InspectOptions,
) -> str:
    invalid_reason = _invalid_symbol_query_reason(query, repo.root)
    if invalid_reason is not None:
        return _bounded_text(f"invalid_input:\n  reason: {invalid_reason}\n", options.max_total_chars)

    candidates = _inspect_candidates(repo, query, kind=kind, language=language)
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
    members = _members_for_symbol(repo, symbol, limit=options.max_members)
    references = _references_for_inspect(repo, symbol, limit=options.max_references)
    implementors = (
        tuple(repo.storage.implementation_candidates(symbol, limit=options.max_implementors, offset=0).items)
        if options.max_implementors > 0
        else ()
    )
    callers = _callers_for_symbol(repo, symbol, references, limit=options.max_callers)
    callees = _callees_for_symbol(repo, symbol, source_range, limit=options.max_callees)

    lines = ["symbol:"]
    lines.extend(_format_symbol_fields(symbol, indent=2, range_=source_range))
    lines.extend(_format_source_block(source, source_range, options.max_source_chars))
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
    kind: str | None,
    language: str | None,
) -> list[Symbol]:
    if "." in query:
        container, name = query.rsplit(".", 1)
        matches = repo.search_symbols(name, kind=kind, language=language, limit=MAX_INSPECT_CANDIDATES + 1)
        matches = [
            symbol
            for symbol in matches
            if symbol.name == name and symbol.container is not None and symbol.container.split(".")[-1] == container
        ]
    else:
        matches = repo.search_symbols(query, kind=kind, language=language, limit=MAX_INSPECT_CANDIDATES + 1)
        exact = [symbol for symbol in matches if symbol.name == query]
        prefix = [symbol for symbol in matches if symbol.name.startswith(query)]
        matches = exact or prefix
    return matches


def _resolve_inspect_symbol(
    repo: CodeIndex,
    query: str,
    *,
    kind: str | None,
    language: str | None,
) -> Symbol:
    invalid_reason = _invalid_symbol_query_reason(query, repo.root)
    if invalid_reason is not None:
        raise SymbolNotFoundError(invalid_reason)
    candidates = _inspect_candidates(repo, query, kind=kind, language=language)
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


def _references_for_inspect(repo: CodeIndex, symbol: Symbol, *, limit: int) -> tuple[Reference, ...]:
    if limit <= 0:
        return ()
    if isinstance(repo, Repository):
        return tuple(repo._references_for_symbol(symbol, limit=limit, offset=0).items)
    return tuple(repo.storage.references_for(symbol, limit=limit, offset=0).items)


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
    callers: list[Symbol] = []
    seen: set[str] = set()
    for reference in references:
        file_symbols = file_symbols_cache.setdefault(reference.path, repo.storage.symbols_in_file(reference.path))
        caller = _enclosing_symbol(file_symbols, reference.range, exclude_id=symbol.id)
        if caller is None or caller.id in seen:
            continue
        callers.append(caller)
        seen.add(caller.id)
        if len(callers) >= limit:
            break
    return tuple(callers)


def _callees_for_symbol(repo: CodeIndex, symbol: Symbol, range_: Range, *, limit: int) -> tuple[Symbol, ...]:
    if limit <= 0:
        return ()
    indexed = _parse_file(repo.root, symbol.path, repo.languages)
    if indexed is None:
        return ()
    known = repo.storage.symbol_names_by_language().get(symbol.language, set())
    names: list[str] = []
    seen_names: set[str] = set()
    for reference in indexed.references:
        if reference.range.start.line < range_.start.line or reference.range.start.line >= range_.end.line + 1:
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
        for candidate in repo.search_symbols(name, language=symbol.language, limit=1):
            if candidate.id not in seen_ids:
                callees.append(candidate)
                seen_ids.add(candidate.id)
            break
    return tuple(callees)


def _enclosing_symbol(symbols: list[Symbol], range_: Range, *, exclude_id: str) -> Symbol | None:
    candidates = [
        symbol
        for symbol in symbols
        if symbol.id != exclude_id
        and symbol.range.start.line <= range_.start.line
        and range_.start.line < symbol.range.end.line
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda symbol: symbol.range.start.line)


def _definition_range(repo: CodeIndex, symbol: Symbol) -> Range | None:
    source = repo.storage.file_source(repo.root, symbol.path)
    if source is None:
        return None
    spec = _spec_for_path(symbol.path, repo.languages)
    if spec is None:
        return None
    source_bytes = source.encode("utf-8")
    tree = _parser_for_language(spec.name).parse(source)
    root_node = tree.root_node() if callable(tree.root_node) else tree.root_node

    def walk(node: Node) -> Range | None:
        kind = spec.definitions.get(_node_kind(node))
        if kind == symbol.kind:
            name_node = _name_node(node, spec)
            if name_node is not None and _node_start_byte(name_node) == symbol.range.start_byte:
                return _node_range(node)
        for child in _node_children(node):
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(root_node)


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
        f"{prefix}  context: {reference.context}",
    ]


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


def _format_search_text(repo: CodeIndex, query: str, symbols: list[Symbol]) -> str:
    lines = [f"query: {query}", f"count: {len(symbols)}", "", "symbols:"]
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
        lines.append(f"    score: {_match_score(query, symbol)}")
        if symbol.language:
            lines.append(f"    language: {symbol.language}")
        if symbol.container:
            lines.append(f"    container: {symbol.container}")
    return "\n".join(lines) + "\n"


def _format_outline_text(repo: CodeIndex, path: Path, page: Page) -> str:
    source = repo.storage.file_source(repo.root, path)
    total_lines = len(source.splitlines()) if source is not None else 0
    symbols = tuple(item for item in page.items if isinstance(item, Symbol))
    lines = [
        f"file: {path.as_posix()}",
        f"range: 0:{total_lines}",
        f"count: {len(symbols)}",
    ]
    if page.has_more:
        lines.append(f"has_more: true")
        lines.append(f"limit: {page.limit}")
    lines.extend(["", "outline:"])
    if not symbols:
        lines.append("  []")
        return "\n".join(lines) + "\n"

    for symbol in symbols:
        depth = _outline_depth(symbol)
        range_ = _definition_range(repo, symbol) or symbol.range
        indent = "  " * (depth + 1)
        lines.append(f"{indent}{symbol.kind} {symbol.name} {_line_range(range_)} {symbol.signature}")
    return "\n".join(lines) + "\n"


def _outline_depth(symbol: Symbol) -> int:
    if not symbol.container:
        return 0
    return min(symbol.container.count(".") + 1, 8)


def _index_status(
    *,
    root: Path,
    languages: Iterable[str] | None,
    include: Iterable[str],
    exclude: Iterable[str],
    db_path: Path | None,
    check: bool,
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
        if check:
            pending_changes: int | str = _pending_index_changes(
                root=root,
                languages=languages,
                include=include,
                exclude=exclude,
                indexed_files=data["indexed_files"],
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
) -> int:
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
    for path_text, stat_info in current_files.items():
        if filtered_indexed_files.get(path_text) != stat_info:
            pending += 1
    for path_text in filtered_indexed_files:
        if path_text not in current_files:
            pending += 1
    return pending


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


def _format_source_block(source: str, range_: Range, max_source_chars: int) -> list[str]:
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

    lines = [
        "source:",
        f"  status: {status}",
        f"  range: {start}:{end}",
        f"  shown_range: {start}:{shown_end}",
        f"  total_lines: {total_lines}",
        "",
    ]
    for line_number, line in enumerate(shown_lines, start=start):
        lines.append(f"  {line_number} |{line}")
    if status == "truncated":
        lines.extend(_format_chunks(start, end, shown_end))
    return lines


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
    if isinstance(value, (Symbol, Reference, Inspection, IndexStatus, Page, Position, Range)):
        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(_to_jsonable(value), default=_json_default, ensure_ascii=False, indent=2))


def _print_cli_json(value: Any) -> None:
    print(json.dumps(_to_cli_jsonable(value), ensure_ascii=False, indent=2))


def _to_cli_jsonable(value: Any) -> Any:
    if isinstance(value, Page):
        return _readable_page(value)
    if isinstance(value, Symbol):
        return _readable_symbol(value)
    if isinstance(value, Reference):
        return _readable_reference(value)
    if isinstance(value, Inspection):
        return _readable_inspection(value)
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
        "context": reference.context,
    }


def _readable_inspection(inspection: Inspection) -> dict[str, Any]:
    result: dict[str, Any] = {
        "definition": _readable_symbol(inspection.definition),
    }
    if inspection.source_preview:
        result["source"] = inspection.source_preview
    result["references"] = [_readable_reference(reference) for reference in inspection.references]
    result["references_has_more"] = inspection.references_has_more
    if inspection.references_next_offset is not None:
        result["references_next_offset"] = inspection.references_next_offset
    result["implementations"] = [_readable_symbol(symbol) for symbol in inspection.implementations]
    result["implementations_has_more"] = inspection.implementations_has_more
    if inspection.implementations_next_offset is not None:
        result["implementations_next_offset"] = inspection.implementations_next_offset
    return result


class _CliProgress:
    def __init__(self, stream: Any | None = None) -> None:
        self.stream = stream
        self.visible = False

    def __call__(self, event: str, *, done: int, total: int) -> None:
        stream = self.stream if self.stream is not None else sys.stderr
        if event == "scan":
            stream.write("\rscanning files...")
            stream.flush()
            self.visible = True
            return
        if event == "delete_start":
            stream.write("\rremoving stale index entries...")
            stream.flush()
            self.visible = True
            return
        if event == "delete_finish":
            stream.write("\rremoved stale index entries")
            stream.flush()
            self.visible = True
            return
        if event == "write_start":
            stream.write("\r" + _progress_line(done, total, label="writing index", unit="rows"))
            stream.flush()
            self.visible = True
            return
        if event == "write_tick":
            stream.write("\r" + _progress_line(done, total, label="writing index", unit="rows"))
            stream.flush()
            self.visible = True
            return
        if event == "commit_batch":
            stream.write("\rcommitting batch...")
            stream.flush()
            self.visible = True
            return
        if event == "finalize":
            stream.write("\rfinalizing index...")
            stream.flush()
            self.visible = True
            return
        if event == "finish":
            if self.visible:
                stream.write("\n")
                stream.flush()
            self.visible = False
            return
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
    parser.add_argument("--kind", help="Prefer or filter by symbol kind.")
    parser.add_argument("--sync", action="store_true", help="Refresh the index before querying.")


def _add_page_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=_positive_int, default=DEFAULT_PAGE_LIMIT)
    parser.add_argument("--offset", type=_non_negative_int, default=0)


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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-symbol-index")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search symbols in a codebase.")
    _add_index_options(search)
    search.add_argument("query")
    _add_match_options(search)
    search.add_argument("--limit", type=_search_limit, default=DEFAULT_SEARCH_LIMIT)
    search.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    inspect = subparsers.add_parser("inspect", help="Inspect the best symbol match for a keyword.")
    _add_index_options(inspect)
    inspect.add_argument("query")
    _add_match_options(inspect)
    inspect.add_argument("--limit", type=_positive_int, default=DEFAULT_PAGE_LIMIT)
    inspect.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")
    inspect.add_argument("--max-source-chars", type=_positive_int, default=DEFAULT_MAX_SOURCE_CHARS)
    inspect.add_argument("--max-total-chars", type=_positive_int, default=DEFAULT_MAX_TOTAL_CHARS)
    inspect.add_argument("--max-members", type=_non_negative_int, default=DEFAULT_MAX_MEMBERS)
    inspect.add_argument("--max-callers", type=_non_negative_int, default=DEFAULT_MAX_CALLERS)
    inspect.add_argument("--max-callees", type=_non_negative_int, default=DEFAULT_MAX_CALLEES)
    inspect.add_argument("--max-references", type=_non_negative_int, default=DEFAULT_MAX_REFERENCES)
    inspect.add_argument("--max-implementors", type=_non_negative_int, default=DEFAULT_MAX_IMPLEMENTORS)

    refs = subparsers.add_parser("refs", help="Find references for the best symbol match.")
    _add_index_options(refs)
    refs.add_argument("query")
    _add_match_options(refs)
    _add_page_options(refs)
    refs.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    impls = subparsers.add_parser("impls", help="Find implementation candidates for the best symbol match.")
    _add_index_options(impls)
    impls.add_argument("query")
    _add_match_options(impls)
    _add_page_options(impls)
    impls.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    outline_parser = subparsers.add_parser("outline", help="Print an indexed file outline.")
    _add_index_options(outline_parser)
    outline_parser.add_argument("path")
    outline_parser.add_argument("--sync", action="store_true", help="Refresh the index before querying.")
    outline_parser.add_argument("--max-symbols", type=_positive_int, default=DEFAULT_MAX_OUTLINE_SYMBOLS)
    outline_parser.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    status_parser = subparsers.add_parser("status", help="Print index status.")
    _add_index_options(status_parser)
    status_parser.add_argument("--check", action="store_true", help="Scan files to compute stale state and pending changes.")
    status_parser.add_argument("--json", action="store_true", help="Print JSON instead of LLM-friendly text.")

    index_parser = subparsers.add_parser("index", help="Refresh the on-disk code-symbol-index index.")
    _add_index_options(index_parser)

    clean_parser = subparsers.add_parser("clean", help="Delete the on-disk code-symbol-index index.")
    clean_parser.add_argument("--root", default=".", help="Codebase root. Defaults to the current directory.")

    languages = subparsers.add_parser("languages", help="Print configured languages with available parsers.")
    languages.set_defaults(command="languages")
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
    )


def main(argv: list[str] | None = None) -> int:
    try:
        raw_args = list(sys.argv[1:] if argv is None else argv)
        commands = {"search", "inspect", "refs", "impls", "outline", "status", "index", "clean", "languages"}
        if raw_args and raw_args[0] not in commands and not raw_args[0].startswith("-"):
            raw_args.insert(0, "search")

        parser = build_arg_parser()
        args = parser.parse_args(raw_args)
        if args.command == "languages":
            _print_json(list(supported_languages()))
            return 0
        if args.command == "clean":
            clean(args.root)
            return 0
        if args.command == "status":
            payload = _index_status(
                root=Path(args.root).resolve(),
                languages=args.languages,
                include=args.include,
                exclude=args.exclude,
                db_path=Path(args.db) if args.db is not None else None,
                check=args.check,
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
        elif args.command == "search":
            symbols = repo.search(args.query, kind=args.kind, language=language, limit=args.limit)
            if args.json:
                _print_cli_json(symbols)
            else:
                print(_format_search_text(repo, args.query, symbols), end="")
        elif args.command == "inspect":
            if args.json:
                _print_cli_json(repo.inspect(args.query, kind=args.kind, language=language, limit=args.limit))
            else:
                print(
                    repo.inspect_text(
                        args.query,
                        kind=args.kind,
                        language=language,
                        options=_inspect_options_from_args(args),
                    ),
                    end="",
                )
        elif args.command == "refs":
            page = repo.refs(args.query, kind=args.kind, language=language, limit=args.limit, offset=args.offset)
            if args.json:
                _print_cli_json(page)
            else:
                print(_format_page_text(repo, "references", page), end="")
        elif args.command == "impls":
            page = repo.impls(args.query, kind=args.kind, language=language, limit=args.limit, offset=args.offset)
            if args.json:
                _print_cli_json(page)
            else:
                print(_format_page_text(repo, "implementors", page), end="")
        elif args.command == "outline":
            page = repo.outline(args.path, max_symbols=args.max_symbols)
            if args.json:
                _print_cli_json(page)
            else:
                print(repo.outline_text(args.path, max_symbols=args.max_symbols), end="")
        else:
            parser.error(f"unknown command: {args.command}")
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
    except IndexNotFoundError:
        sys.stderr.write("index not found; run `code-symbol-index index` first\n")
        return 2


__all__ = [
    "BinaryFileError",
    "CodeIndex",
    "CodeSymbolIndexError",
    "IndexNotFoundError",
    "IndexStatus",
    "Inspection",
    "InspectOptions",
    "Page",
    "Position",
    "Range",
    "Reference",
    "Repository",
    "Symbol",
    "SymbolNotFoundError",
    "UnsupportedLanguageError",
    "best_symbol",
    "build_arg_parser",
    "clean",
    "impls",
    "index",
    "inspect",
    "inspect_text",
    "main",
    "outline",
    "outline_text",
    "refs",
    "search",
    "search_text",
    "status",
    "status_text",
    "supported_languages",
]


if __name__ == "__main__":
    raise SystemExit(main())
