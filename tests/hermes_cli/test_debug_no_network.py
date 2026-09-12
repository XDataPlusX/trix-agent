"""`hermes debug share` must never ship client logs off the VM."""

import types

import pytest

import hermes_cli.debug as debug


class _Args:
    """Stand-in for argparse namespace with upload paths requested."""

    lines = 10
    expire = 7
    local = False
    nous = False
    no_redact = False
    yes = True


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Neutralise every outbound call the share path can make."""
    monkeypatch.setattr(
        debug, "collect_share_bundle", lambda **k: {"report": "REPORT"}
    )
    # The local branch sweeps previously-created pastes, which is network I/O.
    monkeypatch.setattr(debug, "_best_effort_sweep_expired_pastes", lambda: None)


def test_default_share_does_not_upload(monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError("network upload attempted")

    monkeypatch.setattr(debug, "build_debug_share", _boom)

    debug.run_debug_share(_Args())

    assert "REPORT" in capsys.readouterr().out


def test_nous_share_does_not_upload(monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError("nous upload attempted")

    monkeypatch.setattr(debug, "_run_debug_share_nous", _boom)

    args = _Args()
    args.nous = True
    debug.run_debug_share(args)

    assert "REPORT" in capsys.readouterr().out
