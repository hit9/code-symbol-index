import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import code_symbol_index as c


def test_summary_defaults_defer_then_filter_after_prefix(tmp_path, monkeypatch):
    # Use the production 32-check budget and one-second age guard together.
    assert c.NAME_SUMMARY_SCAN_PREFIX == 32
    assert c.NAME_SUMMARY_MIN_AGE_NS == 1_000_000_000
    for i in range(40):
        (tmp_path / f'file_{i:02}.py').write_text(f'def unused_{i}(): return 1\n')
    monkeypatch.setattr(c, 'MAX_WORKERS', 1)
    monkeypatch.setattr(c, 'time_ns', lambda: 0)
    repo = c.Repository(tmp_path, create_index=True).refresh()
    assert repo.storage.connection.execute('SELECT count(*) FROM files WHERE name_summary IS NULL').fetchone()[0] == 40
    latest = max(path.stat().st_ctime_ns for path in tmp_path.glob('*.py'))
    monkeypatch.setattr(c, 'time_ns', lambda: latest + c.NAME_SUMMARY_MIN_AGE_NS)
    with mock.patch.object(c, '_parse_file', side_effect=AssertionError('AST rebuilt')):
        repo.refresh()
    names = c._NameFilter(repo)
    for i in range(32):
        assert names.may_contain(Path(f'file_{i:02}.py'), (b'missing',))
        assert names.rows is None
    assert not names.may_contain(Path('file_32.py'), (b'missing',))
    assert names.rows is not None
    # Changed files after the prefix must fall back to the live scanner.
    (tmp_path / 'file_39.py').write_text('def caller(): return missing()\n')
    assert names.may_contain(Path('file_39.py'), (b'missing',))


@pytest.mark.parametrize("source", [b"", b"\n", b"a\r\nb\n", "名字 = 'é'\nend".encode(), b"no newline"])
def test_line_table_matches_byte_positions(source):
    starts = c._line_starts(source)
    for offset in range(len(source) + 1):
        assert c._byte_position(source, offset, starts) == c._byte_position(source, offset)


