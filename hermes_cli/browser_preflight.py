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


def _selected_browser_backend() -> str:
    """Какой браузерный движок выбран КЛИЕНТОМ, а не предполагается нами.

    Возвращает ``"camofox"``, если включён Camofox (он включается
    переменной ``CAMOFOX_URL``, а не полем конфига — см.
    ``tools/browser_camofox.py::is_camofox_mode``), иначе значение
    ``browser.backend`` из конфига, иначе ``"off"``.

    Никогда не бросает: у проверки перед запуском нет права уронить
    установку или проход поддержки из-за нечитаемого конфига.
    """
    try:
        from tools.browser_camofox import is_camofox_mode

        if is_camofox_mode():
            return "camofox"
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        backend = ((load_config() or {}).get("browser") or {}).get("backend")
        return (backend or "off").strip() or "off"
    except Exception:
        return "off"


def check_chromium_backend() -> BrowserPreflightResult:
    """Report whether the browser tools the CLIENT chose will actually work.

    **Проверка обязана смотреть на выбор клиента, а не на один
    предполагаемый режим.** Раньше она безусловно спрашивала про локальный
    Chromium, потому что клиентский шаблон конфига фиксирует
    ``browser.backend: "off"``. Но мастер настройки позволяет выбрать и
    другое, и тогда вердикт получался неверным в ОБЕ стороны — проверено
    исполнением на живых машинах 2026-09-04:

    - клиент выбрал **Browser Use** — проверка всё равно требовала
      Chromium, не находила его и объявляла неполадку. Проход поддержки
      превращал это в «часть неполадок исправить самостоятельно не
      удалось… напишите в поддержку» после успешной настройки. Тот же
      класс, что и советы npm, только источник другой;
    - клиент выбрал **Camofox** — проверка находила локальный Chromium,
      рапортовала «всё хорошо» и этим МАСКИРОВАЛА то, что Camofox на
      машине не установлен и его сервер никто не поднял. То есть в самом
      неприятном случае она успокаивала вместо того, чтобы предупредить.

    Теперь ветвление идёт по фактическому выбору. Для режимов, где
    локальный Chromium не при чём, проверка честно говорит «не
    применимо», а не выдумывает вердикт.

    Returns:
        A :class:`BrowserPreflightResult` with ``ok`` mirroring the real
        check_fn's verdict and a Russian, administrator-facing message.
    """
    backend = _selected_browser_backend()

    if backend == "camofox":
        return _check_camofox_backend()

    if backend not in ("", "off"):
        # Облачные и CLI-движки (browser-use, browserbase, firecrawl, ...)
        # к локальному Chromium отношения не имеют. Молчать «ok» с пустым
        # сообщением нельзя — читающий отчёт должен видеть, ПОЧЕМУ про
        # Chromium ничего не сказано.
        return BrowserPreflightResult(
            ok=True,
            message=(
                f"Локальный Chromium не проверялся: выбран другой браузерный "
                f"движок (browser.backend: \"{backend}\"). Для него Chromium не нужен."
            ),
        )

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


def _check_camofox_backend() -> BrowserPreflightResult:
    """Camofox работает только с ЗАПУЩЕННЫМ сервером — это и проверяем.

    Установка npm-пакета сервер не поднимает (её post_setup печатает
    инструкцию запустить его отдельно), поэтому «пакет установлен» ничего
    не обещает. Единственный честный признак — отвечает ли сервер по
    адресу, который клиент указал в ``CAMOFOX_URL``.

    Делегируем в ``tools.browser_camofox.check_camofox_available`` — тот
    же принцип, по которому Chromium-ветка делегирует в настоящий
    ``check_fn`` схемы: проверка не должна расходиться с тем, чем
    пользуется сам продукт.

    Проверено на живой машине 2026-09-04: после выбора Camofox в мастере
    npm-пакета на диске не оказалось, порт 9377 никто не слушал, а
    прежняя проверка рапортовала «локальный Chromium найден и готов к
    работе» — то есть успокаивала ровно там, где надо было предупредить.
    """
    from tools.browser_camofox import check_camofox_available, get_camofox_url

    url = get_camofox_url()
    if not url:
        return BrowserPreflightResult(
            ok=False,
            message="Выбран Camofox, но адрес его сервера (CAMOFOX_URL) пуст.",
        )

    if check_camofox_available():
        return BrowserPreflightResult(
            ok=True,
            message=f"Camofox отвечает по адресу {url} — браузерные инструменты готовы.",
        )

    return BrowserPreflightResult(
        ok=False,
        message=(
            f"Выбран Camofox, но его сервер по адресу {url} не отвечает. "
            "Браузерные инструменты в этом режиме работать не будут: установка "
            "пакета сервер не поднимает, его нужно запустить отдельно "
            "(npx @askjo/camofox-browser или контейнер)."
        ),
    )
