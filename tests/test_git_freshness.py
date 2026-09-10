"""Git hint coverage uses only disposable local repositories; no remotes."""
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import code_symbol_index as c


def git(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'user.name=hit9', '-c', 'user.email=hit9@icloud.com',
         '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
         '-C', str(root), *args], stderr=subprocess.PIPE, text=True,
    ).strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, 'init', '-b', 'first')
    (tmp_path / 'app.py').write_text('def target():\n    return 1\ndef caller():\n    return target()\n')
    git(tmp_path, 'add', 'app.py')
    git(tmp_path, 'commit', '-m', 'fixture')
    return c.Repository(tmp_path, create_index=True).refresh()


@pytest.mark.parametrize('command', [
    ['search', 'target'], ['inspect', 'target'], ['refs', 'target'],
    ['callers', 'target'], ['callees', 'caller'], ['impls', 'target'], ['outline', 'app.py'],
])
@pytest.mark.parametrize('json_output', [False, True])
def test_queries_warn_on_same_commit_branch_switch(repository, capsys, command, json_output):
    repo = repository
    args = command + ['--root', str(repo.root)] + (['--json'] if json_output else [])
    assert c.main(args) == 0
    before = capsys.readouterr()
    assert before.err == ''
    git(repo.root, 'checkout', '-b', 'second')
    with mock.patch.object(subprocess, 'run', side_effect=AssertionError('query launches Git')):
        assert c.main(args) == 0
    after = capsys.readouterr()
    assert after.out == before.out
    assert 'index may be stale' in after.err
    assert after.err.count('warning:') == 1
    if json_output:
        json.loads(after.out)


def test_partial_update_full_check_and_sync_have_distinct_effects(repository, capsys):
    repo = repository
    git(repo.root, 'checkout', '-b', 'second')
    repo.update(['app.py'])
    assert c.status(repo.root).git_freshness == 'changed'
    assert c.status(repo.root).status == 'stale'
    baseline = repo.storage.connection.execute("SELECT value FROM meta WHERE key='git_baseline'").fetchone()[0]
    checked = c.status(repo.root, check=True)
    assert checked.status == 'ready'  # Same source, despite different Git branch.
    assert checked.git_freshness == 'changed'
    assert repo.storage.connection.execute("SELECT value FROM meta WHERE key='git_baseline'").fetchone()[0] == baseline
    assert c.main(['search', 'target', '--root', str(repo.root), '--sync']) == 0
    assert 'warning:' not in capsys.readouterr().err
    assert c.status(repo.root).git_freshness == 'unchanged'


def test_old_schema_five_database_is_not_rebuilt_or_backfilled_on_read(repository, capsys):
    repo = repository
    with repo.storage.connection:
        repo.storage.connection.execute("DELETE FROM meta WHERE key='git_baseline'")
    before = repo.storage.connection.execute('SELECT * FROM symbols ORDER BY id').fetchall()
    with mock.patch.object(c.Repository, 'refresh', side_effect=AssertionError('read rebuilt index')):
        assert c.main(['search', 'target', '--root', str(repo.root)]) == 0
    assert 'Git freshness unknown' in capsys.readouterr().err
    assert repo.storage.schema_version() == 5
    assert repo.storage.connection.execute("SELECT value FROM meta WHERE key='git_baseline'").fetchone() is None
    assert repo.storage.connection.execute('SELECT * FROM symbols ORDER BY id').fetchall() == before
    with mock.patch.object(repo, '_parse_files', wraps=repo._parse_files) as parse:
        repo.refresh()
    assert parse.call_args.args[0] == []
    assert c.status(repo.root).git_freshness == 'unchanged'


def test_remote_tracking_refs_do_not_change_checkout_state(repository):
    repo = repository
    before = c._git_state(repo.root)
    metadata = repo.root / '.git'
    (metadata / 'FETCH_HEAD').write_text('f' * 40 + '\n')
    (metadata / 'refs/remotes/origin').mkdir(parents=True)
    (metadata / 'refs/remotes/origin/first').write_text('e' * 40 + '\n')
    assert c._git_state(repo.root) == before
    assert c.status(repo.root).git_freshness == 'unchanged'


