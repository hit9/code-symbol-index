import json
from pathlib import Path
from unittest import mock

import pytest

import code_symbol_index as c


def run_index(root, capsys, *args):
    assert c.main(["index", "--root", str(root), *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_refresh_reports_actual_changes_and_retries_only_failures(tmp_path, capsys):
    for name in ("keep", "change", "remove", "bad"):
        (tmp_path / f"{name}.py").write_text(f"def {name}(): return 1\n")
    initial = run_index(tmp_path, capsys)
    assert initial["complete"]
    assert initial["counts"] == {"updated": 4, "removed": 0, "failed": 0, "unchanged": 0}
    assert initial["updated"] == ["bad.py", "change.py", "keep.py", "remove.py"]

    (tmp_path / "change.py").write_text("def changed(): return 22\n")
    (tmp_path / "add.py").write_text("def added(): return 1\n")
    (tmp_path / "remove.py").unlink()
    (tmp_path / "bad.py").write_bytes(b"\xff\x00broken\n")
    result = run_index(tmp_path, capsys)
    assert not result["complete"]
    assert result["counts"] == {"updated": 2, "removed": 1, "failed": 1, "unchanged": 1}
    assert result["updated"] == ["add.py", "change.py"]
    assert result["removed"] == ["remove.py"]
    assert result["failed"] == ["bad.py"]
    assert not result["paths_truncated"]
    repo = c.Repository(tmp_path)
    try:
        assert repo.search_symbols("bad", exact_only=True)  # Failure preserves old entries.
        assert not repo.search_symbols("remove", exact_only=True)
        assert repo.search_symbols("changed", exact_only=True)
    finally:
        repo.storage.connection.close()

    (tmp_path / "bad.py").write_text("def repaired(): return 1\n")
    with mock.patch.object(c, "_parse_file", wraps=c._parse_file) as parse:
        retry = run_index(tmp_path, capsys)
    assert [call.args[1] for call in parse.call_args_list] == [Path("bad.py")]
    assert retry["complete"]
    assert retry["counts"] == {"updated": 1, "removed": 0, "failed": 0, "unchanged": 3}


def test_refresh_report_reuses_scan_and_resets_between_calls(tmp_path):
    (tmp_path / "app.py").write_text("def target(): pass\n")
    repo = c.Repository(tmp_path, create_index=True)
    try:
        repo.refresh()
        assert repo.last_refresh_updated == ("app.py",)
        with mock.patch.object(repo, "_iter_indexable_files", wraps=repo._iter_indexable_files) as scan, \
             mock.patch.object(c, "_parse_file", side_effect=AssertionError("unchanged AST rebuilt")):
            repo.refresh()
        assert scan.call_count == 1
        assert repo.last_refresh_updated == repo.last_refresh_removed == repo.last_refresh_failed == ()
        assert repo.last_refresh_unchanged == 1
    finally:
        repo.storage.connection.close()


@pytest.mark.parametrize("limit", [0, 1, 50])
def test_bounded_paths_do_not_hide_failures_or_counts(tmp_path, capsys, limit):
    for i in range(3):
        (tmp_path / f"bad{i}.py").write_bytes(b"\xff\x00broken")
        (tmp_path / f"good{i}.py").write_text(f"symbol_{i} = 1\n")
    result = run_index(tmp_path, capsys, "--max-result-files", str(limit))
    assert not result["complete"]
    assert result["counts"] == {"updated": 3, "removed": 0, "failed": 3, "unchanged": 0}
    assert len(result["updated"]) == len(result["failed"]) == min(limit, 3)
    assert result["paths_truncated"] == (limit < 3)


def test_permission_failure_is_reported_without_deleting_previous_rows(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("def target(): pass\n")
    repo = c.Repository(tmp_path, create_index=True).refresh()
    stat = Path.stat

    def denied(file, *args, **kwargs):
        if file == path:
            raise PermissionError("fixture")
        return stat(file, *args, **kwargs)

    try:
        with mock.patch.object(Path, "stat", denied):
            repo.refresh()
        assert repo.last_refresh_failed == ("app.py",)
        assert repo.last_refresh_removed == repo.last_refresh_updated == ()
        assert repo.last_refresh_unchanged == 0
        assert repo.search_symbols("target", exact_only=True)
    finally:
        repo.storage.connection.close()


def test_filtered_refresh_reports_only_its_scope(tmp_path, capsys):
    (tmp_path / "app.py").write_text("def target(): pass\n")
    (tmp_path / "app.c").write_text("int target(void) { return 1; }\n")
    run_index(tmp_path, capsys)
    (tmp_path / "app.py").unlink()
    result = run_index(tmp_path, capsys, "--language", "c")
    assert result["complete"]
    assert result["counts"] == {"updated": 0, "removed": 0, "failed": 0, "unchanged": 1}
    repo = c.Repository(tmp_path)
    try:
        assert repo.search_symbols("target", language="python", exact_only=True)
    finally:
        repo.storage.connection.close()


def test_update_reports_removed_paths_separately(tmp_path, capsys):
    path = tmp_path / "app.py"
    path.write_text("def target(): pass\n")
    run_index(tmp_path, capsys)
    path.unlink()
    assert c.main(["update", "app.py", "--root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["updated"] == []
    assert result["removed"] == ["app.py"]


@pytest.mark.parametrize("outdated_schema", [False, True])
def test_update_full_refresh_reports_failures_and_resets_results(tmp_path, outdated_schema):
    (tmp_path / "good.py").write_text("def good(): pass\n")
    repo = c.Repository(tmp_path, create_index=True).refresh()
    try:
        repo.update(["good.py"])
        (tmp_path / "bad.py").write_bytes(b"\xffbroken")
        if outdated_schema:
            repo.storage.set_schema_version(-1)
        repo.update(["bad.py"] if outdated_schema else None)
        assert repo.last_update_failed == ("bad.py",)
        assert repo.last_update_updated == (("good.py",) if outdated_schema else ())

        (tmp_path / "bad.py").unlink()
        (tmp_path / "good.py").unlink()
        repo.update()
        assert repo.last_update_failed == repo.last_update_updated == ()
        assert repo.last_update_removed == ("good.py",)
    finally:
        repo.storage.connection.close()


def test_cli_update_reports_schema_rebuild_failure(tmp_path, capsys):
    (tmp_path / "good.py").write_text("def good(): pass\n")
    run_index(tmp_path, capsys)
    repo = c.Repository(tmp_path)
    repo.storage.set_schema_version(-1)
    repo.storage.connection.close()
    (tmp_path / "bad.py").write_bytes(b"\xffbroken")
    assert c.main(["update", "bad.py", "--root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["updated"] == ["good.py"]
    assert result["removed"] == []
    assert result["failed"] == ["bad.py"]
