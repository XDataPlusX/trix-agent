"""Проверка браузера обязана смотреть на выбор клиента, а не на один режим.

Раньше `check_chromium_backend()` безусловно спрашивала про локальный
Chromium, потому что клиентский шаблон конфига фиксирует
`browser.backend: "off"`. Но мастер позволяет выбрать и другое, и тогда
вердикт был неверным в ОБЕ стороны — оба случая сняты с живых машин
2026-09-04:

- выбран **Browser Use** — проверка всё равно требовала Chromium, не
  находила его и объявляла неполадку; проход поддержки превращал это в
  «напишите в поддержку» после успешной настройки;
- выбран **Camofox** — проверка находила локальный Chromium и
  рапортовала «всё хорошо», МАСКИРУЯ то, что Camofox не установлен и его
  сервер никто не поднял.

Второй случай опаснее первого: там, где надо было предупредить, проверка
успокаивала.
"""

from __future__ import annotations

import pytest

from hermes_cli import browser_preflight


@pytest.fixture
def no_camofox(monkeypatch):
    """По умолчанию Camofox выключен — иначе он перебивает любой выбор."""
    monkeypatch.setattr(
        "tools.browser_camofox.is_camofox_mode", lambda: False, raising=False
    )


def _set_backend(monkeypatch, backend):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda *a, **kw: {"browser": {"backend": backend}},
        raising=False,
    )


class TestBackendResolution:
    def test_camofox_wins_over_the_config_field(self, monkeypatch):
        """Camofox включается переменной окружения, а не полем конфига,
        поэтому при включённом Camofox значение `browser.backend` (у него
        то же самое `"off"`, что и у Chromium) ничего не решает."""
        _set_backend(monkeypatch, "off")
        monkeypatch.setattr(
            "tools.browser_camofox.is_camofox_mode", lambda: True, raising=False
        )
        assert browser_preflight._selected_browser_backend() == "camofox"

    def test_empty_or_missing_backend_reads_as_local_chromium(self, monkeypatch, no_camofox):
        _set_backend(monkeypatch, "")
        assert browser_preflight._selected_browser_backend() == "off"

    def test_unreadable_config_never_raises(self, monkeypatch, no_camofox):
        """У проверки перед запуском нет права уронить установку."""
        def boom(*a, **kw):
            raise RuntimeError("конфиг не читается")

        monkeypatch.setattr("hermes_cli.config.load_config", boom, raising=False)
        assert browser_preflight._selected_browser_backend() == "off"


class TestVerdictFollowsTheChoice:
    def test_a_cloud_or_cli_backend_is_not_judged_by_chromium(self, monkeypatch, no_camofox):
        """Это и есть случай Browser Use: Chromium ему не нужен, и его
        отсутствие не является неполадкой машины."""
        _set_backend(monkeypatch, "browser-use")
        monkeypatch.setattr(
            "tools.browser_tool.check_browser_requirements",
            lambda: (_ for _ in ()).throw(AssertionError("Chromium не должен проверяться")),
            raising=False,
        )
        result = browser_preflight.check_chromium_backend()
        assert result.ok is True
        # Молчаливое «ok» с пустым текстом было бы не лучше прежней лжи:
        # читающий отчёт должен понять, ПОЧЕМУ про Chromium ничего не сказано.
        assert "browser-use" in result.message

    def test_local_chromium_mode_still_reports_a_missing_chromium(self, monkeypatch, no_camofox):
        """Обратная сторона: «не судить по Chromium» не должно превратиться
        в «никогда ни на что не жаловаться»."""
        _set_backend(monkeypatch, "off")
        monkeypatch.setattr(
            "tools.browser_tool.check_browser_requirements", lambda: False, raising=False
        )
        result = browser_preflight.check_chromium_backend()
        assert result.ok is False
        assert "Chromium" in result.message

    def test_local_chromium_mode_reports_success_when_present(self, monkeypatch, no_camofox):
        _set_backend(monkeypatch, "off")
        monkeypatch.setattr(
            "tools.browser_tool.check_browser_requirements", lambda: True, raising=False
        )
        assert browser_preflight.check_chromium_backend().ok is True


class TestCamofoxIsJudgedByItsOwnServer:
    def _camofox(self, monkeypatch, *, url, alive):
        monkeypatch.setattr(
            "tools.browser_camofox.is_camofox_mode", lambda: True, raising=False
        )
        monkeypatch.setattr(
            "tools.browser_camofox.get_camofox_url", lambda: url, raising=False
        )
        monkeypatch.setattr(
            "tools.browser_camofox.check_camofox_available", lambda: alive, raising=False
        )

    def test_a_dead_camofox_server_is_a_failure_not_a_success(self, monkeypatch):
        """Самый опасный из двух случаев: прежняя проверка находила
        локальный Chromium и говорила «готово» про неработающий Camofox."""
        self._camofox(monkeypatch, url="http://localhost:9377", alive=False)
        monkeypatch.setattr(
            "tools.browser_tool.check_browser_requirements", lambda: True, raising=False
        )
        result = browser_preflight.check_chromium_backend()
        assert result.ok is False
        assert "Camofox" in result.message
        assert "9377" in result.message

    def test_a_live_camofox_server_is_a_success(self, monkeypatch):
        self._camofox(monkeypatch, url="http://localhost:9377", alive=True)
        result = browser_preflight.check_chromium_backend()
        assert result.ok is True
        assert "Camofox" in result.message

    def test_camofox_selected_without_an_address_is_a_failure(self, monkeypatch):
        self._camofox(monkeypatch, url="", alive=True)
        result = browser_preflight.check_chromium_backend()
        assert result.ok is False