def test_packed_refs_detached_reset_and_worktree(repository, tmp_path):
    repo = repository
    state = c._git_state(repo.root)
    git(repo.root, 'pack-refs', '--all', '--prune')
    assert c._git_state(repo.root) == state
    (repo.root / 'app.py').write_text('def changed():\n    return 2\n')
    git(repo.root, 'add', 'app.py')
    git(repo.root, 'commit', '-m', 'change')
    assert c.status(repo.root).git_freshness == 'changed'
    git(repo.root, 'reset', '--hard', 'HEAD~1')
    assert c._git_state(repo.root) == state
    git(repo.root, 'checkout', '--detach')
    assert c.status(repo.root).git_freshness == 'changed'
    worktree = tmp_path / 'worktree'
    git(repo.root, 'worktree', 'add', '-b', 'linked', str(worktree))
    linked = c.Repository(worktree, create_index=True).refresh()
    assert c.status(linked.root).git_freshness == 'unchanged'
    git(worktree, 'checkout', '-b', 'linked-second')
    assert c.status(linked.root).git_freshness == 'changed'
    sub = worktree / 'sub'; sub.mkdir()
    assert c._git_state(sub) == c._git_state(worktree)


def test_unknown_git_metadata_is_bounded_and_not_reported_unchanged(repository, monkeypatch):
    repo = repository
    git(repo.root, 'pack-refs', '--all', '--prune')
    monkeypatch.setattr(c, 'GIT_PACKED_REFS_MAX_BYTES', 8)
    assert c._git_state(repo.root) is None
    assert c.status(repo.root).git_freshness == 'unknown'
    (repo.root / '.git/reftable').mkdir()
    assert c._git_state(repo.root) is None
    (repo.root / '.git/HEAD').write_text('ref: refs/../../outside\n')
    assert c._git_state(repo.root) is None
    (repo.root / '.git/HEAD').write_text('x' * 4097)
    assert c._git_state(repo.root) is None


def test_unborn_head_and_git_change_during_refresh(tmp_path):
    git(tmp_path, 'init', '-b', 'unborn')
    assert json.loads(c._git_state(tmp_path))[-1] == 'unborn'
    repo = c.Repository(tmp_path, create_index=True)
    with mock.patch.object(c, '_git_state', return_value='checkout-at-scan-start'):
        repo.refresh()
    assert c.status(repo.root).git_freshness == 'changed'


def test_non_git_malformed_baseline_and_root_mismatch(tmp_path):
    assert c._git_freshness(tmp_path, None) == 'not-applicable'
    with mock.patch.object(c, '_git_state', return_value='some-checkout'):
        for invalid in ('{}', '[]', 'null', '42', '"value"', 'invalid', '["root", null]'):
            assert c._git_freshness(tmp_path, invalid) == 'unknown'
        assert c._git_freshness(tmp_path, json.dumps(['elsewhere', 'some-checkout'])) == 'changed'


def test_git_check_does_not_load_manifest_or_scan_sources(repository):
    repo = repository
    with mock.patch.object(c.CodeIndex, '_iter_indexable_files', side_effect=AssertionError('scan')), \
         mock.patch.object(c, '_read_index_metadata', wraps=c._read_index_metadata) as metadata:
        assert c.status(repo.root).git_freshness == 'unchanged'
    assert metadata.call_args.kwargs['include_files'] is False


def test_failed_parse_does_not_publish_known_git_baseline(repository):
    repo = repository
    (repo.root / 'app.py').write_text('def changed():\n    return 22\n')
    with mock.patch.object(repo, '_parse_files', return_value=[]):
        repo.refresh()
    assert c.status(repo.root).git_freshness == 'unknown'
