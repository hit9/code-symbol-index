"""Independent-process benchmark for the C/C++ and other-language extraction step.

Run: python benchmarks/bench_language_support.py --samples 7 --output /tmp/language-support.json

Compares the baseline revision against the working copy with alternating order,
the same Python environment and the same disposable fixtures, and never touches a
user index. Unlike benchmarks/bench_restart.py this step intentionally changes
what is indexed for some languages, so persisted row counts are reported rather
than asserted equal, and a one-time rule-upgrade case is measured separately.
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

from bench_restart import run

C_TEMPLATE = """\
#include "widget.h"

struct Point{i} {{ int x; int y; }};
enum Mode{i} {{ MODE_FAST, MODE_SLOW }};
typedef int (*Handler{i})(int);

#define LIMIT{i} {i}
#define SCALE{i}(value) ((value) * LIMIT{i})

static int helper_{i}(int value) {{ return value + LIMIT{i}; }}
int declared_{i}(int);
int reference_{i};

int caller_{i}(int value) {{
    struct Point{i} point;
    point.x = helper_{i}(value);
    return declared_{i}(point.x);
}}
"""

CPP_TEMPLATE = """\
#include "widget.hpp"

namespace demo{i} {{

class Widget{i} : public Base{i} {{
public:
    Widget{i}();
    ~Widget{i}();
    int run(int value) const;
    int helper(int value) {{ return value + limit_; }}
private:
    int limit_{i} = {i};
    int (*callback_)(int);
}};

int Widget{i}::run(int value) const {{ return helper(value); }}
int free_function_{i}(int value) {{ return value; }}

}}  // namespace demo{i}
"""

GO_TEMPLATE = """\
package fixture{i}

type Point{i} struct {{
	X int
	Y int
}}

type Handler{i} func(int) int

const Limit{i} = {i}

func helper{i}(value int) int {{ return value + Limit{i} }}

func Caller{i}(value int) int {{
	point := Point{i}{{X: value}}
	return helper{i}(point.X)
}}
"""

SLIM_C_TEMPLATE = """\
#include "slim.h"

enum SlimMode{i} {{ SLIM_A, SLIM_B }};
int slim_value_{i};
int slim_function_{i}(int value) {{ return value + {i}; }}
int slim_caller_{i}(int value) {{ return slim_function_{i}(value); }}
"""

LARGE_C_TEMPLATE = "#include \"large.h\"\n\n" + "".join(
    f"int large_{index}(int value) {{ return value + {index}; }}\n"
    f"int large_caller_{index}(int value) {{ return large_{index}(value); }}\n"
    for index in range(1000)
)


def write_corpus(root: Path, corpus: str) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    if corpus == "c_small":
        for index in range(64):
            name = f"unit_{index:03}.c"
            (root / name).write_text(C_TEMPLATE.format(i=index))
            names.append(name)
        (root / "widget.h").write_text("int header_declared(int);\nstruct HeaderPoint;\n")
    elif corpus == "cpp_small":
        for index in range(64):
            name = f"unit_{index:03}.cpp"
            (root / name).write_text(CPP_TEMPLATE.format(i=index))
            names.append(name)
        (root / "widget.hpp").write_text("struct Base0 {};\n")
    elif corpus == "control_small":
        for index in range(64):
            name = f"unit_{index:03}.go"
            (root / name).write_text(GO_TEMPLATE.format(i=index))
            names.append(name)
    elif corpus in ("c_1k", "c_10k"):
        count = 1000 if corpus == "c_1k" else 10000
        for index in range(count):
            name = f"slim_{index:05}.c"
            (root / name).write_text(SLIM_C_TEMPLATE.format(i=index))
            names.append(name)
        (root / "slim.h").write_text("int slim_declared(int);\n")
    elif corpus == "c_large":
        for index in range(4):
            name = f"large_{index}.c"
            (root / name).write_text(LARGE_C_TEMPLATE)
            names.append(name)
        (root / "large.h").write_text("int large_declared(int);\n")
    else:
        raise ValueError(corpus)
    return names


def row_counts(db: Path) -> dict[str, int]:
    with sqlite3.connect(str(db)) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("files", "symbols", "refs")
        }
        try:
            counts["revision_rows"] = connection.execute(
                "SELECT COUNT(*) FROM files WHERE extractor_revision IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            counts["revision_rows"] = 0
    return counts


def measure(
    scripts: dict[str, Path],
    command: list[str],
    root: Path,
    db: Path,
    samples: int,
    *,
    rebuild: bool,
    with_flags: bool = True,
) -> dict:
    """Time ``command`` in both implementations, alternating the order.

    ``rebuild`` starts index commands with no database, but prepares a populated
    database for update commands. Otherwise the index is built once and the
    case measures steady-state reads (including an unchanged refresh).
    """
    times: dict[str, list[float]] = {key: [] for key in scripts}
    outputs: dict[str, str] = {}
    if not rebuild:
        for key, script in scripts.items():
            prepare(script, root, Path(str(db) + f"-{key}"))
    for sample in range(samples):
        order = list(scripts) if sample % 2 == 0 else list(reversed(list(scripts)))
        for key in order:
            target = Path(str(db) + f"-{key}")
            if rebuild:
                if command == ["index"]:
                    for suffix in ("", "-wal", "-shm"):
                        Path(str(target) + suffix).unlink(missing_ok=True)
                else:
                    prepare(scripts[key], root, target)
            flags = ["--root", str(root), "--db", str(target)] if with_flags else []
            elapsed, output = run(scripts[key], [*command, *flags])
            times[key].append(elapsed)
            outputs[key] = output
    return {
        key: {
            "p50_ms": statistics.median(values) * 1000,
            "min_ms": min(values) * 1000,
            "max_ms": max(values) * 1000,
            "samples_ms": [value * 1000 for value in values],
        }
        for key, values in times.items()
    } | {
        "after_before": statistics.median(times["after"]) / statistics.median(times["before"]),
        "output_equal": outputs["before"] == outputs["after"],
    }


def prepare(script: Path, root: Path, db: Path) -> None:
    """Build an index with ``script``, leaving a state for the measured run."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    run(script, ["index", "--root", str(root), "--db", str(db)])


