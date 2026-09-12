"""Сторож numpy: он обязан называть причину, по которой не починил.

Тестов на этот модуль не было вовсе — файл заведён 2026-09-06, когда
живой прогон показал, что сторож глотает провал установщика и выдаёт
клиенту причину, которой не было.
"""

class TestInstallerFailureIsNamed:
    """Найдено на живой машине 2026-09-06 (VM 31.29.151.3).

    Сторож не смотрел на код возврата установщика: uv падал с
    `Permission denied` на venv, принадлежащем root, а клиент читал
    «откат на 1.x не помог» — то есть про несовместимость процессора — и
    шёл повторять `hermes doctor --fix`, который упадёт так же. Совет,
    который нельзя выполнить, хуже отсутствия совета.
    """

    def _run_with(self, monkeypatch, returncode, stderr):
        import subprocess as sp

        from hermes_cli import trix_numpy_guard as g

        monkeypatch.setattr(g, "_numpy_present", lambda exe: True)
        monkeypatch.setattr(g, "_numpy_imports", lambda exe: False)
        monkeypatch.setattr(g, "_installer_command", lambda exe: ["true"])
        monkeypatch.setattr(
            g.subprocess, "run",
            lambda *a, **k: sp.CompletedProcess(
                args=["true"], returncode=returncode, stdout="", stderr=stderr,
            ),
        )
        return g.ensure_runnable_numpy("/x/python")

    def test_permission_denied_is_reported_as_permissions(self, monkeypatch):
        r = self._run_with(
            monkeypatch, 2,
            "error: failed to remove file `/usr/local/.../bin/f2py`: "
            "Permission denied (os error 13)",
        )
        assert r.ok is False
        assert "прав на запись" in r.message
        # Главное: причина НЕ названа свойством процессора.
        assert "откат на 1.x не помог" not in r.message

    def test_read_only_filesystem_lands_in_the_same_bucket(self, monkeypatch):
        r = self._run_with(monkeypatch, 1, "OSError: Read-only file system")
        assert "прав на запись" in r.message

    def test_other_failures_name_the_exit_code_not_the_cpu(self, monkeypatch):
        r = self._run_with(monkeypatch, 7, "network unreachable")
        assert "код 7" in r.message
        assert "откат на 1.x не помог" not in r.message

    def test_clean_run_that_still_does_not_import_stays_a_cpu_verdict(
        self, monkeypatch,
    ):
        """Установщик отработал успешно, а numpy всё равно не грузится —
        вот это и есть настоящая несовместимость, её формулировку не
        трогаем."""
        r = self._run_with(monkeypatch, 0, "")
        assert "откат на 1.x не помог" in r.message


class TestClientFacingReport:
    """Текст, который читает клиент, обязан совпадать с причиной.

    Отчёт раньше строился независимо от вердикта сторожа, поэтому на
    машине с правами на venv клиент читал про процессор и получал совет
    `hermes doctor --fix`, который зовёт того же сторожа и упадёт так же.
    """

    def _lines(self, message, ok=False, repaired=True):
        from types import SimpleNamespace

        from hermes_cli.trix_update_numpy import numpy_report_lines

        return numpy_report_lines(
            SimpleNamespace(checked=True, ok=ok, repaired=repaired, message=message),
        )

    def test_permission_case_names_permissions_and_drops_the_dead_advice(self):
        lines = self._lines(
            "numpy не запускается на этом процессоре, а заменить его нечем: "
            "нет прав на запись в окружение агента.",
        )
        text = "\n".join(lines)
        assert "прав на запись" in text
        assert "hermes doctor --fix" not in text
        assert "поддержку" in text

    def test_genuine_cpu_case_still_offers_the_retry(self):
        text = "\n".join(self._lines(
            "numpy не запускается на этом процессоре, и откат на 1.x не помог.",
        ))
        assert "hermes doctor --fix" in text

    def test_healthy_machine_stays_silent(self):
        assert self._lines("numpy импортируется.", ok=True, repaired=False) == []

    def test_missing_message_still_produces_a_usable_report(self):
        """Сторож мог не заполнить message — отчёт не должен опустеть."""
        text = "\n".join(self._lines(""))
        assert "Распознавание голосовых" in text
