"""ddgs search-backend preflight check for the Trix Agent installer.

The curated config template (``assets/config/trix-config.yaml``) sets
``web.search_backend: ddgs`` — DuckDuckGo search via the ``ddgs`` PyPI
package, the only search backend that needs neither an API key nor a side
service (spec §4.2). ``scripts/install.sh``'s ``install_ddgs_search_backend()``
eager-installs it (the ``.[ddgs]`` extra), but — unlike Docker and Chromium,
whose outcomes ARE reported at the tail of ``print_success()``
(``hermes_cli/docker_preflight.py``, ``hermes_cli/browser_preflight.py``) —
nothing previously reported whether that install actually succeeded.

That gap matters more than it looks because of how the backend resolves at
runtime: ``_get_capability_backend("search")`` in ``tools/web_tools.py``
asks the configured backend's ``is_available()`` and, on a negative answer,
silently falls through to the ``firecrawl`` default — so an install that
failed silently (``uv`` missing, Termux, a transient network error,
``pip install -e ".[ddgs]"`` erroring) turns "free DuckDuckGo search" into
"buy a Firecrawl key / log into Nous Portal", and the client is the one who
finds out, not us. This is the same failure class the Chromium preflight
already exists to close for ``browser_*`` — see its own module docstring,
which named this exact gap as still open. It is also NOT closable at
runtime the way exa/firecrawl/parallel close it: ``DDGSWebSearchProvider``
(``plugins/web/ddgs/provider.py``) cannot self-heal in ``search()`` because
its ``is_available()`` gate checks the very import a lazy install would
need to repair, so an install-time failure is only ever caught here, at
install time.

:func:`check_ddgs_backend` reuses the SAME chokepoint the runtime resolver
uses (``tools.web_tools._is_backend_available("ddgs")``) so this report can
never disagree with what ``web_search`` actually gets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class DdgsPreflightResult:
    """Outcome of checking whether ``web.search_backend: ddgs`` will
    actually resolve at runtime instead of silently falling through to the
    ``firecrawl`` default."""

    ok: bool
    message: str

    def to_dict(self) -> dict:
        """Machine-readable form for programmatic callers.

        Same shape as :meth:`hermes_cli.docker_preflight.DockerPreflightResult.to_dict`
        and :meth:`hermes_cli.browser_preflight.BrowserPreflightResult.to_dict` --
        ``check``, ``ok``, ``message``, ``details`` -- so a caller (e.g. the
        future spec 12 support page) can iterate all three preflight checks
        uniformly instead of special-casing each module's field names.
        This check has no extra structured fields beyond ``ok``/``message``,
        so ``details`` is empty.
        """
        return {"check": "ddgs", "ok": self.ok, "message": self.message, "details": {}}


def check_ddgs_backend() -> DdgsPreflightResult:
    """Report whether the ``ddgs`` search backend is actually usable.

    Delegates to ``tools.web_tools._is_backend_available("ddgs")`` — the
    exact check ``_get_capability_backend()`` uses to decide whether to
    honor the configured backend or silently fall through to firecrawl. A
    local import keeps ``tools.web_tools`` out of anything that imports
    this module for reasons other than running the installer's preflight
    report.
    """
    from tools.web_tools import _is_backend_available

    if _is_backend_available("ddgs"):
        return DdgsPreflightResult(
            ok=True,
            message="ddgs (поиск DuckDuckGo без ключа и без отдельного сервиса) установлен и готов к работе.",
        )

    return DdgsPreflightResult(
        ok=False,
        message=(
            "Пакет ddgs не установлен. Конфигурация выбирает "
            "web.search_backend: ddgs (поиск без ключа и без отдельного "
            "сервиса), а без пакета веб-поиск молча переключается на "
            "другой бэкенд по умолчанию и вместо рабочего поиска предложит "
            "клиенту платный ключ Firecrawl. Установите вручную: cd "
            "<каталог установки> && uv pip install -e '.[ddgs]' (или "
            "запустите hermes tools и заново выберите DuckDuckGo (ddgs))."
        ),
    )


# ─── Проверка ФАКТИЧЕСКИ выбранного бэкенда (разбор 2026-09-04) ─────────────
#
# check_ddgs_backend() выше проверяет одно: импортируется ли пакет ddgs.
# Это осмысленно для scripts/install.sh (он спрашивает "встала ли установка
# ddgs, которую я только что попытался сделать" — вопрос про конкретный
# пакет сразу после попытки его поставить) и это НЕ то же самое, что
# "жив ли бэкенд, который реально выбран в конфиге прямо сейчас". Разбор
# нашёл, что _check_search() в trix_support.py (проход поддержки) годами
# спрашивал первое там, где ему нужен был ответ на второе:
#
# - клиент на SearXNG получал "всё хорошо" за наличие пакета ddgs, которым
#   он не пользуется, даже когда SearXNG лежит;
# - клиент на Brave с рабочим ключом получал "не работает" без причины,
#   если ddgs просто не установлен.
#
# check_search_backend() ниже — замена ИМЕННО для прохода поддержки:
# 1) берёт бэкенд тем же способом, что и рантайм
#    (tools.web_tools._get_search_backend() — тот же вызов, что делает
#    _get_capability_backend("search") перед каждым web_search);
# 2) делает живой запрос через tools.web_tools.web_search_tool() — ТУ ЖЕ
#    функцию, которую вызывает модель, а не провайдера напрямую (см.
#    докстринг check_search_backend() ниже про то, почему);
# 3) называет бэкенд и причину в сообщении.
#
# check_ddgs_backend()/DdgsPreflightResult не трогаем — install.sh и их
# собственные тесты завязаны на буквальном "ddgs".


@dataclass(frozen=True)
class SearchBackendPreflightResult:
    """Итог живой проверки ФАКТИЧЕСКИ выбранного веб-поисковика.

    В отличие от :class:`DdgsPreflightResult` (проверка одного пакета),
    здесь ``backend`` — имя реально сработавшего/проверенного бэкенда, а
    не всегда ``"ddgs"``."""

    ok: bool
    message: str
    backend: str = ""

    def to_dict(self) -> dict:
        """Та же форма ``check/ok/message/details``, что у
        :meth:`DdgsPreflightResult.to_dict` и соседних preflight-модулей —
        ``details`` несёт имя бэкенда, а не пустой словарь, потому что это
        и есть та структурная деталь, которой не было у старой проверки."""
        return {
            "check": "search",
            "ok": self.ok,
            "message": self.message,
            "details": {"backend": self.backend},
        }


# Короткий, широко проиндексированный запрос: цель — не содержание ответа,
# а сам факт "хотя бы один результат пришёл". Общий предмет, а не что-то
# специфичное для региона/языка, чтобы любой бэкенд (DDG, SearXNG, Brave,
# Tavily, Exa, ...) на него ответил.
_SEARCH_HEALTH_QUERY = "python programming language"

# Дефолтный жёсткий дедлайн живого запроса. Меньше внешнего таймаута
# SupportAction("search", ..., 15.0) в trix_support.py, чтобы наша же
# понятная причина ("не ответил за N с") успела сработать раньше, чем
# внешний _execute() молча оборвёт ожидание общей фразой.
_SEARCH_HEALTH_TIMEOUT_S = 12.0


def _resolve_configured_search_backend() -> str:
    """Имя бэкенда тем же способом, что и рантайм веб-поиска.

    Обёрнуто в try/except: у preflight-проверки нет права падать из-за
    нечитаемого конфига — это тот же принцип, что и у
    ``browser_preflight._selected_browser_backend()``.
    """
    try:
        from tools.web_tools import _get_search_backend

        return (_get_search_backend() or "").strip()
    except Exception:
        return ""


def _run_live_search_with_timeout(query: str, limit: int, timeout_s: float):
    """Запускает ``tools.web_tools.web_search_tool`` в отдельном потоке с
    жёстким дедлайном по факту ожидания.

    ``web_search_tool`` синхронна и не принимает собственный таймаут, а
    сами провайдеры несут разные внутренние бюджеты (ddgs — секунды,
    firecrawl/tavily/xai — десятки секунд), так что полагаться на их
    внутренний таймаут нельзя: проверка обязана иметь СВОЙ жёсткий предел
    независимо от того, что решит провайдер.

    Поток создаётся ``daemon=True`` умышленно: если поисковик реально
    завис дольше ``timeout_s``, проверка не обязана ждать его вечно и не
    должна блокировать завершение процесса (в отличие от
    ``ThreadPoolExecutor``, чьи воркеры не демоны и держат интерпретатор
    до своего завершения). Мы просто перестаём ждать и честно
    докладываем таймаут; осиротевший поток либо тихо вернётся в никуда,
    либо уйдёт вместе с процессом.

    Возвращает JSON-строку ответа или ``None``, если дедлайн истёк.
    Исключение из самого запроса пробрасывается вызывающему.
    """
    import queue
    import threading

    result_queue: "queue.Queue" = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            from tools.web_tools import web_search_tool

            result_queue.put(("ok", web_search_tool(query, limit)))
        except Exception as exc:  # noqa: BLE001 — донести причину наверх
            result_queue.put(("error", exc))

    thread = threading.Thread(target=_worker, daemon=True, name="search-preflight-live-check")
    thread.start()
    try:
        status, payload = result_queue.get(timeout=timeout_s)
    except queue.Empty:
        return None
    if status == "error":
        raise payload
    return payload


def check_search_backend(timeout_s: float = _SEARCH_HEALTH_TIMEOUT_S) -> SearchBackendPreflightResult:
    """Живая проверка ФАКТИЧЕСКИ выбранного веб-поисковика.

    Вызывает ``tools.web_tools.web_search_tool`` целиком, а не провайдера
    напрямую — сознательный выбор, а не лень:

    1. ``web_search_tool`` сама резолвит бэкенд через
       ``_get_search_backend()`` / реестр провайдеров, включая ветки
       "провайдер отключён как плагин" и "провайдер не зарегистрирован" —
       дублировать этот резолвинг здесь означало бы второй источник
       правды, который может разойтись с рантаймом (ровно тот класс
       дефекта, который эта проверка чинит).
    2. Она уже прогоняет ответ через ``_reject_empty_search_success`` —
       "200 и ноль результатов" превращается в честный отказ ДО того, как
       ответ попадёт сюда. Не дублируем и эту логику: она принадлежит
       модулю, который её ввёл, и заново её здесь писать значило бы два
       места, которые нужно чинить синхронно при следующем изменении
       контракта пустой выдачи.

    Обратная сторона: сообщение при успехе не содержит имени бэкенда (его
    туда не кладёт сам ``web_search_tool``), поэтому имя бэкенда для
    текста берём отдельно, тем же вызовом (``_get_search_backend``),
    которым пользуется рантайм — с ним расхождения быть не может, это
    один и тот же чекпоинт.

    Никогда не бросает — как и у ``check_ddgs_backend``/
    ``check_chromium_backend``, у preflight-проверки нет права уронить
    установку или проход поддержки.
    """
    backend = _resolve_configured_search_backend()
    if not backend:
        return SearchBackendPreflightResult(
            ok=False,
            message=(
                "Не удалось определить, какой веб-поисковик выбран "
                "(web.search_backend/web.backend не читаются или пусты)."
            ),
            backend="",
        )

    try:
        raw = _run_live_search_with_timeout(_SEARCH_HEALTH_QUERY, 1, timeout_s)
    except Exception as exc:  # noqa: BLE001 — донести причину клиенту/оператору
        return SearchBackendPreflightResult(
            ok=False,
            message=f"Поисковик '{backend}' упал с ошибкой при тестовом запросе: {exc}",
            backend=backend,
        )

    if raw is None:
        return SearchBackendPreflightResult(
            ok=False,
            message=f"Поисковик '{backend}' не ответил за {timeout_s:g} с (таймаут).",
            backend=backend,
        )

    try:
        data = json.loads(raw)
    except Exception:
        return SearchBackendPreflightResult(
            ok=False,
            message=f"Поисковик '{backend}' вернул нечитаемый (не-JSON) ответ на тестовый запрос.",
            backend=backend,
        )

    if isinstance(data, dict) and data.get("success"):
        return SearchBackendPreflightResult(
            ok=True,
            message=f"Поисковик '{backend}' отвечает и возвращает результаты.",
            backend=backend,
        )

    error = (data.get("error") if isinstance(data, dict) else None) or "неизвестная ошибка"
    return SearchBackendPreflightResult(
        ok=False,
        message=f"Поисковик '{backend}' не прошёл проверку: {error}",
        backend=backend,
    )