def corpus_queries(corpus: str, names: list[str]) -> dict[str, list[str]]:
    """Query commands whose targets exist in both implementations."""
    last = len(names) - 1
    if corpus == "cpp_small":
        return {
            "search": ["search", "run", "--limit", "20"],
            "search_late": ["search", f"free_function_{last}", "--limit", "1"],
            "inspect": ["inspect", "run", "--path", "unit_000.cpp"],
            "refs": ["refs", "Widget0"],
            "callers": ["callers", "run", "--depth", "1", "--path", "unit_000.cpp"],
            "callees": ["callees", "run", "--depth", "1", "--path", "unit_000.cpp"],
            "impls": ["impls", "Base0"],
        }
    if corpus == "control_small":
        return {
            "search": ["search", "Caller0", "--limit", "20"],
            "search_late": ["search", f"Caller{last}", "--limit", "1"],
            "inspect": ["inspect", "Caller0"],
            "refs": ["refs", "Limit0"],
            "callers": ["callers", "helper0", "--depth", "1"],
            "callees": ["callees", "Caller0", "--depth", "1"],
            "impls": ["impls", "Point0"],
        }
    if corpus in ("c_1k", "c_10k"):
        return {
            "search": ["search", "slim_caller_0", "--limit", "20"],
            "search_late": ["search", f"slim_caller_{last}", "--limit", "1"],
            "inspect": ["inspect", "slim_caller_0"],
            "refs": ["refs", "slim_value_0"],
            "callers": ["callers", "slim_function_0", "--depth", "1"],
            "callees": ["callees", "slim_caller_0", "--depth", "1"],
            "impls": ["impls", "SlimMode0"],
        }
    if corpus == "c_large":
        return {
            "search": ["search", "large_0", "--limit", "20"],
            "search_late": ["search", "large_999", "--limit", "1"],
            "inspect": ["inspect", "large_0", "--path", names[0]],
            "refs": ["refs", "large_0"],
            "callers": ["callers", "large_0", "--depth", "1", "--path", names[0]],
            "callees": ["callees", "large_caller_0", "--depth", "1", "--path", names[0]],
        }
    return {
        "search": ["search", "caller_0", "--limit", "20"],
        "search_late": ["search", f"caller_{last}", "--limit", "1"],
        "inspect": ["inspect", "caller_0"],
        "refs": ["refs", "reference_0"],
        "callers": ["callers", "declared_0", "--depth", "1"],
        "callees": ["callees", "caller_0", "--depth", "1"],
        "impls": ["impls", "Mode0"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--baseline", default="4d942db")
    parser.add_argument(
        "--corpora",
        default="c_small,cpp_small,control_small,c_large,c_1k,c_10k",
        help="comma separated corpus names",
    )
    parser.add_argument("--cases", default="all", help="comma separated case names or 'all'")
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")

    project = Path(__file__).resolve().parents[1]
    baseline_source = subprocess.check_output(
        ["git", "show", f"{args.baseline}:code_symbol_index.py"], cwd=project
    )
    report: dict = {
        "python": sys.version,
        "platform": platform.platform(),
        "baseline": args.baseline,
        "samples": args.samples,
        "cache": "warm filesystem and bytecode; independent import/main processes",
        "corpora": {},
    }

    all_cases = (
        "index_new",
        "index_no_change",
        "upgrade",
        "update_one",
        "update_two",
        "search",
        "search_late",
        "inspect",
        "refs",
        "callers",
        "callees",
        "impls",
        "outline",
        "status",
        "version",
    )
    cases = all_cases if args.cases == "all" else tuple(args.cases.split(","))

    with tempfile.TemporaryDirectory(prefix="symbol-language-bench-", dir=args.temp_parent) as directory:
        temp = Path(directory).resolve()
        (temp / "baseline").mkdir()
        (temp / "current").mkdir()
        scripts = {
            "before": temp / "baseline" / "code_symbol_index.py",
            "after": temp / "current" / "code_symbol_index.py",
        }
        scripts["before"].write_bytes(baseline_source)
        scripts["after"].write_bytes((project / "code_symbol_index.py").read_bytes())
        for script in scripts.values():
            run(script, ["version"])

        for corpus in args.corpora.split(","):
            root = temp / corpus
            names = write_corpus(root, corpus)
            db = temp / f"{corpus}.sqlite"
            entry = {"files": len(names), "cases": {}}
            if corpus in ("c_1k", "c_10k"):
                entry["files"] = len(names) + 1

            # New index, unchanged index and the one-time rule upgrade share the
            # same pair of databases per implementation.
            if "index_new" in cases:
                for key in scripts:
                    for suffix in ("", "-wal", "-shm"):
                        Path(str(db) + f"-{key}{suffix}").unlink(missing_ok=True)
            index_cases = [name for name in ("index_new", "index_no_change") if name in cases]
            if "upgrade" in cases:
                # A database written by the baseline, then upgraded by each side:
                # the baseline column becomes a no-change run, the current one
                # has to re-extract the files whose rules changed.
                upgrade_times: dict[str, list[float]] = {key: [] for key in scripts}
                upgrade_deltas: dict[str, dict[str, int]] = {}
                for sample in range(args.samples):
                    order = list(scripts) if sample % 2 == 0 else list(reversed(list(scripts)))
                    for key in order:
                        upgrade_db = temp / f"{corpus}-upgrade-{key}.sqlite"
                        for suffix in ("", "-wal", "-shm"):
                            Path(str(upgrade_db) + suffix).unlink(missing_ok=True)
                        run(scripts["before"], ["index", "--root", str(root), "--db", str(upgrade_db)])
                        before_rows = row_counts(upgrade_db)
                        started = time.perf_counter()
                        run(scripts[key], ["index", "--root", str(root), "--db", str(upgrade_db)])
                        upgrade_times[key].append(time.perf_counter() - started)
                        after_rows = row_counts(upgrade_db)
                        upgrade_deltas[key] = {
                            name: after_rows[name] - before_rows[name] for name in ("symbols", "refs")
                        } | {
                            "revision_rows_after": after_rows["revision_rows"],
                            "revision_rows_before": before_rows["revision_rows"],
                        }
                entry["cases"]["upgrade"] = {
                    key: {
                        "p50_ms": statistics.median(values) * 1000,
                        "min_ms": min(values) * 1000,
                        "max_ms": max(values) * 1000,
                        "samples_ms": [value * 1000 for value in values],
                    }
                    for key, values in upgrade_times.items()
                } | {
                    "after_before": statistics.median(upgrade_times["after"])
                    / statistics.median(upgrade_times["before"]),
                    "delta_rows": upgrade_deltas,
                }
                print(f"{corpus}.upgrade: {entry['cases']['upgrade']['after_before']:.3f}x", flush=True)
                args.output.write_text(json.dumps(report, indent=2) + "\n")

            for name, rebuild in (("index_new", True), ("index_no_change", False)):
                if name not in cases:
                    continue
                entry["cases"][name] = measure(scripts, ["index"], root, db, args.samples, rebuild=rebuild)
                print(
                    f"{corpus}.{name}: {entry['cases'][name]['after_before']:.3f}x",
                    flush=True,
                )
                report["corpora"][corpus] = entry
                args.output.write_text(json.dumps(report, indent=2) + "\n")
            if index_cases:
                entry["rows"] = {
                    key: row_counts(Path(str(db) + f"-{key}")) for key in scripts
                }

            queries = corpus_queries(corpus, names)
            first = names[0]
            queries["outline"] = ["outline", first]
            queries["status"] = ["status"]
            queries["version"] = ["version"]
            for name in ("update_one", "update_two"):
                if name in cases:
                    queries[name] = ["update", first] if name == "update_one" else ["update", first, names[1]]
            for name, command in queries.items():
                if name not in cases:
                    continue
                try:
                    entry["cases"][name] = measure(
                        scripts,
                        command,
                        root,
                        db,
                        args.samples,
                        rebuild=name in ("update_one", "update_two"),
                        with_flags=name != "version",
                    )
                except RuntimeError as exc:
                    # A query target that one side cannot resolve is reported, not
                    # hidden and not fatal for the remaining cases.
                    entry["cases"][name] = {"error": str(exc)}
                report["corpora"][corpus] = entry
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                result = entry["cases"][name]
                summary = "error" if "error" in result else f"{result['after_before']:.3f}x"
                print(f"{corpus}.{name}: {summary}", flush=True)


if __name__ == "__main__":
    main()
