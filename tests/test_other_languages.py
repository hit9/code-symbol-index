from __future__ import annotations

from pathlib import Path

import pytest

from code_symbol_index import CodeIndex


def _symbols(index: CodeIndex, language: str):
    """Every symbol of ``language`` in the index, keyed by name."""
    return {symbol.name: symbol for symbol in index.search_symbols("", language=language, limit=200)}


KOTLIN_CONSTRUCTOR_PARAMS = (
    "class Box(val name: String, input: Int)\n"
    "class Wrapped(private val hidden: Int, plain: String = \"x\")\n"
    "class Empty(input: Int) {\n"
    "    fun use(): Int {\n"
    "        return input\n"
    "    }\n"
    "}\n"
)


def test_kotlin_constructor_parameter_needs_val_or_var(tmp_path: Path) -> None:
    (tmp_path / "app.kt").write_text(KOTLIN_CONSTRUCTOR_PARAMS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "kotlin")
    # ``val``/``var`` parameters declare members of the class they belong to.
    assert (symbols["name"].kind, symbols["name"].container) == ("property", "Box")
    assert (symbols["hidden"].kind, symbols["hidden"].container) == ("property", "Wrapped")
    # Without the keyword the parameter is a parameter, not a property.
    assert index.search_symbols("input", language="kotlin", exact_only=True) == []
    assert index.search_symbols("plain", language="kotlin", exact_only=True) == []


def test_kotlin_constructor_parameter_keeps_other_members(tmp_path: Path) -> None:
    (tmp_path / "app.kt").write_text(KOTLIN_CONSTRUCTOR_PARAMS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "kotlin")
    assert (symbols["Box"].kind, symbols["Wrapped"].kind, symbols["Empty"].kind) == (
        "class",
        "class",
        "class",
    )
    assert (symbols["use"].kind, symbols["use"].container) == ("function", "Empty")


