"""Adversarial acceptance cases for the language-support change."""
import pickle
from pathlib import Path
from unittest import mock

import pytest

import code_symbol_index as c


def test_definition_preference_does_not_replace_an_exact_variable_with_a_prefix(tmp_path):
    (tmp_path / 'app.py').write_text('Tool = 1\ndef Toolbox(): return 1\nprint(Tool)\n')
    repo = c.Repository(tmp_path, create_index=True).refresh()
    with mock.patch.object(c, '_defined_symbol_ids', side_effect=AssertionError('unrelated candidates parsed')):
        assert repo.best_symbol('Tool').name == 'Tool'
        assert repo.refs('Tool').items[0].name == 'Tool'


@pytest.mark.parametrize('decorator', ['', '    @decorate(target())\n'])
def test_default_argument_callers_and_callees_agree_on_outer_owner(tmp_path, decorator):
    (tmp_path / 'app.py').write_text(
        'def target(): return 1\ndef decorate(value): return lambda f: f\n'
        'def outer():\n' + decorator +
        '    def inner(value=target()): return value\n    return inner()\n'
    )
    repo = c.Repository(tmp_path, create_index=True).refresh()
    assert [n.symbol.name for n in repo.callers('target', depth=1).roots] == ['outer']
    assert 'target' in {n.symbol.name for n in repo.callees('outer', depth=1).roots}
    assert 'target' not in {n.symbol.name for n in repo.callees('inner', depth=1).roots}


def test_module_default_argument_has_no_function_caller(tmp_path):
    (tmp_path / 'app.py').write_text('def target(): return 1\ndef f(value=target()): return value\n')
    repo = c.Repository(tmp_path, create_index=True).refresh()
    assert not repo.callers('target', depth=1).roots


def test_reused_repository_reads_header_language_again_after_conversion(tmp_path):
    (tmp_path / 'app.h').write_text('int target() { return 1; }\nint caller() { return target(); }\n')
    repo = c.Repository(tmp_path, create_index=True).refresh()
    repo.inspect('caller')
    assert repo._language_cache is None
    other = c.Repository(tmp_path).refresh(header_language='cpp')
    with mock.patch.object(c, '_parse_file', wraps=c._parse_file) as parse:
        assert [n.symbol.name for n in repo.callees('caller', depth=1).roots] == ['target']
    assert all(call.kwargs.get('language') == 'cpp' for call in parse.call_args_list)
    repo.update(['app.h'])
    assert other.storage.file_languages(['app.h']) == {'app.h': 'cpp'}
    assert repo._stored_language(Path('app.h')) == 'cpp'


@pytest.mark.parametrize(('source', 'name', 'expected'), [
    ('namespace demo { int f(); }\nint demo::f() { return 1; }\n', 'f', 'function'),
    ('struct S { S(); };\nS::S() {}\n', 'S', 'constructor'),
])
def test_cpp_qualified_definition_agrees_with_declaration_kind(tmp_path, source, name, expected):
    (tmp_path / 'app.cpp').write_text(source)
    repo = c.Repository(tmp_path, create_index=True).refresh()
    matches = [s for s in repo.search_symbols(name, exact_only=True) if s.kind in c.FUNCTION_KINDS]
    assert len(matches) == 2
    assert {s.kind for s in matches} == {expected}


def test_cpp_revision_reextracts_old_namespace_classification(tmp_path):
    (tmp_path / 'app.cpp').write_text('namespace demo { int f(); }\nint demo::f() { return 1; }\n')
    repo = c.Repository(tmp_path, create_index=True).refresh()
    with repo.storage.connection:
        repo.storage.connection.execute("UPDATE files SET extractor_revision='cpp:1'")
    with mock.patch.object(c, '_parse_file', wraps=c._parse_file) as parse:
        repo.refresh()
    assert parse.call_count == 1
    with mock.patch.object(c, '_parse_file', side_effect=AssertionError('upgrade repeated')):
        repo.refresh()


def test_index_writer_does_not_build_or_return_query_only_bodies(tmp_path):
    (tmp_path / 'app.py').write_text('def target(): return 1\n')
    repo = c.Repository(tmp_path, create_index=True)
    with mock.patch.object(c, '_callable_bodies', side_effect=AssertionError('query metadata on write')):
        results = repo._parse_files([Path('app.py')], include_references=False)
    assert len(results) == 1 and results[0].bodies == ()
    assert results[0].symbols[0].name == 'target'