def test_symbol_only_index_skips_reference_work(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\ndef target():\n    return VALUE\n")
    with mock.patch.object(c, "_classify_reference", side_effect=AssertionError("references during index")), \
         mock.patch.object(c, "_child_reference_context", side_effect=AssertionError("reference context during index")):
        repo = c.Repository(tmp_path, create_index=True).refresh()
        before = repo.storage.connection.execute("SELECT * FROM symbols ORDER BY id").fetchall()
        repo.update([Path("app.py")])
        after = repo.storage.connection.execute("SELECT * FROM symbols ORDER BY id").fetchall()
        assert [tuple(row) for row in before] == [tuple(row) for row in after]
        assert repo.storage.connection.execute("SELECT count(*) FROM refs").fetchone()[0] == 0


@pytest.mark.parametrize("language,source", [
    ("python", "from pkg import Target\n@decorator(Target)\nclass Derived(Target):\n    def call(self, x: Target):\n        Target = x\n        return obj.Target(Target), TargetExtra, 'Target', '名字'\n"),
    ("javascript", "import {Target} from 'pkg'; class Derived extends Target { call() { const x = Target(); return obj.Target(x); } }"),
    ("typescript", "interface Target {} class Derived implements Target { call(x: Target) { return Target(x); } }"),
    ("rust", "use pkg::Target; fn call(x: Target) { let y = Target::new(); x.Target(); }"),
    ("go", "package main\nfunc Target() {}\nfunc call() { Target(); obj.Target() }\n"),
    ("c", "int Target(int x) { return x; } int call() { return Target(1); }"),
    ("cpp", "class Target {}; int call() { Target x; return obj.Target(); }"),
    ("java", "class Derived extends Target { Target call(Target x) { return x.Target(); } }"),
    ("csharp", "class Derived : Target { Target Call(Target x) { return x.Target(); } }"),
    ("swift", "class Derived: Target { func call(_ x: Target) { Target(); x.Target(); let y = x[Target] } }"),
    ("kotlin", "class Derived : Target { fun call(x: Target) { Target(); x.Target(); var y = Target } }"),
    ("ruby", "class Derived < Target\n def call(x)\n  Target.new\n  x.Target()\n end\nend\n"),
    ("php", "<?php class Derived extends Target { function call($x) { Target(); $x->Target(); Target::make(); } }"),
])
def test_named_extraction_matches_full_extraction(language, source):
    spec = c.LANGUAGE_BY_NAME[language]
    for text in (source, source.replace("\n", "\r\n")):
        data = text.encode()
        tree = c._parse_source(c._parser_for_language(language), text)
        node = tree.root_node() if callable(tree.root_node) else tree.root_node
        _, references = c._extract_symbols_and_references(source=data, root_node=node, path=Path("sample"), language=spec)
        assert references
        symbols, _ = c._extract_symbols_and_references(
            source=data, root_node=node, path=Path("sample" + spec.extensions[0]), language=spec,
        )
        from types import SimpleNamespace
        repo = SimpleNamespace(languages=(language,))
        for subset in ([symbol] for symbol in symbols):
            actual = c._definition_ranges_for_symbols(repo, subset[0].path, subset, source=text)
            with mock.patch.object(c, "NATIVE_DEFINITION_MAX_SYMBOLS", 0):
                expected = c._definition_ranges_for_symbols(repo, subset[0].path, subset, source=text)
            assert actual == expected
        names = frozenset(ref.name for ref in references if ref.name.startswith(("T", "c", "x")))
        assert c._extract_named_references(data, node, Path("sample"), spec, names) == [
            ref for ref in references if ref.name in names
        ]
        for name in {ref.name for ref in references} | {"missing", "TargetExtra", "Target"}:
            assert c._extract_named_references(data, node, Path("sample"), spec, name) == [
                ref for ref in references if ref.name == name
            ]


def test_named_query_prunes_unrelated_nodes_and_builds_no_symbols(tmp_path):
    source = "def target():\n    return 1\n" + "".join(
        f"def unrelated_{i}():\n    return other(value + {i})\n" for i in range(100)
    ) + "def caller():\n    return target()\n"
    (tmp_path / "app.py").write_text(source)
    repo = c.Repository(tmp_path, create_index=True).refresh()
    with mock.patch.object(c, "_symbol_from_node", side_effect=AssertionError("query extracts unrelated symbols")), \
         mock.patch.object(c, "_classify_reference", wraps=c._classify_reference) as classify:
        refs = repo.refs("target", limit=100)
    assert len(refs.items) == 1
    # Only the declaration and call, rather than hundreds of identifiers.
    assert classify.call_count == 2


@pytest.mark.parametrize("limit,offset", [(0, 0), (1, 0), (2, 1), (10, 0), (2, 20)])
@pytest.mark.parametrize("kinds", [None, "call", "read", "import,attribute"])
def test_named_query_keeps_pagination_and_live_source(tmp_path, limit, offset, kinds):
    source = "def target():\n    pass\n\ndef caller():\n    target()\n    x = target\n    obj.target()\n"
    (tmp_path / "app.py").write_text(source)
    (tmp_path / "other.py").write_text("from app import target\ndef another():\n    target()\n")
    memory = c.CodeIndex(tmp_path).build()
    disk = c.Repository(tmp_path, create_index=True).refresh()
    if limit == 0:
        for repo in (memory, disk):
            with pytest.raises(ValueError, match="limit must be >= 1"):
                repo.refs("target", limit=limit, offset=offset, ref_kinds=kinds)
        return
    assert disk.refs("target", limit=limit, offset=offset, ref_kinds=kinds) == memory.refs(
        "target", limit=limit, offset=offset, ref_kinds=kinds
    )
    (tmp_path / "other.py").write_text("def another():\n    target()\n    target()\n")
    memory.build()
    assert disk.refs("target", limit=limit, offset=offset, ref_kinds=kinds) == memory.refs(
        "target", limit=limit, offset=offset, ref_kinds=kinds
    )


def test_text_results_parse_each_file_once(tmp_path):
    (tmp_path / "app.py").write_text("".join(f"def target_{i}():\n    return {i}\n" for i in range(20)))
    repo = c.Repository(tmp_path, create_index=True).refresh()
    page = repo.search_page("target", limit=20)
    for render in (
        lambda: c._format_search_text(repo, "target", page),
        lambda: c._format_page_text(repo, "implementations", page),
        lambda: "\n".join(c._format_relation_section(repo, "members", page.items, 20)),
    ):
        with mock.patch.object(c, "_parse_source", wraps=c._parse_source) as parse:
            output = render()
        assert parse.call_count == 1
        assert "target_19" in output
    (tmp_path / "app.py").write_text("def target_0():\n    return 'changed'\n")
    # There is no cache across renders/requests, even on the same Repository.
    with mock.patch.object(c, "_parse_source", wraps=c._parse_source) as parse:
        c._format_search_text(repo, "target", page)
    assert parse.call_count == 1


def test_cli_outline_queries_only_once(tmp_path, capsys):
    (tmp_path / "app.py").write_text("class Target:\n    def method(self):\n        pass\n")
    c.Repository(tmp_path, create_index=True).refresh()
    original = c.Repository.outline
    with mock.patch.object(c.Repository, "outline", autospec=True, side_effect=original) as outline:
        assert c.main(["outline", "app.py", "--root", str(tmp_path), "--symbol", "Target"]) == 0
    assert outline.call_count == 1
    assert "method" in capsys.readouterr().out


def test_status_loads_manifest_only_for_explicit_check(tmp_path):
    (tmp_path / "app.py").write_text("def target():\n    pass\n")
    c.Repository(tmp_path, create_index=True).refresh()
    statements = []
    connect = c.sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    with mock.patch.object(c.sqlite3, "connect", side_effect=traced):
        assert c.status(root=tmp_path).status == "ready"
        assert "SELECT path, language, mtime_ns, size FROM files" not in statements
        c.status(root=tmp_path, check=True)
        assert "SELECT path, language, mtime_ns, size FROM files" in statements


@pytest.mark.parametrize("command", ["version", "status", "search", "refs"])
def test_light_queries_do_not_load_unused_dependencies(tmp_path, command):
    (tmp_path / "app.py").write_text("def target():\n    pass\n")
    c.Repository(tmp_path, create_index=True).refresh()
    args = [command]
    if command in ("search", "refs"):
        args += ["target", "--json"]
    if command != "version":
        args += ["--root", str(tmp_path)]
    # An isolated process prevents imports from fixture construction hiding an
    # accidental eager import. refs needs its parser, but never ignore rules.
    source = (
        "import sys; import code_symbol_index as c; "
        f"assert c.main({args!r}) == 0; "
        "assert 'pathspec' not in sys.modules; "
        + ("assert 'tree_sitter_language_pack' not in sys.modules" if command != "refs" else "")
    )
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, cwd=Path(c.__file__).parent)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("depth,limit,max_nodes", [(1, 3, 200), (3, 3, 200), (4, 50, 200), (3, 50, 2), (3, 50, 45)])
def test_batched_call_graph_matches_individual_queries(tmp_path, depth, limit, max_nodes):
    (tmp_path / "target.py").write_text("def target():\n    branch_0()\n")
    (tmp_path / "branches.py").write_text("".join(
        f"def branch_{i}():\n    target()\n    target()\n" for i in range(40)
    ))
    (tmp_path / "entries.py").write_text("".join(
        f"def entry_{i}():\n    branch_{i}()\n    branch_{(i + 1) % 40}()\n" for i in range(40)
    ))
    repo = c.Repository(tmp_path, create_index=True).refresh()
    target = repo.search_symbols("target", exact_only=True)[0]
    def graph():
        return c._build_call_graph(repo, target, direction="callers", depth=depth, limit=limit, max_nodes=max_nodes)

    def individual(repo, symbols, *, limit):
        return {symbol.id: c._direct_callers(repo, symbol, limit=limit) for symbol in symbols}

    with mock.patch.object(c, "_direct_callers_batch", side_effect=individual):
        expected = graph()
    with mock.patch.object(c, "_file_contains_pattern", wraps=c._file_contains_pattern) as scans:
        actual = graph()
    assert actual == expected
    assert scans.call_count <= 3 * 2 * max(depth - 1, 0)
    (tmp_path / "entries.py").write_text("def changed_entry():\n    branch_0()\n")
    repo.update([Path("entries.py")])
    with mock.patch.object(c, "_direct_callers_batch", side_effect=individual):
        expected_after_edit = graph()
    assert graph() == expected_after_edit


