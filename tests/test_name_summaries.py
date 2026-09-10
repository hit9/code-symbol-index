import os
from pathlib import Path
from unittest import mock

import pytest

import code_symbol_index as c


@pytest.fixture(autouse=True)
def immediate_summaries(monkeypatch):
    monkeypatch.setattr(c, 'NAME_SUMMARY_SCAN_PREFIX', 0)
    monkeypatch.setattr(c, 'NAME_SUMMARY_MIN_AGE_NS', 0)


def fixture_repo(tmp_path):
    (tmp_path / 'a.py').write_text('def target():\n    return 1\n')
    (tmp_path / 'b.py').write_text('def unused():\n    return 1\n')
    return c.Repository(tmp_path, create_index=True).refresh()


def test_unchanged_negative_skips_open_and_edit_is_seen_on_reused_repository(tmp_path):
    repo = fixture_repo(tmp_path)
    with mock.patch.object(c, '_file_contains_bytes', wraps=c._file_contains_bytes) as reads:
        assert repo.refs('target').items == ()
    assert all(call.args[0].name != 'b.py' for call in reads.call_args_list)
    (tmp_path / 'b.py').write_text('def caller():\n    return target()\n')
    assert len(repo.refs('target').items) == 1
    repo.update(['b.py'])
    assert len(repo.refs('target').items) == 1
    (tmp_path / 'b.py').write_text('def caller():\n    return unused()\n')
    assert repo.refs('target').items == ()
    assert repo._name_filter is None


def test_same_size_mtime_replacement_and_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'NAME_SUMMARY_MIN_AGE_NS', 1_000_000_000)
    repo = fixture_repo(tmp_path)
    path = tmp_path / 'b.py'
    path.write_text('def caller():\n    return absent()\n')
    repo.update(['b.py'])
    before = path.stat()
    path.write_text('def caller():\n    return target()\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert len(repo.refs('target').items) == 1
    replacement = tmp_path / 'replacement'
    replacement.write_text('def caller():\n    return target()\n')
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert len(repo.refs('target').items) == 1
    path.unlink()
    assert repo.refs('target').items == ()


def test_summary_query_matches_scanner_for_graph_and_clears_context(tmp_path):
    repo = fixture_repo(tmp_path)
    (tmp_path / 'b.py').write_text('def branch():\n    target()\ndef entry():\n    branch()\n')
    repo.update(['b.py'])
    for method in (lambda: repo.callers('target'), lambda: repo.inspect('target'), lambda: repo.inspect_text('target')):
        with mock.patch.object(c._NameFilter, 'may_contain', return_value=True):
            expected = method()
        assert method() == expected
        assert repo._name_filter is None
    with mock.patch.object(c._NameFilter, 'may_contain', side_effect=RuntimeError('interrupted')):
        with pytest.raises(RuntimeError):
            repo.callers('target')
    assert repo._name_filter is None
    assert repo.callers('target')


def legacy_files_table(repo):
    with repo.storage.connection:
        repo.storage.connection.executescript('''
            ALTER TABLE files RENAME TO saved_files;
            CREATE TABLE files(path TEXT PRIMARY KEY, language TEXT, mtime_ns INTEGER, size INTEGER);
            INSERT INTO files SELECT path,language,mtime_ns,size FROM saved_files;
            DROP TABLE saved_files;
        ''')


def test_old_database_reads_do_not_migrate_and_refresh_only_fills_metadata(tmp_path):
    repo = fixture_repo(tmp_path)
    legacy_files_table(repo)
    before = repo.storage.connection.execute('SELECT * FROM symbols ORDER BY id').fetchall()
    assert repo.refs('target').items == ()
    assert len(repo.storage.connection.execute('PRAGMA table_info(files)').fetchall()) == 4
    with mock.patch.object(c, '_parse_file', side_effect=AssertionError('unchanged AST rebuilt')):
        repo.refresh()
    assert repo.storage.schema_version() == 5
    assert repo.storage.connection.execute('SELECT * FROM symbols ORDER BY id').fetchall() == before
    assert repo.storage.connection.execute('SELECT count(*) FROM files WHERE name_summary IS NOT NULL').fetchone()[0] == 2
    with mock.patch.object(c, '_file_name_summary', side_effect=AssertionError('backfill repeated')):
        repo.refresh()


def test_partial_update_only_populates_its_own_summary(tmp_path):
    repo = fixture_repo(tmp_path)
    legacy_files_table(repo)
    repo.update(['a.py'])
    assert repo.storage.connection.execute('SELECT path FROM files WHERE name_summary IS NOT NULL').fetchall()[0][0] == 'a.py'
    assert repo.storage.connection.execute('SELECT count(*) FROM files WHERE name_summary IS NOT NULL').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['utime', 'chmod', 'replace'])
def test_refresh_repairs_metadata_invalidated_summary_without_parsing(tmp_path, change):
    repo = fixture_repo(tmp_path)
    path = tmp_path / 'b.py'
    before = path.stat()
    if change == 'replace':
        replacement = tmp_path / 'replacement'
        replacement.write_bytes(path.read_bytes())
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
    elif change == 'chmod':
        path.chmod(before.st_mode ^ 0o100)
    else:
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    # Some filesystems can give consecutive mutations the same timestamp tick.
    if (path.stat().st_ino, path.stat().st_ctime_ns) == (before.st_ino, before.st_ctime_ns):
        pytest.skip('filesystem timestamp tick did not advance')
    assert c._NameFilter(repo).may_contain(Path('b.py'), (b'missing',))
    with mock.patch.object(c, '_parse_file', side_effect=AssertionError('unchanged AST rebuilt')):
        repo.refresh()
    assert not c._NameFilter(repo).may_contain(Path('b.py'), (b'missing',))
    with mock.patch.object(c, '_file_name_summary', side_effect=AssertionError('backfill repeated')):
        repo.refresh()


