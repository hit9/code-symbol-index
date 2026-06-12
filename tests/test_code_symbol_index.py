from __future__ import annotations

import json
import hashlib
import threading
from pathlib import Path

import code_symbol_index
from code_symbol_index import CodeIndex, IndexNotFoundError, Repository, main


def test_indexes_python_symbols_and_references(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        """
class Handler:
    def handle(self):
        return helper()

def helper():
    return "ok"

value = helper()
""".strip()
        + "\n",
        encoding="utf-8",
    )

    index = CodeIndex(tmp_path).build()

    symbols = index.search_symbols("helper")
    assert symbols
    helper = symbols[0]
    assert helper.name == "helper"
    assert helper.kind == "function"
    assert helper.path == Path("app.py")

    inspection = index.inspect("helper")
    assert inspection.definition == helper
    assert any("return helper()" in reference.context for reference in inspection.references)
    assert "def helper" in (inspection.source_preview or "")


def test_indexes_python_top_level_constants_and_dict_keys(tmp_path: Path, capsys) -> None:
    (tmp_path / "settings.py").write_text(
        "MODEL_NAME = 'ask-syft'\n"
        "SETTINGS = {\n"
        "    'endpoint': '/ask',\n"
        "    'retry_count': 3,\n"
        "}\n"
        "\n"
        "def load_settings():\n"
        "    return SETTINGS\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    constant = code_symbol_index.search("MODEL_NAME", root=tmp_path, kind="constant", exact_only=True)
    dict_key = code_symbol_index.search("endpoint", root=tmp_path, kind="dict_key", exact_only=True)
    outline = code_symbol_index.outline_text("settings.py", root=tmp_path)
    cli_exit = main(["search", "retry_count", "--root", str(tmp_path), "--kind", "dict_key", "--exact-only", "--json"])
    cli_output = json.loads(capsys.readouterr().out)

    assert constant[0].signature == "MODEL_NAME = 'ask-syft'"
    assert dict_key[0].container == "SETTINGS"
    assert dict_key[0].signature == "'endpoint': '/ask'"
    assert "| MODEL_NAME = 'ask-syft'" in outline
    assert "| 'endpoint': '/ask'" in outline
    assert cli_exit == 0
    assert cli_output["symbols"][0]["name"] == "retry_count"


def test_parsers_are_thread_local() -> None:
    main_parser = code_symbol_index._parser_for_language("python")
    assert code_symbol_index._parser_for_language("python") is main_parser

    worker_parser_ids = []

    def load_parser() -> None:
        parser = code_symbol_index._parser_for_language("python")
        assert code_symbol_index._parser_for_language("python") is parser
        worker_parser_ids.append(id(parser))

    thread = threading.Thread(target=load_parser)
    thread.start()
    thread.join()

    assert worker_parser_ids
    assert worker_parser_ids[0] != id(main_parser)


def test_indexes_javascript_symbols(tmp_path: Path) -> None:
    (tmp_path / "ui.js").write_text(
        """
class Widget {
  render() {
    return mount();
  }
}

function mount() {
  return true;
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    index = CodeIndex(tmp_path).build()

    assert index.search_symbols("Widget", kind="class", language="javascript")
    assert index.search_symbols("mount", kind="function", language="javascript")


def test_indexes_rust_impl_candidates(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text(
        """
trait Greeter {
    fn greet(&self);
}

struct Person;

impl Greeter for Person {
    fn greet(&self) {}
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    index = CodeIndex(tmp_path).build()
    trait = index.search_symbols("Greeter", kind="trait", language="rust")[0]

    implementations = index.impls("Greeter", kind="trait", language="rust")
    assert any(symbol.kind == "impl" and "Greeter" in symbol.signature for symbol in implementations)


def test_gitignore_and_update(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    kept = tmp_path / "kept.py"
    ignored = tmp_path / "ignored.py"
    kept.write_text("def kept():\n    pass\n", encoding="utf-8")
    ignored.write_text("def ignored():\n    pass\n", encoding="utf-8")

    index = CodeIndex(tmp_path).build()

    assert index.search_symbols("kept")
    assert not index.search_symbols("ignored")

    kept.write_text("def renamed():\n    pass\n", encoding="utf-8")
    index.update([kept])

    assert not index.search_symbols("kept")
    assert index.search_symbols("renamed")


def test_repository_uses_disk_index_and_refreshes_changed_files(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("def first_name():\n    pass\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()

    assert repo.search("first_name")
    assert (tmp_path / ".code-symbol-index" / "index.sqlite").exists()

    source.write_text("def second_name():\n    pass\n", encoding="utf-8")

    assert repo.search("first_name")
    assert not repo.search("second_name")
    repo.refresh()
    assert not repo.search("first_name")
    assert repo.search("second_name")


def test_top_level_update_refreshes_only_given_paths(tmp_path: Path) -> None:
    changed = tmp_path / "changed.py"
    unchanged = tmp_path / "unchanged.py"
    changed.write_text("def old_name():\n    pass\n", encoding="utf-8")
    unchanged.write_text("def stable_name():\n    pass\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)

    changed.write_text("def new_name():\n    pass\n", encoding="utf-8")
    unchanged.write_text("def stale_on_disk():\n    pass\n", encoding="utf-8")
    code_symbol_index.update([changed], root=tmp_path)

    assert not code_symbol_index.search("old_name", root=tmp_path)
    assert code_symbol_index.search("new_name", root=tmp_path)
    assert code_symbol_index.search("stable_name", root=tmp_path)
    assert not code_symbol_index.search("stale_on_disk", root=tmp_path)


def test_top_level_update_removes_deleted_paths(tmp_path: Path) -> None:
    source = tmp_path / "gone.py"
    source.write_text("def removed_name():\n    pass\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)

    source.unlink()
    code_symbol_index.update(source, root=tmp_path)

    assert not code_symbol_index.search("removed_name", root=tmp_path)


def test_refresh_async_indexes_in_background(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_symbol_index, "MAX_WORKERS", 1)
    (tmp_path / "app.py").write_text("def async_target():\n    pass\n", encoding="utf-8")
    events: list[tuple[str, int, int, str | None]] = []

    def record(event: str, *, done: int = 0, total: int = 0, path: str | None = None) -> None:
        events.append((event, done, total, path))

    thread = code_symbol_index.refresh_async(tmp_path, progress=record)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert code_symbol_index.search("async_target", root=tmp_path)
    assert events == [
        ("scan", 0, 0, None),
        ("start", 0, 1, None),
        ("file", 1, 1, "app.py"),
        ("finish", 1, 1, None),
    ]


def test_repository_refresh_progress_callback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_symbol_index, "MAX_WORKERS", 1)
    (tmp_path / "alpha.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("def beta():\n    pass\n", encoding="utf-8")
    events: list[tuple[str, int, int, str | None]] = []

    def record(event: str, *, done: int = 0, total: int = 0, path: str | None = None) -> None:
        events.append((event, done, total, path))

    repo = Repository(tmp_path, create_index=True, progress=record).refresh()

    assert repo.search("alpha")
    assert events[0] == ("scan", 0, 0, None)
    assert ("start", 0, 2, None) in events
    file_events = [item for item in events if item[0] == "file"]
    assert [done for _, done, _, _ in file_events] == [1, 2]
    assert {total for _, _, total, _ in file_events} == {2}
    assert {path for _, _, _, path in file_events} == {"alpha.py", "beta.py"}
    assert events[-1] == ("finish", 2, 2, None)
    assert {event for event, _, _, _ in events} == {"scan", "start", "file", "finish"}


def test_repository_update_progress_callback_can_be_per_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_symbol_index, "MAX_WORKERS", 1)
    source = tmp_path / "app.py"
    source.write_text("def before():\n    pass\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()
    source.write_text("def after():\n    pass\n", encoding="utf-8")
    events: list[tuple[str, int, int, str | None]] = []

    def record(event: str, *, done: int = 0, total: int = 0, path: str | None = None) -> None:
        events.append((event, done, total, path))

    repo.update([source], progress=record)

    assert repo.search("after")
    assert not repo.search("before")
    assert events == [
        ("start", 0, 1, None),
        ("file", 1, 1, "app.py"),
        ("finish", 1, 1, None),
    ]


def test_progress_callback_errors_do_not_break_refresh(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def resilient():\n    pass\n", encoding="utf-8")

    def broken(event: str, *, done: int = 0, total: int = 0, path: str | None = None) -> None:
        raise RuntimeError(event)

    repo = Repository(tmp_path, create_index=True, progress=broken).refresh()

    assert repo.search("resilient")


def test_repository_skips_unchanged_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "app.py").write_text("def unchanged_name():\n    pass\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()

    assert repo.search("unchanged_name")

    calls = 0
    original = Repository._index_file

    def count_index_file(self: Repository, path: Path) -> None:
        nonlocal calls
        calls += 1
        original(self, path)

    monkeypatch.setattr(Repository, "_index_file", count_index_file)

    assert repo.search("unchanged_name")
    assert calls == 0


def test_repository_query_does_not_refresh_by_default(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "app.py").write_text("def indexed_name():\n    pass\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()

    def fail_refresh(self: Repository) -> Repository:
        raise AssertionError("refresh should not run during query")

    monkeypatch.setattr(Repository, "refresh", fail_refresh)

    assert repo.search("indexed_name")


def test_repository_indexes_multiple_changed_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(code_symbol_index, "MAX_WORKERS", 2)
    for index in range(3):
        (tmp_path / f"module_{index}.py").write_text(
            f"def multi_target_{index}():\n    pass\n",
            encoding="utf-8",
        )

    repo = Repository(tmp_path, create_index=True).refresh()

    assert repo.search("multi_target_0")
    assert repo.search("multi_target_1")
    assert repo.search("multi_target_2")


def test_repository_computes_references_on_demand_without_persisting_refs(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        """
def helper():
    local_only = 1
    return local_only

value = helper()
""".strip()
        + "\n",
        encoding="utf-8",
    )

    repo = Repository(tmp_path, create_index=True).refresh()
    persisted_refs = repo.storage.connection.execute("SELECT count(*) FROM refs").fetchone()[0]
    references = repo.refs("helper")

    assert persisted_refs == 0
    assert references
    assert all(reference.name == "helper" for reference in references)


def test_inspect_limits_references_and_exposes_next_offset(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def helper():\n    return 'ok'\n\n"
        + "\n".join(f"value_{index} = helper()" for index in range(5))
        + "\n",
        encoding="utf-8",
    )
    repo = Repository(tmp_path, create_index=True).refresh()

    inspection = repo.inspect("helper", limit=2)
    page = repo.refs("helper", limit=2, offset=2)

    assert len(inspection.references) == 2
    assert inspection.references_has_more is True
    assert inspection.references_next_offset == 2
    assert len(page.items) == 2
    assert page.limit == 2
    assert page.offset == 2
    assert page.has_more is True
    assert page.next_offset == 4


def test_repository_prefilters_reference_parse_by_symbol_name(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "app.py").write_text(
        "def helper():\n    return 'ok'\n\nvalue = helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "other.py").write_text("def unrelated():\n    return 1\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()

    parsed_paths: list[Path] = []
    original_parse_file = code_symbol_index._parse_file

    def count_parse_file(*args, **kwargs):
        parsed_paths.append(args[1])
        return original_parse_file(*args, **kwargs)

    monkeypatch.setattr(code_symbol_index, "_parse_file", count_parse_file)

    assert repo.refs("helper")
    assert Path("app.py") in parsed_paths
    assert Path("other.py") not in parsed_paths


def test_top_level_refs_returns_page(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def helper():\n    return 'ok'\n\n"
        + "\n".join(f"value_{index} = helper()" for index in range(3))
        + "\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    page = code_symbol_index.refs("helper", root=tmp_path, limit=2, offset=1)

    assert len(page.items) == 2
    assert page.limit == 2
    assert page.offset == 1
    assert page.has_more is False


def test_repository_populates_symbol_fts_when_available(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def substring_target():\n    pass\n", encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()
    if not repo.storage.has_symbol_fts:
        return

    fts_count = repo.storage.connection.execute("SELECT count(*) FROM symbol_fts").fetchone()[0]

    assert fts_count > 0
    assert repo.search("string_tar")


def test_default_excludes_prune_virtualenv(tmp_path: Path) -> None:
    package_dir = tmp_path / ".venv" / "lib"
    package_dir.mkdir(parents=True)
    package_dir.joinpath("dependency.py").write_text("def OnlyDependency():\n    pass\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def local_index():\n    pass\n", encoding="utf-8")

    index = CodeIndex(tmp_path).build()

    assert not index.search("OnlyDependency")
    assert index.search("local_index")


def test_default_excludes_skip_common_generated_dirs(tmp_path: Path) -> None:
    coverage_dir = tmp_path / "coverage"
    next_dir = tmp_path / ".next"
    coverage_dir.mkdir()
    next_dir.mkdir()
    coverage_dir.joinpath("report.py").write_text("def coverage_symbol():\n    pass\n", encoding="utf-8")
    next_dir.joinpath("page.py").write_text("def next_symbol():\n    pass\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def app_symbol():\n    pass\n", encoding="utf-8")

    repo = Repository(tmp_path, create_index=True).refresh()

    assert not repo.search("coverage_symbol")
    assert not repo.search("next_symbol")
    assert repo.search("app_symbol")


def test_nested_gitignore_is_respected(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    package.joinpath(".gitignore").write_text("ignored.py\n", encoding="utf-8")
    package.joinpath("ignored.py").write_text("def ignored_symbol():\n    pass\n", encoding="utf-8")
    package.joinpath("kept.py").write_text("def kept_symbol():\n    pass\n", encoding="utf-8")

    repo = Repository(tmp_path, create_index=True).refresh()

    assert not repo.search("ignored_symbol")
    assert repo.search("kept_symbol")


def test_binary_looking_text_extension_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_bytes(b"def binary_symbol():\x00\n    pass\n")
    (tmp_path / "good.py").write_text("def good_symbol():\n    pass\n", encoding="utf-8")

    repo = Repository(tmp_path, create_index=True).refresh()

    assert not repo.search("binary_symbol")
    assert repo.search("good_symbol")


def test_default_root_api_uses_current_directory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "local.py").write_text("def local_target():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code_symbol_index.index()

    assert code_symbol_index.search("local_target")[0].path == Path("local.py")


def test_top_level_api_sync_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("def first_sync_name():\n    pass\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)
    source.write_text("def second_sync_name():\n    pass\n", encoding="utf-8")

    assert not code_symbol_index.search("second_sync_name", root=tmp_path)
    assert code_symbol_index.search("second_sync_name", root=tmp_path, sync=True)


def test_top_level_api_search_limit_defaults_to_twenty(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "\n".join(f"def target_{index}():\n    pass\n" for index in range(25)),
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    assert len(code_symbol_index.search("target", root=tmp_path)) == 20
    assert len(code_symbol_index.search("target", root=tmp_path, limit=3)) == 3
    limited_text = code_symbol_index.search("target", root=tmp_path, limit=3, format="text")
    limited_json = code_symbol_index.search("target", root=tmp_path, limit=3, format="json")
    assert "limit: 3" in limited_text
    assert "has_more: true" in limited_text
    assert limited_json["count"] == 3
    assert limited_json["limit"] == 3
    assert limited_json["has_more"] is True
    assert len(limited_json["symbols"]) == 3


def test_top_level_api_search_accepts_multiple_queries(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "class Tool:\n"
        "    pass\n"
        "\n"
        "class Agent:\n"
        "    pass\n"
        "\n"
        "class ToolRunner:\n"
        "    pass\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    symbols = code_symbol_index.search(["Agent", "Tool"], root=tmp_path)
    output = code_symbol_index.search_text(["Agent", "Tool"], root=tmp_path)

    assert [symbol.name for symbol in symbols[:2]] == ["Agent", "Tool"]
    assert "queries:\n" in output
    assert "  - Agent" in output
    assert "  - Tool" in output
    assert "matched_query: Agent" in output
    assert "matched_query: Tool" in output


def test_top_level_api_search_text_is_llm_friendly(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def target_tool():\n    pass\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)

    output = code_symbol_index.search_text("target", root=tmp_path)

    assert "query: target" in output
    assert "count: 1" in output
    assert "limit: 20" in output
    assert "has_more: false" in output
    assert "symbols:" in output
    assert "name: target_tool" in output
    assert "range: 0:2" in output
    assert "score: prefix" in output
    assert "source:" not in output


def test_search_filters_kind_path_exact_and_pipe_queries(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "tools.py").write_text(
        "class Tool:\n"
        "    pass\n"
        "\n"
        "class ToolRunner:\n"
        "    pass\n"
        "\n"
        "def build_tool():\n"
        "    return Tool()\n",
        encoding="utf-8",
    )
    (tmp_path / "other.py").write_text(
        "class Tool:\n"
        "    pass\n"
        "\n"
        "def helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    exact = code_symbol_index.search("Tool", root=tmp_path, path="pkg", kind="class", exact_only=True)
    fuzzy = code_symbol_index.search("Tool", root=tmp_path, path="pkg", kind="class")
    combined = code_symbol_index.search("Tool|helper", root=tmp_path, kind=("class", "function"))

    assert [symbol.name for symbol in exact] == ["Tool"]
    assert [symbol.name for symbol in fuzzy[:2]] == ["Tool", "ToolRunner"]
    assert {symbol.name for symbol in combined} >= {"Tool", "helper"}


def test_cli_search_filters_kind_path_exact(tmp_path: Path, capsys) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "tools.py").write_text(
        "class Tool:\n"
        "    pass\n"
        "\n"
        "class ToolRunner:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "other.py").write_text("class Tool:\n    pass\n", encoding="utf-8")
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "search",
            "Tool",
            "--root",
            str(tmp_path),
            "--kind",
            "class",
            "--path",
            "pkg",
            "--exact-only",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert [symbol["name"] for symbol in output["symbols"]] == ["Tool"]
    assert output["symbols"][0]["path"] == "pkg/tools.py"


def test_top_level_api_format_parameter(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def target_tool():\n    return 1\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)

    search_text = code_symbol_index.search("target", root=tmp_path, format="text")
    search_json = code_symbol_index.search("target", root=tmp_path, format="json")
    inspect_text = code_symbol_index.inspect("target_tool", root=tmp_path, format="text")
    outline_json = code_symbol_index.outline("app.py", root=tmp_path, format="json")
    status_text = code_symbol_index.status(tmp_path, format="text")
    status_json = code_symbol_index.status(tmp_path, format="json")

    assert isinstance(code_symbol_index.search("target", root=tmp_path), list)
    assert "query: target" in search_text
    assert search_json["symbols"][0]["name"] == "target_tool"
    assert search_json["symbols"][0]["path"] == "app.py"
    assert search_json["symbols"][0]["range"]["start"]["line"] == 0
    assert search_json["has_more"] is False
    assert "source:" in inspect_text
    assert outline_json["items"][0]["name"] == "target_tool"
    assert "index:\n" in status_text
    assert status_json["status"] == "ready"


def test_top_level_api_rejects_unknown_format(tmp_path: Path) -> None:
    try:
        code_symbol_index.search("target", root=tmp_path, format="yaml")
    except ValueError as exc:
        assert "format must be one of" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_outline_text_returns_file_structure_without_source_or_ids(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        """
class Tool:
    def cli_args(cls, args):
        return args

def main(argv=None):
    return Tool()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    output = code_symbol_index.outline_text("app.py", root=tmp_path)

    assert "file: app.py" in output
    assert "range: 0:6" in output
    assert "count: 3" in output
    assert "outline:" in output
    assert "0:3 | class Tool:" in output
    assert "1:3 |     def cli_args(cls, args):" in output
    assert "4:6 | def main(argv=None):" in output
    assert "id:" not in output
    assert "source:" not in output


def test_outline_text_uses_indexed_signatures_when_file_changes(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "def first():\n"
        "    return 1\n"
        "\n"
        "def second():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)
    source.write_text(
        "# changed after indexing\n"
        "# this line should not be used as a signature\n"
        "def first():\n"
        "    return 1\n"
        "\n"
        "def second():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    output = code_symbol_index.outline_text("app.py", root=tmp_path)

    assert "| def first():" in output
    assert "| def second():" in output
    assert "changed after indexing" not in output


def test_outline_text_parses_file_once_for_definition_ranges(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "app.py").write_text(
        "\n".join(f"def target_{index}():\n    pass\n" for index in range(12)),
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)
    parser_calls = 0
    original_parser_for_language = code_symbol_index._parser_for_language

    def count_parser(language: str):
        nonlocal parser_calls
        if language == "python":
            parser_calls += 1
        return original_parser_for_language(language)

    monkeypatch.setattr(code_symbol_index, "_parser_for_language", count_parser)

    output = code_symbol_index.outline_text("app.py", root=tmp_path)

    assert "target_11" in output
    assert parser_calls <= 2


def test_outline_supports_local_symbol_filter_for_api_and_cli(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        """
class Tool:
    def cli_args(cls, args):
        return args

class Agent:
    def run(self):
        return Tool()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    api_output = code_symbol_index.outline_text("app.py", root=tmp_path, symbol="Tool")
    cli_exit = main(["outline", "app.py", "--root", str(tmp_path), "--symbol", "Tool"])
    cli_output = capsys.readouterr().out

    assert "symbol: Tool" in api_output
    assert "class Tool:" in api_output
    assert "def cli_args" in api_output
    assert "class Agent:" not in api_output
    assert cli_exit == 0
    assert "symbol: Tool" in cli_output
    assert "class Agent:" not in cli_output


def test_inspect_text_outputs_llm_friendly_source(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        """
class Handler:
    def handle(self):
        return helper()

def helper():
    return "ok"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    output = code_symbol_index.inspect_text("Handler.handle", root=tmp_path)

    assert "symbol:\n" in output
    assert "name: handle" in output
    assert "range: 1:3" in output
    assert "source:\n" in output
    assert "status: full" in output
    assert "  1 |    def handle(self):" in output
    assert "  2 |        return helper()" in output
    assert "callees:" in output


def test_inspect_text_supports_hashline_anchors(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    output = code_symbol_index.inspect_text("helper", root=tmp_path, anchors=True)
    first_hash = hashlib.sha256("def helper():".encode("utf-8")).hexdigest()[:8]
    second_hash = hashlib.sha256("    return 1".encode("utf-8")).hexdigest()[:8]

    assert "note: Use line:hash as edit anchor; code starts after |" in output
    assert f"0:{first_hash}|def helper():" in output
    assert f"1:{second_hash}|    return 1" in output
    assert "  0 |def helper():" not in output


def test_inspect_json_includes_current_file_source_anchors(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)
    source.write_text(
        "def helper():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    output = code_symbol_index.inspect("helper", root=tmp_path, format="json", anchors=True)
    body_hash = hashlib.sha256("    return 2".encode("utf-8")).hexdigest()[:8]

    assert output["source_anchor"]["path"] == "app.py"
    assert output["source_anchor"]["start_line"] == 0
    assert output["source_anchor"]["end_line"] == 2
    assert output["source_anchor"]["lines"][1] == {"line": 1, "hash": body_hash, "text": "    return 2"}
    assert output["source_anchor"]["end_anchor"] == f"1:{body_hash}"


def test_cli_inspect_json_supports_source_anchors(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    exit_code = main(["inspect", "helper", "--root", str(tmp_path), "--anchors", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["source_anchor"]["path"] == "app.py"
    assert output["source_anchor"]["lines"][0]["line"] == 0
    assert output["source_anchor"]["lines"][0]["text"] == "def helper():"


def test_inspect_includes_imports_for_api_and_cli_json(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def helper():\n"
        "    return Path(os.getcwd())\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    text_output = code_symbol_index.inspect_text("helper", root=tmp_path)
    json_exit = main(["inspect", "helper", "--root", str(tmp_path), "--json"])
    json_output = json.loads(capsys.readouterr().out)

    assert "summary:" in text_output
    assert "imports: 2" in text_output
    assert "statement: import os" in text_output
    assert "statement: from pathlib import Path" in text_output
    assert json_exit == 0
    assert [item["statement"] for item in json_output["imports"]] == ["import os", "from pathlib import Path"]


def test_inspect_callees_ignore_nested_symbol_definitions(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "class Tool:\n"
        "    def run(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)

    output = code_symbol_index.inspect_text("Tool", root=tmp_path)

    assert "callees:\n  []" in output


def test_inspect_text_handles_invalid_not_found_and_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)

    assert "invalid_input:" in code_symbol_index.inspect_text("where is helper", root=tmp_path)
    assert "invalid_input:" in code_symbol_index.inspect_text("app.py", root=tmp_path)
    assert "not_found:" in code_symbol_index.inspect_text("missing_symbol", root=tmp_path)
    ambiguous = code_symbol_index.inspect_text("helper", root=tmp_path)
    assert "ambiguous:" in ambiguous
    assert "candidates:" in ambiguous


def test_cli_search_outputs_text_by_default_and_json_with_flag(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text("def cli_target():\n    pass\n", encoding="utf-8")
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    exit_code = main(["search", "cli_target", "--root", str(tmp_path), "--language", "python"])
    text_output = capsys.readouterr().out
    json_exit = main(["search", "cli_target", "--root", str(tmp_path), "--language", "python", "--json"])
    raw_json = capsys.readouterr().out

    assert exit_code == 0
    assert "query: cli_target" in text_output
    assert "limit: 20" in text_output
    assert "has_more: false" in text_output
    assert "symbols:" in text_output
    assert "name: cli_target" in text_output
    assert "score: exact" in text_output
    assert "source:" not in text_output
    assert json_exit == 0
    output = json.loads(raw_json)
    assert output["symbols"][0]["name"] == "cli_target"
    assert output["symbols"][0]["path"] == "app.py"
    assert output["symbols"][0]["line"] == 1
    assert output["symbols"][0]["column"] == 5
    assert output["count"] == 1
    assert output["limit"] == 20
    assert output["has_more"] is False
    assert raw_json.startswith("{\n")
    assert '"range"' not in raw_json


def test_cli_search_accepts_multiple_queries(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        "class Tool:\n"
        "    pass\n"
        "\n"
        "class Agent:\n"
        "    pass\n",
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    exit_code = main(["search", "Tool", "Agent", "--root", str(tmp_path), "--language", "python"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "queries:\n" in output
    assert "  - Tool" in output
    assert "  - Agent" in output
    assert "name: Tool" in output
    assert "name: Agent" in output
    assert "matched_query: Tool" in output
    assert "matched_query: Agent" in output


def test_cli_search_limit_defaults_to_twenty_and_accepts_option(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        "\n".join(f"def target_{index}():\n    pass\n" for index in range(25)),
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    default_exit = main(["target", "--root", str(tmp_path), "--language", "python", "--json"])
    default_output = json.loads(capsys.readouterr().out)
    limited_exit = main(["target", "--root", str(tmp_path), "--language", "python", "--limit", "3", "--json"])
    limited_output = json.loads(capsys.readouterr().out)

    assert default_exit == 0
    assert len(default_output["symbols"]) == 20
    assert default_output["has_more"] is True
    assert limited_exit == 0
    assert len(limited_output["symbols"]) == 3
    assert limited_output["limit"] == 3
    assert limited_output["has_more"] is True


def test_cli_query_does_not_sync_by_default(tmp_path: Path, capsys) -> None:
    source = tmp_path / "app.py"
    source.write_text("def first_cli_name():\n    pass\n", encoding="utf-8")
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()
    source.write_text("def second_cli_name():\n    pass\n", encoding="utf-8")

    stale_exit = main(["second_cli_name", "--root", str(tmp_path), "--language", "python"])
    stale_output = capsys.readouterr()
    synced_exit = main(["second_cli_name", "--root", str(tmp_path), "--language", "python", "--sync"])
    synced_output = capsys.readouterr()

    assert stale_exit == 0
    assert "count: 0" in stale_output.out
    assert synced_exit == 0
    assert "name: second_cli_name" in synced_output.out
    assert "indexed" in synced_output.err


def test_cli_bare_keyword_search_and_inspect_best_match(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        """
def helper():
    return "ok"

value = helper()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    search_exit = main(["helper", "--root", str(tmp_path), "--language", "python"])
    assert search_exit == 0
    search_output = capsys.readouterr().out
    assert "name: helper" in search_output

    inspect_exit = main(["inspect", "helper", "--root", str(tmp_path), "--language", "python"])
    assert inspect_exit == 0
    inspect_output = capsys.readouterr().out
    assert "symbol:" in inspect_output
    assert "name: helper" in inspect_output
    assert "source:" in inspect_output
    assert "  0 |def helper():" in inspect_output
    assert "references:" in inspect_output

    json_exit = main(["inspect", "helper", "--root", str(tmp_path), "--language", "python", "--json"])
    assert json_exit == 0
    json_output = json.loads(capsys.readouterr().out)
    assert json_output["definition"]["name"] == "helper"


def test_cli_inspect_and_refs_support_pagination(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        "def helper():\n    return 'ok'\n\n"
        + "\n".join(f"value_{index} = helper()" for index in range(5))
        + "\n",
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    inspect_exit = main(
        [
            "inspect",
            "helper",
            "--root",
            str(tmp_path),
            "--language",
            "python",
            "--max-references",
            "2",
        ]
    )
    inspect_output = capsys.readouterr().out
    refs_exit = main(
        [
            "refs",
            "helper",
            "--root",
            str(tmp_path),
            "--language",
            "python",
            "--limit",
            "2",
            "--offset",
            "2",
            "--json",
        ]
    )
    refs_output = json.loads(capsys.readouterr().out)

    assert inspect_exit == 0
    assert inspect_output.count("context:") == 2
    assert "kind: usage" not in inspect_output
    assert refs_exit == 0
    assert len(refs_output["items"]) == 2
    assert refs_output["limit"] == 2
    assert refs_output["offset"] == 2
    assert refs_output["has_more"] is True
    assert refs_output["next_offset"] == 4


def test_cli_refs_defaults_to_text_and_supports_json(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        "def helper():\n    return 'ok'\n\nvalue = helper()\n",
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    text_exit = main(["refs", "helper", "--root", str(tmp_path), "--language", "python"])
    text_output = capsys.readouterr().out
    json_exit = main(["refs", "helper", "--root", str(tmp_path), "--language", "python", "--json"])
    json_output = json.loads(capsys.readouterr().out)

    assert text_exit == 0
    assert "references:" in text_output
    assert "items:" in text_output
    assert "context: value = helper()" in text_output
    assert "kind: usage" not in text_output
    assert json_exit == 0
    assert json_output["items"][0]["context"] == "value = helper()"


def test_cli_outline_defaults_to_text_and_supports_json(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(
        """
class Tool:
    def cli_args(cls, args):
        return args

def main(argv=None):
    return Tool()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()

    text_exit = main(["outline", "app.py", "--root", str(tmp_path)])
    text_output = capsys.readouterr().out
    json_exit = main(["outline", "app.py", "--root", str(tmp_path), "--json"])
    json_output = json.loads(capsys.readouterr().out)

    assert text_exit == 0
    assert "file: app.py" in text_output
    assert "outline:" in text_output
    assert "0:3 | class Tool:" in text_output
    assert "1:3 |     def cli_args(cls, args):" in text_output
    assert "id:" not in text_output
    assert "source:" not in text_output
    assert json_exit == 0
    assert json_output["items"][0]["name"] == "Tool"


def test_cli_impls_defaults_to_text_and_supports_json(tmp_path: Path, capsys) -> None:
    (tmp_path / "lib.rs").write_text(
        """
trait Greeter {
    fn greet(&self);
}

struct Person;

impl Greeter for Person {
    fn greet(&self) {}
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    assert main(["index", "--root", str(tmp_path), "--language", "rust"]) == 0
    capsys.readouterr()

    text_exit = main(["impls", "Greeter", "--root", str(tmp_path), "--language", "rust", "--kind", "trait"])
    text_output = capsys.readouterr().out
    json_exit = main(["impls", "Greeter", "--root", str(tmp_path), "--language", "rust", "--kind", "trait", "--json"])
    json_output = json.loads(capsys.readouterr().out)

    assert text_exit == 0
    assert "implementors:" in text_output
    assert "items:" in text_output
    assert "kind: impl" in text_output
    assert json_exit == 0
    assert json_output["items"][0]["kind"] == "impl"


def test_cli_progress_goes_to_stderr(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text("def progress_target():\n    pass\n", encoding="utf-8")

    exit_code = main(["index", "--root", str(tmp_path), "--language", "python"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["root"] == str(tmp_path.resolve())
    assert "indexed" in captured.err
    assert "writing index" not in captured.err


def test_cli_index_command_refreshes_disk_index(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text("def indexed_target():\n    pass\n", encoding="utf-8")

    exit_code = main(["index", "--root", str(tmp_path), "--language", "python"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["root"] == str(tmp_path.resolve())
    assert (tmp_path / ".code-symbol-index" / "index.sqlite").exists()
    assert "indexed" in captured.err
    assert "writing index" not in captured.err


def test_cli_update_command_refreshes_known_paths(tmp_path: Path, capsys) -> None:
    source = tmp_path / "app.py"
    source.write_text("def first_name():\n    pass\n", encoding="utf-8")
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()
    source.write_text("def second_name():\n    pass\n", encoding="utf-8")

    update_exit = main(["update", "app.py", "--root", str(tmp_path), "--language", "python"])
    update_output = json.loads(capsys.readouterr().out)
    search_exit = main(["search", "second_name", "--root", str(tmp_path), "--language", "python", "--json"])
    search_output = json.loads(capsys.readouterr().out)

    assert update_exit == 0
    assert update_output["updated"] == ["app.py"]
    assert search_exit == 0
    assert search_output["symbols"][0]["name"] == "second_name"


def test_status_reports_missing_ready_and_stale(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("def status_target():\n    pass\n", encoding="utf-8")

    missing = code_symbol_index.status(tmp_path)
    code_symbol_index.index(tmp_path)
    ready = code_symbol_index.status(tmp_path)
    ready_checked = code_symbol_index.status(tmp_path, check=True)
    source.write_text("def status_target_changed():\n    pass\n", encoding="utf-8")
    fast_after_change = code_symbol_index.status(tmp_path)
    stale = code_symbol_index.status(tmp_path, check=True)

    assert missing.status == "missing"
    assert missing.reason == "index not initialized"
    assert ready.status == "ready"
    assert ready.files == 1
    assert ready.symbols == 1
    assert ready.languages == ("python",)
    assert ready.language_breakdown == ({"language": "python", "files": 1, "percent": 100.0},)
    assert ready.updated_at is not None
    assert ready.pending_changes == "unknown"
    assert ready_checked.status == "ready"
    assert ready_checked.pending_changes == 0
    assert fast_after_change.status == "ready"
    assert fast_after_change.pending_changes == "unknown"
    assert stale.status == "stale"
    assert stale.pending_changes == 1
    assert stale.pending_files == ("app.py",)
    assert stale.reason == "files changed after last index update"


def test_status_text_is_readable(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def status_text_target():\n    pass\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)

    output = code_symbol_index.status_text(tmp_path)
    checked_output = code_symbol_index.status_text(tmp_path, check=True)

    assert "index:" in output
    assert "status: ready" in output
    assert f"root: {tmp_path}" in output
    assert "files: 1" in output
    assert "symbols: 1" in output
    assert "languages: python" in output
    assert "language_breakdown:" in output
    assert "- python: 1 files (100.0%)" in output
    assert "pending_changes: unknown" in output
    assert "pending_changes: 0" in checked_output


def test_status_reports_bounded_pending_files_for_api_and_cli(tmp_path: Path, capsys) -> None:
    source = tmp_path / "app.py"
    source.write_text("def first():\n    pass\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)
    source.write_text("def second():\n    pass\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("def new_file():\n    pass\n", encoding="utf-8")

    status = code_symbol_index.status(tmp_path, check=True, max_pending_files=1)
    text = code_symbol_index.status_text(tmp_path, check=True, max_pending_files=2)
    json_exit = main(["status", "--root", str(tmp_path), "--check", "--max-pending-files", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert status.status == "stale"
    assert status.pending_changes == 2
    assert len(status.pending_files) == 1
    assert "pending_files:" in text
    assert json_exit == 0
    assert payload["pending_changes"] == 2
    assert set(payload["pending_files"]) == {"app.py", "new.py"}


def test_cli_status_defaults_to_text_and_supports_json(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text("def cli_status_target():\n    pass\n", encoding="utf-8")

    missing_exit = main(["status", "--root", str(tmp_path)])
    missing_output = capsys.readouterr().out
    assert main(["index", "--root", str(tmp_path), "--language", "python"]) == 0
    capsys.readouterr()
    text_exit = main(["status", "--root", str(tmp_path)])
    text_output = capsys.readouterr().out
    checked_exit = main(["status", "--root", str(tmp_path), "--check"])
    checked_output = capsys.readouterr().out
    json_exit = main(["status", "--root", str(tmp_path), "--json"])
    json_output = json.loads(capsys.readouterr().out)

    assert missing_exit == 0
    assert "status: missing" in missing_output
    assert "reason: index not initialized" in missing_output
    assert text_exit == 0
    assert "status: ready" in text_output
    assert "pending_changes: unknown" in text_output
    assert checked_exit == 0
    assert "pending_changes: 0" in checked_output
    assert json_exit == 0
    assert json_output["status"] == "ready"
    assert json_output["files"] == 1
    assert json_output["pending_changes"] == "unknown"
    assert json_output["language_breakdown"][0]["percent"] == 100.0


def test_cli_keyboard_interrupt_returns_130(monkeypatch, capsys) -> None:
    def interrupt(self: Repository) -> Repository:
        raise KeyboardInterrupt

    monkeypatch.setattr(Repository, "refresh", interrupt)

    exit_code = main(["index"])

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "interrupted" in captured.err


def test_cli_version(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected argparse version action to exit")

    captured = capsys.readouterr()
    assert captured.out.strip() == f"code-symbol-index {code_symbol_index.__version__}"

    assert main(["version"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"code-symbol-index {code_symbol_index.__version__}"


def test_install_skill_writes_codex_skill(tmp_path: Path) -> None:
    path = code_symbol_index.install_skill(codex_home=tmp_path)

    assert path == tmp_path / "skills" / "code-symbol-index" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "name: code-symbol-index" in text
    assert "code-symbol-index search Tool Agent" in text
    assert "If status is `missing`, ask the user before initializing the index" in text
    assert "Do not refresh the whole index automatically during ordinary status checks" in text
    assert "reason: files changed after last index update" in text
    assert "Do not ask for approval for incremental updates of known changed paths" in text
    assert "After each round of edits, sync the index for the files you changed" in text
    assert "code-symbol-index update src/app.py src/lib.py --root <repo>" in text
    assert "This is expected to be fast, including in large repositories" in text
    assert "Only ask before full-index refresh" in text
    assert "python -c" not in text

    second = code_symbol_index.install_skill(codex_home=tmp_path)
    assert second == path


def test_install_skill_refuses_overwrite_without_force(tmp_path: Path) -> None:
    path = tmp_path / "skills" / "code-symbol-index" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("custom skill\n", encoding="utf-8")

    try:
        code_symbol_index.install_skill(codex_home=tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError")

    overwritten = code_symbol_index.install_skill(codex_home=tmp_path, force=True)

    assert overwritten == path
    assert "name: code-symbol-index" in path.read_text(encoding="utf-8")


def test_cli_install_skill(tmp_path: Path, capsys) -> None:
    exit_code = main(["install-skill", "--codex-home", str(tmp_path)])

    captured = capsys.readouterr()
    skill_path = tmp_path / "skills" / "code-symbol-index" / "SKILL.md"
    assert exit_code == 0
    assert str(skill_path) in captured.out
    assert skill_path.exists()

    skill_path.write_text("custom skill\n", encoding="utf-8")
    conflict_exit = main(["install-skill", "--codex-home", str(tmp_path)])
    conflict = capsys.readouterr()
    force_exit = main(["install-skill", "--codex-home", str(tmp_path), "--force"])

    assert conflict_exit == 2
    assert "use --force" in conflict.err
    assert force_exit == 0


def test_api_requires_existing_index(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def missing_index_target():\n    pass\n", encoding="utf-8")

    try:
        code_symbol_index.search("missing_index_target", root=tmp_path)
    except IndexNotFoundError:
        pass
    else:
        raise AssertionError("expected IndexNotFoundError")

    try:
        code_symbol_index.update([tmp_path / "app.py"], root=tmp_path)
    except IndexNotFoundError:
        pass
    else:
        raise AssertionError("expected IndexNotFoundError")


def test_cli_requires_existing_index(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text("def missing_index_target():\n    pass\n", encoding="utf-8")

    exit_code = main(["missing_index_target", "--root", str(tmp_path), "--language", "python"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "code-symbol-index index" in captured.err


_REF_KIND_SOURCE = """\
from mod import widget


class Thing:
    pass


class Child(widget.Base):
    field: Thing = None

    def run(self):
        local = widget()
        member = self.widget
        widget = 5
        return local


def widget():
    return 1
"""


def _kinds_by_name(references, name):
    return {ref.reference_kind for ref in references if ref.name == name}


def test_references_classified_by_behavior(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()

    every = repo.refs("widget", ref_kinds="all", limit=50)
    kinds = _kinds_by_name(every, "widget")

    assert "call" in kinds       # widget()
    assert "write" in kinds      # widget = 5
    assert "inherit" in kinds    # class Child(widget.Base)
    assert "import" in kinds     # from mod import widget
    assert "attribute" in kinds  # self.widget

    type_refs = repo.refs("Thing", ref_kinds="all", limit=50)
    assert "type" in _kinds_by_name(type_refs, "Thing")  # field: Thing


def test_refs_default_hides_import_and_attribute_noise(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()

    default_kinds = _kinds_by_name(repo.refs("widget", limit=50), "widget")

    assert "import" not in default_kinds
    assert "attribute" not in default_kinds
    assert "call" in default_kinds
    assert "write" in default_kinds


def test_refs_ref_kinds_explicit_filter(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    repo = Repository(tmp_path, create_index=True).refresh()

    only_calls = repo.refs("widget", ref_kinds="call", limit=50)

    assert only_calls
    assert all(ref.reference_kind == "call" for ref in only_calls)


def test_refs_storage_path_filters_by_kind(tmp_path: Path) -> None:
    # CodeIndex (not Repository) reads persisted refs via SQL.
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    index = CodeIndex(tmp_path).build()

    all_kinds = _kinds_by_name(index.refs("widget", ref_kinds="all", limit=50), "widget")
    behavioral = _kinds_by_name(index.refs("widget", limit=50), "widget")

    assert "import" in all_kinds
    assert "import" not in behavioral
    assert "call" in behavioral


def test_refs_text_and_json_expose_kind(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    code_symbol_index.index(tmp_path)

    text = code_symbol_index.refs("widget", root=tmp_path, format="text", limit=50)
    payload = code_symbol_index.refs("widget", root=tmp_path, format="json", limit=50)

    assert "kind:" in text
    assert payload["items"]
    # The Python API json mirrors the dataclass field name.
    assert all("reference_kind" in item for item in payload["items"])

    # The CLI --json output uses the readable "kind" key.
    main(["refs", "widget", "--root", str(tmp_path), "--json", "--all-kinds", "--limit", "50"])
    cli_payload = json.loads(capsys.readouterr().out)
    assert all("kind" in item for item in cli_payload["items"])


def test_inspect_summary_reports_reference_kinds(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    code_symbol_index.index(tmp_path)

    text = code_symbol_index.inspect_text("widget", root=tmp_path)
    assert "reference_kinds:" in text

    main(["inspect", "widget", "--root", str(tmp_path), "--json", "--max-references", "50"])
    payload = json.loads(capsys.readouterr().out)
    assert "call" in payload["reference_kinds"]


def test_refs_javascript_classification(tmp_path: Path) -> None:
    (tmp_path / "app.js").write_text(
        "import { widget } from './m';\n"
        "class Child extends widget {\n"
        "  run() {\n"
        "    const value = widget();\n"
        "    return obj.widget;\n"
        "  }\n"
        "}\n"
        "function widget() { return 1; }\n",
        encoding="utf-8",
    )
    repo = Repository(tmp_path, create_index=True).refresh()

    kinds = _kinds_by_name(repo.refs("widget", ref_kinds="all", limit=50), "widget")

    assert "call" in kinds
    assert "inherit" in kinds
    assert "import" in kinds


def test_cli_ref_kind_flag_and_all_kinds(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    code_symbol_index.index(tmp_path)

    exit_code = main(["refs", "widget", "--root", str(tmp_path), "--ref-kind", "call", "--limit", "50"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "kind: call" in out
    assert "kind: import" not in out

    exit_code = main(["refs", "widget", "--root", str(tmp_path), "--all-kinds", "--limit", "50"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "kind: import" in out


def test_cli_ref_kind_rejects_unknown_kind(tmp_path: Path, capsys) -> None:
    (tmp_path / "app.py").write_text(_REF_KIND_SOURCE, encoding="utf-8")
    code_symbol_index.index(tmp_path)

    try:
        main(["refs", "widget", "--root", str(tmp_path), "--ref-kind", "bogus"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit for invalid --ref-kind")
    assert "unknown reference kind" in capsys.readouterr().err


class _FakeStream:
    def __init__(self, interactive: bool) -> None:
        self._interactive = interactive
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return self._interactive

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass


def test_cli_progress_compresses_when_non_tty() -> None:
    stream = _FakeStream(interactive=False)
    progress = code_symbol_index._CliProgress(stream)

    progress("scan")
    progress("start", done=0, total=3)
    for done in range(1, 4):
        progress("file", done=done, total=3)
    progress("finish")

    output = "".join(stream.writes)
    assert output == "indexed 3 files\n"
    assert "\r" not in output


def test_cli_progress_silent_for_noop_sync_when_non_tty() -> None:
    stream = _FakeStream(interactive=False)
    progress = code_symbol_index._CliProgress(stream)

    progress("scan")
    progress("finish")

    assert stream.writes == []


def test_cli_progress_keeps_live_bar_when_interactive() -> None:
    stream = _FakeStream(interactive=True)
    progress = code_symbol_index._CliProgress(stream)

    progress("start", done=0, total=2)
    progress("file", done=1, total=2)
    progress("finish")

    output = "".join(stream.writes)
    assert "\r" in output          # live, self-rewriting bar
    assert "indexing" in output
    assert "indexed 2 files" not in output


def _write_call_chain_fixture(tmp_path: Path) -> None:
    (tmp_path / "app" / "api").mkdir(parents=True)
    (tmp_path / "app" / "workers").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "app" / "jobs.py").write_text(
        "def handle_agent_job_run(job):\n"
        "    return _do_work(job)\n\n"
        "def _do_work(job):\n"
        "    return 1\n\n"
        "def dispatch_job(job):\n"
        "    return handle_agent_job_run(job)\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "api" / "agents.py").write_text(
        "from app.jobs import dispatch_job\n\n"
        "@router.post('/agents/{id}/run')\n"
        "def run_agent_endpoint(id):\n"
        "    return dispatch_job(id)\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "workers" / "queue.py").write_text(
        "from app.jobs import handle_agent_job_run\n\n"
        "@shared_task\n"
        "def process_queue(msg):\n"
        "    return handle_agent_job_run(msg)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_jobs.py").write_text(
        "from app.jobs import handle_agent_job_run\n\n"
        "def test_handle_agent_job_run():\n"
        "    assert handle_agent_job_run({}) is not None\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "run.py").write_text(
        "from app.jobs import dispatch_job\n\n"
        "def main():\n"
        "    dispatch_job({})\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )


def test_callers_groups_entry_points(tmp_path: Path) -> None:
    _write_call_chain_fixture(tmp_path)
    code_symbol_index.index(tmp_path)
    graph = code_symbol_index.callers("handle_agent_job_run", root=tmp_path, depth=3)

    by_type: dict[str, set[str]] = {}
    for entry in graph.entry_points:
        by_type.setdefault(entry.entry_type, set()).add(entry.symbol.name)

    assert by_type.get("http_route") == {"run_agent_endpoint"}
    assert by_type.get("worker") == {"process_queue"}
    assert by_type.get("test") == {"test_handle_agent_job_run"}
    assert "main" in by_type.get("script", set())

    route_entry = next(e for e in graph.entry_points if e.symbol.name == "run_agent_endpoint")
    assert [s.name for s in route_entry.path] == ["run_agent_endpoint", "dispatch_job", "handle_agent_job_run"]


def test_callees_descend_depth(tmp_path: Path) -> None:
    _write_call_chain_fixture(tmp_path)
    code_symbol_index.index(tmp_path)
    graph = code_symbol_index.callees("dispatch_job", root=tmp_path, depth=2)

    level1 = {node.symbol.name for node in graph.roots}
    assert "handle_agent_job_run" in level1
    level2 = {child.symbol.name for node in graph.roots for child in node.children}
    assert "_do_work" in level2


def test_callees_ignore_non_call_references(tmp_path: Path) -> None:
    # A type annotation referencing a known symbol must not become a callee.
    (tmp_path / "m.py").write_text(
        "class Widget:\n    pass\n\n"
        "def helper():\n    return 1\n\n"
        "def use(x: Widget):\n    return helper()\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)
    graph = code_symbol_index.callees("use", root=tmp_path, depth=1)
    names = {node.symbol.name for node in graph.roots}
    assert "helper" in names
    assert "Widget" not in names


def test_callers_cycle_terminates(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "def a():\n    return b()\n\n"
        "def b():\n    return a()\n",
        encoding="utf-8",
    )
    code_symbol_index.index(tmp_path)
    graph = code_symbol_index.callers("a", root=tmp_path, depth=5)
    # b calls a; a calls b — traversal must terminate and find b as a caller.
    assert any(node.symbol.name == "b" for node in graph.roots)


def test_callers_depth_is_bounded(tmp_path: Path) -> None:
    _write_call_chain_fixture(tmp_path)
    code_symbol_index.index(tmp_path)
    shallow = code_symbol_index.callers("handle_agent_job_run", root=tmp_path, depth=1)
    # depth 1 reaches direct callers only (dispatch_job, process_queue, test_*),
    # not the http_route two hops away.
    names_depth1 = {node.symbol.name for node in shallow.roots}
    assert "dispatch_job" in names_depth1
    assert all(not node.children for node in shallow.roots)


def test_cli_callers_text_and_json(tmp_path: Path, capsys) -> None:
    _write_call_chain_fixture(tmp_path)
    code_symbol_index.index(tmp_path)

    assert main(["callers", "handle_agent_job_run", "--root", str(tmp_path), "--depth", "3"]) == 0
    text = capsys.readouterr().out
    assert "entry_points:" in text
    assert "http_route:" in text
    assert "path: run_agent_endpoint -> dispatch_job -> handle_agent_job_run" in text

    assert main(["callees", "dispatch_job", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["direction"] == "callees"
    assert payload["confidence"] == "low"
    assert any(node["name"] == "handle_agent_job_run" for node in payload["callees"])


def test_cli_callers_depth_validation(tmp_path: Path) -> None:
    _write_call_chain_fixture(tmp_path)
    code_symbol_index.index(tmp_path)
    try:
        main(["callers", "handle_agent_job_run", "--root", str(tmp_path), "--depth", "99"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit for out-of-range --depth")


def test_cli_callers_ambiguous_symbol_clean_error(tmp_path: Path, capsys) -> None:
    (tmp_path / "a.py").write_text("def dup():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def dup():\n    return 2\n", encoding="utf-8")
    code_symbol_index.index(tmp_path)

    exit_code = main(["callers", "dup", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "mbiguous" in err or "narrow with" in err
