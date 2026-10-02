from collections import Counter
from pathlib import Path
from unittest import mock

import pytest

import code_symbol_index as c


def make_repo(root):
    (root / "target.py").write_text("def target(): pass\n")
    (root / "caller.py").write_text("def caller():\n    target()\n")
    return c.Repository(root, create_index=True).refresh()


def test_matching_small_file_is_opened_once_and_next_query_reads_live_source(tmp_path):
    repo = make_repo(tmp_path)
    opens = Counter()
    original_open = Path.open

    def counted(path, *args, **kwargs):
        opens[path.name] += 1
        return original_open(path, *args, **kwargs)

    try:
        with mock.patch.object(Path, "open", counted):
            references = repo.refs("target")
        assert len(references.items) == 1
        assert references.items[0].path == Path("caller.py")
        assert opens["caller.py"] == 1

        (tmp_path / "caller.py").write_text("def caller():\n    unrelated()\n")
        assert not repo.refs("target").items
        (tmp_path / "caller.py").write_text("def caller():\n    target()\n    target()\n")
        assert len(repo.refs("target").items) == 2
    finally:
        repo.storage.connection.close()


def test_batched_callers_reuse_the_prefilter_source(tmp_path):
    (tmp_path / "targets.py").write_text("def first(): pass\ndef second(): pass\n")
    (tmp_path / "caller.py").write_text("def caller():\n    first()\n    second()\n")
    repo = c.Repository(tmp_path, create_index=True).refresh()
    targets = repo.search_symbols("first") + repo.search_symbols("second")
    parse = c._parse_file

    def parse_without_read(*args, **kwargs):
        with mock.patch.object(c, "_read_text_file", side_effect=AssertionError("duplicate text read")):
            return parse(*args, **kwargs)

    try:
        # Caller ownership/range lookup can read source independently; the
        # reference extraction pass itself must not reopen the prefiltered file.
        with mock.patch.object(c, "_parse_file", side_effect=parse_without_read):
            callers = c._direct_callers_batch(repo, targets, limit=20)
        assert {symbol.name for values in callers.values() for symbol in values} == {"caller"}
        assert all(len(values) == 1 for values in callers.values())
    finally:
        repo.storage.connection.close()


@pytest.mark.parametrize("source", [b"\xfftarget()", b"\0target()", b"target()\n\xff"])
def test_invalid_matching_source_is_still_rejected(tmp_path, source):
    repo = make_repo(tmp_path)
    try:
        (tmp_path / "caller.py").write_bytes(source)
        assert repo.refs("target").items == ()
    finally:
        repo.storage.connection.close()


def test_unicode_crlf_source_preserves_reference_ranges(tmp_path):
    repo = make_repo(tmp_path)
    try:
        (tmp_path / "caller.py").write_bytes("# 名字\r\ndef caller():\r\n    target()\r\n".encode())
        actual = repo.refs("target")
        original_read = c._read_matching_source

        def without_reuse(*args):
            matched, _ = original_read(*args)
            return matched, None

        with mock.patch.object(c, "_read_matching_source", side_effect=without_reuse):
            expected = repo.refs("target")
        assert actual == expected
        assert actual.items[0].range.start.line == 2  # Internal ranges are zero-based.
        assert actual.items[0].range.start.column == 4
    finally:
        repo.storage.connection.close()


@pytest.mark.parametrize("size", [63, 64, 65, 256])
def test_only_complete_first_chunk_is_reused(tmp_path, monkeypatch, size):
    monkeypatch.setattr(c, "FILE_SCAN_CHUNK_SIZE", 64)
    path = tmp_path / "caller.py"
    source = b"target()\n#" + b" " * (size - 10)
    path.write_bytes(source)
    matched, text = c._read_matching_source(path, b"target", 5)
    assert matched
    assert text == (source.decode() if size < 64 else None)


def test_large_file_uses_original_reader_and_rejects_binary_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "FILE_SCAN_CHUNK_SIZE", 16)
    repo = make_repo(tmp_path)
    try:
        with mock.patch.object(c, "_read_text_file", wraps=c._read_text_file) as read:
            assert len(repo.refs("target").items) == 1
        assert any(call.args[0].name == "caller.py" for call in read.call_args_list)
        # The NUL is past the prefilter chunk, but within the text reader's sample.
        (tmp_path / "caller.py").write_bytes(b"target()\n#" + b" " * 32 + b"\0")
        assert repo.refs("target").items == ()
    finally:
        repo.storage.connection.close()


def test_missing_file_is_not_a_match(tmp_path):
    assert c._read_matching_source(tmp_path / "gone.py", b"target", 5) == (False, None)
