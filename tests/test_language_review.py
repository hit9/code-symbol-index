"""Adversarial acceptance cases for the language-support change."""
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