def test_batch_prefilter_preserves_chunk_boundary_matches(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr(c, "FILE_SCAN_CHUNK_SIZE", 7)
    path = tmp_path / "bytes"
    needles = [b"long_target_name", "名字".encode(), b"a.b"]
    pattern = re.compile(b"|".join(re.escape(value) for value in needles))
    for needle in needles:
        for padding in range(15):
            path.write_bytes(b" " * padding + needle + b" tail")
            assert c._file_contains_pattern(path, pattern, max(map(len, needles)) - 1)
    path.write_bytes(b"aXb completely unrelated")
    assert not c._file_contains_pattern(path, pattern, max(map(len, needles)) - 1)


def test_native_ranges_handle_live_shortened_name_and_wide_results(tmp_path):
    from dataclasses import replace
    source = "def lengthy_name():\n    return 1\n" + "".join(
        f"def worker_{i}():\n    return {i}\n" for i in range(100)
    )
    path = Path("app.py")
    (tmp_path / path).write_text(source)
    repo = c.Repository(tmp_path, create_index=True).refresh()
    symbols = repo.search_symbols("worker", limit=100)
    for subset in (symbols[:1], symbols[:20], symbols):
        actual = c._definition_ranges_for_symbols(repo, path, subset, source=source)
        with mock.patch.object(c, "NATIVE_DEFINITION_MAX_SYMBOLS", 0):
            assert actual == c._definition_ranges_for_symbols(repo, path, subset, source=source)
    target = repo.search_symbols("lengthy_name")[0]
    for text in ("def x():\n pass\n", "", "def x(", "# gone\n"):
        actual = c._definition_ranges_for_symbols(repo, path, [target], source=text)
        with mock.patch.object(c, "NATIVE_DEFINITION_MAX_SYMBOLS", 0):
            assert actual == c._definition_ranges_for_symbols(repo, path, [target], source=text)
    invalid = replace(target, range=replace(target.range, start_byte=len(source) + 1))
    assert c._definition_ranges_for_symbols(repo, path, [invalid], source=source) == {}


@pytest.mark.parametrize("count,size,parallel", [(2, 128, False), (2, 65536, True), (17, 128, True)])
def test_parse_pool_reserved_for_large_batches(tmp_path, monkeypatch, count, size, parallel):
    import concurrent.futures
    monkeypatch.setattr(c, "MAX_WORKERS", 2)
    paths = []
    for i in range(count):
        path = Path(f"file_{i}.py")
        (tmp_path / path).write_text(f"def worker_{i}():\n    return 1\n#" + "x" * size)
        paths.append(path)
    repo = c.Repository(tmp_path, create_index=True)
    with mock.patch.object(concurrent.futures, "ProcessPoolExecutor", wraps=concurrent.futures.ProcessPoolExecutor) as pool:
        results = repo._parse_files(paths, include_references=False)
    assert pool.called is parallel
    assert {result.path for result in results} == set(paths)
    assert all(len(result.symbols) == 1 and not result.references for result in results)


def test_version_avoids_query_and_writer_imports():
    source = """
import sys
import code_symbol_index as c
assert c.main(['version']) == 0
for name in ('concurrent.futures', 'tree_sitter', 'tree_sitter_language_pack', 'pathspec', 'argparse', 'hashlib'):
    assert name not in sys.modules, name
"""
    result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True, cwd=Path(c.__file__).parent)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'code-symbol-index {c.__version__}\n'


def test_version_still_rejects_unrecognized_options():
    with pytest.raises(SystemExit) as error:
        c.main(['version', '--invalid-option'])
    assert error.value.code == 2
