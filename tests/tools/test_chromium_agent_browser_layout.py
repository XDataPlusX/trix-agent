"""Chromium, установленный самим agent-browser, должен обнаруживаться.

Найдено исполнением на чистой клиентской VM (2026-09-03). `agent-browser
install` отработал успешно и напечатал:

    ✓ Chrome 152.0.7977.75 installed successfully
      Location: ~/.agent-browser/browsers/chrome-152.0.7977.75

а проверка доступности браузера в тот же момент отвечала «не найден», и
кэш Playwright на машине вообще отсутствовал. То есть установщик клал
рабочий браузер, а продукт его не видел — и весь набор `browser_*` молча
исчезал из схемы модели, без единого сообщения об ошибке.

Причина: поиск знал два места (переменная с явным путём и кэш Playwright)
и два имени каталога (`chromium-*`, `chromium_headless_shell-*`). Нынешний
agent-browser качает Chrome for Testing в свой собственный каталог и
называет его `chrome-<версия>` — ни одно из условий не срабатывало.

Тесты держатся за наблюдаемое свойство «браузер на диске в том виде, в
каком его кладёт установщик, считается установленным», а не за список
каталогов внутри функции.
"""

import os

import pytest

import tools.browser_tool as bt


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Полная изоляция от машины, на которой идёт прогон: свой HOME, пустой
    PATH и сброшенный кэш — иначе тест на «не найден» проходил бы или падал
    в зависимости от того, стоит ли браузер у разработчика."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setattr(bt.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(bt, "_cached_chromium_installed", None, raising=False)
    yield
    monkeypatch.setattr(bt, "_cached_chromium_installed", None, raising=False)


def _install_agent_browser_chrome(home, version="152.0.7977.75", executable=True):
    d = home / ".agent-browser" / "browsers" / f"chrome-{version}"
    d.mkdir(parents=True)
    binary = d / "chrome"
    binary.write_text("#!/bin/sh\nexit 0\n")
    if executable:
        binary.chmod(0o755)
    return binary


def test_browser_installed_by_agent_browser_is_found(tmp_path):
    """Ровно раскладка с живой машины."""
    _install_agent_browser_chrome(tmp_path)
    assert bt._chromium_installed() is True


def test_nothing_installed_is_still_reported_missing(tmp_path):
    """Обратная сторона: пустая машина по-прежнему честно даёт «нет».
    Без этой проверки первый тест был бы зелёным и при функции,
    возвращающей True всегда."""
    assert bt._chromium_installed() is False


def test_directory_without_a_usable_binary_is_not_counted(tmp_path):
    """Каталог получает финальное имя с самого начала загрузки, поэтому
    одного имени мало: недокачанная установка не должна выдаваться за
    рабочую. Ложное «есть» хуже ложного «нет» — инструменты попадут в
    схему модели и откажут при использовании."""
    _install_agent_browser_chrome(tmp_path, executable=False)
    assert bt._chromium_installed() is False


def test_playwright_layout_still_works(tmp_path):
    """Прежний путь не сломан: каталог Playwright по-прежнему считается
    установленным браузером."""
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1208").mkdir(parents=True)
    assert bt._chromium_installed() is True
