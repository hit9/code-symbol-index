from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import code_symbol_index
from code_symbol_index import CodeIndex, Repository

C_SOURCE = "int declared(int);\nint helper(int v) { return v; }\nint caller(int v) { return helper(v); }\n"
CPP_SOURCE = "int helper(int v);\nint caller(int v) { return helper(v); }\n"
PY_SOURCE = "def target(): return 1\ndef caller(): return target()\n"


def _write_python(tmp_path: Path) -> Path:
    path = tmp_path / "app.py"
    path.write_text(PY_SOURCE, encoding="utf-8")
    return path


def _file_rows(repo: Repository) -> dict[str, dict]:
    return {path: dict(row) for path, row in repo.storage.files().items()}


def test_registered_languages_get_a_revision_and_others_stay_null(tmp_path: Path) -> None:
    _write_python(tmp_path)
    (tmp_path / "app.c").write_text(C_SOURCE, encoding="utf-8")
    (tmp_path / "app.cpp").write_text(CPP_SOURCE, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True, progress=None).build()
    rows = _file_rows(repo)

    assert rows["app.c"]["extractor_revision"] == code_symbol_index.EXTRACTOR_REVISIONS["c"]
    assert rows["app.cpp"]["extractor_revision"] == code_symbol_index.EXTRACTOR_REVISIONS["cpp"]
    # A language without registered rules is not part of the upgrade story.
    assert rows["app.py"]["extractor_revision"] is None


def test_unchanged_c_files_are_upgraded_without_touching_other_languages(tmp_path: Path, monkeypatch) -> None:
    _write_python(tmp_path)
    c_path = tmp_path / "app.c"
    c_path.write_text(C_SOURCE, encoding="utf-8")
    cpp_path = tmp_path / "app.cpp"
    cpp_path.write_text(CPP_SOURCE, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True, progress=None).build()

    # Simulate an index written before the revision column existed.
    with repo.storage.connection:
        repo.storage.connection.execute("UPDATE files SET extractor_revision = NULL")

    parsed: list[str] = []
    original = code_symbol_index._parse_file

    def counting_parse(*args, **kwargs):
        parsed.append(args[1].as_posix())
        return original(*args, **kwargs)

    monkeypatch.setattr(code_symbol_index, "_parse_file", counting_parse)
    events: list[tuple[str, int, int]] = []
    repo.refresh(progress=lambda event, *, done=0, total=0, path=None: events.append((event, done, total)))

    # Only the two C/C++ files are re-parsed; the unrelated language is not.
    assert sorted(parsed) == ["app.c", "app.cpp"]
    assert ("upgrade", 0, 2) in events
    rows = _file_rows(repo)
    assert rows["app.c"]["extractor_revision"] == "c:1"
    assert rows["app.cpp"]["extractor_revision"] == "cpp:1"
    assert rows["app.py"]["extractor_revision"] is None


@pytest.mark.parametrize("layout", ["raw", "summary", "summary_and_revision"])
def test_old_files_table_layouts_stay_readable_and_gain_the_column_on_write(tmp_path: Path, layout: str) -> None:
    _write_python(tmp_path)
    db_dir = tmp_path / code_symbol_index.DEFAULT_INDEX_DIR
    db_dir.mkdir()
    db_path = db_dir / code_symbol_index.DEFAULT_INDEX_DB
    connection = sqlite3.connect(db_path)
    extra_columns = {
        "raw": "",
        "summary": ", name_summary BLOB",
        "summary_and_revision": ", name_summary BLOB, extractor_revision TEXT",
    }[layout]
    connection.executescript(
        f"""
        CREATE TABLE files (path TEXT PRIMARY KEY, language TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
                            size INTEGER NOT NULL{extra_columns});
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE symbols (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                              language TEXT NOT NULL, path TEXT NOT NULL, start_line INTEGER NOT NULL,
                              start_col INTEGER NOT NULL, end_line INTEGER NOT NULL, end_col INTEGER NOT NULL,
                              start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, signature TEXT NOT NULL,
                              container TEXT);
        CREATE TABLE refs (name TEXT NOT NULL, language TEXT NOT NULL, path TEXT NOT NULL,
                           start_line INTEGER NOT NULL, start_col INTEGER NOT NULL, end_line INTEGER NOT NULL,
                           end_col INTEGER NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL,
                           context TEXT NOT NULL, reference_kind TEXT NOT NULL);
        INSERT INTO meta(key, value) VALUES ('schema_version', '{code_symbol_index.SCHEMA_VERSION}');
        """
    )
    connection.commit()
    connection.close()

    columns_before = _columns(db_path)
    repo = Repository(tmp_path, create_index=True, progress=None)
    # Reading an old layout never migrates it.
    assert repo.storage.files() == {}
    assert _columns(db_path) == columns_before

    repo.refresh()
    columns_after = _columns(db_path)
    assert "name_summary" in columns_after
    assert "extractor_revision" in columns_after
    assert repo.search("target", language="python", exact_only=True)


