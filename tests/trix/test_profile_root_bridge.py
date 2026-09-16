"""Фаза 1.3 плана 2026-09-14: мост isolation.mode → HERMES_ISOLATION.

Мост один на оба входа (cli.py и gateway/run.py) — здесь исполняется сама
функция моста на словаре конфига, как в существующих тестах terminal.*.
"""

import os

import hermes_constants
from hermes_constants import bridge_isolation_config


def _clean(monkeypatch):
    monkeypatch.delenv("HERMES_ISOLATION", raising=False)
    hermes_constants.reset_isolation_warnings()


def test_bridges_contained(monkeypatch):
    _clean(monkeypatch)
    assert bridge_isolation_config({"isolation": {"mode": "contained"}}) == "contained"
    assert os.environ["HERMES_ISOLATION"] == "contained"


def test_bridges_shared_host(monkeypatch):
    _clean(monkeypatch)
    assert bridge_isolation_config({"isolation": {"mode": "shared-host"}}) == "shared-host"
    assert os.environ["HERMES_ISOLATION"] == "shared-host"


def test_env_wins_over_config(monkeypatch):
    """Аварийный выход: HERMES_ISOLATION в юните сильнее конфига."""
    _clean(monkeypatch)
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    assert bridge_isolation_config({"isolation": {"mode": "contained"}}) is None
    assert os.environ["HERMES_ISOLATION"] == "shared-host"


def test_missing_section_is_noop(monkeypatch):
    _clean(monkeypatch)
    for cfg in (None, {}, {"isolation": None}, {"isolation": {}}, {"isolation": "contained"},
                {"terminal": {"home_mode": "profile"}}):
        assert bridge_isolation_config(cfg) is None
    assert "HERMES_ISOLATION" not in os.environ


def test_garbage_mode_is_ignored_with_warning(monkeypatch, capsys):
    _clean(monkeypatch)
    assert bridge_isolation_config({"isolation": {"mode": "полная"}}) is None
    assert "HERMES_ISOLATION" not in os.environ
    assert "isolation.mode" in capsys.readouterr().err


def test_bridged_value_drives_the_resolver(monkeypatch, tmp_path):
    """Мост и резолвер согласованы: что намостили, то режим и увидел."""
    _clean(monkeypatch)
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert hermes_constants.get_isolation_mode() == "shared-host"
    bridge_isolation_config({"isolation": {"mode": "contained"}})
    assert hermes_constants.get_isolation_mode() == "contained"
