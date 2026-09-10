from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

import code_symbol_index as c


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
