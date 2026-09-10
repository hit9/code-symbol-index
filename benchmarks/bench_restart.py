"""Compare independent CLI processes against master without touching user indexes.

Run: python benchmarks/bench_restart.py --samples 7 --output /tmp/restart.json
All source fixtures and databases live in a temporary directory. No dependencies
beyond the project's runtime dependencies are needed. Times include CLI startup.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(script: Path, args: list[str]) -> tuple[float, str]:
    started = time.perf_counter()
    result = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, timeout=120)
    elapsed = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(f"{args}: {result.stderr}")
    return elapsed, result.stdout


def snapshot(db: Path) -> dict:
    with sqlite3.connect(str(db)) as connection:
        return {
            table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in ("files", "symbols", "refs")
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--baseline", default="55e843a")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    project = Path(__file__).resolve().parents[1]
    baseline = subprocess.check_output(["git", "show", f"{args.baseline}:code_symbol_index.py"], cwd=project)
    report = {"python": sys.version, "platform": platform.platform(), "baseline": args.baseline,
              "samples": args.samples, "cache": "warm filesystem; independent CLI processes", "cases": {}}
    with tempfile.TemporaryDirectory(prefix="symbol-restart-bench-") as directory:
        temp = Path(directory)
        scripts = {"before": temp / "before.py", "after": project / "code_symbol_index.py"}
        scripts["before"].write_bytes(baseline)
        for size, count, functions in (("small", 16, 8), ("large", 4, 1000)):
            root = temp / size
            root.mkdir()
            for file_index in range(count):
                text = "def target():\n    return 1\n\n" if file_index == 0 else ""
                text += "".join(
                    f"def worker_{file_index}_{i}(value):\n    # {'padding ' * 12}\n"
                    f"    result = value + {i}\n    return target() + result\n\n"
                    for i in range(functions)
                )
                (root / f"file_{file_index:02}.py").write_text(text)
            databases = {key: temp / f"{size}-{key}.sqlite" for key in scripts}
            paths = ["file_00.py", "file_01.py"]
            cases = {
                "index_new": ["index"], "index_no_change": ["index"],
                "update_one": ["update", paths[0]], "update_two": ["update", *paths],
                "search": ["search", "worker_0", "--limit", "20"],
                "inspect": ["inspect", "target"], "refs": ["refs", "target"],
                "callers": ["callers", "target", "--depth", "1"],
                "callees": ["callees", "worker_0_0", "--depth", "1"],
                "impls": ["impls", "target"], "outline": ["outline", paths[0]],
                "status": ["status"], "status_check": ["status", "--check"],
                "missing": ["search", "no_such_symbol"], "version": ["version"],
            }
            for name, command in cases.items():
                times = {key: [] for key in scripts}
                outputs = {}
                for sample in range(args.samples):
                    # Alternate order to reduce systematic filesystem/cache bias.
                    order = list(scripts) if sample % 2 == 0 else list(reversed(scripts))
                    for key in order:
                        db = databases[key]
                        if name == "index_new":
                            for suffix in ("", "-wal", "-shm"):
                                Path(str(db) + suffix).unlink(missing_ok=True)
                        flags = [] if name == "version" else ["--root", str(root), "--db", str(db)]
                        elapsed, output = run(scripts[key], command + flags)
                        times[key].append(elapsed)
                        outputs[key] = output
                if name in ("index_new", "index_no_change", "update_one", "update_two"):
                    assert snapshot(databases["before"]) == snapshot(databases["after"]), (size, name)
                elif name not in ("status", "status_check"):
                    assert outputs["before"] == outputs["after"], (size, name, "output changed")
                measurements = {key: {"p50_ms": statistics.median(values) * 1000,
                                      "max_ms": max(values) * 1000, "samples_ms": [v * 1000 for v in values]}
                                for key, values in times.items()}
                measurements["after_before"] = statistics.median(times["after"]) / statistics.median(times["before"])
                report["cases"][f"{size}.{name}"] = measurements
                print(f"{size}.{name}: {measurements['after_before']:.3f}x after/before", flush=True)
                args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
