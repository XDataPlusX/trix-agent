"""Установочные хуки обязаны запускать npm/npx с managed Node в PATH.

Снято с клиентской машины 81.222.148.68 (2026-09-10). Мастер настройки
объявлял «Инструмент установить не удалось» для браузера, три попытки
подряд отваливались за доли секунды, а в логе не было ни одной причины.
Ручной повтор хука показал настоящую:

    /usr/bin/env: ‘node’: No such file or directory

``npm`` и ``npx`` — не бинарники, а скрипты с шапкой ``#!/usr/bin/env
node``. Хук аккуратно находил их абсолютный путь через
``find_node_executable`` (комментарий там прямо говорит: «$HERMES_HOME/node
не лежит в PATH»), но запускал БЕЗ окружения — и та же самая причина,
из-за которой нужен особый поиск, ломала запуск. На машине, где Node
только наш (а рецепт кладёт его ровно туда), браузер не мог установиться
никогда.

Отказ выглядел сетевым и лечился повторами, которые не могли помочь:
причина не в сети, а в том, что процесс не стартовал вовсе.

Правильный приём в продукте уже был — ``with_hermes_node_path()``, и им
уже пользуются `update_cmd`, `npm_engine`, `web_server`, whatsapp-мост и
сам рантайм браузера (`_merge_browser_path`). Не пользовались только
установочные хуки. Тесты держат за PATH все три их запуска: npm для
agent-browser, npx для Chromium и npm для Camofox.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def _fake_managed_node(home: Path) -> Path:
    """Создать дерево managed Node, как его кладёт рецепт установки."""
    bin_dir = home / "node" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("node", "npm", "npx"):
        tool = bin_dir / name
        tool.write_text("#!/usr/bin/env node\n")
        tool.chmod(0o755)
    return bin_dir


class _Recorder:
    """Подменяет subprocess.run и запоминает, с чем его звали."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[dict] = []
        self.returncode = returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": list(cmd), "kwargs": kwargs})

        class _Result:
            returncode = self.returncode
            stdout = ""
            stderr = ""

        return _Result()

    def call_with(self, *needles: str) -> dict:
        """Единственный вызов, чья команда содержит все *needles*."""
        found = [
            call
            for call in self.calls
            if all(any(needle in part for part in call["cmd"]) for needle in needles)
        ]
        assert len(found) == 1, (
            f"ожидался ровно один запуск с {needles}, найдено {len(found)}: "
            f"{[c['cmd'] for c in self.calls]}"
        )
        return found[0]


def _path_entries(call: dict) -> list[str]:
    env = call["kwargs"].get("env")
    assert env is not None, (
        "подпроцесс запущен без env — значит наследует PATH процесса, "
        "в котором managed Node нет"
    )
    return [p for p in env.get("PATH", "").split(os.pathsep) if p]


@pytest.fixture
def managed_node_env(tmp_path, monkeypatch):
    """HERMES_HOME с managed Node и пустой корень проекта."""
    import hermes_constants
    from hermes_cli import tools_config

    home = tmp_path / "hermes-home"
    home.mkdir()
    bin_dir = _fake_managed_node(home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    project_root = tmp_path / "install-dir"
    project_root.mkdir()
    monkeypatch.setattr(tools_config, "PROJECT_ROOT", project_root)

    # Поиск инструмента не проверяем — он и так работал. Проверяем запуск.
    monkeypatch.setattr(
        hermes_constants,
        "find_node_executable",
        lambda command: str(bin_dir / command),
    )

    return {"home": home, "bin_dir": bin_dir, "project_root": project_root}


@pytest.mark.skipif(sys.platform == "win32", reason="шапка #!/usr/bin/env — POSIX")
class TestPostSetupSpawnsNodeToolsWithManagedPath:
    def test_npm_install_for_agent_browser_sees_managed_node(
        self, managed_node_env, monkeypatch
    ):
        """npm ставит agent-browser — и обязан получить PATH с нашим node."""
        import subprocess

        from hermes_cli import tools_config

        recorder = _Recorder()
        monkeypatch.setattr(subprocess, "run", recorder)
        # Chromium уже на месте — ветка npx в этом тесте не участвует.
        monkeypatch.setattr(
            "tools.browser_tool._chromium_installed", lambda: True, raising=False
        )

        tools_config._run_post_setup_impl("agent_browser")

        call = recorder.call_with("npm", "install")
        assert str(managed_node_env["bin_dir"]) in _path_entries(call)

    def test_npx_chromium_install_sees_managed_node(
        self, managed_node_env, monkeypatch
    ):
        """npx ставит Chromium — тот же скрипт с шапкой, та же беда."""
        import subprocess

        from hermes_cli import tools_config

        recorder = _Recorder()
        monkeypatch.setattr(subprocess, "run", recorder)
        monkeypatch.setattr(
            "tools.browser_tool._chromium_installed", lambda: False, raising=False
        )
        monkeypatch.setattr(
            "tools.browser_tool._running_in_docker", lambda: False, raising=False
        )

        tools_config._run_post_setup_impl("agent_browser")

        call = recorder.call_with("agent-browser", "install")
        assert str(managed_node_env["bin_dir"]) in _path_entries(call)

    def test_npm_install_for_camofox_sees_managed_node(
        self, managed_node_env, monkeypatch
    ):
        """Третий запуск того же класса — Camofox."""
        import subprocess

        from hermes_cli import tools_config

        recorder = _Recorder()
        monkeypatch.setattr(subprocess, "run", recorder)

        tools_config._run_post_setup_impl("camofox")

        call = recorder.call_with("npm", "camofox-browser")
        assert str(managed_node_env["bin_dir"]) in _path_entries(call)

    def test_existing_path_is_kept(self, managed_node_env, monkeypatch):
        """PATH дополняется, а не подменяется: /usr/bin обязан уцелеть.

        Иначе npm потеряет git, python и всё остальное, что ему нужно для
        сборки нативных пакетов, — и мы поменяем один отказ на другой.
        """
        import subprocess

        from hermes_cli import tools_config

        recorder = _Recorder()
        monkeypatch.setattr(subprocess, "run", recorder)
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
        monkeypatch.setattr(
            "tools.browser_tool._chromium_installed", lambda: True, raising=False
        )

        tools_config._run_post_setup_impl("agent_browser")

        entries = _path_entries(recorder.call_with("npm", "install"))
        assert str(managed_node_env["bin_dir"]) in entries
        assert "/usr/bin" in entries
        assert entries.index(str(managed_node_env["bin_dir"])) < entries.index("/usr/bin")
