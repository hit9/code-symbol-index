from __future__ import annotations

from pathlib import Path

import pytest

import code_symbol_index
from code_symbol_index import CodeIndex, HeaderLanguageError, Repository, main

HEADER_CPP = "namespace demo { class Widget { public: int run(int v); int stop(); }; }\n"
CALLER_C = "int caller(void) { return helper(1); }\nint helper(int v) { return v; }\n"


DECLARATION_AND_DEFINITION_CPP = (
    "namespace demo {\n"
    "inline namespace v1 {\n"
    "class Widget {\n"
    "public:\n"
    "    int value() const;\n"
    "    int a, b, c;\n"
    "};\n"
    "int Widget::value() const { return 0; }\n"
    "}\n"
    "}\n"
)


def _fixture(tmp_path: Path) -> Path:
    (tmp_path / "widget.h").write_text(HEADER_CPP, encoding="utf-8")
    (tmp_path / "app.c").write_text(CALLER_C, encoding="utf-8")
    return tmp_path


def test_header_files_default_to_c(tmp_path: Path) -> None:
    _fixture(tmp_path)
    repo = Repository(tmp_path, create_index=True).build()

    assert repo.storage.file_languages(["widget.h"]) == {"widget.h": "c"}
    assert repo.storage.meta_value(code_symbol_index.HEADER_LANGUAGE_META) is None
    # C++ content in a .h stays parsed as C until explicitly configured.
    assert repo.search_symbols("run", language="cpp", exact_only=True) == []
    assert repo.search_symbols("run", language="c", exact_only=True)


def test_header_language_cpp_switch_changes_index_and_queries(tmp_path: Path) -> None:
    _fixture(tmp_path)
    # First index stores .h as C, then the switch converts the existing rows.
    repo = Repository(tmp_path, create_index=True).build()
    assert repo.last_header_language == ("c", 0, 0)
    repo.refresh(header_language="cpp")

    assert repo.storage.meta_value(code_symbol_index.HEADER_LANGUAGE_META) == "cpp"
    assert repo.storage.file_languages(["widget.h"]) == {"widget.h": "cpp"}
    assert repo.last_header_language == ("cpp", 1, 1)

    run = repo.search_symbols("run", language="cpp", exact_only=True)
    assert [(symbol.kind, symbol.container) for symbol in run] == [("method", "demo.Widget")]
    assert repo.search_symbols("run", language="c", exact_only=True) == []
    # Outline, inspect and anchors consume the same corrected definitions.
    assert [(symbol.name, symbol.kind) for symbol in repo.outline("widget.h").items] == [
        ("demo", "namespace"),
        ("Widget", "class"),
        ("run", "method"),
        ("stop", "method"),
    ]
    assert repo.inspect("run", exact_only=True).definition.container == "demo.Widget"

    # Other C++ extensions are untouched by the header setting.
    (tmp_path / "other.hpp").write_text("int hppa(void) { return 1; }\n", encoding="utf-8")
    repo.refresh()
    assert repo.storage.file_languages(["other.hpp"]) == {"other.hpp": "cpp"}


def test_saved_header_language_is_used_by_later_writes(tmp_path: Path) -> None:
    _fixture(tmp_path)
    Repository(tmp_path, create_index=True).refresh(header_language="cpp")

    (tmp_path / "widget.h").write_text(
        "namespace demo { class Widget { public: int added(int v); }; }\n", encoding="utf-8"
    )
    # A fresh instance has no in-memory setting: the saved value decides.
    later = Repository(tmp_path, create_index=True)
    later.update(["widget.h"])

    assert later.storage.file_languages(["widget.h"]) == {"widget.h": "cpp"}
    assert [symbol.kind for symbol in later.search_symbols("added", language="cpp", exact_only=True)] == ["method"]
    assert later.last_update_failed == ()


def test_reads_use_the_stored_language_of_each_file(tmp_path: Path) -> None:
    _fixture(tmp_path)
    Repository(tmp_path, create_index=True).refresh(header_language="cpp")
    # Simulate the interrupted switch: the pending setting moves back while the
    # stored row still says cpp. Reads must keep parsing this file as C++.
    pending = Repository(tmp_path, create_index=True)
    pending.storage.set_meta_value(code_symbol_index.HEADER_LANGUAGE_META, "c")

    symbol = pending.search_symbols("run", language="cpp", exact_only=True)[0]
    assert pending.inspect("run", kind="method", language="cpp", exact_only=True).definition.container == "demo.Widget"
    assert [node.symbol.name for node in pending.callers("run", kind="method", language="cpp", exact_only=True).roots] == []
    assert code_symbol_index._definition_range(pending, symbol) is not None
    assert pending.outline("widget.h").items


