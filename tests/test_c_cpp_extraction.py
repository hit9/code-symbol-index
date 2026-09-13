from __future__ import annotations

from pathlib import Path

import pytest

import code_symbol_index
from code_symbol_index import CodeIndex

C_DECLARATOR_SOURCE = (
    "int declared(int);\n"
    "int a, *b, c[2];\n"
    "int *returning_pointer(int);\n"
    "int (*function_pointer)(int);\n"
    "int (*pointer_table[2])(int);\n"
    "typedef int (*Handler)(int);\n"
    "int reference_target;\n"
    "int *reference_target_pointer;\n"
)


def _symbols(index: CodeIndex, language: str):
    return {symbol.name: symbol for symbol in index.search_symbols("", language=language, limit=200)}


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("declared", "function"),
        ("a", "variable"),
        ("b", "variable"),
        ("c", "variable"),
        ("returning_pointer", "function"),
        ("function_pointer", "variable"),
        ("pointer_table", "variable"),
        ("Handler", "type"),
        ("reference_target", "variable"),
        ("reference_target_pointer", "variable"),
    ],
)
def test_c_declarators_are_classified_by_structure(tmp_path: Path, name: str, kind: str) -> None:
    (tmp_path / "app.c").write_text(C_DECLARATOR_SOURCE, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbol = _symbols(index, "c")[name]
    assert symbol.kind == kind

    # The declaration is the preview scope even when it declares several names.
    range_ = code_symbol_index._definition_range(index, symbol)
    assert range_ is not None
    assert "\n" not in code_symbol_index._read_text_file(tmp_path / "app.c")[range_.start_byte:range_.end_byte]


def test_multiple_declarators_each_produce_a_symbol(tmp_path: Path) -> None:
    (tmp_path / "app.c").write_text(C_DECLARATOR_SOURCE, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "c")
    for name in ("a", "b", "c"):
        assert symbols[name].signature == "int a, *b, c[2];"
        assert symbols[name].container is None
    # Distinct byte ranges, shared declaration preview.
    starts = {symbols[name].range.start_byte for name in ("a", "b", "c")}
    assert len(starts) == 3


def test_locals_and_parameters_do_not_become_global_symbols(tmp_path: Path) -> None:
    (tmp_path / "app.c").write_text(
        "int helper(int argument) {\n"
        "    int local = argument;\n"
        "    int other;\n"
        "    return local;\n"
        "}\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    assert index.search_symbols("local", language="c", exact_only=True) == []
    assert index.search_symbols("other", language="c", exact_only=True) == []
    assert index.search_symbols("argument", language="c", exact_only=True) == []
    assert index.search_symbols("helper", language="c", exact_only=True)[0].kind == "function"


def test_union_is_indexed_as_a_struct_with_fields(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "union Value { int as_int; unsigned as_uint; };\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "cpp")
    assert symbols["Value"].kind == "struct"
    assert symbols["as_int"].kind == "field"
    assert symbols["as_int"].container == "Value"
    assert symbols["as_uint"].container == "Value"


def test_using_alias_is_a_type(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text("using Count = unsigned long;\n", encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbol = index.search_symbols("Count", language="cpp", exact_only=True)[0]
    assert symbol.kind == "type"


def test_macros_are_constants_and_never_callees(tmp_path: Path) -> None:
    (tmp_path / "app.c").write_text(
        "#define LIMIT 10\n"
        "#define ADD(a, b) ((a) + (b))\n"
        "int caller(void) { return ADD(1, LIMIT); }\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "c")
    assert symbols["LIMIT"].kind == "constant"
    assert symbols["ADD"].kind == "constant"
    # A function-like macro is not a resolvable call target.
    assert index.search_symbols("a", language="c", exact_only=True) == []
    assert [node.symbol.name for node in index.callees("caller", language="c", exact_only=True).roots] == []


def test_enum_members_are_constants_in_the_enum_container(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "enum Color { RED, GREEN = 2 };\n"
        "enum class Mode : int { FAST, SLOW };\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    symbols = _symbols(index, "cpp")
    assert symbols["Color"].kind == "enum"
    assert symbols["RED"].kind == "constant"
    assert symbols["RED"].container == "Color"
    assert symbols["Mode"].kind == "enum"
    assert symbols["SLOW"].container == "Mode"


CPP_CLASS_SOURCE = (
    "namespace demo {\n"
    "class Widget {\n"
    "public:\n"
    "    Widget();\n"
    "    ~Widget();\n"
    "    int run(int v);\n"
    "    Widget operator+(const Widget &other);\n"
    "    int value;\n"
    "};\n"
    "}\n"
    "void demo::Widget::run(int v) { helper(v); }\n"
    "void unknown::helper(int v) {}\n"
    "void helper(int v) {}\n"
)


def test_class_members_keep_distinguishable_names_and_containers(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(CPP_CLASS_SOURCE, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    by_key = {(symbol.name, symbol.kind): symbol for symbol in index.search_symbols("", language="cpp", limit=200)}
    assert by_key[("Widget", "class")].container == "demo"
    assert by_key[("Widget", "constructor")].container == "demo.Widget"
    assert by_key[("~Widget", "method")].container == "demo.Widget"
    assert by_key[("run", "method")].container == "demo.Widget"
    assert by_key[("operator+", "method")].container == "demo.Widget"
    assert by_key[("value", "field")].container == "demo.Widget"

    # Destructors keep the tilde, so the preview starts where the name starts.
    destructor = by_key[("~Widget", "method")]
    range_ = code_symbol_index._definition_range(index, destructor)
    source = (tmp_path / "app.cpp").read_text(encoding="utf-8")
    assert source[range_.start_byte:range_.end_byte] == "~Widget();"


def test_out_of_class_definition_restores_only_explicit_scopes(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(CPP_CLASS_SOURCE, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    definitions = [
        symbol
        for symbol in index.search_symbols("run", language="cpp", exact_only=True)
        if symbol.signature.startswith("void")
    ]
    assert [symbol.name for symbol in definitions] == ["run"]
    assert definitions[0].kind == "method"
    assert definitions[0].container == "demo.Widget"

    # A scope this file never defines as a type stays a free function, but the
    # explicit container is still recorded instead of being dropped.
    unknown = [
        symbol
        for symbol in index.search_symbols("helper", language="cpp", exact_only=True)
        if symbol.container == "unknown"
    ]
    assert [symbol.kind for symbol in unknown] == ["function"]


def test_nested_namespaces_nest_containers(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "namespace outer { namespace inner { class Box { void run(); }; } }\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    by_key = {(symbol.name, symbol.kind): symbol for symbol in index.search_symbols("", language="cpp", limit=200)}
    assert by_key[("Box", "class")].container == "outer.inner"
    assert by_key[("run", "method")].container == "outer.inner.Box"


def test_inheritance_is_classified_and_consumed_by_impls(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "struct Base { virtual void go(); };\n"
        "struct Derived : public Base { void go() override {} };\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    kinds = {reference.reference_kind for reference in index.refs("Base", language="cpp", exact_only=True, ref_kinds="all").items}
    assert kinds == {"inherit"}
    assert [symbol.name for symbol in index.impls("Base", language="cpp", exact_only=True)] == ["Derived"]


def test_qualified_call_is_a_call_and_keeps_the_written_scope(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "namespace demo { void helper(); }\n"
        "void caller() { demo::helper(); }\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    references = [
        reference
        for reference in index.refs("helper", language="cpp", exact_only=True, ref_kinds="all").items
        if reference.reference_kind == "call"
    ]
    assert len(references) == 1
    assert "demo::helper()" in references[0].context
    assert [node.symbol.name for node in index.callers("helper", language="cpp", exact_only=True).roots] == ["caller"]


def test_declaration_and_definition_prefer_the_definition_target(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "class Widget { public: int run(int v); };\n"
        "int used(int v);\n"
        "int Widget::run(int v) { return used(v); }\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    inspection = index.inspect("run", language="cpp", exact_only=True)
    assert inspection.definition.signature == "int Widget::run(int v) { return used(v); }"
    assert [node.symbol.name for node in index.callees("run", language="cpp", exact_only=True).roots] == ["used"]
    callers = index.callers("used", language="cpp", exact_only=True).roots
    assert [node.symbol.name for node in callers] == ["run"]


def test_template_declarations_do_not_publish_template_parameters(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "template <typename Element>\n"
        "Element maxof(Element a, Element b) { return a > b ? a : b; }\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    assert index.search_symbols("Element", language="cpp", exact_only=True) == []
    symbols = index.search_symbols("maxof", language="cpp", exact_only=True)
    assert [(symbol.kind, symbol.container) for symbol in symbols] == [("function", None)]


def test_type_only_declaration_does_not_duplicate_the_struct(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text("struct Point { int x; };\n", encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    assert [symbol.kind for symbol in index.search_symbols("Point", language="cpp", exact_only=True)] == ["struct"]


def test_struct_variable_declaration_publishes_both_the_type_and_the_variable(tmp_path: Path) -> None:
    (tmp_path / "app.c").write_text("struct Point { int x; };\nstruct Point origin;\n", encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    origin = index.search_symbols("origin", language="c", exact_only=True)
    assert [(symbol.kind, symbol.container) for symbol in origin] == [("variable", None)]
    assert [symbol.kind for symbol in index.search_symbols("Point", language="c", exact_only=True)] == ["struct", "struct"]


def test_anonymous_lambda_body_does_not_own_the_enclosing_function_calls(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "void left();\n"
        "void right();\n"
        "void caller() {\n"
        "    auto fn = []() { left(); };\n"
        "    right();\n"
        "}\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    assert [node.symbol.name for node in index.callees("caller", language="cpp", exact_only=True).roots] == ["right"]
    assert index.callers("left", language="cpp", exact_only=True).roots == ()


def test_lf_and_crlf_sources_produce_the_same_symbols_and_edges(tmp_path: Path) -> None:
    source = (
        "namespace demo {\n"
        "class Widget {\n"
        "public:\n"
        "    int run(int v) { return helper(v); }\n"
        "    int slot;\n"
        "};\n"
        "}\n"
        "int helper(int v);\n"
    )
    results = []
    for newline in ("\n", "\r\n"):
        root = tmp_path / ("lf" if newline == "\n" else "crlf")
        root.mkdir()
        (root / "app.cpp").write_bytes(source.replace("\n", newline).encode("utf-8"))
        index = CodeIndex(root).build()
        symbols = [
            (symbol.name, symbol.kind, symbol.container)
            for symbol in index.search_symbols("", language="cpp", limit=200)
        ]
        callers = [node.symbol.name for node in index.callers("helper", language="cpp", exact_only=True).roots]
        callees = [node.symbol.name for node in index.callees("run", language="cpp", exact_only=True).roots]
        results.append((sorted(symbols), callers, callees))

    assert results[0] == results[1]
    assert results[0][1] == ["run"]
    assert results[0][2] == ["helper"]
