# Restart from master: measured results

Baseline: `55e843a`. Measurements use independent CLI processes, alternating
before/after order, the same Python environment and source fixture, and temporary
databases on local `/tmp`. They do not measure cold filesystem caches or promise
the same absolute latency on a shared mount. No user index is refreshed.

## Step 1: byte positions and symbol-only extraction

Reproduce: `python benchmarks/bench_restart.py --samples 5 --output /tmp/step1.json`.
The script records Python/platform metadata and all samples. These are medians
of five runs, not p95 estimates.

| Fixture / command | master ms | step 1 ms |
| --- | ---: | ---: |
| 16 small files / new index | 115.71 | 111.52 |
| 16 small files / unchanged index | 88.03 | 89.33 |
| 16 small files / update one | 89.53 | 86.85 |
| 16 small files / update two | 97.33 | 98.90 |
| 4 large files / new index | 263.99 | 175.25 |
| 4 large files / unchanged index | 84.20 | 84.12 |
| 4 large files / update one | 210.19 | 119.83 |
| 4 large files / update two | 246.33 | 152.99 |
| 4 large files / inspect target | 2373.86 | 670.47 |
| 4 large files / refs target | 987.10 | 143.24 |

Large files contain 1,000 functions each. Small cases are dominated by interpreter
startup; the 1–2 ms differences above need more samples before being treated as a
regression. Large-file indexing and updates improved rather than paying for query
acceleration. No schema or stored-data change: the benchmark compares files,
symbols and refs rows after every write case, and query stdout against master.
Existing and new tests: 145 passed. This step does not resolve the full real-world
query bottleneck or implement automatic stale warnings.

## Step 2: extract only the queried reference name

The query-only extractor prunes AST subtrees without the requested bytes and
retains the full extractor's classification/ancestor rules. It does not persist
references, change the schema, or change refresh/update processing.

179 tests passed, including differential extraction across 13 languages,
pagination/kinds, live edits, and assertions that queries do not construct
unrelated symbols or classify unrelated identifiers.

Write guard: `python benchmarks/bench_restart.py --samples 15 --write-only
--output /tmp/write-guard.json` (one shell command). Python 3.13.13,
Linux 6.8 aarch64. Median CLI milliseconds:

| Fixture / command | master | step 2 |
| --- | ---: | ---: |
| small / new index | 113.46 | 114.88 |
| small / unchanged index | 87.84 | 87.61 |
| small / update one | 89.91 | 90.39 |
| small / update two | 109.81 | 112.94 |
| large / new index | 269.61 | 179.65 |
| large / unchanged index | 89.26 | 89.97 |
| large / update one | 215.19 | 125.40 |
| large / update two | 251.20 | 160.21 |

Small/startup-dominated cases vary by 0–3%; these measurements do not establish
a repeatable regression or an absolute no-regression guarantee on every machine.
The query change adds no work to the write path. Large-file writes are faster
and files/symbols/refs rows remain identical to master.

