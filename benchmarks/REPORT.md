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


## Step 3: batching, outline reuse and status summary

182 tests passed. No index/update implementation changed in this step.
The benchmark now imports the module and invokes main, matching the installed
entry point and allowing warm bytecode caches. Earlier step-1/2 measurements
executed the source script directly; do not compare their startup times as if
the harness were identical. Both sides of every comparison use the same harness.

Seven independent processes per command/variant; median milliseconds below.
The fixture is the large four-file case unless labelled small. `impls target`
has no implementors here; it measures the empty-result path only. `clean` and
`install-skill` operate exclusively in the disposable benchmark directory.

| Command | master ms | step 3 ms |
| --- | ---: | ---: |
| index_new | 249.40 | 158.16 |
| index_no_change | 68.63 | 67.54 |
| update_one | 197.99 | 105.61 |
| update_two | 230.80 | 140.10 |
| search | 235.61 | 80.76 |
| inspect | 2461.96 | 195.10 |
| refs | 1005.24 | 88.14 |
| callers | 1118.52 | 126.79 |
| callees | 1010.96 | 144.11 |
| impls | 65.36 | 66.82 |
| outline | 90.61 | 81.98 |
| status | 65.42 | 64.59 |
| status_check | 69.62 | 69.47 |
| missing | 65.87 | 65.52 |
| version | 65.15 | 65.18 |
| languages | 64.22 | 64.60 |
| clean | 63.97 | 65.07 |
| install_skill | 63.77 | 65.64 |

Reference/query-heavy cases improve; startup-dominated commands mostly remain
unchanged at this step. No automatic stale warnings have been added.

## Step 4: load parser/ignore dependencies only when used

186 tests passed. Seven runs per variant through the import/main entry point,
with `.gitignore` enabled. Fixtures now also include 20 Protocol implementations
to exercise nonempty impls output; compare within this table, not across fixture
revisions. All query stdout matched (status timestamps excluded).

| Command | small master ms | small new ms | large master ms | large new ms |
| --- | ---: | ---: | ---: | ---: |
| index_new | 89.45 | 86.71 | 247.75 | 152.69 |
| index_no_change | 67.72 | 66.53 | 64.31 | 64.57 |
| update_one | 67.82 | 66.58 | 194.81 | 103.32 |
| update_two | 77.13 | 77.81 | 229.28 | 138.40 |
| search | 67.25 | 52.30 | 233.51 | 66.07 |
| inspect | 79.55 | 57.63 | 2463.27 | 181.96 |
| refs | 67.82 | 52.76 | 998.65 | 75.72 |
| callers | 68.97 | 54.13 | 1124.62 | 111.79 |
| callees | 67.84 | 53.55 | 1021.25 | 131.01 |
| impls | 64.57 | 41.42 | 68.69 | 42.89 |
| outline | 65.61 | 52.14 | 102.29 | 80.41 |
| impls_hits | 70.71 | 52.60 | 403.77 | 74.69 |
| status | 63.92 | 40.72 | 64.76 | 41.67 |
| status_check | 65.75 | 67.26 | 67.27 | 67.15 |
| missing | 64.97 | 42.32 | 65.88 | 42.98 |
| version | 63.19 | 40.47 | 79.63 | 49.26 |
| languages | 65.31 | 51.04 | 68.95 | 55.44 |
| clean | 63.14 | 40.72 | 67.43 | 43.00 |
| install_skill | 62.97 | 41.05 | 67.43 | 42.65 |

Shared-mount guard: 11 runs, small fixture and `.gitignore`, disposable directory
under the checkout, using `--write-only --fixture small --with-gitignore
--temp-parent .`. No real repository index was touched.

| Command | master ms | new ms |
| --- | ---: | ---: |
| small.index_new | 122.68 | 124.76 |
| small.index_no_change | 71.17 | 68.17 |
| small.update_one | 81.43 | 81.42 |
| small.update_two | 97.65 | 96.52 |

The mounted first-build median was 1.7% higher in this sample; other mounted
write medians were equal or lower. This small difference is reported rather
than represented as a speedup. No database work or schema change was introduced.


## Step 5: batch caller expansion within a graph layer

Caller expansion batches at most 32 targets, grouped by language, and shares
one prefilter/parse pass. Consumption order, per-target limits, node truncation,
reference classification and graph structure are unchanged. No write path or
schema changes. 192 tests passed, including batched-vs-individual graph equality,
cycles/cross-links, node limits, overlapping names and read-chunk boundaries.

Further read-side latency work remains. No private repository identifiers, paths,
source snippets or private benchmark output belong in this report.


## Step 6: native lookup for small definition result sets

Locate up to 64 requested definitions through Tree-sitter byte lookup; preserve
the original traversal for large result sets or ambiguous grammar wrappers.
Indexed name ends are not trusted after live edits. Schema and write code are
unchanged. 193 tests passed, including 13-language range comparisons and live
shortened names.

Five independent CLI samples on the existing large fixture against step 5:
index new +1.4%, unchanged index +1.0%, update one -5.5%, update two -2.2%;
impls with hits -13.2%. Early-file search was unchanged. A separate synthetic
802-file fixture with a 2,000-function file (7 samples) measured late-definition
search 85.93 -> 73.13 ms and 20-result search 84.58 -> 70.95 ms. New index there
was 231.61 -> 234.94 ms; update one 118.21 -> 118.56 ms. Small write differences
are reported as measurement variation, not speedups or an absolute guarantee.
`bench_restart.py` now includes `search_late` to cover late-file lookup.
