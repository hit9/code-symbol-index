"""Check benchmark preconditions, independently of the measured implementation."""
import importlib
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
