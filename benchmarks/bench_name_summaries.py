"""Synthetic many-file query/write comparison; never uses real repository data."""
import argparse
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import tempfile

from bench_restart import run, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='fd7a2b1')
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--temp-parent', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error('--samples must be at least 2')
    project = Path(__file__).resolve().parents[1]
    result = {}
    with tempfile.TemporaryDirectory(prefix='symbol-summary-', dir=args.temp_parent) as directory:
        temp = Path(directory)
        root = temp / 'source'
        root.mkdir()
        (root / 'a.py').write_text('def target():\n    return 1\n' + ''.join(
            f'def branch_{i}():\n    return target()\n' for i in range(16)))
        for i in range(800):
            source = f'def filler_{i}(value):\n    return value + {i}\n' + '# padding data\n' * 50
            if i >= 780:
                source += f'def entry_{i}():\n    return branch_{i % 16}()\n'
            (root / f'f{i:04}.py').write_text(source)
        (root / 'wide.py').write_text(''.join(
            f'def wide_{i}(value):\n    result = value + {i}\n    return result\n' for i in range(2000)))
        scripts = {}
        for key in ('before', 'after'):
            folder = temp / key
            folder.mkdir()
            scripts[key] = folder / 'code_symbol_index.py'
        scripts['before'].write_bytes(subprocess.check_output(['git', 'show', args.baseline + ':code_symbol_index.py'], cwd=project))
        scripts['after'].write_bytes((project / 'code_symbol_index.py').read_bytes())
        dbs = {key: temp / (key + '.sqlite') for key in scripts}
        for script in scripts.values():
            run(script, ['version'])
        cases = {
            'index_new': ['index'], 'index_unchanged': ['index'],
            'update_one': ['update', 'wide.py'], 'refs': ['refs', 'branch_0'],
            'refs_early': ['refs', 'target', '--limit', '1'],
            'callers': ['callers', 'target'], 'inspect': ['inspect', 'target'],
        }
        for label, command in cases.items():
            times = {key: [] for key in scripts}
            for sample in range(args.samples):
                outputs = {}
                for key in list(scripts)[::1 if sample % 2 else -1]:
                    if label == 'index_new':
                        for suffix in ('', '-wal', '-shm'):
                            Path(str(dbs[key]) + suffix).unlink(missing_ok=True)
                    elapsed, outputs[key] = run(scripts[key], command + ['--root', str(root), '--db', str(dbs[key])])
                    times[key].append(elapsed * 1000)
                if label.startswith(('index', 'update')):
                    assert snapshot(dbs['before']) == snapshot(dbs['after'])
                else:
                    assert outputs['before'] == outputs['after'], label
            result[label] = {key: {'p50_ms': statistics.median(values), 'samples_ms': values} for key, values in times.items()}
            print(label, {key: round(values['p50_ms'], 2) for key, values in result[label].items()}, flush=True)
            args.output.write_text(json.dumps(result, indent=2) + '\n')
        with sqlite3.connect(dbs['after']) as connection:
            result['summary_bytes'] = connection.execute('SELECT sum(length(name_summary)) FROM files').fetchone()[0]
        args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
