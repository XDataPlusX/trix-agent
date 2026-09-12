"""Мастер настройки обязан начать работать по новому коду после обновления.

`hermes update` перезапускает шлюз — и только его. Мастер живёт в
собственной службе, которая по замыслу (спека 8) остаётся открытой
навсегда, и потому держит в памяти код той версии, при которой её
запустили.

Замерено на живой машине 2026-09-05: мастер стартовал в 01:58 при
установке, код обновлялся в 02:39 и дважды позже — три обновления подряд
до мастера не дошли. Видно было по `key_checked`: ответ мастера нёс
`false`, хотя вызванная руками проверка ключа на той же машине отвечала
`200`. После перезапуска службы — `true`.
"""

import subprocess

import pytest

from hermes_cli import trix_wizard_restart as mod


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _systemctl_saying(answers, calls=None):
    def fake(verb):
        if calls is not None:
            calls.append(verb)
        return answers.get(verb)

    return fake


def test_a_running_wizard_is_restarted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mod, "_systemctl",
        _systemctl_saying({"is-active": _Result("active"), "restart": _Result()}, calls),
    )
    lines = mod.restart_wizard_after_update()

    assert "restart" in calls
    assert lines and "перезапущен" in lines[0]


def test_a_stopped_wizard_is_left_alone(monkeypatch):
    """Поднимать службу тому, кто её сознательно закрыл, мы не имеем права."""
    calls = []
    monkeypatch.setattr(
        mod, "_systemctl",
        _systemctl_saying({"is-active": _Result("inactive", 3)}, calls),
    )
    assert mod.restart_wizard_after_update() == []
    assert "restart" not in calls


def test_a_machine_without_the_wizard_gets_no_noise(monkeypatch):
    """Отчёт об обновлении не обрастает строчками про службу, которой нет."""
    monkeypatch.setattr(
        mod, "_systemctl", _systemctl_saying({"is-active": _Result("", 4)})
    )
    assert mod.restart_wizard_after_update() == []


def test_systemctl_that_cannot_be_asked_is_not_read_as_stopped(monkeypatch):
    """Пустой ответ — «не смогли спросить», и трогать тоже нечего."""
    monkeypatch.setattr(mod, "_systemctl", lambda verb: None)
    assert mod.wizard_is_running() is False
    assert mod.restart_wizard_after_update() == []


def test_a_failed_restart_is_reported_honestly_with_a_next_step(monkeypatch):
    """Честный отказ дороже тихого успеха — и обязан говорить, что делать."""
    monkeypatch.setattr(
        mod, "_systemctl",
        _systemctl_saying({"is-active": _Result("active"), "restart": _Result("", 1)}),
    )
    joined = " ".join(mod.restart_wizard_after_update())

    assert "не удалось" in joined
    assert "systemctl --user restart trix-setup" in joined
    assert "не портятся" in joined, "клиент должен понимать, что настройки целы"


def test_the_restart_never_raises(monkeypatch):
    """Обновление уже прошло: провал перезапуска не смеет выглядеть его провалом."""
    def explode(verb):
        raise RuntimeError("systemctl взорвался")

    monkeypatch.setattr(mod, "_systemctl", explode)
    assert mod.restart_wizard_after_update() == []


def test_a_dead_systemctl_binary_is_survived(monkeypatch):
    """На машине без systemd вызов вообще не выполнится."""
    def boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(subprocess, "run", boom)
    assert mod._systemctl("is-active") is None


def test_update_completion_restarts_the_wizard(monkeypatch, capsys):
    """Сквозная проверка: обновление доводит новый код до мастера."""
    from hermes_cli import update_cmd
    import hermes_cli.trix_update_numpy as numpy_mod

    monkeypatch.setattr(numpy_mod, "repair_numpy_after_update", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(
        mod, "restart_wizard_after_update", lambda: called.append(True) or []
    )
    update_cmd._print_update_completion("✓ Update complete!")

    assert called == [True], "обновление объявило успех, не тронув мастера"


def test_nothing_is_restarted_when_there_was_nothing_to_update(monkeypatch, capsys):
    """«Already up to date» — чинить и перезапускать нечего."""
    from hermes_cli import update_cmd
    import hermes_cli.trix_update_numpy as numpy_mod

    touched = []
    monkeypatch.setattr(
        numpy_mod, "repair_numpy_after_update", lambda *a, **k: touched.append("numpy")
    )
    monkeypatch.setattr(
        mod, "restart_wizard_after_update", lambda: touched.append("wizard") or []
    )
    update_cmd._print_update_completion("✓ Already up to date!", updated=False)

    assert touched == []
    assert "Already up to date" in capsys.readouterr().out
