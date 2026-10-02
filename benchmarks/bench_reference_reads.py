"""Compare reference prefilter reads on disposable, many-file source trees."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from bench_restart import run, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="53217ec")
    parser.add_argument("--samples", type=int, default=9)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2 or args.files < 2:
        parser.error("samples and files must be >= 2")
    os.environ["CODE_SYMBOL_INDEX_NO_UPDATE_CHECK"] = "1"
    project = Path(__file__).resolve().parents[1]
    report = {"baseline": args.baseline, "python": sys.version, "platform": platform.platform(),
              "files": args.files, "samples": args.samples,
              "cache": "warm filesystem/bytecode; independent CLI processes", "cases": {}}
    baseline = subprocess.check_output(["git", "show", f"{args.baseline}:code_symbol_index.py"], cwd=project)
    with tempfile.TemporaryDirectory(prefix="symbol-reference-reads-") as folder:
        temp = Path(folder)
        scripts = {}
        for key, source in (("before", baseline), ("after", (project / "code_symbol_index.py").read_bytes())):
            directory = temp / key
            directory.mkdir()
            scripts[key] = directory / "code_symbol_index.py"
            scripts[key].write_bytes(source)
            run(scripts[key], ["version"])
        for corpus in ("dense", "sparse", "large"):
            root = temp / corpus
            root.mkdir()
            (root / "a.py").write_text("def target(): return 1\n")
            count = args.files if corpus != "large" else 4
            for i in range(count):
                name = "target" if corpus != "sparse" or i % 100 == 0 else "unrelated"
                padding = "# " + "padding " * (140000 if corpus == "large" else 8) + "\n"
                (root / f"f{i:05}.py").write_text(padding + f"def caller_{i}():\n    return {name}()\n")
            # Both indexes must get the same production name summaries. Files
            # newer than one second otherwise defer them depending on run order.
            time.sleep(1.1)
            dbs = {key: temp / f"{corpus}-{key}.sqlite" for key in scripts}
            for key, script in scripts.items():
                run(script, ["index", "--root", str(root), "--db", str(dbs[key])])
            assert snapshot(dbs["before"]) == snapshot(dbs["after"])
            summaries = {}
            for key in scripts:
                with closing(sqlite3.connect(dbs[key])) as connection:
                    summaries[key] = connection.execute(
                        "SELECT path, name_summary FROM files ORDER BY path",
                    ).fetchall()
            assert summaries["before"] == summaries["after"], (corpus, "different name summaries")
            cases = {"refs_all": ["refs", "target", "--limit", str(count + 1), "--json"],
                     "refs_early": ["refs", "target", "--limit", "1", "--json"],
                     "callers": ["callers", "target", "--depth", "2", "--limit", "20", "--json"]}
            for label, command in cases.items():
                # Warm parser libraries and storage equally before timed samples.
                for key, script in scripts.items():
                    run(script, [*command, "--root", str(root), "--db", str(dbs[key])])
                times = {key: [] for key in scripts}
                for sample in range(args.samples):
                    outputs = {}
                    for key in list(scripts)[::1 if sample % 2 else -1]:
                        elapsed, outputs[key] = run(scripts[key], [
                            *command, "--root", str(root), "--db", str(dbs[key]),
                        ])
                        times[key].append(elapsed * 1000)
                    assert outputs["before"] == outputs["after"], (corpus, label)
                result = {key: {"p50_ms": statistics.median(values), "samples_ms": values}
                          for key, values in times.items()}
                result["after_before"] = result["after"]["p50_ms"] / result["before"]["p50_ms"]
                report["cases"][f"{corpus}.{label}"] = result
                print(f"{corpus}.{label}: {result['before']['p50_ms']:.2f} -> "
                      f"{result['after']['p50_ms']:.2f} ms ({result['after_before']:.3f}x)", flush=True)
                args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
