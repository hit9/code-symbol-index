import io
import json
from pathlib import Path

import pytest

import code_symbol_index as csi


def invoke(monkeypatch, root, name="Write", tool_input=None):
    event = {"hook_event_name": "PostToolUse", "cwd": str(root),
             "tool_name": name, "tool_input": tool_input or {"file_path": "app.py"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert csi.main(["hook"]) == 0


def names(root):
    repo = csi.Repository(root)
    try:
        return {row[0] for row in repo.storage.connection.execute("SELECT name FROM symbols")}
    finally:
        repo.storage.connection.close()


def init(root):
    repo = csi.index(root)
    repo.storage.connection.close()


def test_codex_merge_and_idempotence(tmp_path):
    config = {"description": "keep", "hooks": {"Stop": [], "PostToolUse": [
        {"matcher": "Write", "hooks": [{"type": "command", "command": "echo custom"}]}]}}
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(config))
    csi.install_skill(codex_home=tmp_path, with_hooks=True)
    once = target.read_text()
    csi.install_skill(codex_home=tmp_path, with_hooks=True)
    assert target.read_text() == once
    result = json.loads(once)
    assert result["description"] == "keep"
    assert result["hooks"]["Stop"] == []
    assert result["hooks"]["PostToolUse"][0] == config["hooks"]["PostToolUse"][0]
    assert len(result["hooks"]["PostToolUse"]) == 2


def test_claude_frontmatter(tmp_path):
    skill = csi.install_skill(target="claude", claude_dir=tmp_path, with_hooks=True)
    config = json.loads(next(line[7:] for line in skill.read_text().splitlines() if line.startswith("hooks: ")))
    assert config["PostToolUse"][0]["hooks"][0]["command"] == "code-symbol-index hook"
    assert not (tmp_path / "hooks.json").exists()
    csi.install_skill(target="claude", claude_dir=tmp_path, with_hooks=True)


@pytest.mark.parametrize("content", ['[]', '{', '{"hooks": []}', '{"hooks": {"PostToolUse": {}}}'])
def test_invalid_config_preserved(tmp_path, content):
    target = tmp_path / "hooks.json"
    target.write_text(content)
    with pytest.raises(ValueError):
        csi.install_skill(codex_home=tmp_path, with_hooks=True, force=True)
    assert target.read_text() == content
    assert not (tmp_path / "skills").exists()


def test_default_has_no_hooks(tmp_path):
    path = csi.install_skill(codex_home=tmp_path)
    assert "\nhooks:" not in path.read_text()
    assert not (tmp_path / "hooks.json").exists()


def test_missing_index_no_creation(tmp_path, monkeypatch):
    invoke(monkeypatch, tmp_path)
    assert not (tmp_path / csi.DEFAULT_INDEX_DIR).exists()


def test_write_and_edit_only_selected_file(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text("old_name = 1\n")
    (tmp_path / "other.py").write_text("untouched = 1\n")
    init(tmp_path)
    (tmp_path / "app.py").write_text("new_name = 2\n")
    (tmp_path / "other.py").write_text("external_change = 1\n")
    monkeypatch.setattr(csi._UpdateCheck, "start", lambda: pytest.fail("network update check"))
    invoke(monkeypatch, tmp_path, "Edit")
    assert names(tmp_path) == {"new_name", "untouched"}
    assert capsys.readouterr().out == ""


def test_patch_add_delete_move(tmp_path, monkeypatch):
    (tmp_path / "old.py").write_text("old_name = 1\n")
    (tmp_path / "deleted.py").write_text("deleted_name = 1\n")
    init(tmp_path)
    (tmp_path / "old.py").unlink()
    (tmp_path / "deleted.py").unlink()
    (tmp_path / "new.py").write_text("moved_name = 1\n")
    (tmp_path / "added.py").write_text("added_name = 1\n")
    patch = "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n@@\n-old_name = 1\n+moved_name = 1\n*** Delete File: deleted.py\n*** Add File: added.py\n+added_name = 1\n*** End Patch"
    invoke(monkeypatch, tmp_path, "apply_patch", {"command": patch})
    assert names(tmp_path) == {"moved_name", "added_name"}


def test_subdirectory_and_outside_path(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    subdir = root / "src"
    subdir.mkdir()
    init(root)
    (subdir / "app.py").write_text("inside = 1\n")
    invoke(monkeypatch, subdir)
    assert names(root) == {"inside"}
    (tmp_path / "outside.py").write_text("outside = 1\n")
    invoke(monkeypatch, root, tool_input={"file_path": "../outside.py"})
    assert names(root) == {"inside"}


def test_nested_git_boundary(tmp_path, monkeypatch):
    init(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    (nested / "app.py").write_text("nested = 1\n")
    invoke(monkeypatch, nested)
    assert names(tmp_path) == set()


def test_invalid_event_is_nonblocking(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert csi.main(["hook"]) == 0
    assert "hook:" in capsys.readouterr().err


def test_old_schema_never_refreshes(tmp_path, monkeypatch, capsys):
    init(tmp_path)
    monkeypatch.setattr(csi._Storage, "schema_version", lambda self: -1)
    monkeypatch.setattr(csi.Repository, "refresh", lambda self: pytest.fail("full refresh"))
    invoke(monkeypatch, tmp_path)
    assert "outdated" in capsys.readouterr().err


def test_bash_event_ignored(tmp_path, monkeypatch):
    init(tmp_path)
    (tmp_path / "app.py").write_text("shell_change = 1\n")
    invoke(monkeypatch, tmp_path, "Bash")
    assert names(tmp_path) == set()


def test_concurrent_hook_processes(tmp_path):
    import subprocess
    import sys

    init(tmp_path)
    processes = []
    for i in range(4):
        path = tmp_path / f"file{i}.py"
        path.write_text(f"symbol_{i} = {i}\n")
        event = {"hook_event_name": "PostToolUse", "cwd": str(tmp_path),
                 "tool_name": "Write", "tool_input": {"file_path": str(path)}}
        process = subprocess.Popen(
            [sys.executable, str(Path(csi.__file__).resolve()), "hook"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        process.stdin.write(json.dumps(event))
        process.stdin.close()
        process.stdin = None
        processes.append(process)
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0
        assert stdout == stderr == ""
    assert names(tmp_path) == {f"symbol_{i}" for i in range(4)}


def test_skill_conflict_does_not_change_codex_hooks(tmp_path):
    path = csi.install_skill(codex_home=tmp_path)
    path.write_text("custom skill")
    hooks = tmp_path / "hooks.json"
    hooks.write_text('{"description": "keep"}')
    with pytest.raises(FileExistsError):
        csi.install_skill(codex_home=tmp_path, with_hooks=True)
    assert hooks.read_text() == '{"description": "keep"}'


def test_cli_installs_hooks(tmp_path, capsys):
    assert csi.main(["install-skill", "--with-hooks", "--codex-home", str(tmp_path)]) == 0
    assert "/hooks" in capsys.readouterr().out
    assert (tmp_path / "hooks.json").exists()


def test_wizolt_edit_path(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("old_name = 1\n")
    init(tmp_path)
    (tmp_path / "app.py").write_text("wizolt_updated = 2\n")
    invoke(monkeypatch, tmp_path, "Edit", {"path": "app.py", "edits": []})
    assert names(tmp_path) == {"wizolt_updated"}
