from pathlib import Path
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