def test_header_language_change_is_refused_with_a_filtered_scan(tmp_path: Path) -> None:
    _fixture(tmp_path)
    Repository(tmp_path, create_index=True).refresh()

    filtered = Repository(tmp_path, languages=["cpp"], create_index=True)
    with pytest.raises(HeaderLanguageError):
        filtered.refresh(header_language="cpp")

    exit_code = main(["index", "--root", str(tmp_path), "--language", "c", "--header-language", "cpp"])
    assert exit_code == 2


def test_partial_conversion_keeps_failed_files_on_their_previous_language(tmp_path: Path, capsys) -> None:
    _fixture(tmp_path)
    Repository(tmp_path, create_index=True).refresh()
    (tmp_path / "broken.h").write_text("namespace demo { class Broken { int value; }; }\n", encoding="utf-8")
    Repository(tmp_path, create_index=True).refresh()
    # Now make it unreadable as text and switch the header language.
    (tmp_path / "broken.h").write_bytes(b"\xff\x00 broken\n")

    repo = Repository(tmp_path, create_index=True)
    repo.refresh(header_language="cpp")

    assert repo.last_header_language == ("cpp", 1, 2)
    assert repo.storage.file_languages(["widget.h", "broken.h"]) == {"widget.h": "cpp", "broken.h": "c"}
    # The failed file keeps its previous language and its previous symbols.
    assert [symbol.name for symbol in repo.search_symbols("Broken", language="c", exact_only=True)] == ["Broken"]
    assert repo.search_symbols("Broken", language="cpp", exact_only=True) == []
    # The next refresh keeps retrying the file that is still on its old language.
    repo.refresh()
    assert repo.last_header_language == ("cpp", 0, 1)

    exit_code = main(["index", "--root", str(tmp_path), "--header-language", "cpp"])
    assert exit_code == 0
    warning = capsys.readouterr().err
    assert "could not be converted" in warning


def test_successful_switch_reports_the_converted_count(tmp_path: Path, capsys) -> None:
    _fixture(tmp_path)
    Repository(tmp_path, create_index=True).build()
    assert main(["index", "--root", str(tmp_path), "--header-language", "cpp"]) == 0

    assert "converted 1 header files" in capsys.readouterr().err


def test_in_memory_index_uses_the_instance_configuration(tmp_path: Path) -> None:
    _fixture(tmp_path)
    default = CodeIndex(tmp_path).build()
    assert default.search_symbols("Widget", exact_only=True)[0].language == "c"

    configured = CodeIndex(tmp_path).build(header_language="cpp")
    symbols = configured.search_symbols("Widget", exact_only=True)
    assert [(symbol.name, symbol.language, symbol.container) for symbol in symbols] == [("Widget", "cpp", "demo")]


@pytest.mark.parametrize("value", ["c", "cpp"])
def test_invalid_header_language_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        CodeIndex(tmp_path, header_language=value + "x")
    # The supported values are accepted without error.
    CodeIndex(tmp_path, header_language=value)


def test_definition_is_preferred_inside_a_cpp_header(tmp_path: Path, capsys) -> None:
    """A header stored as C++ must also be re-read as C++ when a body decides.

    The declaration and the out-of-class definition share name, kind and container,
    so only the body tells them apart. Parsing the header with the default C parser
    instead produces an error tree whose bodies point at the declaration, which used
    to make ``inspect`` answer with the declaration and to hide the definition.
    """
    (tmp_path / "widget.h").write_text(DECLARATION_AND_DEFINITION_CPP, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True)
    repo.refresh(header_language="cpp")
    assert repo.storage.file_languages(["widget.h"]) == {"widget.h": "cpp"}

    candidates = repo.search_symbols("value", language="cpp", exact_only=True)
    assert [(symbol.kind, symbol.signature) for symbol in candidates] == [
        ("method", "int value() const;"),
        ("method", "int Widget::value() const { return 0; }"),
    ]

    inspection = repo.inspect("value", language="cpp", exact_only=True)
    assert inspection.definition.signature == "int Widget::value() const { return 0; }"
    # Ranges stay 0-based on the line, so the definition line is indexed directly.
    lines = DECLARATION_AND_DEFINITION_CPP.splitlines()
    assert lines[inspection.definition.range.start.line] == "int Widget::value() const { return 0; }"

    # The text command resolves the same target as the object API and the JSON form.
    assert main(["inspect", "Widget.value", "--path", "widget.h", "--root", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert "int Widget::value() const { return 0; }" in printed
    assert not printed.startswith("ambiguous:")
