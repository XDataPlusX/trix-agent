"""``check_search_backend()`` — здоровье РЕАЛЬНО выбранного веб-поисковика.

Разбор 2026-09-04 нашёл, что старая проверка (``check_ddgs_backend()``,
вызываемая из прохода поддержки как ``_check_search``) проверяла ровно
одно: импортируется ли пакет ``ddgs``. Она не смотрела на
``web.search_backend`` и не делала ни одного живого запроса. Из-за этого:

- клиент на SearXNG получал зелёную галочку за наличие пакета ddgs,
  которым он не пользуется, даже когда SearXNG лежит;
- клиент на Brave с рабочим ключом получал красную проверку без
  причины, если ddgs просто не установлен.

``check_search_backend()`` чинит оба случая: берёт бэкенд тем же
способом, что и рантайм (``tools.web_tools._get_search_backend()``), и
дёргает ``tools.web_tools.web_search_tool`` — ТУ ЖЕ функцию, которую
вызывает модель, — с коротким тестовым запросом. Успех и неуспех
проверяются через границу этой функции (замоканную), а не через
настоящую сеть — детали см. в docstring самого модуля.

Старый ``check_ddgs_backend()`` (проверка импортируемости пакета ddgs)
остаётся нетронутым: он используется ``scripts/install.sh`` сразу после
попытки установить ddgs — это другой вопрос ("успела ли встать
установка пакета только что"), не "жив ли выбранный клиентом бэкенд
сейчас", и его тесты (``test_search_preflight.py``) не трогаем.
"""

from __future__ import annotations

import json

from hermes_cli.search_preflight import check_search_backend


def _ok_response(n: int = 1) -> str:
    return json.dumps(
        {
            "success": True,
            "data": {
                "web": [
                    {"title": f"r{i}", "url": f"https://example.com/{i}", "description": "", "position": i}
                    for i in range(n)
                ]
            },
        }
    )


def _empty_success_turned_failure(backend: str) -> str:
    """То, что реально возвращает ``web_search_tool`` для пустой выдачи —
    ``_reject_empty_search_success`` в tools/web_tools.py уже превращает
    её в отказ ДО того, как ответ попадает сюда."""
    return json.dumps(
        {
            "success": False,
            "error": (
                f"Поисковик '{backend}' ответил, но не вернул ни одного "
                f"результата по запросу «test»."
            ),
            "data": {"web": []},
        }
    )


class TestNonDdgsBackendAlive:
    """Клиент выбрал НЕ ddgs (например SearXNG), и он реально отвечает."""

    def test_ok_when_chosen_backend_returns_results(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_get_search_backend", lambda: "searxng")
        monkeypatch.setattr(wt, "web_search_tool", lambda query, limit=5: _ok_response())

        result = check_search_backend()

        assert result.ok is True
        assert "searxng" in result.message.lower()


class TestNonDdgsBackendDownOrEmpty:
    """Клиент выбрал НЕ ddgs, а он лёг или отвечает пустой выдачей."""

    def test_not_ok_when_chosen_backend_returns_empty(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_get_search_backend", lambda: "searxng")
        monkeypatch.setattr(
            wt, "web_search_tool", lambda query, limit=5: _empty_success_turned_failure("searxng")
        )

        result = check_search_backend()

        assert result.ok is False
        assert "searxng" in result.message.lower()

    def test_not_ok_when_chosen_backend_errors_outright(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_get_search_backend", lambda: "brave-free")
        monkeypatch.setattr(
            wt,
            "web_search_tool",
            lambda query, limit=5: json.dumps({"success": False, "error": "401 Unauthorized"}),
        )

        result = check_search_backend()

        assert result.ok is False
        assert "brave-free" in result.message.lower()
        assert "unauthorized" in result.message.lower() or "401" in result.message

    def test_not_ok_when_chosen_backend_hangs_past_the_hard_timeout(self, monkeypatch):
        """Живой запрос обязан иметь жёсткий таймаут — зависший поисковик
        не должен вешать саму проверку."""
        import time

        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_get_search_backend", lambda: "searxng")

        def _hang(query, limit=5):
            time.sleep(2.0)
            return _ok_response()

        monkeypatch.setattr(wt, "web_search_tool", _hang)

        result = check_search_backend(timeout_s=0.1)

        assert result.ok is False
        assert "searxng" in result.message.lower()


class TestDdgsStillWorks:
    """ddgs остаётся рабочим случаем — фикс не сужает список бэкендов,
    а расширяет проверку на все."""

    def test_ddgs_still_reports_ok(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_get_search_backend", lambda: "ddgs")
        monkeypatch.setattr(wt, "web_search_tool", lambda query, limit=5: _ok_response())

        result = check_search_backend()

        assert result.ok is True
        assert "ddgs" in result.message.lower()

    def test_ddgs_still_reports_failure_with_its_own_name(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_get_search_backend", lambda: "ddgs")
        monkeypatch.setattr(
            wt,
            "web_search_tool",
            lambda query, limit=5: json.dumps({"success": False, "error": "package not installed"}),
        )

        result = check_search_backend()

        assert result.ok is False
        assert "ddgs" in result.message.lower()


class TestBackendUnresolvable:
    """Если даже имя бэкенда получить не удалось, проверка не падает, а
    возвращает честный отказ вместо необработанного исключения."""

    def test_reports_failure_without_raising(self, monkeypatch):
        import tools.web_tools as wt

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(wt, "_get_search_backend", _boom)

        result = check_search_backend()

        assert result.ok is False

    def test_to_dict_shape_matches_sibling_preflight_checks(self):
        """Обратная совместимость формы: check/ok/message/details — как у
        DockerPreflightResult/BrowserPreflightResult/DdgsPreflightResult."""
        from hermes_cli.search_preflight import SearchBackendPreflightResult

        payload = SearchBackendPreflightResult(ok=True, message="ok", backend="ddgs").to_dict()

        assert {"check", "ok", "message", "details"}.issubset(payload.keys())
        assert payload["ok"] is True
        assert payload["message"] == "ok"

        import json as _json

        _json.dumps(payload)  # JSON-serializable, как у соседей
