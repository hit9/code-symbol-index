"""Offline update-check tests; no real network access."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

import code_symbol_index as c


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "update-check.json"
    monkeypatch.setenv(c.UPDATE_CHECK_CACHE_ENV, str(path))
    monkeypatch.delenv(c.UPDATE_CHECK_ENV_DISABLE, raising=False)
    return path


def fake_pypi(monkeypatch, version, calls=None):
    def urlopen(request, timeout=None):
        if calls is not None:
            calls.append(request)

        class Response:
            def read(self):
                return json.dumps({"info": {"version": version}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def run_cli(monkeypatch):
    return c.main(["version"])


def read_cache(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_hint_shown_once_per_interval(cache_path, monkeypatch, capsys):
    calls = []
    fake_pypi(monkeypatch, "99.0.0", calls)

    run_cli(monkeypatch)
    out, err = capsys.readouterr()
    assert "code-symbol-index 99.0.0 is available" in err
    assert f"installed {c.__version__}" in err
    assert "uv tool upgrade code-symbol-index" in err
    assert "note:" not in out  # stdout stays clean
    assert len(calls) == 1
    assert read_cache(cache_path)["hint_emitted"] is True

    run_cli(monkeypatch)  # within the interval: no second check, no second hint
    out, err = capsys.readouterr()
    assert "note:" not in err
    assert "note:" not in out
    assert len(calls) == 1


def test_throttled_check_skips_network(cache_path, monkeypatch, capsys):
    calls = []
    fake_pypi(monkeypatch, c.__version__, calls)  # same version: no hint at all

    run_cli(monkeypatch)
    assert len(calls) == 1
    assert "note:" not in capsys.readouterr().err

    run_cli(monkeypatch)  # interval not elapsed: no new request
    assert len(calls) == 1
    assert "note:" not in capsys.readouterr().err


def test_network_failure_is_silent(cache_path, monkeypatch, capsys):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert run_cli(monkeypatch) == 0
    out, err = capsys.readouterr()
    assert out == f"code-symbol-index {c.__version__}\n"
    assert err == ""
    assert len(calls) == 1

    run_cli(monkeypatch)  # failed check still counts; no retry storm
    assert len(calls) == 1


def test_env_disable_skips_everything(cache_path, monkeypatch, capsys):
    calls = []
    fake_pypi(monkeypatch, "99.0.0", calls)
    monkeypatch.setenv(c.UPDATE_CHECK_ENV_DISABLE, "1")

    run_cli(monkeypatch)
    assert calls == []
    assert not cache_path.exists()
    assert "note:" not in capsys.readouterr().err


def test_pre_release_and_malformed_versions_are_ignored(cache_path, monkeypatch, capsys):
    for version in ("0.6.0rc1", "1.0.0a1", "not.a.version!", "0.5"):
        fake_pypi(monkeypatch, version)
        cache_path.unlink(missing_ok=True)
        run_cli(monkeypatch)
        assert "note:" not in capsys.readouterr().err, version


def test_corrupt_cache_treated_as_missing(cache_path, monkeypatch, capsys):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not json", encoding="utf-8")
    calls = []
    fake_pypi(monkeypatch, c.__version__, calls)

    run_cli(monkeypatch)
    assert len(calls) == 1  # corrupt state forces a fresh check
    assert read_cache(cache_path)["latest_version"] == c.__version__


def test_unfinished_check_retries_without_network(cache_path, monkeypatch, capsys):
    calls = []
    finished = threading.Event()

    def urlopen(request, timeout=None):
        calls.append(request)
        assert finished.wait(timeout=5)  # stay in flight past the join timeout
        return Response()

    class Response:
        def read(self):
            return json.dumps({"info": {"version": "99.0.0"}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(c, "UPDATE_CHECK_JOIN_TIMEOUT_S", 0.0)

    run_cli(monkeypatch)  # check still in flight when the command ends
    assert "note:" not in capsys.readouterr().err
    assert len(calls) == 1

    finished.set()  # let the daemon thread complete and persist its state
    for _ in range(100):
        if cache_path.exists() and read_cache(cache_path).get("latest_version"):
            break
        threading.Event().wait(0.01)
    assert read_cache(cache_path)["latest_version"] == "99.0.0"

    run_cli(monkeypatch)  # hint is due; served from cache, no new request
    assert len(calls) == 1
    assert "code-symbol-index 99.0.0 is available" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.5.5", "0.5.4", True),
        ("0.6.0", "0.5.4", True),
        ("1.0.0", "0.99.99", True),
        ("0.5.4", "0.5.4", False),
        ("0.5.3", "0.5.4", False),
        ("0.5", "0.5.4", False),
        ("0.6.0rc1", "0.5.4", False),
        ("0.6.0.dev1", "0.5.4", False),
        ("garbage", "0.5.4", False),
        ("0.6.0", "garbage", False),
        ("", "0.5.4", False),
    ],
)
def test_is_newer_release(latest, current, expected):
    assert c._is_newer_release(latest, current) is expected


def test_cache_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(c.UPDATE_CHECK_CACHE_ENV, str(tmp_path / "x.json"))
    assert c._update_check_cache_path() == tmp_path / "x.json"