@pytest.mark.parametrize('bad', [None, b'', b'\x00', b'\x02' + b'\0' * 104, 'not bytes'])
def test_unknown_or_missing_summary_falls_back(tmp_path, bad):
    repo = fixture_repo(tmp_path)
    with repo.storage.connection:
        repo.storage.connection.execute('UPDATE files SET name_summary=? WHERE path=?', (bad, 'b.py'))
    assert c._NameFilter(repo).may_contain(Path('b.py'), (b'missing',)) is True


def test_unicode_names_budget_and_false_positives_fall_back(tmp_path, monkeypatch):
    repo = fixture_repo(tmp_path)
    assert c._NameFilter(repo).may_contain(Path('b.py'), ('名字'.encode(),))
    monkeypatch.setattr(c, 'NAME_SUMMARY_MAX_QUERY_BYTES', 0)
    assert c._NameFilter(repo).may_contain(Path('b.py'), (b'missing',))
    monkeypatch.undo()
    row = repo.storage.connection.execute("SELECT name_summary FROM files WHERE path='b.py'").fetchone()[0]
    with repo.storage.connection:
        repo.storage.connection.execute("UPDATE files SET name_summary=? WHERE path='b.py'", (row[:41] + b'\xff' * (len(row)-41),))
    assert repo.refs('target').items == ()


def test_ascii_names_have_no_false_negatives(tmp_path):
    path = tmp_path / 'names.py'
    names = [f'word_{i}'.encode() for i in range(2000)] + [b'x', b'_', b'CamelCase', b'NAME_9']
    source = b' \n'.join(names) + ' 名字alpha targeté '.encode()
    path.write_bytes(source)
    summary = c._file_name_summary(source, path.stat())
    repo = fixture_repo(tmp_path)
    for needle in names + [b'alpha', b'target']:
        name_filter = c._NameFilter(repo)
        name_filter.rows = {'names.py': summary}
        assert name_filter.may_contain(Path('names.py'), (needle,))


def test_large_files_marked_once_and_queries_remain_live(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'NAME_SUMMARY_MAX_SOURCE_BYTES', 8)
    repo = fixture_repo(tmp_path)
    assert all(row[0] == b'\x00' for row in repo.storage.connection.execute('SELECT name_summary FROM files'))
    with mock.patch.object(c, '_file_name_summary', side_effect=AssertionError('large file retried')):
        repo.refresh()
    (tmp_path / 'b.py').write_text('def caller():\n    return target()\n')
    assert len(repo.refs('target').items) == 1


def test_early_hit_does_not_load_summary_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'NAME_SUMMARY_SCAN_PREFIX', 32)
    (tmp_path / 'a.py').write_text('def target():\n    return 1\n' + 'target()\n' * 30)
    repo = c.Repository(tmp_path, create_index=True).refresh()
    statements = []
    repo.storage.connection.set_trace_callback(statements.append)
    try:
        assert len(repo.refs('target', limit=1).items) == 1
    finally:
        repo.storage.connection.set_trace_callback(None)
    assert not any('SELECT path, name_summary' in statement for statement in statements)


def test_windows_and_wide_file_identities_use_normal_scanning(tmp_path, monkeypatch):
    from types import SimpleNamespace
    repo = fixture_repo(tmp_path)
    stat = (tmp_path / 'b.py').stat()
    wide = SimpleNamespace(st_dev=stat.st_dev, st_ino=1 << 128, st_size=stat.st_size,
                           st_mtime_ns=stat.st_mtime_ns, st_ctime_ns=stat.st_ctime_ns)
    assert c._file_name_summary(b'word', wide) == b'\x00'
    monkeypatch.setattr(c.sys, 'platform', 'win32')
    assert c._file_name_summary(b'word', stat) == b'\x00'
    assert c._NameFilter(repo).may_contain(Path('b.py'), (b'missing',))


def test_recent_file_summary_deferred_then_filled_without_ast_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'NAME_SUMMARY_MIN_AGE_NS', 1_000_000_000)
    repo = fixture_repo(tmp_path)
    assert repo.storage.connection.execute('SELECT count(*) FROM files WHERE name_summary IS NULL').fetchone()[0] == 2
    now = c.time_ns()
    monkeypatch.setattr(c, 'time_ns', lambda: now + 2_000_000_000)
    with mock.patch.object(c, '_parse_file', side_effect=AssertionError('AST rebuilt')):
        repo.refresh()
    assert repo.storage.connection.execute('SELECT count(*) FROM files WHERE name_summary IS NULL').fetchone()[0] == 0


def test_old_cli_explains_how_to_enable_query_speedup(tmp_path, capsys):
    repo = fixture_repo(tmp_path)
    legacy_files_table(repo)
    assert c.main(['refs', 'target', '--root', str(tmp_path), '--json']) == 0
    result = capsys.readouterr()
    assert 'query summaries are not ready' in result.err
    assert 'query summaries' not in result.out
    assert len(repo.storage.connection.execute('PRAGMA table_info(files)').fetchall()) == 4


def test_summary_progress_overwrites_line_and_is_silent_when_captured():
    import io
    stream = io.StringIO()
    progress = c._CliProgress(stream)
    progress.interactive = True
    progress('summary', done=0, total=3975)
    progress('summary', done=3975, total=3975)
    progress('start', done=0, total=0)
    progress('finish')
    assert stream.getvalue() == 'query summaries 0/3975 files (0%)\rquery summaries 3975/3975 files (100%)\n'
    stream.seek(0)
    stream.truncate()
    progress.interactive = False
    progress('summary', done=0, total=3975)
    progress('summary', done=3975, total=3975)
    assert stream.getvalue() == ''
