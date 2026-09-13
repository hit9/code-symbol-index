"""Check benchmark preconditions, independently of the measured implementation."""
import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('command', [['index'], ['update', 'app.c']])
def test_write_benchmark_starts_with_the_intended_database(tmp_path, monkeypatch, command):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'benchmarks'))
    bench = importlib.import_module('bench_language_support')
    measured = []

    def prepare(script, root, db):
        db.write_text('populated')

    def run(script, args):
        db = Path(args[args.index('--db') + 1])
        if command == ['index']:
            assert not any(Path(str(db) + suffix).exists() for suffix in ('', '-wal', '-shm'))
        else:
            assert db.read_text() == 'populated'
        for suffix in ('', '-wal', '-shm'):
            Path(str(db) + suffix).write_text('measured')
        measured.append(script)
        return 1.0, ''

    monkeypatch.setattr(bench, 'prepare', prepare)
    monkeypatch.setattr(bench, 'run', run)
    scripts = {'before': Path('before.py'), 'after': Path('after.py')}
    bench.measure(scripts, command, tmp_path, tmp_path / 'index.sqlite', 2, rebuild=True)
    assert measured == [scripts[key] for key in ('before', 'after', 'after', 'before')]


@pytest.mark.parametrize('case', ['index_new', 'upgrade'])
def test_report_keeps_write_only_cases(tmp_path, monkeypatch, case):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'benchmarks'))
    bench = importlib.import_module('bench_language_support')
    output = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['bench', '--corpora', 'c_small', '--cases', case,
                                    '--samples', '2', '--output', str(output)])
    monkeypatch.setattr(bench.subprocess, 'check_output', lambda *a, **kw: b'')
    monkeypatch.setattr(bench, 'write_corpus', lambda *a: ['app.c'])
    monkeypatch.setattr(bench, 'run', lambda *a: (1.0, ''))
    monkeypatch.setattr(bench, 'row_counts', lambda *a: {'files': 1, 'symbols': 1, 'refs': 0, 'revision_rows': 1})
    monkeypatch.setattr(bench, 'measure', lambda *a, **kw: {'after_before': 1.0})
    bench.main()
    corpus = json.loads(output.read_text())['corpora']['c_small']
    assert case in corpus['cases']
    if case == 'index_new':
        assert corpus['rows']['after']['files'] == 1