def _columns(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info(files)")}
    finally:
        connection.close()


def test_stale_symbols_are_replaced_by_the_rule_upgrade(tmp_path: Path) -> None:
    (tmp_path / "app.c").write_text(C_SOURCE, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True, progress=None).build()

    # An old index classified the declaration as a variable and had no revision.
    with repo.storage.connection:
        repo.storage.connection.execute("DELETE FROM symbols")
        repo.storage.connection.execute(
            "INSERT INTO symbols(id, name, kind, language, path, start_line, start_col, end_line, end_col, "
            "start_byte, end_byte, signature, container) VALUES "
            "('c:app.c:variable:declared:0', 'declared', 'variable', 'c', 'app.c', 0, 4, 0, 12, 4, 12, 'int declared(int);', NULL)"
        )
        repo.storage.connection.execute("UPDATE files SET extractor_revision = NULL")

    assert repo.search("declared", language="c", exact_only=True)[0].kind == "variable"
    repo.refresh()

    assert repo.search("declared", language="c", exact_only=True)[0].kind == "function"
    assert [node.symbol.name for node in repo.callers("helper", language="c", exact_only=True).roots] == ["caller"]


def test_path_update_does_not_upgrade_unrelated_c_files(tmp_path: Path) -> None:
    (tmp_path / "one.c").write_text("int one(void) { return 1; }\n", encoding="utf-8")
    (tmp_path / "two.c").write_text("int two(void) { return 2; }\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True, progress=None).build()
    with repo.storage.connection:
        repo.storage.connection.execute("UPDATE files SET extractor_revision = NULL")

    repo.update(["one.c"])
    rows = _file_rows(repo)
    assert rows["one.c"]["extractor_revision"] == "c:1"
    assert rows["two.c"]["extractor_revision"] is None


def test_failed_update_keeps_the_previous_rows_and_reports_the_failure(tmp_path: Path, capsys) -> None:
    source = tmp_path / "app.c"
    source.write_text("int stable(void) { return 1; }\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True, progress=None).build()
    assert repo.search("stable", language="c", exact_only=True)

    source.write_bytes(b"\xff\x00 int broken(void);\n")
    repo.update(["app.c"], progress=code_symbol_index._CliProgress())

    # The old symbols survive: a file that still exists but cannot be parsed is
    # not treated as deleted, and it is not reported as updated.
    assert repo.search("stable", language="c", exact_only=True)
    assert repo.last_update_updated == ()
    assert repo.last_update_failed == ("app.c",)
    assert "files could not be indexed" in capsys.readouterr().err


def test_failed_upgrade_retries_next_time_without_reparsing_successes(tmp_path: Path, monkeypatch) -> None:
    good = tmp_path / "good.c"
    good.write_text(C_SOURCE, encoding="utf-8")
    bad = tmp_path / "bad.c"
    bad.write_text("int bad(void) { return 0; }\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True, progress=None).build()
    with repo.storage.connection:
        repo.storage.connection.execute("UPDATE files SET extractor_revision = NULL")

    bad.write_bytes(b"\xff\x00 broken\n")
    repo.refresh()
    rows = _file_rows(repo)
    assert rows["good.c"]["extractor_revision"] == "c:1"
    assert rows["bad.c"]["extractor_revision"] is None
    assert repo.search("bad", language="c", exact_only=True)

    # The retry touches only the file that is still missing its revision.
    bad.write_text("int bad(void) { return 0; }\n", encoding="utf-8")
    parsed: list[str] = []
    original = code_symbol_index._parse_file

    def counting_parse(*args, **kwargs):
        parsed.append(args[1].as_posix())
        return original(*args, **kwargs)

    monkeypatch.setattr(code_symbol_index, "_parse_file", counting_parse)
    repo.refresh()
    assert parsed == ["bad.c"]
    assert _file_rows(repo)["bad.c"]["extractor_revision"] == "c:1"


def test_language_filtered_refresh_does_not_delete_other_languages(tmp_path: Path) -> None:
    _write_python(tmp_path)
    (tmp_path / "app.c").write_text(C_SOURCE, encoding="utf-8")
    Repository(tmp_path, create_index=True, progress=None).build()

    filtered = Repository(tmp_path, languages=["python"], create_index=True, progress=None)
    filtered.refresh()

    assert filtered.search("target", language="python", exact_only=True)
    assert filtered.search("helper", language="c", exact_only=True)


def test_unchanged_refresh_parses_nothing(tmp_path: Path, monkeypatch) -> None:
    _write_python(tmp_path)
    (tmp_path / "app.c").write_text(C_SOURCE, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True, progress=None).build()

    parsed: list[str] = []
    original = code_symbol_index._parse_file

    def counting_parse(*args, **kwargs):
        parsed.append(args[1].as_posix())
        return original(*args, **kwargs)

    monkeypatch.setattr(code_symbol_index, "_parse_file", counting_parse)
    repo.refresh()
    assert parsed == []


def test_code_index_build_sets_revisions_without_a_database(tmp_path: Path) -> None:
    (tmp_path / "app.c").write_text(C_SOURCE, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    rows = {path: dict(row) for path, row in index.storage.files().items()}
    assert rows["app.c"]["extractor_revision"] == "c:1"