def test_kotlin_class_body_properties_are_still_properties(tmp_path: Path) -> None:
    (tmp_path / "app.kt").write_text(
        "class Holder {\n"
        "    val stored = 1\n"
        "    var mutable: String = \"x\"\n"
        "\n"
        "    fun read(): Int {\n"
        "        val local = 2\n"
        "        return local\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "kotlin")
    assert (symbols["stored"].kind, symbols["stored"].container) == ("property", "Holder")
    assert (symbols["mutable"].kind, symbols["mutable"].container) == ("property", "Holder")
    # Function-body locals stay out of the index.
    assert index.search_symbols("local", language="kotlin", exact_only=True) == []


@pytest.mark.parametrize("name", ["name", "hidden", "stored", "mutable"])
def test_kotlin_property_preview_is_the_declaration(tmp_path: Path, name: str) -> None:
    (tmp_path / "app.kt").write_text(
        KOTLIN_CONSTRUCTOR_PARAMS + "class Holder {\n    val stored = 1\n    var mutable: String = \"x\"\n}\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    symbol = _symbols(index, "kotlin")[name]
    assert symbol.signature.startswith(("class ", "val ", "var ", "private val "))
    assert symbol.range.start_byte < symbol.range.end_byte


TYPESCRIPT_DECLARATIONS = (
    "interface Repo {\n"
    "  find(id: number): Row;\n"
    "  limit: number;\n"
    "}\n"
    "\n"
    "abstract class Base {\n"
    "  abstract run(input: number): number;\n"
    "}\n"
    "\n"
    "declare function ambient(a: number): void;\n"
    "\n"
    "namespace N {\n"
    "  function inNamespace(): void;\n"
    "}\n"
    "\n"
    "module Old {\n"
    "  function inModule(): void;\n"
    "}\n"
)


@pytest.mark.parametrize(
    ("name", "kind", "container"),
    [
        ("Repo", "interface", None),
        ("find", "method", "Repo"),
        ("limit", "field", "Repo"),
        ("Base", "class", None),
        ("run", "method", "Base"),
        ("ambient", "function", None),
        ("inNamespace", "function", "N"),
        ("inModule", "function", "Old"),
    ],
)
def test_typescript_declarations_without_bodies_are_indexed(
    tmp_path: Path, name: str, kind: str, container: str | None
) -> None:
    (tmp_path / "decls.ts").write_text(TYPESCRIPT_DECLARATIONS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbol = _symbols(index, "typescript")[name]
    assert (symbol.kind, symbol.container) == (kind, container)


def test_typescript_ambient_wrapper_declares_once(tmp_path: Path) -> None:
    (tmp_path / "ambient.d.ts").write_text(
        "declare module M {\n  function g(): void;\n}\ndeclare function h(): void;\n", encoding="utf-8"
    )
    index = CodeIndex(tmp_path).build()

    assert len(index.search_symbols("g", language="typescript", exact_only=True)) == 1
    assert len(index.search_symbols("h", language="typescript", exact_only=True)) == 1
    found = index.inspect("g", language="typescript", exact_only=True)
    assert (found.definition.kind, found.definition.container) == ("function", "M")


DESTRUCTURING_SOURCE = (
    "const {first, second: renamed} = source;\n"
    "const [one, two = 2, ...rest] = list;\n"
    "const {nested: {deep}} = outer;\n"
    "const {alpha = makeDefault(), ...tail} = config;\n"
    "const arrow = (x) => x;\n"
    "\n"
    "function scoped() {\n"
    "  const {local} = source;\n"
    "  return local;\n"
    "}\n"
)


@pytest.mark.parametrize(
    ("filename", "language"),
    [("bindings.ts", "typescript"), ("bindings.tsx", "tsx"), ("bindings.js", "javascript")],
)
def test_js_binding_patterns_declare_the_binding_side(tmp_path: Path, filename: str, language: str) -> None:
    (tmp_path / filename).write_text(DESTRUCTURING_SOURCE, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, language)
    for name in ("first", "renamed", "one", "two", "rest", "deep", "alpha", "tail"):
        assert symbols[name].kind == "variable", name
    # The property key is a name being read, not a new binding.
    assert index.search_symbols("second", language=language, exact_only=True) == []
    # Function-body locals stay out, and the default expression is a reference.
    assert index.search_symbols("local", language=language, exact_only=True) == []
    assert index.search_symbols("makeDefault", language=language, exact_only=True) == []
    # Named arrow functions keep their existing classification.
    assert symbols["arrow"].kind == "function"


def test_tsx_component_bindings_and_jsx_reads(tmp_path: Path) -> None:
    (tmp_path / "view.tsx").write_text(
        "export function View({title, body: content}) {\n"
        "  const {label} = meta;\n"
        "  return <section title={label}>{content}</section>;\n"
        "}\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    # A parameter pattern is a parameter, not a module-level binding.
    assert index.search_symbols("title", language="tsx", exact_only=True) == []
    assert index.search_symbols("content", language="tsx", exact_only=True) == []
    assert [
        (symbol.name, symbol.kind, symbol.container)
        for symbol in index.search_symbols("", language="tsx", limit=20)
    ] == [("View", "function", None)]


def test_binding_results_do_not_depend_on_line_endings(tmp_path: Path) -> None:
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    lf.mkdir()
    crlf.mkdir()
    (lf / "bindings.ts").write_text(DESTRUCTURING_SOURCE, encoding="utf-8")
    (crlf / "bindings.ts").write_bytes(DESTRUCTURING_SOURCE.replace("\n", "\r\n").encode("utf-8"))

    lf_index = CodeIndex(lf).build()
    crlf_index = CodeIndex(crlf).build()
    lf_symbols = {(symbol.name, symbol.kind) for symbol in lf_index.search_symbols("", language="typescript", limit=50)}
    crlf_symbols = {
        (symbol.name, symbol.kind) for symbol in crlf_index.search_symbols("", language="typescript", limit=50)
    }
    assert lf_symbols == crlf_symbols
