"""Camofox должен подниматься службой, а не советом в консоли.

Установочный шаг Camofox ставил npm-пакет и ПЕЧАТАЛ «запустите
`npx @askjo/camofox-browser`». Инструкция адресована человеку с консолью,
а у клиента её нет по устройству продукта: ни SSH, ни терминала,
единственная дверь — мастер настройки. Проверено на клиентской машине
2026-09-04: после выбора Camofox порт 9377 не слушал никто, и браузерные
инструменты в этом режиме были обречены на сетевую ошибку при первом же
вызове. То есть вариант, предлагаемый мастером наравне с остальными,
довести до рабочего состояния было нельзя в принципе.

Здесь проверяется то, что можно проверить без systemd: устройство юнита и
поведение на отказах. Что служба реально поднимается, переживает
перезагрузку и отвечает на порту — проверено исполнением на живой машине,
это не задача модульного теста.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import trix_camofox_service as svc


class TestUnitFile:
    def test_execstart_names_the_installed_binary_not_npx(self, tmp_path):
        """`npx` в юните — плохой выбор: он идёт в сеть при каждом старте
        и молча падает, когда сети нет. Пакет к этому моменту уже стоит
        рядом, запускать надо ровно его."""
        binary = tmp_path / "node_modules" / ".bin" / "camofox-browser"
        text = svc.unit_file_text(binary, None)
        assert f"ExecStart={binary}" in text
        assert "npx" not in text

    def test_port_is_passed_through_the_environment(self, tmp_path):
        text = svc.unit_file_text(tmp_path / "camofox-browser", None, port=9999)
        assert 'Environment="CAMOFOX_PORT=9999"' in text

    def test_node_directory_is_named_explicitly_when_known(self, tmp_path):
        """У пользовательской службы PATH куда беднее, чем у интерактивной
        сессии, а бинарь — js-скрипт с шебангом на `node`. Тот же класс, из
        за которого юнит шлюза когда-то «плавал» по тому, какой node
        подвернулся."""
        text = svc.unit_file_text(tmp_path / "camofox-browser", Path("/opt/node/bin"))
        assert "/opt/node/bin" in text

    def test_no_path_line_when_node_directory_is_unknown(self, tmp_path):
        """Пустой `Environment="PATH="` был бы хуже отсутствия строки —
        он обрезал бы службе PATH до пустоты."""
        text = svc.unit_file_text(tmp_path / "camofox-browser", None)
        assert "PATH=" not in text

    def test_the_unit_restarts_itself_and_survives_a_reboot(self, tmp_path):
        text = svc.unit_file_text(tmp_path / "camofox-browser", None)
        assert "Restart=on-failure" in text
        assert "WantedBy=default.target" in text


class TestFailuresAreReportedNotRaised:
    """Это установочный шаг мастера: его провал не должен ронять настройку
    целиком — клиент обязан увидеть предупреждение и сохранённые
    настройки, а не пустой экран."""

    def test_missing_package_is_a_verdict_not_an_exception(self, tmp_path):
        result = svc.ensure_camofox_service(tmp_path)
        assert result.ok is False
        assert "не установлен" in result.message

    def test_systemctl_failure_is_reported_with_its_reason(self, tmp_path, monkeypatch):
        binary = tmp_path / "node_modules" / ".bin" / "camofox-browser"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(svc, "unit_path", lambda: tmp_path / "unit" / svc.SERVICE_NAME)

        import subprocess

        def fake(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Failed to connect to bus\n"
            )

        monkeypatch.setattr(svc.subprocess, "run", fake)
        result = svc.ensure_camofox_service(tmp_path)
        assert result.ok is False
        assert "Failed to connect to bus" in result.message

    def test_a_started_service_whose_port_never_answers_is_not_a_success(
        self, tmp_path, monkeypatch
    ):
        """`active` у Type=simple означает лишь «процесс стартовал».
        Camofox после этого поднимает виртуальный дисплей и на первом
        запуске тянет движок — пока порт молчит, браузер не работает."""
        binary = tmp_path / "node_modules" / ".bin" / "camofox-browser"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(svc, "unit_path", lambda: tmp_path / "unit" / svc.SERVICE_NAME)

        import subprocess

        monkeypatch.setattr(
            svc.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(svc, "_port_answers", lambda port, wait: False)

        result = svc.ensure_camofox_service(tmp_path, wait_s=1)
        assert result.ok is False
        assert "не ответил" in result.message

    def test_success_requires_the_port_to_actually_answer(self, tmp_path, monkeypatch):
        binary = tmp_path / "node_modules" / ".bin" / "camofox-browser"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(svc, "unit_path", lambda: tmp_path / "unit" / svc.SERVICE_NAME)

        import subprocess

        monkeypatch.setattr(
            svc.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=a, returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(svc, "_port_answers", lambda port, wait: True)

        result = svc.ensure_camofox_service(tmp_path)
        assert result.ok is True
        assert "отвечает" in result.message

    def test_an_unexpected_error_still_returns_a_verdict(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("что угодно")

        monkeypatch.setattr(svc, "_server_binary", boom)
        result = svc.ensure_camofox_service(tmp_path)
        assert result.ok is False
        assert "Не удалось поднять" in result.message