def test_many_nested_bodies_do_not_leak_calls_to_outer(tmp_path):
    source = 'def target(): return 1\ndef other(): return 2\ndef outer():\n'
    source += ''.join(f'    def inner_{i}(): return target()\n' for i in range(80))
    source += '    return other()\n'
    (tmp_path / 'app.py').write_text(source)
    repo = c.Repository(tmp_path, create_index=True).refresh()
    assert [n.symbol.name for n in repo.callees('outer', depth=1).roots] == ['other']
    assert len(repo.callers('target', depth=1, limit=100).roots) == 80


@pytest.mark.parametrize('operation', ['refresh', 'update'])
def test_stat_permission_error_preserves_existing_rows_and_reports_failure(tmp_path, operation):
    file = tmp_path / 'app.h'
    file.write_text('int target() { return 1; }\n')
    repo = c.Repository(tmp_path, create_index=True).refresh()
    before = [tuple(row) for row in repo.storage.connection.execute('SELECT * FROM symbols')]
    stat = Path.stat
    events = []
    def denied(path, *args, **kwargs):
        if path == file:
            raise PermissionError('fixture')
        return stat(path, *args, **kwargs)
    def progress(event, **kwargs):
        events.append((event, kwargs))
    with mock.patch.object(Path, 'stat', denied):
        if operation == 'refresh':
            repo.refresh(header_language='cpp', progress=progress)
            assert repo.last_header_language == ('cpp', 0, 1)
        else:
            repo.update(['app.h'], progress=progress)
            assert repo.last_update_failed == ('app.h',)
    assert [tuple(row) for row in repo.storage.connection.execute('SELECT * FROM symbols')] == before
    assert events[-1][1]['done'] == 0 and events[-1][1]['total'] == 1


def test_filtered_refresh_does_not_acknowledge_a_new_checkout(tmp_path):
    (tmp_path / 'app.py').write_text('def target(): pass\n')
    with mock.patch.object(c, '_git_state', return_value='old'):
        repo = c.Repository(tmp_path, create_index=True).refresh()
    baseline = repo.storage.meta_value('git_baseline')
    with mock.patch.object(c, '_git_state', return_value='new'):
        c.Repository(tmp_path, languages=['python']).refresh()
        assert repo.storage.meta_value('git_baseline') == baseline
        assert c._git_freshness(tmp_path, baseline) == 'changed'


def test_unreadable_directory_aborts_refresh_before_deleting_rows(tmp_path):
    (tmp_path / 'app.py').write_text('def target(): pass\n')
    repo = c.Repository(tmp_path, create_index=True).refresh()
    def unreadable_walk(root, *, onerror):
        onerror(PermissionError('fixture directory'))
        return iter(())
    with mock.patch.object(c.os, 'walk', unreadable_walk), pytest.raises(PermissionError):
        repo.refresh()
    assert repo.search_symbols('target', exact_only=True)


@pytest.mark.parametrize(('extension', 'source', 'name', 'declaration'), [
    ('py', 'first, second = (\n 1,\n 2\n)\n', 'second', 'first, second = (\n 1,\n 2\n)'),
    ('ts', 'const {first, second: renamed} = {\n first: 1, second: 2\n};\n', 'renamed',
     '{first, second: renamed} = {\n first: 1, second: 2\n}'),
    ('swift', 'struct Box {\n let first = 1, second = 2\n}\n', 'second', 'let first = 1, second = 2'),
    ('kt', 'val (first, second) = Pair(1,2)\n', 'second', 'val (first, second) = Pair(1,2)'),
])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_multi_binding_preview_uses_full_declaration_in_both_paths(tmp_path, monkeypatch, extension, source, name, declaration, newline):
    source = source.replace('\n', newline)
    declaration = declaration.replace('\n', newline)
    path = tmp_path / f'app.{extension}'
    path.write_bytes(source.encode())
    repo = c.Repository(tmp_path, create_index=True).refresh()
    symbol = repo.best_symbol(name)
    native = c._definition_range(repo, symbol)
    monkeypatch.setattr(c, 'NATIVE_DEFINITION_MAX_SYMBOLS', 0)
    assert c._definition_range(repo, symbol) == native
    assert native is not None
    assert source.encode()[native.start_byte:native.end_byte].decode() == declaration


def test_full_status_check_respects_cpp_header_configuration(tmp_path):
    (tmp_path / 'app.h').write_text('class Box {};\n')
    c.Repository(tmp_path, create_index=True).refresh(header_language='cpp')
    result = c.status(tmp_path, language='cpp', check=True)
    assert result.status == 'ready'
    assert result.pending_changes == 0


