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
