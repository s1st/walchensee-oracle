"""CLI surface for the `oracle ml` subcommand.

Phase B delivers the CLI shell + the deps guard. Phase C replaces the
`train` body with the actual training loop; the test cases here pin the
Phase B contract (--help works, flags are present, deps-guard fires
cleanly, --label validation runs before the deps check). New tests
covering the Phase C body go in test_ml_train.py once it ships.
"""
from __future__ import annotations

import importlib.util

from typer.testing import CliRunner

from oracle.cli import app


runner = CliRunner()


def test_root_help_lists_ml_subcommand():
    """`oracle --help` must show the ml subcommand, not bury it."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ml" in result.output


def test_ml_help_lists_train_command():
    result = runner.invoke(app, ["ml", "--help"])
    assert result.exit_code == 0
    assert "train" in result.output


def test_ml_train_help_shows_all_four_flags():
    """The Phase B contract: --csv, --label, --horizon, --out are all wired.

    Pinning the flag surface here means a future Phase C refactor can't
    silently drop a flag without a test failure — `--horizon` and `--out`
    are accepted now (validation only) so the contract is stable for
    downstream callers / docs to depend on.
    """
    result = runner.invoke(app, ["ml", "train", "--help"])
    assert result.exit_code == 0
    for flag in ("--csv", "--label", "--horizon", "--out"):
        assert flag in result.output, f"missing flag {flag} in train --help"


def test_ml_train_rejects_invalid_label_with_helpful_list():
    """Label validation runs *before* the deps check — calling with a bad
    label on a host without sklearn should still surface the validation
    error, not the install-ml-extras error.
    """
    result = runner.invoke(app, ["ml", "train", "--csv", "/tmp/x.csv", "--label", "nope"])
    assert result.exit_code != 0
    assert "peak" in result.output and "duration" in result.output and "thermal" in result.output


def test_ml_train_rejects_zero_horizon():
    result = runner.invoke(app, ["ml", "train", "--csv", "/tmp/x.csv", "--horizon", "0"])
    assert result.exit_code != 0
    assert "horizon" in result.output.lower()


def test_ml_train_deps_guard_fires_when_sklearn_missing(monkeypatch):
    """On the prod images sklearn is not installed; the CLI must fail
    with a clear, installable message rather than a traceback.

    Simulates the missing-extras state by stubbing `importlib.util.find_spec`
    to return None for any 'sklearn' lookup. The guard uses find_spec (not
    a real `import sklearn`) precisely so this test is independent of
    whether the test env actually has sklearn installed.
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "sklearn":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(app, ["ml", "train", "--csv", "/tmp/x.csv"])
    assert result.exit_code != 0
    assert "ml" in result.output.lower() and ("extra" in result.output.lower() or "install" in result.output.lower())


def test_ml_train_body_not_yet_implemented_when_deps_present(monkeypatch):
    """Phase C will replace the stub body. Until then, with sklearn
    available, the command must exit with a clear 'not yet implemented'
    message — not a silent success, not a crash.
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        # Pretend sklearn is importable; let everything else use the real impl.
        if name == "sklearn":
            return importlib.util.spec_from_loader("sklearn", loader=None)
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(app, ["ml", "train", "--csv", "/tmp/x.csv"])
    assert result.exit_code != 0
    assert "Phase C" in result.output
