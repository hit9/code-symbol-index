"""Compare writes on installed public C++ headers and Python's standard library.

Sources and modules are copied to a disposable local directory. No source index
is opened or changed. Uses the same independent-process timer as the synthetic
benchmark; C++ headers get .hpp suffixes so both versions use the C++ parser.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from bench_language_support import measure, row_counts
from bench_restart import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpp-headers', type=Path, default=Path('/usr/include/c++/13/bits'))
    parser.add_argument('--python-stdlib', type=Path, default=Path(sysconfig.get_paths()['stdlib']))
    parser.add_argument('--samples', type=int, default=7)
    parser.add_argument('--baseline', default='4d942db')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error('--samples must be at least 2')
    corpora = {
        'libstdcpp': sorted(args.cpp_headers.glob('*.h'))[:36],
        'python_stdlib': sorted(path for path in args.python_stdlib.rglob('*.py')
                                if 'site-packages' not in path.parts and '__pycache__' not in path.parts),
    }
    if any(not paths for paths in corpora.values()):
        parser.error('both public source directories must contain source files')
    project = Path(__file__).resolve().parents[1]
    report = {'baseline': args.baseline, 'python': sys.version, 'samples': args.samples, 'corpora': {}}
    with tempfile.TemporaryDirectory(prefix='symbol-public-bench-') as directory:
        temp = Path(directory)
        scripts = {}
        for key in ('before', 'after'):
            script = temp / key / 'code_symbol_index.py'
            script.parent.mkdir()
            script.write_bytes(subprocess.check_output(
                ['git', 'show', f'{args.baseline}:code_symbol_index.py'], cwd=project,
            ) if key == 'before' else (project / 'code_symbol_index.py').read_bytes())
            scripts[key] = script
            run(script, ['version'])
        for name, paths in corpora.items():
            root = temp / name
            root.mkdir()
            for i, path in enumerate(paths):
                shutil.copyfile(path, root / (f'file_{i:05}' + ('.hpp' if name == 'libstdcpp' else '.py')))
            db = temp / (name + '.sqlite')
            result = {'files': len(paths), 'bytes': sum(path.stat().st_size for path in paths)}
            result['fresh'] = measure(scripts, ['index'], root, db, args.samples, rebuild=True)
            result['rows'] = {key: row_counts(Path(str(db) + '-' + key)) for key in scripts}
            result['unchanged'] = measure(scripts, ['index'], root, db, args.samples, rebuild=False)
            # An explicit update always re-extracts the requested source.
            update_path = sorted(root.iterdir())[0]
            result['update_source'] = paths[0].relative_to(
                args.cpp_headers if name == 'libstdcpp' else args.python_stdlib,
            ).as_posix()
            result['update_one'] = measure(
                scripts, ['update', update_path.name], root, db, args.samples, rebuild=True,
            )
            report['corpora'][name] = result
            args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(name, {key: round(result[key]['after_before'], 3)
                         for key in ('fresh', 'unchanged', 'update_one')}, flush=True)


if __name__ == '__main__':
    main()
