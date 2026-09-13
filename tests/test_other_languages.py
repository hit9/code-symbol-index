from __future__ import annotations

from pathlib import Path

import pytest

from code_symbol_index import CodeIndex, SymbolNotFoundError


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


PYTHON_BINDINGS = (
    "first, second = 1, 2\n"
    "chained_one = chained_two = 3\n"
    "[listed, *rest_names] = items\n"
    "(paren_one, paren_two) = pair\n"
    "single = 1\n"
    "CONFIG = {'alpha': 1, 'beta': 2}\n"
    "obj.attribute = 4\n"
    "mapping['key'] = 5\n"
    "\n"
    "class Holder:\n"
    "    annotated: int = 1\n"
    "    declared: str\n"
    "    plain = 2\n"
    "    member_one, member_two = 1, 2\n"
    "\n"
    "    class Nested:\n"
    "        inner = 3\n"
    "\n"
    "    def method(self):\n"
    "        first_local, second_local = 6, 7\n"
    "        return first_local\n"
    "\n"
    "def outer():\n"
    "    tuple_local_a, tuple_local_b = 8, 9\n"
    "    return tuple_local_a\n"
)


@pytest.mark.parametrize(
    ("name", "kind", "container"),
    [
        ("first", "variable", None),
        ("second", "variable", None),
        ("chained_one", "variable", None),
        ("chained_two", "variable", None),
        ("listed", "variable", None),
        ("rest_names", "variable", None),
        ("paren_one", "variable", None),
        ("paren_two", "variable", None),
        ("annotated", "field", "Holder"),
        ("declared", "field", "Holder"),
        ("plain", "field", "Holder"),
        ("member_one", "field", "Holder"),
        ("member_two", "field", "Holder"),
        ("inner", "field", "Holder.Nested"),
    ],
)
def test_python_bindings_cover_multi_target_chains_and_class_bodies(
    tmp_path: Path, name: str, kind: str, container: str | None
) -> None:
    (tmp_path / "bindings.py").write_text(PYTHON_BINDINGS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbol = _symbols(index, "python")[name]
    assert (symbol.kind, symbol.container) == (kind, container)


def test_python_multi_bindings_keep_one_preview_and_no_duplicates(tmp_path: Path) -> None:
    (tmp_path / "bindings.py").write_text(PYTHON_BINDINGS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "python")
    assert symbols["first"].signature == "first, second = 1, 2"
    assert symbols["second"].signature == "first, second = 1, 2"
    assert symbols["first"].range.start_byte != symbols["second"].range.start_byte
    # Dictionary keys stay one symbol each, attached to the first bound name.
    for key in ("alpha", "beta"):
        assert (symbols[key].kind, symbols[key].container) == ("dict_key", "CONFIG")
    assert len(index.search_symbols("alpha", language="python", exact_only=True)) == 1


def test_python_attribute_and_local_targets_define_nothing(tmp_path: Path) -> None:
    (tmp_path / "bindings.py").write_text(PYTHON_BINDINGS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    for absent in ("obj", "attribute", "mapping", "key", "first_local", "second_local", "tuple_local_a"):
        assert index.search_symbols(absent, language="python", exact_only=True) == [], absent


SWIFT_BINDINGS = (
    "let first = 1, second = 2\n"
    "var (tuple_a, tuple_b) = pair\n"
    "let alpha = makeDefault(), beta = compute()\n"
    "\n"
    "struct Sizes {\n"
    "    let width: Int, height: Int\n"
    "    var count = 0\n"
    "\n"
    "    func area() -> Int {\n"
    "        let local_one = 1, local_two = 2\n"
    "        return local_one\n"
    "    }\n"
    "}\n"
)


@pytest.mark.parametrize(
    ("name", "kind", "container"),
    [
        ("first", "property", None),
        ("second", "property", None),
        ("tuple_a", "property", None),
        ("tuple_b", "property", None),
        ("alpha", "property", None),
        ("beta", "property", None),
        ("width", "property", "Sizes"),
        ("height", "property", "Sizes"),
        ("count", "property", "Sizes"),
    ],
)
def test_swift_multi_property_declarations_produce_every_name(
    tmp_path: Path, name: str, kind: str, container: str | None
) -> None:
    (tmp_path / "bindings.swift").write_text(SWIFT_BINDINGS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbol = _symbols(index, "swift")[name]
    assert (symbol.kind, symbol.container) == (kind, container)


def test_swift_property_names_do_not_come_from_initialisers(tmp_path: Path) -> None:
    (tmp_path / "bindings.swift").write_text(SWIFT_BINDINGS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "swift")
    assert symbols["first"].signature == "let first = 1, second = 2"
    assert symbols["second"].signature == "let first = 1, second = 2"
    assert symbols["second"].range.start_byte > symbols["first"].range.start_byte
    for absent in ("makeDefault", "compute", "pair", "local_one", "local_two"):
        assert index.search_symbols(absent, language="swift", exact_only=True) == [], absent
    # Protocol members, initialisers and functions keep their existing kinds.
    assert (symbols["area"].kind, symbols["area"].container) == ("function", "Sizes")


KOTLIN_DESTRUCTURING = (
    "val (destructured_one, destructured_two) = pair\n"
    "val (_, skipped) = ignored\n"
    "\n"
    "class Holder {\n"
    "    val (member_one, member_two) = pair\n"
    "\n"
    "    fun local(): Int {\n"
    "        val (inner_one, inner_two) = pair\n"
    "        return inner_one\n"
    "    }\n"
    "}\n"
)


@pytest.mark.parametrize(
    ("name", "container"),
    [
        ("destructured_one", None),
        ("destructured_two", None),
        ("skipped", None),
        ("member_one", "Holder"),
        ("member_two", "Holder"),
    ],
)
def test_kotlin_destructuring_declares_every_binding(tmp_path: Path, name: str, container: str | None) -> None:
    (tmp_path / "bindings.kt").write_text(KOTLIN_DESTRUCTURING, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbol = _symbols(index, "kotlin")[name]
    assert (symbol.kind, symbol.container) == ("property", container)


def test_kotlin_destructuring_skips_placeholder_and_locals(tmp_path: Path) -> None:
    (tmp_path / "bindings.kt").write_text(KOTLIN_DESTRUCTURING, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    assert index.search_symbols("_", language="kotlin", exact_only=True) == []
    for absent in ("inner_one", "inner_two", "pair"):
        assert index.search_symbols(absent, language="kotlin", exact_only=True) == [], absent
    symbols = _symbols(index, "kotlin")
    assert symbols["destructured_one"].signature == "val (destructured_one, destructured_two) = pair"
    assert symbols["destructured_one"].range.start_byte != symbols["destructured_two"].range.start_byte


RUST_DECLARATIONS = (
    "trait Greeter {\n"
    "    fn greet(&self) -> u32;\n"
    "    fn provided(&self) -> u32 { 1 }\n"
    "    type Item;\n"
    "}\n"
    "\n"
    "extern \"C\" {\n"
    "    fn ffi_helper(value: u32) -> u32;\n"
    "}\n"
    "\n"
    "struct Person;\n"
    "\n"
    "impl Greeter for Person {\n"
    "    fn greet(&self) -> u32 { 2 }\n"
    "}\n"
    "\n"
    "fn user() -> u32 {\n"
    "    ffi_helper(1)\n"
    "}\n"
)


def test_rust_bodyless_function_declarations_are_indexed(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text(RUST_DECLARATIONS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    greets = index.search_symbols("greet", language="rust", exact_only=True)
    assert sorted((symbol.kind, symbol.container) for symbol in greets) == [
        ("function", "Greeter"),
        ("function", "Person"),
    ]
    # The declaration keeps its own position and preview inside the trait.
    declaration = next(symbol for symbol in greets if symbol.container == "Greeter")
    assert declaration.signature == "fn greet(&self) -> u32;"
    symbols = _symbols(index, "rust")
    assert (symbols["provided"].kind, symbols["provided"].container) == ("function", "Greeter")
    assert (symbols["ffi_helper"].kind, symbols["ffi_helper"].container) == ("function", None)
    # Associated types are part of the untested acceptance matrix, not indexed.
    assert index.search_symbols("Item", language="rust", exact_only=True) == []


def test_rust_declaration_does_not_hide_the_definition(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text(RUST_DECLARATIONS, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    # Navigation prefers the implementation that has a body.
    assert index.inspect("greet", language="rust", exact_only=True).definition.container == "Person"
    # A declaration without any definition stays the only target and is used by
    # the call graph.
    assert [node.symbol.name for node in index.callees("user", language="rust").roots] == ["ffi_helper"]


RUST_SHARED_NAME = (
    "trait Mixed {\n"
    "    fn mixed(value: u32) -> u32;\n"
    "}\n"
    "\n"
    "fn mixed(value: u32) -> u32 { value }\n"
    "\n"
    "fn user() -> u32 {\n"
    "    mixed(1)\n"
    "}\n"
)


def test_rust_declaration_does_not_steal_an_existing_call_target(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text(RUST_SHARED_NAME, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    # Case-insensitive search finds the trait as well as both declarations.
    assert {(symbol.name, symbol.container) for symbol in index.search_symbols(
        "mixed", language="rust", exact_only=True
    )} == {("Mixed", None), ("mixed", "Mixed"), ("mixed", None)}
    # The call still resolves to the definition, not to the new declaration.
    assert [(node.symbol.container, node.depth) for node in index.callees("user", language="rust").roots] == [
        (None, 1)
    ]
    assert [node.symbol.name for node in index.callers("mixed", language="rust", exact_only=True).roots] == ["user"]
    assert index.inspect("mixed", language="rust", exact_only=True).definition.container is None


def test_rust_two_definitions_stay_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text(
        "trait Dup {\n"
        "    fn dup() -> u32;\n"
        "}\n"
        "\n"
        "struct A;\n"
        "struct B;\n"
        "\n"
        "impl A {\n"
        "    fn dup() -> u32 { 1 }\n"
        "}\n"
        "\n"
        "impl B {\n"
        "    fn dup() -> u32 { 2 }\n"
        "}\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    with pytest.raises(SymbolNotFoundError):
        index.inspect("dup", language="rust", exact_only=True)
    with pytest.raises(SymbolNotFoundError):
        index.callers("dup", language="rust", exact_only=True)
