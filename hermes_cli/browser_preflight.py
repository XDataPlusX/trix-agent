"""Chromium preflight check for the Trix Agent installer.

The curated config template (``assets/config/trix-config.yaml``) fixes
``browser.backend: "off"``, which selects the built-in ``browser_*`` tools
running on top of a locally installed Chromium (Ruling 4 of
``docs/product/specs/2026-08-17-trix-agent-standard-build-design.md``).
Chromium install is attempted by ``scripts/install.sh`` (``install_node_deps()``
via Playwright), but nothing previously reported the *outcome* of that
attempt back to the operator at the end of the install.

That gap matters because of how the toolset actually resolves: the exact
``check_fn`` gating ``browser_navigate``/``browser_snapshot``/etc. in the
model's tool schema (``tools.browser_tool.check_browser_requirements``)
returns ``False`` whenever no Chromium build is found on disk, and that
happens BEFORE the model ever gets a chance to call a browser tool — so the
whole ``browser_*`` surface silently disappears from the schema with no
error text anywhere, not even the runtime auto-install/actionable-error
path that a live tool call would otherwise hit. Proven by direct
execution: with no Chromium anywhere on PATH/disk and no cloud/CLI browser
backend configured, ``registry.get_definitions(resolve_toolset("browser"))``
returns zero schema entries. This is the same class of defect that made
``web.search_backend: ddgs`` silently degrade to ``firecrawl`` without the
``ddgs`` package — except here nothing is left in the schema to even
degrade gracefully.

:func:`check_chromium_backend` reuses the SAME check the schema resolver
uses (:func:`tools.browser_tool.check_browser_requirements`) rather than
re-deriving Chromium-discovery logic, so this report can never drift out of
sync with what actually gates the tools. ``scripts/install.sh`` prints its
verdict in the same preflight report as the Docker check, at the tail of
``print_success()``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserPreflightResult:
    """Outcome of checking whether the local-Chromium browser_* tools will
    actually be advertised to the model."""

    ok: bool
    message: str

    def to_dict(self) -> dict:
        """Machine-readable form for programmatic callers.

        Same shape as :meth:`hermes_cli.docker_preflight.DockerPreflightResult.to_dict`
        and :meth:`hermes_cli.search_preflight.DdgsPreflightResult.to_dict` --
        ``check``, ``ok``, ``message``, ``details`` -- so a caller (e.g. the
        future spec 12 support page) can iterate all three preflight checks
        uniformly instead of special-casing each module's field names.
        This check has no extra structured fields beyond ``ok``/``message``,
        so ``details`` is empty.
        """
        return {"check": "chromium", "ok": self.ok, "message": self.message, "details": {}}


def check_chromium_backend() -> BrowserPreflightResult:
    """Report whether the browser_* tools will be visible in the schema.

    Delegates entirely to :func:`tools.browser_tool.check_browser_requirements`
    — the exact ``check_fn`` used at tool-schema assembly time — so this
    check can never disagree with what the agent actually gets. A local
    import keeps ``tools.browser_tool`` (a large module with browser-CDP
    machinery) out of anything that imports this module for reasons other
    than running the installer's preflight report.

    Returns:
        A :class:`BrowserPreflightResult` with ``ok`` mirroring the real
        check_fn's verdict and a Russian, administrator-facing message.
    """
    from tools.browser_tool import check_browser_requirements

    if check_browser_requirements():
        return BrowserPreflightResult(
            ok=True,
            message="Локальный Chromium для браузерных инструментов найден и готов к работе.",
        )

    return BrowserPreflightResult(
        ok=False,
        message=(
            "Chromium не найден. Конфигурация выбирает встроенные "
            "инструменты browser_* поверх локального Chromium "
            "(browser.backend: \"off\"), а без Chromium они молча не "
            "попадают в схему инструментов — модель не увидит ни одного "
            "browser_*, без единого сообщения об ошибке. Установите вручную: "
            "npx agent-browser install --with-deps (или npx playwright "
            "install --with-deps chromium)."
        ),
    )
