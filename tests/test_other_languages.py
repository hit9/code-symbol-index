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
