from __future__ import annotations

from pathlib import Path

import pytest

import code_symbol_index
from code_symbol_index import CodeIndex

# Shared depth-1 call attribution (SPEC sections 10.2 and 11.2). Every sample
# puts two callables on one line and asserts both directions, so a line-based
# range can no longer pass by accident.
SAME_LINE_SAMPLES = {
    "app.cpp": (
        "cpp",
        "int left(); int right();\n"
        "struct Box { int first() { return left(); } int second() { return right(); } };\n",
        {"first": "left", "second": "right"},
    ),
    "app.rs": (
        "rust",
        "fn left() -> i32 { 1 }\n"
        "fn right() -> i32 { 2 }\n"
        "struct Box;\n"
        "impl Box { fn first() -> i32 { left() } fn second() -> i32 { right() } }\n",
        {"first": "left", "second": "right"},
    ),
    "app.ts": (
        "typescript",
        "function left() { return 1; }\n"
        "function right() { return 2; }\n"
        "class Box { first() { return left(); } second() { return right(); } }\n",
        {"first": "left", "second": "right"},
    ),
    "app.swift": (
        "swift",
        "func left() -> Int { return 1 }\n"
        "func right() -> Int { return 2 }\n"
        "struct Box { func first() -> Int { return left() }; func second() -> Int { return right() } }\n",
        {"first": "left", "second": "right"},
    ),
    "app.kt": (
        "kotlin",
        "fun left(): Int = 1\n"
        "fun right(): Int = 2\n"
        "class Box {\n fun first(): Int = left(); fun second(): Int = right()\n}\n",
        {"first": "left", "second": "right"},
    ),
}


@pytest.mark.parametrize("filename", sorted(SAME_LINE_SAMPLES))
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_same_line_methods_are_attributed_by_byte_range(tmp_path: Path, filename: str, newline: str) -> None:
    language, source, expected = SAME_LINE_SAMPLES[filename]
    (tmp_path / filename).write_bytes(source.replace("\n", newline).encode("utf-8"))
    index = CodeIndex(tmp_path).build()

    for caller, callee in expected.items():
        callers = index.callers(callee, language=language, exact_only=True, depth=1)
        assert [node.symbol.name for node in callers.roots] == [caller], (filename, callee)

        callees = index.callees(caller, language=language, exact_only=True, depth=1)
        assert [node.symbol.name for node in callees.roots] == [callee], (filename, caller)

    # The neighbouring method and its callee must never show up.
    left_callers = index.callers("left", language=language, exact_only=True, depth=1)
    assert "second" not in {node.symbol.name for node in left_callers.roots}
    first_callees = index.callees("first", language=language, exact_only=True, depth=1)
    assert "right" not in {node.symbol.name for node in first_callees.roots}


PYTHON_NESTED = (
    "def left(): return 1\n"
    "def right(): return 2\n"
    "def outer():\n"
    "    def inner(): return left()\n"
    "    return right()\n"
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_python_nested_function_bodies_own_their_calls(tmp_path: Path, newline: str) -> None:
    (tmp_path / "app.py").write_bytes(PYTHON_NESTED.replace("\n", newline).encode("utf-8"))
    index = CodeIndex(tmp_path).build()

    assert [node.symbol.name for node in index.callers("left", language="python", exact_only=True).roots] == ["inner"]
    assert [node.symbol.name for node in index.callers("right", language="python", exact_only=True).roots] == ["outer"]
    # ``left`` is called from inner's body, so depth-1 outer -> left must not exist.
    assert [node.symbol.name for node in index.callees("outer", language="python", exact_only=True).roots] == ["right"]
    assert [node.symbol.name for node in index.callees("inner", language="python", exact_only=True).roots] == ["left"]


def test_explicitly_called_nested_function_becomes_a_direct_edge(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def left(): return 1\n"
        "def outer():\n"
        "    def inner(): return left()\n"
        "    return inner()\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    assert [node.symbol.name for node in index.callees("outer", language="python", exact_only=True).roots] == ["inner"]
    # ``left`` is only reachable one level further: the graph expands through the
    # real call edge instead of assuming that declaring ``inner`` calls it.
    deeper = index.callees("outer", language="python", exact_only=True, depth=2)
    assert [(node.symbol.name, node.depth) for node in deeper.roots] == [("inner", 1)]
    assert [(child.symbol.name, child.depth) for child in deeper.roots[0].children] == [("left", 2)]


def test_decorators_and_default_arguments_stay_with_the_outer_function(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def deco(fn): return fn\n"
        "def left(): return 1\n"
        "def right(): return 2\n"
        "def outer():\n"
        "    @deco\n"
        "    def inner(value=left()): return right()\n"
        "    return inner()\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    # Decorators and default arguments live outside ``inner``'s body: the default
    # argument call stays visible on the outer function, and ``inner``'s body
    # keeps only its own call.
    outer_callees = {node.symbol.name for node in index.callees("outer", language="python", exact_only=True).roots}
    assert outer_callees == {"left", "inner"}, outer_callees
    inner_callees = {node.symbol.name for node in index.callees("inner", language="python", exact_only=True).roots}
    assert inner_callees == {"right"}


def test_lambda_bodies_fence_off_the_enclosing_function(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def left(): return 1\n"
        "def right(): return 2\n"
        "def outer():\n"
        "    fn = lambda: left()\n"
        "    (lambda: left())()\n"
        "    return right()\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    # Anonymous bodies are ownership boundaries even though they publish no symbol,
    # so ``left`` is not reported as a direct callee of ``outer``.
    assert [node.symbol.name for node in index.callees("outer", language="python", exact_only=True).roots] == ["right"]
    assert index.callers("left", language="python", exact_only=True).roots == ()


def test_cpp_out_of_class_definition_owns_its_calls(tmp_path: Path) -> None:
    (tmp_path / "app.cpp").write_text(
        "int helper(int x);\n"
        "class Widget { public: int run(int v); };\n"
        "int Widget::run(int v) { return helper(v); }\n",
        encoding="utf-8",
    )
    index = CodeIndex(tmp_path).build()

    assert [node.symbol.name for node in index.callers("helper", language="cpp", exact_only=True).roots] == ["run"]
    assert [node.symbol.name for node in index.callees("run", language="cpp", exact_only=True).roots] == ["helper"]


def test_definition_range_is_used_for_symbols_without_a_body(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def helper(): return 1\nclass Box:\n    value = helper()\n", encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    # A class-level initialiser has no callable body; the class still owns the call
    # and the value binding does not hijack it.
    callers = index.callers("helper", language="python", exact_only=True).roots
    assert [node.symbol.name for node in callers] == ["Box"]


def test_enclosing_symbol_ignores_symbols_without_a_definition_range(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def helper(): return 1\ndef caller(): return helper()\n", encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    symbols = index.storage.symbols_in_file(Path("app.py"))
    reference = next(ref for ref in index.storage.references_for(symbols[0], limit=10, offset=0).items)
    # Without definition ranges nothing can own the reference: no same-line fallback.
    assert code_symbol_index._enclosing_symbol(symbols, {}, reference.range, exclude_id="") is None