def test_filtered_refresh_preserves_header_after_failed_conversion(tmp_path):
    (tmp_path / 'app.h').write_text('int target() { return 1; }\n')
    repo = c.Repository(tmp_path, create_index=True).refresh()
    with mock.patch.object(c, '_parse_file', return_value=None):
        repo.refresh(header_language='cpp')
    c.Repository(tmp_path, languages=['c']).refresh()
    assert repo.storage.file_languages(['app.h']) == {'app.h': 'c'}
    assert repo.search_symbols('target', exact_only=True)
    repo.refresh()
    assert repo.storage.file_languages(['app.h']) == {'app.h': 'cpp'}


def test_decorated_python_class_fields_and_upgrade(tmp_path):
    (tmp_path / 'app.py').write_text(
        '@decorate\nclass Box:\n    a, b = 1, 2\n'
        '    @decorate\n    class Inner:\n        value: int = 1\n'
        '    @decorate\n    def method(self):\n        local = 1\n'
    )
    repo = c.Repository(tmp_path, create_index=True).refresh()
    fields = {(s.name, s.container) for s in repo.search_symbols('', kind='field')}
    assert fields == {('a', 'Box'), ('b', 'Box'), ('value', 'Box.Inner')}
    assert not repo.search_symbols('local', exact_only=True)
    with repo.storage.connection:
        repo.storage.connection.execute("UPDATE files SET extractor_revision='python:1'")
    with mock.patch.object(c, '_parse_file', wraps=c._parse_file) as parse:
        repo.refresh()
    assert parse.call_count == 1
    with mock.patch.object(c, '_parse_file', side_effect=AssertionError('upgrade repeated')):
        repo.refresh()


@pytest.mark.parametrize(('extension', 'source'), [
    ('c', 'int target(int);\nint target(int x) { return x; }\n'),
    ('cpp', 'namespace n { struct Box { Box(); ~Box(); int run(); }; }\n'
     'n::Box::Box() {}\nn::Box::~Box() {}\nint n::Box::run() { return 1; }\n'),
    ('cpp', 'template<class T> T target(T);\ntemplate<class T> T target(T x) { return x; }\n'),
])
def test_candidate_body_lookup_matches_full_extraction_without_reextracting(tmp_path, extension, source):
    path = Path(f'app.{extension}')
    (tmp_path / path).write_text(source)
    repo = c.Repository(tmp_path, create_index=True).refresh()
    candidates = repo.search_symbols('', kind=c.FUNCTION_KINDS)
    parsed = c._parse_file(tmp_path, path, None)
    expected = {s.id for s in candidates if c._owning_body(parsed.bodies, s) is not None}
    assert expected and len(expected) < len(candidates)
    with mock.patch.object(c, '_parse_file', side_effect=AssertionError('full re-extraction')):
        assert c._defined_symbol_ids(repo, candidates) == expected


@pytest.mark.parametrize('include_references', [False, True])
def test_worker_result_serialization_preserves_all_metadata(tmp_path, include_references):
    path = Path('app.cpp')
    (tmp_path / path).write_text('namespace demo { struct Box { int run() { return target(); } }; }\n')
    indexed = c._parse_file(tmp_path, path, include_references=include_references,
                            collect_bodies=include_references)
    assert indexed.symbols and indexed.revision
    assert pickle.loads(pickle.dumps(indexed)) == indexed


def test_batched_pool_preserves_every_result_and_file_progress(tmp_path, monkeypatch):
    paths = [Path(f'unit_{i}.c') for i in range(70)]
    for i, path in enumerate(paths):
        (tmp_path / path).write_text(f'int target_{i}(void) {{ return {i}; }}\n')
    repo = c.Repository(tmp_path, create_index=True)
    monkeypatch.setattr(c, 'MAX_WORKERS', 2)
    events = []
    pooled = repo._parse_files(paths, include_references=False,
                              progress=lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(c, 'MAX_WORKERS', 1)
    serial = repo._parse_files(paths, include_references=False)
    assert sorted(pooled, key=lambda f: f.path) == sorted(serial, key=lambda f: f.path)
    assert [kw['done'] for event, kw in events if event == 'file'] == list(range(1, 71))
    assert {kw['path'] for event, kw in events if event == 'file'} == {p.as_posix() for p in paths}


def test_worker_batch_keeps_neighbours_when_one_parse_raises(tmp_path, monkeypatch):
    def parse(root, path, *args, **kwargs):
        if path == Path('bad.c'):
            raise RuntimeError('fixture parse failure')
        return path
    monkeypatch.setattr(c, '_parse_file', parse)
    paths = [Path('first.c'), Path('bad.c'), Path('last.c')]
    assert c._parse_file_batch(tmp_path, paths, None, False, None) == [paths[0], None, paths[2]]

