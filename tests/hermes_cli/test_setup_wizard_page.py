"""Task 9d: the wizard's single-page Russian form (spec §5, §7.1-§7.3).

``render_page()`` is a pure function (no request/session/catalog data —
those are fetched client-side from ``/api/form`` after login) — every test
here just calls it and inspects the returned HTML string. No TestClient,
no fixtures.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

# Pre-existing collection bug (unrelated to spec 7 / plan B1, fixed here
# only because it blocked running this file's tests at all): several
# @requires_node-decorated tests appear well before this name used to be
# defined further down the file, which is a NameError at import time, not
# a skip — pytest never even reached collection. Hoisted next to the
# imports it depends on (pytest, shutil) so every decorator use below it
# resolves regardless of where in the file it appears.
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _StructureParser(HTMLParser):
    """A real structural index of render_page()'s static markup: for every
    element id, records its own attributes and the ids of its ancestor
    elements (nearest first). Used where a test needs an actual
    parent/child fact (e.g. "#success is not inside <form id='main'>")
    that a substring grep cannot establish — two ids can sit near each
    other in the text while living in entirely different branches of the
    tree, or vice versa."""

    def __init__(self):
        super().__init__()
        self._stack: list[tuple[str, str | None]] = []
        self.attrs_by_id: dict[str, dict[str, str | None]] = {}
        self.ancestor_ids_by_id: dict[str, list[str]] = {}

    def _record(self, attrs):
        attr_dict = dict(attrs)
        el_id = attr_dict.get("id")
        if el_id:
            self.attrs_by_id[el_id] = attr_dict
            self.ancestor_ids_by_id[el_id] = [t_id for (_tag, t_id) in reversed(self._stack) if t_id]
        return el_id

    def handle_starttag(self, tag, attrs):
        el_id = self._record(attrs)
        if tag not in _VOID_ELEMENTS:
            self._stack.append((tag, el_id))

    def handle_startendtag(self, tag, attrs):
        self._record(attrs)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break


def test_page_is_russian_and_brandless():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "Настройка Trix Agent" in html
    assert "вашей собственной виртуальной машине" in html
    lowered = html.lower()
    assert "hermes" not in lowered and "nous" not in lowered


def test_header_does_not_overpromise():
    """Шапка не имеет права врать при самоподписанном сертификате
    (спека §5): слово о предупреждении браузера обязано присутствовать."""
    from hermes_cli.setup_wizard.page import render_page

    assert "предупреждени" in render_page().lower()


def test_secret_inputs_are_password_type():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for field_id in ("telegram_token", "provider_api_key"):
        assert f'id="{field_id}"' in html
        idx = html.index(f'id="{field_id}"')
        assert 'type="password"' in html[max(0, idx - 200) : idx + 200]


def test_secret_inputs_disable_autocomplete():
    """Секретные поля не должны предлагать браузерный автозаполнитель."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for field_id in ("telegram_token", "provider_api_key"):
        idx = html.index(f'id="{field_id}"')
        window = html[max(0, idx - 200) : idx + 200]
        assert 'autocomplete="off"' in window


def test_header_states_three_things():
    """Спека §5: три утверждения дословно — своя машина, шифрование
    самоподписанным сертификатом, куда уходят данные."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "работает на вашей собственной виртуальной машине, а не" in html
    assert "на нашем сервере" in html
    assert "сертификатом, который машина выписала себе сама" in html
    assert "только вашему провайдеру модели и Telegram" in html


def test_page_has_required_skeleton_elements():
    """Каркас из брифа: секции/элементы фиксированы по id/структуре.

    Spec 8, §8.3: there is no ``#login`` section any more — HTTP Basic
    auth gates every path in front of this module, so the page only ever
    ships the step wizard itself.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for needle in (
        'id="main"',
        'id="provider-block"',
        'id="advanced"',
        'id="done"',
        'id="progress"',
        'id="success"',
        'id="botlink"',
    ):
        assert needle in html, needle
    assert 'id="login"' not in html


def test_main_form_starts_hidden():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    form_idx = html.index('id="main"')
    form_tag = html[max(0, form_idx - 50) : form_idx + 50]
    assert "hidden" in form_tag


def test_advanced_is_a_plain_rows_container_not_a_details_toggle():
    """План B4: #advanced больше не <details> с собственным hidden/open —
    сворачивание теперь на уровне отдельных строк (buildCollapsibleRow()),
    а не всей секции разом. #advanced сам никогда не несёт [hidden] —
    видимость шага 5 целиком управляется его родителем ([data-step="5"])."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "<details id=\"advanced\"" not in html
    advanced_idx = html.index('id="advanced"')
    advanced_tag = html[max(0, advanced_idx - 50) : advanced_idx + 50]
    assert "hidden" not in advanced_tag
    assert 'class="rows"' in advanced_tag


def test_progress_and_success_sections_start_hidden():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for field_id in ("progress", "success"):
        idx = html.index(f'id="{field_id}"')
        tag = html[max(0, idx - 50) : idx + 50]
        assert "hidden" in tag


def test_no_provider_preselected_in_static_html():
    """Спека §7.2: ничего не предвыбрано. Провайдеры рендерятся JS-ом из
    /api/form, так что в статическом HTML их вообще быть не должно — но
    на случай будущего SSR-варианта, страховка: ни один атрибут
    checked/selected рядом со словом provider не встречается."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for m in re.finditer(r"provider", html, re.IGNORECASE):
        window = html[max(0, m.start() - 120) : m.end() + 120]
        assert "checked" not in window
        assert "selected" not in window


def test_submit_uses_post_json_not_query_string():
    """Сабмит — только POST-JSON, никаких значений в URL."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "method=\"get\"" not in html.lower()
    assert "/api/submit" in html


def test_no_external_cdn_references():
    """Инлайновый HTML+CSS+JS без сборки и внешних CDN: ни один
    <script>/<link> не тянет ресурс с внешнего хоста."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert not re.search(r'<script[^>]+src=["\']https?://', html, re.IGNORECASE)
    assert not re.search(r'<link[^>]+href=["\']https?://', html, re.IGNORECASE)
    assert "cdn." not in html.lower()


def test_instructions_present_for_required_fields():
    """Инструкции справа от полей — обязательные тексты (близкие к
    trix.env.example, чтобы не противоречить: те же боты)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "@BotFather" in html
    assert "@userinfobot" in html
    assert "недоступен" in html.lower()


def test_device_code_login_is_a_working_button_not_a_limitation_notice():
    """Owner requirement 2: the old "войдите из командной строки" excuse
    is gone — the device-code sub-block is a real "Войти по аккаунту"
    button wired to the wizard's own /api/device/* endpoints."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="device-login-start"' in html
    assert "Войти по аккаунту" in html
    assert "/api/device/start" in html
    assert "/api/device/status" in html
    assert "из командной строки" not in html


def test_device_login_polls_status_until_terminal_state():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'data.state === "ok"' in html
    assert 'data.state === "error"' in html
    assert "setInterval" in html
    assert "clearInterval" in html


def test_no_install_button_left_in_markup_or_js():
    """Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"):
    installing a chosen tool happens unattended, as part of /api/submit's
    own install stage — there is no separate "Установить" button any more
    (the console wizard never had one either — see
    docs/product/specs/2026-08-23-wizard-content-decisions.md). The route
    itself is gone too — see test_setup_wizard_app_form.py's
    test_removed_install_route_returns_404."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "/api/install" not in html
    assert "appendInstallControl" not in html
    assert "installBtn" not in html


def test_render_page_is_deterministic_and_string():
    from hermes_cli.setup_wizard.page import render_page

    a = render_page()
    b = render_page()
    assert isinstance(a, str) and a == b


def test_camofox_address_is_never_asked_for_or_shown_but_still_submitted():
    """Owner ruling after looking at the live VM: the client is never even
    TOLD Camofox's address, let alone asked to type it — a bare local port
    means nothing to a non-technical user and Camofox just works once
    picked. It must still reach the payload though (CAMOFOX_URL is the
    real on/off switch — tools/browser_camofox.py::is_camofox_mode() reads
    bool(get_secret("CAMOFOX_URL"))), sourced silently from the catalog's
    `auto_default`, with no input AND no note surfacing it in the UI."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    # Still submitted...
    assert "camofox_url:" in html  # payload key in buildPayload()
    assert "auto_default" in html  # ...sourced from the catalog
    # ...but no field to type it into, no note stating it, and no
    # instructions pointing at the "Установить" button that no longer
    # exists.
    assert 'id="camofox_url"' not in html
    assert "Адрес сервера:" not in html
    assert "стандартный, менять не нужно" not in html
    assert "Установите кнопкой выше" not in html
    assert "npx @askjo/camofox-browser" not in html
    # Finding 5/6 stays covered: turning Camofox off happens by picking any
    # other browser, never by clearing a field.
    assert "Пустое поле = Camofox выключен" not in html


def test_bootstrap_load_form_failure_uses_isautherror():
    """Spec 8, §8.3: there is no login step any more — `loadForm()` runs
    once, the instant the script starts, instead of after a successful
    `POST /api/login`. Its top-level `.catch` must still route through
    `isAuthError()`, not a bare string comparison, so a lost-auth
    rejection (already handled by `handleAuthLost()`) doesn't also trigger
    the generic "couldn't load the form" fallback message."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    start = html.index("loadForm().catch(function (err) {")
    end = html.index("});", start)
    chunk = html[start:end]
    assert "isAuthError(err)" in chunk


def test_no_login_form_submit_handler_left_wired():
    """The password login form (spec 6) is gone (spec 8, §8.3 — HTTP
    Basic auth replaces it) — there must be no leftover event listener
    trying to wire a `#login-form` that no longer exists in the markup."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "login-form" not in html
    assert "/api/login" not in html


@requires_node
def test_json_fetch_does_not_mutate_caller_options():
    """jsonFetch() builds its own options object (`Object.assign({},
    options)`) rather than mutating the caller's — a caller reusing the
    same options literal across a retry must not see it change underneath
    it. Confirms the caller's own `options` object (and its `headers`
    sub-object, if it passed one) come back exactly as they went in, while
    the request actually handed to `fetch()` still gets the default
    Content-Type merged alongside whatever headers the caller supplied."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "jsonFetch") + "\n}\n"
    script = (
        "global.fetch = function (path, opts) {\n"
        "  return Promise.resolve({ status: 200, __opts: opts });\n"
        "};\n"
        "%s\n"
        'var input = { method: "POST", headers: { "X-Foo": "1" } };\n'
        "jsonFetch('/x', input).then(function (res) {\n"
        "  console.log(JSON.stringify({\n"
        "    inputHeaderKeys: Object.keys(input.headers).sort(),\n"
        "    fetchHeaderKeys: Object.keys(res.__opts.headers).sort(),\n"
        "  }));\n"
        "});\n"
    ) % body
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert out["inputHeaderKeys"] == ["X-Foo"]
    assert out["fetchHeaderKeys"] == ["Content-Type", "X-Foo"]


def test_render_page_names_the_host_when_given():
    """Docs-враньё 2 fix (спека §5): когда вызывающий (app.py's index()
    route) передаёт хост из заголовка Host, шапка называет реальный
    адрес машины, а не молчаливый плейсхолдер вроде <ip>."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page("203.0.113.7")
    assert "203.0.113.7" in html
    assert "вашей собственной виртуальной машине (203.0.113.7)" in html


def test_render_page_without_host_keeps_old_text():
    """Без хоста (или host=None) — прежняя формулировка без скобок,
    прежние тесты (test_header_states_three_things и т.д.) остаются
    в силе. The self-signed-certificate disclosure's only home now is the
    rail's ``#cert-detail`` reveal (spec 8, §8.3 removed the separate
    login screen's own ``<header>``)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "работает на вашей собственной виртуальной машине, а не" in html
    detail = html.split('id="cert-detail"')[1].split("</div>")[0]
    assert "(" not in detail


def test_render_page_escapes_hostile_host():
    """Host — заголовок, присланный клиентом, не сервером: он не должен
    попадать в HTML сырым. render_page('<script>') не должен содержать
    непойманного тега."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page("<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_out_of_catalog_backend_note_mechanism_present():
    """Round 2 fix: a saved backend value outside the rendered catalog
    (e.g. a hand-configured search/browser backend) must render a note
    instead of silently defaulting a radio to "recommended"."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "outOfCatalog" in html
    assert "настроено вручную" in html


def test_key_checked_false_shows_unverified_notice():
    """Спека §10.1 (review finding "Important 2"): key_checked === false
    в успешном ответе /api/submit обязано показать честную оговорку про
    непроверенный ключ, а не молча пропасть."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "key_checked" in html
    assert "key-check-notice" in html
    assert "Ключ провайдера не проверялся автоматически" in html
    assert "/setup" in html


def test_provider_prefill_from_current_on_matching_selection():
    """Спека finding "Important 3": возврат к тому же провайдеру не
    должен затирать base_url/model, которые сам мастер записал ранее —
    onProviderChange() обязан подставлять их из state.current.provider,
    а не из каталожной строки, когда имена совпадают."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "isCurrentProvider" in html
    assert "currentProvider.base_url" in html
    assert "currentProvider.model" in html


def test_model_select_clears_custom_field_on_change():
    """Model-field shadowing fix: modelFieldValue() prefers
    provider_model_custom over the <select> whenever the free-text field
    is non-empty, so the round-trip prefill above can leave a stale
    custom value that outlives a fresh <select> pick. onProviderChange()
    must wire provider_model's onchange to clear provider_model_custom
    whenever the client picks anything OTHER than "Ввести вручную…" —
    spec B3 collapsed the old select+button+free-text trio into one
    select where manual entry is itself an option (CUSTOM_MODEL_VALUE)
    that reveals the field instead of it sitting there permanently."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "onProviderChange")
    idx = body.index("modelSelect.onchange = function () {")
    handler = body[idx : idx + 300]
    assert 'if (modelSelect.value !== CUSTOM_MODEL_VALUE) modelCustom.value = "";' in handler
    assert "syncModelCustomVisibility(modelSelect, modelCustom);" in handler


# ---- Owner feedback redesign: mutually exclusive tool categories are one
# <select> each, with the chosen option's own settings appearing only
# after it is picked (progressive disclosure) — never before. ------------


def test_browser_is_a_single_select_not_a_radio_group():
    """Спека владельца: браузер может быть только один — значит один
    select, а не набор радиокнопок. Camofox (свой переключатель через
    CAMOFOX_URL, не browser.backend) — один из вариантов этого select,
    не отдельная всегда видимая строка."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert '"browser_choice"' in html
    assert '"advanced-browser"' in html
    assert "Chromium (встроенный, по умолчанию)" in html
    assert "Camofox — анти-детект" in html
    assert "Browser Use" in html
    # No radio input for the browser group any more.
    assert 'name="browser_choice"' not in html
    assert 'radio.name = groupName' not in html


def test_camofox_settings_gated_behind_its_own_selection():
    """Ровно претензия владельца: настройки Camofox не должны быть видны,
    пока пользователь не выбрал Camofox — они строятся внутри
    renderSettings() только когда select.value === CAMOFOX_VALUE, а не
    рендерятся при любом выборе (например, при выбранном по умолчанию
    Chromium)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "value === CAMOFOX_VALUE" in html
    assert 'value === "off"' in html
    assert "Работает из коробки" in html


def test_browser_backend_payload_maps_camofox_to_off():
    """Payload-семантика не меняется: выбор Camofox в едином select всё
    равно отправляет browser_backend "off" + camofox_url — как раньше,
    когда Camofox была отдельной, всегда видимой строкой."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'value === CAMOFOX_VALUE ? "off" : value' in html


def test_search_is_a_single_select_sourced_from_the_web_tools_block():
    """"Поиск в интернете" is a regular tools block (the "web" category —
    spec-approved commit 2 redesign) rendered generically from
    ``toolBlockFor("web")``, not a hardcoded two-backend label map."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert '"search_choice"' in html
    assert 'toolBlockFor("web")' in html
    assert '"search_env_value"' in html
    assert 'name="search_backend_choice"' not in html
    # The old hardcoded label map / firecrawl-only field are gone.
    assert "SEARCH_LABELS" not in html
    assert '"firecrawl_key"' not in html


def test_extract_is_a_separate_select_sourced_from_the_web_extract_tools_block():
    """Owner question (verbatim): "поиск и извлечение страниц это разные
    тулзы или нет?" — yes, and the client used to conflate them into one
    "Поиск и извлечение страниц" picker even though several search
    backends (DuckDuckGo, Brave, SearXNG, Grok) can't extract at all.
    "Чтение страниц" is now its OWN collapsible row, sourced from the
    server's separately-filtered ``toolBlockFor("web_extract")`` catalog
    (extract-capable rows only), with its own select/env-value ids —
    never sharing state with "Поиск в интернете"'s search_choice/
    search_env_value."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert '"extract_choice"' in html
    assert 'toolBlockFor("web_extract")' in html
    assert '"extract_env_value"' in html
    # The two categories are rendered by distinct functions, wired
    # separately into renderAdvanced() — never the same render*Block().
    assert "function renderExtractBlock()" in html
    idx = html.index("renderSearchBlock();")
    assert "renderExtractBlock();" in html[idx : idx + 200]


def test_extract_backend_and_env_wired_into_payload():
    """extract_backend/extract_env must ride along in buildPayload()'s
    returned object literal, read from the SAME select/field ids
    renderExtractBlock() renders — the server-side contract (apply.py/
    app.py) is already live; this is the client actually using it."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "buildPayload")
    assert "extract_backend: extractBackendChoiceValue()" in body
    assert "extract_env: extractEnvPayload()" in body


def test_summary_advanced_includes_extract_choice_category():
    """The "Готово" step's summary line must reflect a chosen extract
    backend too, with the same "never configured is not shown" rule as
    image_gen/video_gen (a legitimate default, not padded into the
    summary as if something were wrong)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    summary_categories_idx = html.index("var SUMMARY_TOOL_CATEGORIES = [")
    summary_categories_end = html.index("];", summary_categories_idx)
    categories_block = html[summary_categories_idx:summary_categories_end]
    assert '"extract_choice"' in categories_block


def test_xai_web_search_row_gets_an_honest_hint_not_a_blank_or_broken_button():
    """Polish (owner review, 2026-08-20): "xAI Web Search (Grok)" (live
    "web" catalog row) carries no env_vars — only post_setup="xai_grok",
    whose install hook drives interactive CLI prompts over stdin
    (tools_config._run_post_setup's "xai_grok" branch), meaningless from
    a headless web request. Selecting it must show an honest note
    pointing at step 4's provider block (which is the field that actually
    activates it — provider_readiness_status's "xai_grok" branch falls
    back to a plain XAI_API_KEY), not an empty panel or a dead
    "Установить" button."""
    from hermes_cli.setup_wizard import tools_view as tv
    from hermes_cli.setup_wizard.page import render_page

    catalog = tv._catalog()
    xai_row = next(
        (p for p in catalog["web"]["providers"] if p.get("post_setup") == "xai_grok"), None
    )
    assert xai_row is not None, "live catalog no longer has an xai_grok web row — re-check this test"
    assert not xai_row.get("env_vars"), "test's premise (no env_vars) no longer holds"

    html = render_page()
    idx = html.index("function renderSearchBlock() {")
    body_end = html.index("\n  }\n", idx)
    body = html[idx:body_end]
    assert '"xai_grok"' in body
    assert "Использует ключ провайдера xAI (Grok) — настройте провайдера на шаге 4." in body
    # The generic install-button path must be gated OFF for this
    # post_setup key specifically — every other keyless+post_setup row
    # (e.g. ddgs) keeps its working "Установить" button untouched.
    assert 'row.post_setup !== "xai_grok"' in body


def test_voice_select_has_custom_option_with_gated_name_field():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert '"voice_choice"' in html
    assert "Голос Светлана (по умолчанию, рекомендуется)" in html
    assert "Свой голос" in html


def test_image_gen_is_a_single_select():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert '"image_gen_choice"' in html
    assert "Выключена (по умолчанию)" in html


def test_no_raw_catalog_badges_shown_to_client():
    """Владелец: «бейджи-теги английские ... не показывай клиенту
    сырыми». Рекомендация выражается предвыбором + словом
    «(рекомендуется)» в option, а не сырым badge/tag текстом из
    каталога."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "★" not in html
    assert "recommended · free" not in html
    assert 'className = "badge"' not in html
    assert "(рекомендуется)" in html


def test_progressive_disclosure_wired_on_every_new_select():
    """Каждый select-блок обязан перерисовывать свои настройки при смене
    выбора (select.onchange), а не показывать их все сразу — video_gen
    joined with the generalization (owner ruling 2026-08-20); x_search/
    homeassistant were dropped from the wizard entirely (план A5/B4) and
    must not be among the six remaining category functions."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "select.onchange = renderSettingsFn;" in html
    for fn in (
        "renderBrowserBlock",
        "renderSearchBlock",
        "renderVoiceBlock",
        "renderSTTBlock",
        "renderImageGenBlock",
        "renderVideoGenBlock",
    ):
        assert f"function {fn}()" in html
    for fn in ("renderXSearchBlock", "renderHomeAssistantBlock"):
        assert f"function {fn}()" not in html


# ---- Review fixes: explicit backend beats stale Camofox preselect, ------
# install feedback survives the same-tick re-render, no duplicate heading.


def test_explicit_browser_backend_beats_stale_camofox_url():
    """Review 'Important 1': a saved CAMOFOX_URL from an earlier session
    must NOT outrank a later, explicit non-"off" browser_backend (e.g.
    "browser-use") — apply_settings() never clears CAMOFOX_URL on its
    own, so without this priority a client who tried Camofox once and
    later switched to Browser Use would see Camofox silently preselected,
    and an untouched resubmit would downgrade browser.backend back to
    "off" underneath them."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert (
        'var savedValue = (backend && backend !== "off")\n'
        "      ? backend\n"
        "      : (current.camofox_url ? CAMOFOX_VALUE : backend);"
    ) in html
    # The old unconditional "camofox_url wins outright" expression must be gone.
    assert 'current.camofox_url ? CAMOFOX_VALUE : (current.browser_backend || "")' not in html


def test_advanced_rows_have_no_leftover_details_wrapper():
    """План B4: восемь развёрнутых блоков внутри мёртвой <details
    id="advanced"> с отключённым <summary> (жалоба владельца — «опять
    лист целый»; Review nit ранее также проверял, что скрытый <summary>
    не дублирует текст видимого <h2> — теперь заголовка нет вовсе) —
    заменены шестью свёрнутыми строками. Ни обёртки, ни общего заголовка
    секции больше нет."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "<details id=\"advanced\"" not in html
    assert "<summary></summary>" not in html
    assert "<h2>Дополнительные настройки</h2>" not in html


# ---- Owner requirement 1: grouped provider picker + способ подключения --


def test_provider_field_is_a_flat_visible_row_not_a_collapsed_details():
    """Owner feedback: the model-provider field used to sit behind a
    collapsed `<details><summary>Провайдер модели ▸ выбрать</summary>`
    disclosure, which the owner didn't recognize as a clickable control —
    it must render as an ordinary always-visible field-row, label
    "Провайдер модели", exactly like the bot-token/Telegram-id rows above
    it, with the select + hint appearing immediately."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "<details id=\"provider-block\">" not in html
    assert "<summary>Провайдер модели ▸ выбрать</summary>" not in html
    assert "▸" not in html
    assert '<div id="provider-block">' in html
    assert 'for="provider_group">Провайдер модели</label>' in html


def test_provider_picker_is_grouped_not_flat():
    """The primary select is now `provider_group` (populated from
    /api/form's provider_groups), not a flat `provider_name` select over
    every catalog row — see providers_view.wizard_provider_groups()."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="provider_group"' in html
    assert 'id="provider_name"' not in html
    assert "state.providerGroups" in html
    assert "renderProviderGroupOptions" in html


def test_auth_choice_card_group_present_and_hidden_by_default():
    """Owner feedback (live walkthrough): the old native-radio list read as
    a minor checkbox row, not the same kind of decision as the provider
    picker above it — clicking OpenAI silently left the client on whatever
    the first radio happened to be without registering there was a second
    (API-key) path at all. onProviderGroupChange() now builds the same
    clickable .prov .p cards renderProviderRow() does, not <input
    type="radio">."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index('id="provider-auth-choice"')
    tag = html[max(0, idx - 50) : idx + 80]
    assert "hidden" in tag
    assert 'id="provider-auth-options" class="prov"' in html
    assert 'type="radio"' not in html
    assert "row.dataset.variantName = variant.name;" in html


def test_single_variant_group_skips_the_radio_and_goes_straight_to_its_subblock():
    """wizard_provider_groups()' own contract: a one-variant group's
    `variants` array has length 1 — onProviderGroupChange() must render
    that variant directly instead of a pointless one-option radio."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "group.variants.length <= 1" in html
    assert "onProviderChange(group.variants[0] || null)" in html


def _css_rule_body(html: str, selector: str) -> str:
    """Тело CSS-правила для точного селектора — а не первое текстовое
    совпадение подстроки.

    Прежняя форма (`html.index(selector)` + окно в 60 символов) ловила
    ЛЮБОЕ упоминание селектора, включая объясняющий комментарий рядом.
    Стоило дописать в page.py комментарий со ссылкой на соседнее правило —
    и тест краснел, хотя само правило было на месте (поймано прогоном
    2026-09-04). Ищем `<селектор> {` , то есть настоящее начало правила.
    """
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
    assert match, f"в отрисованной странице нет правила {selector} {{ ... }}"
    return match.group(1)


def test_field_row_hidden_attribute_actually_hides_it():
    """Owner-observed bug (originally seen on a single-variant group's
    "способ подключения" row, since converted off .field-row entirely —
    see test_auth_choice_card_group_present_and_hidden_by_default): a
    JS-hidden field-row stayed visible because `.field-row { display: grid
    }` (an author rule) silently outranks the browser's default `[hidden]
    { display: none }` at equal origin+specificity. The CSS must carry its
    own `[hidden]` override for `.field-row` so JS-driven hiding of any
    field-row on the page actually works."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "display: none" in _css_rule_body(html, ".field-row[hidden]")


def test_provider_auth_choice_hint_only_shown_for_multi_variant_groups():
    """Owner requirement 2: "Способ подключения" — radio AND its
    "несколько способов подключения" hint — is one hidden field-row that
    only becomes visible for a >1-variant group (authChoice.hidden = false
    in the else branch); a single-variant group never flips it visible."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index('id="provider-auth-choice"')
    tag = html[max(0, idx - 50) : idx + 80]
    assert "hidden" in tag
    assert "У этого провайдера несколько способов подключения" in html
    assert "authChoice.hidden = false;" in html
    assert "authChoice.hidden = true;" in html


# ---- Plan B3: provider list redesign (screen 4 of the approved mockup) ----


def test_provider_list_is_rows_not_a_bare_select():
    """Spec B3 п.1: a row per vendor (name, description_ru, a
    "рекомендуем"/"нужен прокси" tag) — a native <option> can't carry that
    markup, so `#provider_group` is now the empty shell
    renderProviderGroupOptions() fills with .prov .p rows, not a
    <select>."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index('id="provider_group"')
    tag = html[max(0, idx - 20) : idx + 80]
    assert "<select" not in tag
    assert 'class="prov"' in tag
    assert "function renderProviderRow(group)" in html
    assert "function renderProviderGroupOptions()" in html


def test_provider_english_description_never_reaches_the_signup_hint():
    """Spec A1/B3: the upstream catalog's English `description` must never
    reach the client — onProviderChange()'s "signup hint" line and the
    provider list's own rows must both read `description_ru`, and the
    bare (English) `.description` property must not be referenced
    anywhere in the script at all."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    script_start = html.index("<script>")
    script = html[script_start:]
    assert "row.description_ru" in script
    assert "group.description_ru" in script
    # No bare `.description` PROPERTY ACCESS (without `_ru`) anywhere in
    # the script — comments are free to use the English word "description"
    # in prose (several do, explaining exactly this rule), so the check is
    # narrowed to the actual code shapes `row.description` /
    # `group.description` were ever read through, not every appearance of
    # the substring.
    for pattern in (r"row\.description\b", r"group\.description\b"):
        for m in re.finditer(pattern, script):
            window = script[m.start() : m.end() + 3]
            assert window.endswith("_ru"), window


def test_no_fetch_models_button_anywhere():
    """Spec B3 п.4: "получить список моделей ▾" is gone — the live
    catalog loads by itself once a key round-trips through
    /api/check/key (see fetchLiveModelsForRow(), called from
    runProviderKeyCheck())."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="fetch-models"' not in html
    # The removed BUTTON's own label (with its trailing dropdown arrow) —
    # narrower than banning the phrase outright, since the honest fetch-
    # failure message legitimately says "не удалось получить список
    # моделей" in prose (see fetchLiveModelsForRow()'s own error text).
    assert "получить список моделей ▾" not in html
    select_idx = html.index('id="provider_model"')
    select_end = html.index("</select>", select_idx)
    assert "<button" not in html[select_idx:select_end]
    assert "function fetchLiveModelsForRow(row)" in html
    body = _function_body(html, "runProviderKeyCheck")
    assert "fetchLiveModelsForRow(rowAtCallTime)" in body


def test_manual_model_entry_is_a_select_option_not_a_third_control():
    """Spec B3 п.5: the old select + button + free-text trio collapses
    into ONE select per model field — "Ввести вручную…" is itself an
    <option> (CUSTOM_MODEL_VALUE) that reveals the free-text field next
    to it, rather than that field being a permanently-visible third
    control. Covers both the api_key branch (provider_model) and the
    device_code branch (provider_model_device)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'var CUSTOM_MODEL_VALUE = "__custom_model__";' in html
    for select_id, custom_id in (
        ("provider_model", "provider_model_custom"),
        ("provider_model_device", "provider_model_device_custom"),
    ):
        select_idx = html.index(f'id="{select_id}"')
        select_tag_end = html.index("</select>", select_idx)
        select_markup = html[select_idx:select_tag_end]
        assert '<option value="__custom_model__">Ввести вручную…</option>' in select_markup
        custom_idx = html.index(f'id="{custom_id}"')
        tag_start = html.rindex("<input", 0, custom_idx)
        tag_end = html.index(">", custom_idx)
        custom_tag = html[tag_start : tag_end + 1]
        # Starts hidden — only CUSTOM_MODEL_VALUE reveals it (see
        # syncModelCustomVisibility()) — scoped to just this <input ...>
        # tag so a neighboring element's own `hidden` doesn't mask a
        # missing one here.
        assert "hidden" in custom_tag, custom_tag
    assert "function syncModelCustomVisibility(select, custom)" in html


def test_provider_api_key_field_resets_on_provider_switch():
    """Spec B3 п.8 (bug found in review): onProviderChange() reset
    provider_model/provider_base_url on every switch but NOT
    provider_api_key's own .value — a key typed for one provider stayed
    in the field and got submitted under a DIFFERENT provider's env_var
    after a switch. Must be cleared unconditionally, near the top of the
    function, before the row-null/device_code/api_key branches."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "onProviderChange")
    reset_idx = body.index('byId("provider_api_key").value = "";')
    # Above every branch — including the early `if (!row)` return, so a
    # switch to "nothing chosen" clears it too.
    branch_idx = body.index("if (!row) {")
    assert reset_idx < branch_idx


def test_fallback_provider_key_resets_on_selection_change():
    """Spec B3 п.8, second instance of the same bug: the "Запасная
    модель" block's own fallback_name select had NO onchange handler at
    all — a key typed for one fallback provider survived a switch to a
    different one and would be submitted under the new one's env_var,
    same defect class as provider_api_key above."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "renderAdvancedFallback")
    idx = body.index("select.onchange = function () {")
    handler = body[idx : idx + 150]
    assert 'byId("fallback_api_key")' in handler
    assert "keyInput.value = \"\";" in handler


def test_nothing_preselected_in_the_new_provider_list_on_first_entry():
    """Spec §7.2 still holds under the row-list redesign: nothing is
    highlighted (.sel) until the client actually clicks a row —
    state.chosenGroupId starts null, and renderProviderRow() only adds
    the "sel" class when a row's own group_id matches it."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "chosenGroupId: null," in html
    row_fn = _function_body(html, "renderProviderRow")
    assert 'state.chosenGroupId === group.group_id ? " sel" : ""' in row_fn


def test_provider_list_recommended_first_rest_collapsed_behind_a_link():
    """Spec B3 п.1: every group renders (owner ruling — nothing hidden
    entirely), recommended groups always show, the rest collapse behind
    "Показать остальные N →" until state.providerListExpanded flips
    true — N is computed from the actual catalog, never hardcoded."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "renderProviderGroupOptions")
    assert 'g.recommended' in body
    assert '"Показать остальные " + rest.length + " →"' in body
    assert "state.providerListExpanded = true;" in body


def test_provider_reachability_tag_text_depends_on_whether_a_proxy_is_set():
    """Spec B3 п.1: a group the proxy check marked unreachable gets
    "нужен прокси" when no proxy is set yet (a proxy would plausibly fix
    it) or the stronger "недоступен" when a proxy IS set and the check
    still failed through it — never a guess for a group the check never
    covered at all (see app.py's _reachability_providers_by_group, which
    only answers for openai/anthropic/openrouter/deepseek/zai/google)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "reachabilityTag")
    assert "hasOwnProperty" in body
    assert '"недоступен"' in body
    assert '"нужен прокси"' in body


def test_provider_payload_reads_name_from_chosen_variant_row():
    """buildPayload() must submit the chosen VARIANT's slug
    (state.chosenProviderRow.name), never the group_id — the group select
    itself no longer carries the value the server expects."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'name: providerRow ? providerRow.name : ""' in html


def test_device_code_variant_never_submits_stale_api_key_block_values():
    """A device_code variant's api_key/base_url must come back empty even
    if the client previously had an api_key variant selected (whose
    hidden fields could still carry a leftover value) — see buildPayload's
    own device_code branch."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    # Spec B2 wrapped the api_key half in a silent .trim() (whitespace
    # pasted around a key/token is trimmed before it's sent anywhere) —
    # the device_code branch's own "" fallback is unchanged.
    assert 'providerRow && providerRow.kind === "device_code" ? "" : (byId("provider_api_key").value || "").trim()' in html
    assert 'providerRow && providerRow.kind === "device_code" ? "" : byId("provider_base_url").value' in html


def test_device_model_select_resets_when_switching_device_code_providers():
    """Review finding: switching from one device_code variant to another
    left the PREVIOUS provider's stale model options sitting in
    #provider_model_device — mirrors the api_key branch's own "Genuinely
    switching catalogs" reset. Spec B3 moved the reset itself behind the
    shared resetModelSelect() helper (keeps the "" and "Ввести вручную…"
    <option>s, drops everything fetched/fallback in between) and gave the
    onchange handler the same custom-field show/hide behavior as the
    api_key branch's own provider_model select."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index('row.kind === "device_code"')
    window = html[idx : idx + 1600]
    assert 'byId("provider_model_device")' in window
    assert "resetModelSelect(deviceModelSelect)" in window
    assert 'deviceModelCustom.value = ""' in window
    onchange_idx = window.index("deviceModelSelect.onchange = function () {")
    handler = window[onchange_idx : onchange_idx + 300]
    assert 'if (deviceModelSelect.value !== CUSTOM_MODEL_VALUE) deviceModelCustom.value = "";' in handler
    assert "syncModelCustomVisibility(deviceModelSelect, deviceModelCustom);" in handler


def test_device_login_url_only_wired_as_a_link_for_http_schemes():
    """Review finding: link.href was set unconditionally from the server's
    verification_url — an http(s) scheme guard must gate whether it
    becomes a real link or inert text."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "function isHttpUrl" in html
    assert "/^https?:\\/\\//i.test" in html
    assert "function setVerificationLink" in html
    assert "link.removeAttribute(\"href\")" in html
    # buildPayload-adjacent code must go through the guarded setter, not a
    # bare `link.href = data.verification_url` assignment.
    assert "link.href = data.verification_url" not in html


def test_device_status_poll_ignores_mismatched_login_id_and_provider():
    """Review minor: the manager is one login per PROCESS, not per tab —
    a status response must be checked against the login_id/provider the
    client is actually waiting on before being applied."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "data.login_id && data.login_id !== loginId" in html
    assert "data.provider && data.provider !== row.name" in html
    assert "startDevicePoll(row, data.login_id)" in html


# ---- Owner requirement: proxy is the third required field, and gates
# every provider-facing operation from the form, not just Telegram -------


def test_proxy_field_comes_before_the_provider_block():
    """Owner requirement after the live walkthrough: proxy is central for
    RU hosting (the data center can reach GLM/Gemini/DeepSeek directly but
    not OpenAI/OpenRouter/Anthropic/Telegram) — it must sit ahead of the
    model-provider block, not after it. Plan B1 (spec 7) went further and
    moved proxy ahead of the Telegram fields too (step 2, before step 3) —
    Telegram's own autocheck talks to the network, so a client who hasn't
    set a proxy yet shouldn't hit that check first."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    proxy_idx = html.index('id="proxy"')
    token_idx = html.index('id="telegram_token"')
    users_idx = html.index('id="allowed_users"')
    provider_idx = html.index('id="provider-block"')
    assert proxy_idx < token_idx < users_idx < provider_idx


def test_proxy_hint_mentions_telegram_and_provider_and_format():
    """The hint must name both reasons a client would need this field
    (Telegram AND the model provider — not just Telegram, the pre-redesign
    text) and give the two accepted URL formats."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index('id="proxy"')
    window = html[idx : idx + 700]
    assert "Telegram" in window
    assert "провайдер" in window
    assert "socks5://user:pass@host:port" in window
    assert "http://host:port" in window


def test_proxy_value_is_sent_in_models_device_start_requests():
    """The form's own proxy value must be read live (at request time) and
    included in every provider-facing POST body: /api/models (both the
    api-key fetch-models button and the device-code model load) and
    /api/device/start — not just /api/check/telegram, which already had
    it before this pass."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'proxy: byId("proxy").value' in html
    models_calls = [m.start() for m in re.finditer(r'jsonFetch\("/api/models"', html)]
    assert len(models_calls) == 2
    for start in models_calls:
        window = html[start : start + 600]
        assert 'byId("proxy").value' in window
    device_start_idx = html.index('jsonFetch("/api/device/start"')
    device_window = html[device_start_idx : device_start_idx + 600]
    assert 'byId("proxy").value' in device_window


# ---- Step wizard: always step-based, first run AND return visits alike ----


def test_step_containers_present_for_every_step():
    """Steps 2-6 are wrapper divs with data-step, hidden by default. These
    internal ids stayed 2-6 even after spec 8, §8.3 removed the login step
    ahead of them — only the rail's own DISPLAYED numbering (STEPS' `num`)
    was renumbered to start at 1; see
    test_progress_bar_lists_all_five_steps."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for n in (2, 3, 4, 5, 6):
        needle = f'data-step="{n}"'
        assert needle in html, needle
        idx = html.index(needle)
        tag = html[max(0, idx - 30) : idx + 30]
        assert "hidden" in tag, tag


def test_progress_bar_lists_all_five_steps():
    """Spec 8, §8.3 dropped the login step from the rail — the wizard now
    starts directly on Прокси, so the progress bar lists five steps, not
    six, and "Пароль" is gone entirely."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="progress-bar"' in html
    for label in ("Прокси", "Telegram", "Провайдер", "Дополнительно", "Готово"):
        assert f'label: "{label}"' in html
    assert 'label: "Пароль"' not in html


def test_step_navigation_buttons_present():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for button_id in (
        "step-2-next",
        "step-3-back",
        "step-3-next",
        "step-4-back",
        "step-4-next",
        "step-5-back",
        "step-5-next",
        "step-6-back",
    ):
        assert f'id="{button_id}"' in html, button_id
    # Step 2 (Прокси) is now the first step — nothing precedes it.
    assert 'id="step-1-back"' not in html
    # "Готово" lives only on step 6 — never a plain "Далее" button there.
    assert 'id="step-6-next"' not in html


def test_step_five_has_a_single_nav_pair_not_a_duplicate_skip_button():
    """Polish (owner review, 2026-08-20): "Далее" and "Пропустить всё"
    both called goToStep(6) — the same action wearing two labels. Step 5
    keeps exactly one Назад/Далее pair now; the redundant "Пропустить
    всё" button (id=skip-advanced) is gone."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="skip-advanced"' not in html
    idx = html.index('id="step-5"')
    nav_start = html.index('<div class="step-nav">', idx)
    nav_end = html.index("</div>", nav_start)
    nav = html[nav_start:nav_end]
    # The removed label must not linger as a visible button in step 5's
    # own markup (a JS comment elsewhere may still reference the old name
    # for history — that's fine, it's never rendered to the client).
    assert "Пропустить всё" not in nav
    assert 'id="step-5-back"' in nav
    assert 'id="step-5-next"' in nav
    assert nav.count("<button") == 2


def test_no_check_buttons_anywhere_in_the_wizard():
    """Спека B2: «кнопки #telegram-check-btn и #proxy-check-btn удаляются
    целиком» — global sweep, both the ids and the old result-hint elements
    they wrote into."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for stale_id in ("proxy-check-btn", "proxy-check-result", "telegram-check-btn", "telegram-check-result"):
        assert f'id="{stale_id}"' not in html, stale_id
    for stale_text in ("Проверить доступность", ">Проверить<"):
        assert stale_text not in html, stale_text


def test_verdict_hidden_attribute_actually_hides_it():
    """Same UA-vs-author [hidden] fight as .field-row/.botlink (see
    test_field_row_hidden_attribute_actually_hides_it above) — .verdict
    sets `display: flex`, which would otherwise silently outrank the
    browser's own `[hidden] { display: none }` the moment
    setHidden("telegram-verdict", true)/runProviderChange hide one."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "display: none" in _css_rule_body(html, ".verdict[hidden]")


def test_proxy_telegram_and_key_verdicts_all_start_hidden():
    """Owner feedback п.1 (live VM walkthrough): "Прокси" no longer
    autochecks the instant the step is entered (see goToStep()'s own
    comment on why that call was removed) — so its verdict must start
    exactly like telegram/key's already did: [hidden] inside a
    `.verdict-slot` (reserved height even while empty), never a static
    "Проверяем…" that claims a check is already running before the client
    has typed anything. Regression guard for
    test_proxy_verdict_is_never_hidden_telegram_and_key_verdicts_start_hidden
    (this test's own former name/shape), which asserted the OLD,
    now-removed behavior."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "Проверяем…" not in html[: html.index("<script>")]

    for verdict_id in ("proxy-verdict", "telegram-verdict", "key-verdict"):
        idx = html.index(f'id="{verdict_id}"')
        tag = html[idx - 20 : idx + 80]
        assert "hidden" in tag, verdict_id
        # Wrapped in the reserved-space slot, not a bare hidden div.
        slot_idx = html.rindex('class="verdict-slot"', 0, idx)
        assert idx - slot_idx < 80, verdict_id

    assert ".verdict-slot" in html
    slot_css_idx = html.index(".verdict-slot {")
    assert "min-height" in html[slot_css_idx : slot_css_idx + 80]


def test_provider_reachability_by_group_is_the_named_contract_for_step_four():
    """Спека B2 п.2: «Результат проверки сохрани в состоянии клиента
    (объект providers из ответа)... Дай ему явное, самоописательное имя».
    runProxyCheck() must stash /api/check/proxy's own `providers` map on
    `state.providerReachabilityByGroup` on every answer (not just the
    first) so step 4 (a later commit) can grey out unreachable providers."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "providerReachabilityByGroup" in html
    body = _function_body(html, "runProxyCheck")
    assert "state.providerReachabilityByGroup = data.providers" in body


def test_provider_key_autochecks_on_change_and_blur_without_blocking_next():
    """Спека B2 п.4: ключ проверяется сам после ввода (change/blur), не
    блокирует «Далее» — step-4-next must never wait on the key check.

    Owner feedback п.3 (parity pair, live VM walkthrough): change/blur no
    longer call runProviderKeyCheck() directly — both route through
    maybeRunProviderKeyCheck(), which skips a redundant recheck for a
    value that is already verified or already has a probe in flight (same
    double-fire fix as telegram_token's own maybeRunTelegramCheck())."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'byId("provider_api_key").addEventListener("change", maybeRunProviderKeyCheck);' in html
    assert 'byId("provider_api_key").addEventListener("blur", maybeRunProviderKeyCheck);' in html

    guard_body = _function_body(html, "maybeRunProviderKeyCheck")
    assert "runProviderKeyCheck()" in guard_body
    assert "keyCheckSettled" in guard_body
    assert "keyCheckSeqInFlight" in guard_body

    next_idx = html.index('wireStepNav("step-4-back", "step-4-next", 5)')
    surrounding = html[max(0, next_idx - 200) : next_idx + 200]
    assert "runProviderKeyCheck" not in surrounding
    assert "maybeRunProviderKeyCheck" not in surrounding


def test_provider_key_verdict_reset_on_provider_switch_and_empty_field():
    """A stale "Ключ принят" from a previously-chosen provider must not
    survive a switch to a different one (different env var — showing the
    old verdict would be a false claim about a key nobody just checked),
    and an untouched, already-saved secret field (empty .value — see
    applySecretPlaceholderEl) must not trigger a live probe at all."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    change_body = _function_body(html, "onProviderChange")
    assert 'setHidden("key-verdict", true)' in change_body

    check_body = _function_body(html, "runProviderKeyCheck")
    assert 'setHidden("key-verdict", true)' in check_body
    assert "if (!value)" in check_body


def test_provider_key_verdict_never_claims_success_without_a_real_probe():
    """Инвариант: «если проверка не выполнялась... не писать
    «проверено»». check_provider_key answers `{checked:true,
    reachable:false, message:""}` for a provider with no live probe
    (credential_probes.CREDENTIAL_PROBES) — renderKeyVerdict must not
    render that as "Ключ принят.", only the neutral, honest wait copy."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "renderKeyVerdict")
    assert "Ключ принят." in body
    assert "не проверяется автоматически" in body
    ok_idx = body.index("Ключ принят.")
    reachable_guard_idx = body.index("data.reachable && data.ok")
    assert reachable_guard_idx < ok_idx


def test_wizard_client_never_hardcodes_provider_or_telegram_hosts():
    """Спека: «Хосты на клиенте НЕ хардкодить» — the reachability verdict
    is built off /api/check/proxy's own boolean flags and catalog
    group_ids, never a literal hostname the client would have to keep in
    sync with validate.py's own target list."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    script_start = html.index("<script>")
    script = html[script_start:]
    for hostname in (
        "api.telegram.org",
        "api.openai.com",
        "openrouter.ai",
        "anthropic.com",
        "deepseek.com",
        "bigmodel.cn",
        "generativelanguage.googleapis.com",
    ):
        assert hostname not in script, hostname


def test_telegram_and_provider_keys_are_trimmed_before_submit():
    """Спека B2 п.3/п.4: «Токен молча обрезается от пробелов и переносов
    перед отправкой и перед сабмитом» — buildPayload() must send the
    trimmed value, not whatever whitespace-padded text sits in the field
    (a common paste artifact)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "buildPayload")
    assert '(byId("telegram_token").value || "").trim()' in body
    assert '(byId("provider_api_key").value || "").trim()' in body
    assert '(byId("fallback_api_key").value || "").trim()' in body


def test_go_to_step_only_toggles_hidden_never_recreates_inputs():
    """Спека: «не пересоздавай input'ы при переходах — прячь/показывай
    секции». goToStep() must only flip .hidden on [data-step] wrappers —
    it must never assign .value or rebuild innerHTML, which is what would
    actually lose a value typed on an earlier step."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function goToStep(n) {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert 'el.hidden = Number(el.getAttribute("data-step")) !== n;' in body
    assert ".value" not in body
    assert "innerHTML" not in body


def test_proxy_step_has_no_check_button_and_does_not_autocheck_on_entry():
    """Owner feedback п.1 (live VM walkthrough): "надо проверять прокси
    только тогда, когда что-то там вставилось" — "Проверить доступность"
    stays gone, but so does the OLD spec A4/B2 "fires the instant step 2
    is entered" behavior: goToStep() must NOT call runProxyCheck() any
    more. The real triggers are the debounced #proxy "input" listener and
    the unconditional "Далее" handler — checked here structurally; the
    actual debounce/skip-on-empty behavior is covered by the jsdom
    scenarios."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="proxy-check-btn"' not in html
    assert 'id="proxy-check-result"' not in html
    assert '"/api/check/proxy"' in html
    idx = html.index('"/api/check/proxy"')
    window = html[max(0, idx - 400) : idx + 200]
    assert 'byId("proxy").value' in window

    go_to_step_body = _function_body(html, "goToStep")
    # Finding 4 (review 2026-08-26, owner-approved fix): a conditional,
    # ONE-TIME backfill on entering step 4 — never step 2 — is fine (a
    # returning client can jump straight to step 4 via the clickable
    # progress bar, bypassing step 2 entirely, and step 4's own "нужен
    # прокси" markers need providerReachabilityByGroup filled once for
    # that path too — see goToStep()'s own comment). What the owner
    # actually flagged, and what this test still protects, is an
    # UNCONDITIONAL check firing the instant step 2 itself is entered.
    assert "if (n === 4 && state.providerReachabilityByGroup === null) runProxyCheck();" in go_to_step_body
    assert "if (n === 2)" not in go_to_step_body

    input_idx = html.index('byId("proxy").addEventListener("input"')
    input_handler_end = html.index("\n  });", input_idx)
    input_handler = html[input_idx:input_handler_end]
    assert "setTimeout" in input_handler
    assert 'setHidden("proxy-verdict", true)' in input_handler


def test_proxy_step_next_reruns_the_check_and_blocks_on_unreachable_telegram():
    """Спека B2: «При изменении поля и нажатии «Далее» проверка
    повторяется... если прокси введён, но через него Telegram по-прежнему
    недоступен — переход блокируется». The click handler must re-run the
    check (not just trust whatever verdict is already on screen) and only
    advance to step 3 when the fresh answer says Telegram is reachable."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index('byId("step-2-next").addEventListener')
    handler_end = html.index("\n  });", idx)
    handler = html[idx:handler_end]
    assert "runProxyCheck()" in handler
    assert "goToStep(3)" in handler
    assert "data.telegram" in handler


def test_proxy_verdict_texts_match_the_two_mockup_outcomes():
    """Экраны 2/2а из эталона — дословные тексты вердикта, плюс честная
    приписка про зарубежных провайдеров только когда они реально
    недоступны (спека: «если они недоступны», не безусловно)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    render_body = _function_body(html, "renderProxyVerdict")
    assert "Telegram доступен напрямую — прокси не нужен." in render_body
    assert "Telegram отсюда недоступен — нужен прокси." in render_body
    assert "missing.length" in render_body  # conditional addendum, not unconditional
    assert "via_proxy" in render_body


def test_submit_success_hides_the_progress_bar_strip():
    """Finding 10: #progress-bar is a sibling of <form id="main">, not a
    descendant of it, so hiding #main alone leaves it visible — the
    doSubmit() success branch must hide it explicitly too, or a client
    lands on "Готово!" with a fully checked-off step strip still shown
    above it."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    submit_start = html.index("function doSubmit() {")
    idx = html.index("if (data.ok) {", submit_start)
    end = html.index('setHidden("success", false);', idx)
    chunk = html[idx:end]
    assert 'setHidden("progress-bar", true);' in chunk
    assert 'setHidden("main", true);' in chunk


def test_saved_secret_placeholder_never_echoes_server_masked_value():
    """Owner feedback: a returning client must see a neutral "saved,
    leave blank to keep" placeholder for every secret field — never a
    fragment of the real value (the old `881098***W6f4`-style masked
    string `/api/form` sends in `current.*.masked`). The server still
    sends that field (untouched here), but page.py's JS must never read
    `secretState.masked`/`.masked` into the DOM."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert ".masked" not in html
    idx = html.index("function applySecretPlaceholderEl(")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert "masked" not in body
    # Owner feedback (later pass): the placeholder itself must be short
    # enough to fit inside the field (it used to overflow and render
    # clipped, e.g. "...оставьте пустым чт") — the full explanation moved
    # to a caption underneath instead. Both live in this function.
    assert 'input.placeholder = "Сохранён — не меняем";' in body
    assert "оставьте поле пустым, чтобы не менять сохранённое значение" in body


def test_saved_secret_note_element_exists_for_every_secret_field():
    """The caption `applySecretPlaceholderEl()` reveals
    (`byId(input.id + "_saved_note")`) must actually exist for both static
    secret fields (telegram_token, provider_api_key) — a lookup miss would
    silently drop the explanation with no error. Dynamically-built secret
    fields get theirs from `appendSecretField()` itself (asserted by
    reading that function's own source, not by rendering every category)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="telegram_token_saved_note"' in html
    assert 'id="provider_api_key_saved_note"' in html
    idx = html.index("function appendSecretField(")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert 'savedNote.id = id + "_saved_note";' in body


def test_telegram_step_has_no_check_button():
    """Spec B2: "Проверить" is gone from step 3 — the check runs itself
    (change/blur, and again — blocking — on "Далее")."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="telegram-check-btn"' not in html
    assert 'id="telegram-check-result"' not in html
    assert 'id="telegram-verdict"' in html


def test_telegram_verdict_cases_have_distinct_texts_differing_by_structural_fields():
    """Спека — таблица случаев отказа: пустое поле, неверный токен, «не
    добрались» без прокси, «не добрались» с прокси, и кривой формат
    прокси — пять РАЗНЫХ текстов, различаемых по data.network/
    data.proxy_invalid (структурные поля), а не по строке data.error."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    empty_body = _function_body(html, "runTelegramCheck")
    assert "Вставьте токен бота" in empty_body

    verdict_body = _function_body(html, "renderTelegramVerdict")
    assert "data.proxy_invalid" in verdict_body
    assert "data.network" in verdict_body
    # Branches read the STRUCTURAL flags, never data.error text.
    assert "data.error" not in verdict_body

    texts = {
        "empty": "Вставьте токен бота — его выдаёт @BotFather по команде /newbot.",
        "invalid_token": "Telegram не признаёт этот токен.",
        "network_no_proxy": "С этой машины не удалось связаться с Telegram.",
        "network_with_proxy": "Telegram не отвечает через указанный прокси.",
        "proxy_invalid": "Неверный формат прокси на шаге «Прокси»",
    }
    assert len(set(texts.values())) == len(texts), "все пять текстов должны различаться"
    for text in texts.values():
        assert text in (empty_body + verdict_body), text

    # network-with-proxy vs network-without-proxy is the one case gated on
    # a plain JS boolean (hasProxy), not on a response field — confirm both
    # branches exist and use different copy.
    assert "hasProxy" in verdict_body
    no_proxy_idx = verdict_body.index(texts["network_no_proxy"])
    with_proxy_idx = verdict_body.index(texts["network_with_proxy"])
    assert no_proxy_idx != with_proxy_idx


def test_return_mode_signal_is_telegram_token_is_set():
    """Spec §12.4's return-mode signal: current.telegram_token.is_set,
    decided once in loadForm() right after /api/form answers, and fed into
    the ONE step-wizard entry point (owner requirement 3: no separate
    one-page layout for a return visit any more — see
    test_return_mode_still_starts_in_the_step_wizard below)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "state.current.telegram_token && state.current.telegram_token.is_set" in html
    assert "enterStepsMode(isReturning)" in html


def test_return_mode_still_starts_in_the_step_wizard():
    """Owner requirement 3 (2026-08-21): the owner watched the return-visit
    flow live and rejected it — "you type the password and the whole sheet
    appears at once; why isn't it interactive, like the first visit?" The
    one-page "everything unhidden at once, no progress bar" layout
    (formerly ``enterReturnMode()``) is gone outright — a returning client
    goes through ``enterStepsMode()``, the same single entry point a
    first-run client uses, and lands on step 2 exactly like a first run.

    Replaces the old test_return_mode_unhides_every_step_at_once (asserted
    the now-deleted enterReturnMode() unhid every [data-step] at once) and
    test_steps_mode_shows_progress_bar_and_starts_at_step_two (asserted a
    now-stale zero-arg enterStepsMode() signature) — both described a
    two-layout design the owner explicitly rejected."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "function enterReturnMode() {" not in html
    assert "function enterStepsMode(isReturning) {" in html
    idx = html.index("function enterStepsMode(isReturning) {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert 'setHidden("progress-bar", false);' in body
    assert "goToStep(2);" in body
    # No per-mode branch inside enterStepsMode() unhiding every step at
    # once — the [data-step] sections stay individually hidden/shown by
    # goToStep(), whether this is a first run or a return visit.
    assert 'el.hidden = false' not in body


def test_progress_bar_appears_immediately_after_load_not_after_a_later_step():
    """Polish (owner review, 2026-08-20) marker, updated for spec 8, §8.3:
    there is no login screen any more, so ``#progress-bar`` starts
    ``hidden`` in the raw markup and becomes visible the moment
    enterStepsMode() runs — reached from loadForm(), called once at
    script start — at step 2, not deferred to step 3+. Pins the actual
    call ORDER inside enterStepsMode(): unhiding the bar happens before
    goToStep(2) moves the wizard onto step 2, so nothing renders the
    stepper mid-flight without its progress bar."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()

    bar_idx = html.index('id="progress-bar"')
    bar_tag = html[max(0, bar_idx - 30) : bar_idx + 30]
    assert "hidden" in bar_tag

    idx = html.index("function enterStepsMode(isReturning) {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    unhide_idx = body.index('setHidden("progress-bar", false);')
    goto_step_two_idx = body.index("goToStep(2);")
    assert unhide_idx < goto_step_two_idx, (
        "progress bar must be unhidden before the wizard lands on step 2, "
        "not after"
    )

    # enterStepsMode() itself is called synchronously inside loadForm()'s
    # single .then() (no setTimeout/deferred microtask reordering it past
    # a later step) — and loadForm() itself runs once, at script start,
    # not behind a login submit any more.
    bootstrap_idx = html.index("loadForm().catch(function (err) {")
    assert "enterStepsMode(isReturning)" in html[idx:body_end]
    assert bootstrap_idx > 0
    load_form_idx = html.index("function loadForm() {")
    load_form_end = html.index("\n  }", load_form_idx)
    assert "enterStepsMode(isReturning)" in html[load_form_idx:load_form_end]
    assert "setTimeout" not in html[load_form_idx:load_form_end]


# ---- Owner requirement 3: return visits are prefilled steps, and the ------
# progress bar's own clickability is the only thing that differs between a
# first run and a return visit. ---------------------------------------------


def test_progress_bar_is_fully_clickable_in_return_mode():
    """A returning client already has a fully configured agent — nothing
    further along the bar is "not set up yet", so every step (other than
    whichever step is currently open) is a real nav target the moment
    state.returning is true."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function isStepClickable(n) {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert "if (n < 2 || n === state.currentStep) return false;" in body
    assert "if (state.returning) return true;" in body


def test_first_run_progress_bar_only_unlocks_completed_steps():
    """A first-time client must walk every required field in order — the
    progress bar only lets them jump BACK to an already-completed step
    (n < state.currentStep); anything still ahead is locked, reachable
    only through that step's own "Далее" button, never by clicking ahead
    on the bar."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function isStepClickable(n) {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    # The one line reached once state.returning is falsy — the sole
    # first-run rule.
    assert "return n < state.currentStep;" in body


def test_progress_bar_marks_locked_future_steps_in_first_run():
    """Visual contract: a step strictly ahead of the current one, on a
    first run, gets the muted "locked" class (never "clickable") — see
    renderProgressBar()."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function renderProgressBar() {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert 'classes.push("clickable")' in body
    assert 'classes.push("locked")' in body
    assert ".step-item.locked { opacity: 0.45; }" in html
    assert ".step-item.clickable { cursor: pointer; }" in html


def test_field_errors_jump_to_the_earliest_step_with_an_error():
    """422 при «Готово» обязан прыгать на шаг с первым (самым ранним) полем
    ошибки — FIELD_STEP отображает каждое известное поле на его шаг."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "var FIELD_STEP = {" in html
    for field_id, step in (
        ("proxy", 2),
        ("telegram_token", 3),
        ("allowed_users", 3),
        ("provider_name", 4),
        ("provider_api_key", 4),
        ("fallback_name", 4),
        ("fallback_api_key", 4),
    ):
        assert f"{field_id}: {step}," in html
    idx = html.index("function showFieldErrors(errors) {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert 'if (state.mode === "steps" && firstStep !== null) goToStep(firstStep);' in body


def test_fallback_block_relocated_into_provider_step_for_first_run():
    """"+ Запасная модель" belongs at the bottom of step 4 (Провайдер) in
    the first-run stepper — a DOM move (appendChild), not a rebuild, so
    whatever the client already typed into it survives."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="step4-fallback-slot"' in html
    idx = html.index("function relocateFallbackBlockForSteps() {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert "slot.appendChild(block);" in body
    assert ".innerHTML" not in body


def test_done_button_lives_only_inside_step_six():
    """«Готово» — единственная кнопка submit — обязана жить только внутри
    шага 6, а не быть общей для всей формы, как в старой одностраничной
    вёрстке (Table 4's return-mode одностраничник — исключение: там она
    видна вместе со всем остальным, но всё равно физически находится
    внутри data-step="6")."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    step6_idx = html.index('data-step="6"')
    done_idx = html.index('id="done"')
    # rindex, not index — #done belongs to <form id="main">'s own closing
    # tag, the (only) </form> in the document.
    form_close_idx = html.rindex("</form>")
    assert step6_idx < done_idx < form_close_idx
    # Only one #done in the whole document — never duplicated per mode.
    assert html.count('id="done"') == 1


# ---- video_gen / generic tool_env, tool_provider -------------------------
# (owner ruling 2026-08-20 — see tools_view.py/apply.py's own docstrings)


def test_video_gen_block_is_a_single_select_with_off_option():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert '"video_gen_choice"' in html
    assert "Выключена (по умолчанию)" in html  # video_gen (shares text with image_gen)
    assert "Генерация видео" in html


def test_video_gen_container_exists_inside_advanced():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'id="advanced-video-gen"' in html


def test_x_search_and_homeassistant_dropped_from_the_wizard():
    """План A5/B4 (owner ruling 2026-08-23): «Поиск по X» и «Умный дом»
    настраиваются позже через CLI, не через мастер — ни один их след
    (контейнер, select, подписи) не должен остаться в served-разметке."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for needle in (
        'id="advanced-x-search"',
        'id="advanced-homeassistant"',
        '"x_search_choice"',
        '"homeassistant_choice"',
        "Поиск по X (Twitter)",
        "Умный дом",
        "Не подключён (по умолчанию)",
        "Выключен (по умолчанию)",  # x_search's own off-option label
    ):
        assert needle not in html, needle


# ---- Plan B4 (spec 7, 2026-08-23): "Дополнительно" as six collapsed rows -
# instead of eight always-expanded blocks — owner complaint verbatim was
# "опять лист целый" (the whole list again). See the approved mockup's
# screen 5 for the .rows/.row/.row-body markup this section's tests pin
# down, and docs/product/specs/2026-08-23-wizard-content-decisions.md's
# "Шаг 5. Дополнительно" for the content decisions.

# category key (tools_view.WIZARD_TOOL_CATEGORIES) -> the "advanced-*"
# container id SUFFIX page.py's static markup uses for it. This is a pure
# naming-convention correspondence (same idiom test_advanced_block_
# headings_consume_title_ru_with_owner_approved_fallback above already
# uses for TITLES_RU) — the CATEGORY SET/ORDER itself is never hardcoded
# here, it is read live from WIZARD_TOOL_CATEGORIES below.
_CATEGORY_BLOCK_ID_SUFFIX = {
    "browser": "browser",
    "web": "search",
    "web_extract": "extract",
    "tts": "voice",
    "stt": "stt",
    "image_gen": "image-gen",
    "video_gen": "video-gen",
}


def test_advanced_step_holds_exactly_the_seven_catalog_categories_in_order():
    """B4 (+ owner follow-up: search and page-reading are separate tools,
    split into separate rows): «ровно N категорий … в заданном порядке» —
    the wizard's composition must equal ``tools_view.WIZARD_TOOL_CATEGORIES``
    (the server's own authoritative list — six per plan A5, seven once
    "web_extract" split out of "web"), not a second, independently-maintained
    count/order baked into this test. If a category is ever added/removed/
    reordered upstream, this test starts asserting against the NEW list
    automatically — only the id-suffix naming correspondence above needs a
    maintainer's update."""
    from hermes_cli.setup_wizard.page import render_page
    from hermes_cli.setup_wizard.tools_view import WIZARD_TOOL_CATEGORIES

    assert len(WIZARD_TOOL_CATEGORIES) == 7
    assert set(WIZARD_TOOL_CATEGORIES) <= set(_CATEGORY_BLOCK_ID_SUFFIX), (
        "a category in WIZARD_TOOL_CATEGORIES has no id-suffix mapping above — "
        "update _CATEGORY_BLOCK_ID_SUFFIX before this test can mean anything"
    )

    html = render_page()
    advanced_idx = html.index('id="advanced" class="rows"')
    fallback_idx = html.index('id="advanced-fallback"')
    rows_chunk = html[advanced_idx:fallback_idx]

    expected_ids = ["advanced-%s" % _CATEGORY_BLOCK_ID_SUFFIX[category] for category in WIZARD_TOOL_CATEGORIES]
    # Exact composition AND order: every expected id present exactly once,
    # in catalog order, and no OTHER "advanced-*" container id sneaks in
    # (the x_search/homeassistant regression this whole plan closed,
    # caught structurally instead of by name).
    found_ids = re.findall(r'id="(advanced-[a-z-]+)"', rows_chunk)
    assert found_ids == expected_ids


def test_row_toggle_is_a_real_button_not_a_div_with_onclick():
    """Требование клавиатурной доступности: раскрытие/сворачивание строки
    — это настоящий <button type="button">, не <div onclick>. A native
    button gets Enter/Space handling and focus semantics from the browser
    for free — no bespoke keydown listener is needed (and none is
    written), unlike a div+role+tabindex stand-in would require."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "buildCollapsibleRow")
    assert 'document.createElement("button")' in body
    assert 'row.type = "button"' in body
    # No manual keydown handler — the native button doesn't need one.
    assert "keydown" not in body


def test_advanced_row_settings_label_is_screen_reader_only_not_a_visible_duplicate():
    """Owner feedback: opening a category row (e.g. "Браузер") repeated its
    own title as the settings <select>'s visible <label> right below —
    "как будто два браузера... и потом уже выбор". buildSelectRow() (used
    by all six render*Block() category settings, each already titled by
    its own buildCollapsibleRow() header) must keep the label for the
    for/id accessibility association but never render it a second time."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "buildSelectRow")
    assert 'label.className = "sr-only";' in body
    assert 'label.setAttribute("for", id);' in body
    assert 'label.textContent = labelText;' in body
    # The CSS class it relies on must actually be defined, and defined to
    # visually hide (not just re-color) — an absolute/clip-based hide, not
    # display:none (which would drop the accessible name entirely).
    assert ".sr-only {" in html
    css_idx = html.index(".sr-only {")
    css_end = html.index("}", css_idx)
    css_body = html[css_idx:css_end]
    assert "position: absolute" in css_body
    assert "display" not in css_body


@requires_node
def test_collapsible_row_state_reflects_the_live_select_never_a_stale_snapshot():
    """B4 требование 2: «текущее состояние в свёрнутой строке … берётся из
    того же источника, что и предвыбор селекта, — чтобы строка и
    раскрытое содержимое не могли разойтись». Runs the REAL
    buildCollapsibleRow() body against a tiny hand-rolled DOM stub (no
    jsdom dependency in this repo) and proves: (1) the header starts on
    computeState()'s value: while OPEN it always reads "Настраиваем"
    (the live value is already visible in the body itself); (2) on CLOSE
    it recomputes from computeState() fresh — never a value captured once
    at build time — demonstrated by mutating the callback's own return
    value between the two clicks and observing the header pick up the
    NEW value, not the original one."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "buildCollapsibleRow") + "\n}\n"
    script = (
        "function makeEl(tag) {\n"
        "  var el = { tagName: tag, children: [], hidden: false, textContent: '',\n"
        "    _classes: [], _attrs: {}, _listeners: {} };\n"
        "  el.appendChild = function (c) { el.children.push(c); return c; };\n"
        "  el.setAttribute = function (k, v) { el._attrs[k] = v; };\n"
        "  el.getAttribute = function (k) { return el._attrs[k]; };\n"
        "  el.addEventListener = function (t, fn) { el._listeners[t] = fn; };\n"
        "  el.click = function () { el._listeners.click({}); };\n"
        "  el.classList = {\n"
        "    toggle: function (cls, force) {\n"
        "      var i = el._classes.indexOf(cls);\n"
        "      var has = i !== -1;\n"
        "      var want = force === undefined ? !has : !!force;\n"
        "      if (want && !has) el._classes.push(cls);\n"
        "      if (!want && has) el._classes.splice(i, 1);\n"
        "      return want;\n"
        "    },\n"
        "    contains: function (cls) { return el._classes.indexOf(cls) !== -1; },\n"
        "  };\n"
        "  return el;\n"
        "}\n"
        "var document = { createElement: makeEl };\n"
        # buildCollapsibleRow() (this pass) registers every row it builds
        # into state.collapsibleRows for the accordion/close-on-navigate
        # behavior — this isolated harness has no other reason to touch
        # `state`, so a bare array stub is enough.
        "var state = { collapsibleRows: [] };\n"
        "%s\n"
        "var container = makeEl('div');\n"
        "var liveState = 'DuckDuckGo';\n"
        "var rowUI = buildCollapsibleRow(container, 'Поиск в интернете', function () { return liveState; });\n"
        # Every real render*Block() seeds the initial header itself via
        # rowUI.refreshState() right after building the <select> — building
        # the row alone leaves the state column blank until a caller does.
        "rowUI.refreshState();\n"
        "var row = container.children[0];\n"
        "var stateSpan = row.children[1];\n"
        "var body = container.children[1];\n"
        "var out = [];\n"
        "out.push(['initial', stateSpan.textContent, row.classList.contains('open'), body.hidden]);\n"
        "row.click();\n"  # open
        "out.push(['open', stateSpan.textContent, row.classList.contains('open'), body.hidden]);\n"
        "liveState = 'Brave Search';\n"  # client picked something else while open
        "row.click();\n"  # close
        "out.push(['closed', stateSpan.textContent, row.classList.contains('open'), body.hidden]);\n"
        "console.log(JSON.stringify(out));\n"
    ) % body
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert out[0] == ["initial", "DuckDuckGo", False, True]
    assert out[1] == ["open", "Настраиваем", True, False]
    # Closed again: the header picks up liveState's NEW value — proof it
    # re-reads the live source rather than an initial snapshot, so the
    # collapsed row and the (now-hidden-again) select it summarizes can
    # never disagree about what is actually selected.
    assert out[2] == ["closed", "Brave Search", False, True]


@requires_node
def test_select_state_text_strips_catalog_decoration_and_falls_back_honestly():
    """selectStateText() is the SAME function every render*Block() reads
    both the collapsed row's state AND (via the identical <select>) the
    open panel's preselect from — it cannot diverge from what buildPayload
    submits because it reads the live DOM element, not a copy. Catalog
    decoration ("(рекомендуется)", "(по умолчанию)") is stripped so the
    row reads as a plain value; an empty <select> (catalog never arrived)
    falls back to the caller's own honest, neutral text — never a
    fabricated value."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "selectStateText") + "\n}\n"
    script = (
        "%s\n"
        "var withOptions = { selectedIndex: 1, options: [\n"
        "  { textContent: 'Выключена (по умолчанию)' },\n"
        "  { textContent: 'FAL (рекомендуется)' },\n"
        "] };\n"
        "var empty = { selectedIndex: -1, options: [] };\n"
        "console.log(JSON.stringify([\n"
        "  selectStateText(withOptions, 'каталог недоступен'),\n"
        "  selectStateText(empty, 'каталог недоступен'),\n"
        "]));\n"
    ) % body
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert out == ["FAL", "каталог недоступен"]


def test_out_of_catalog_note_survives_to_the_collapsed_row_header():
    """Ревью-находка (pickPreselected/appendOutOfCatalogNote): a saved
    provider that fails to match any rendered <option> must show
    «настроено вручную» — including in the COLLAPSED row, not only in the
    open panel's note — otherwise a client would see a misleading default
    (e.g. "Выключена") on a category that is actually manually configured.
    Every one of the six render*Block()s that uses the out-of-catalog
    guard must special-case it in its own computeState() callback (the
    exact string "настроено вручную", matching appendOutOfCatalogNote()'s
    own wording), not fall through to selectStateText()'s generic
    placeholder-option text."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for name in ("renderBrowserBlock", "renderSearchBlock", "renderVoiceBlock", "renderSTTBlock", "renderImageGenBlock", "renderVideoGenBlock"):
        body = _function_body(html, name)
        assert "buildCollapsibleRow(container," in body, name
        assert 'if (picked.outOfCatalog && select.value === "") return "настроено вручную";' in body, name


def test_openai_codex_auth_hint_rendered_structurally_by_provider_key():
    """Owner ruling 2026-08-20: "OpenAI (Codex auth)" is recognized by its
    provider_key (a structural marker tools_view.py stamps on the row),
    never by display name — and shows a hint pointing at step 4
    ("Провайдер") instead of the generic "no settings here" fallback."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function renderImageGenBlock() {")
    body_end = html.index("\n  }\n", idx)
    body = html[idx:body_end]
    assert 'CODEX_AUTH_PROVIDER_KEY = "openai-codex"' in html
    assert "row.provider_key === CODEX_AUTH_PROVIDER_KEY" in body
    assert "Работает после входа по аккаунту ChatGPT (шаг «Провайдер»)." in body


def test_voice_select_carries_every_non_edge_tts_row():
    """Edge keeps its default/custom split (test_voice_select_has_
    custom_option_with_gated_name_field); every OTHER tts row is now an
    extra option in the SAME select, sharing renderProviderRowSettings()
    (owner ruling 2026-08-20)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function renderVoiceBlock() {")
    body_end = html.index("\n  }\n", idx)
    body = html[idx:body_end]
    assert 'EDGE_PROVIDER_KEY = "edge"' in html
    assert "row.provider_key !== EDGE_PROVIDER_KEY" in body
    assert "renderProviderRowSettings(settings, row, \"tts\", current" in body


def test_stt_select_defaults_to_local_whisper():
    """"local" (Local Whisper) is the STT category's always-active default
    — same "always has a default provider" shape as tts/edge, never an
    "off" state (image_gen/video_gen)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function renderSTTBlock() {")
    body_end = html.index("\n  }\n", idx)
    body = html[idx:body_end]
    assert 'STT_DEFAULT_KEY = "local"' in html
    assert "current.stt_provider || STT_DEFAULT_KEY" in body
    assert 'renderProviderRowSettings(settings, row, "stt", current' in body


def test_generic_tool_env_and_tool_provider_wired_into_payload():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function buildPayload() {")
    body_end = html.index("\n  }\n", idx)
    body = html[idx:body_end]
    assert "tool_env: toolEnvPayload()" in body
    assert "tts: ttsProviderChoiceValue()" in body
    assert "stt: sttProviderChoiceValue()" in body
    assert "image_gen: imageGenProviderChoiceValue()" in body
    assert "video_gen: videoGenProviderChoiceValue()" in body
    # план A5/B4: x_search (and hass) dropped from the wizard entirely —
    # the returned object literal must not carry either key any more (a
    # narrower "<name>:" check, not a bare substring one, since the
    # surrounding comment explaining WHY still legitimately names both).
    assert "x_search:" not in body
    assert "hass:" not in body


def test_tool_env_payload_namespaces_field_ids_per_category():
    """Each provider-select category's env field must use its OWN id
    ("tool_env_value__" + category) — a bare shared id would collide since
    renderVoiceBlock/renderSTTBlock/renderImageGenBlock/renderVideoGenBlock
    all render into the same page at once. Both the reader
    (toolEnvPayload) and the writer (renderProviderRowSettings) build the
    id from the SAME expression — checked by asserting the expression
    string appears in both function bodies, not by asserting a literal
    per-category id (which would never appear verbatim — it's always
    concatenated at runtime)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    read_idx = html.index("function toolEnvPayload() {")
    read_body_end = html.index("\n  }\n", read_idx)
    read_body = html[read_idx:read_body_end]
    assert '"tool_env_value__" + category' in read_body
    # план A5/B4: x_search dropped — only four categories left.
    assert '"tts", "stt", "image_gen", "video_gen"' in read_body

    write_idx = html.index("function renderProviderRowSettings(settings, row, category, current, opts) {")
    write_body_end = html.index("\n  }\n", write_idx)
    write_body = html[write_idx:write_body_end]
    assert '"tool_env_value__" + category' in write_body


def test_render_provider_row_settings_shared_by_all_provider_select_blocks():
    """The generic settings renderer is a single function, called from
    every provider-select block it was built for — proves the "one
    mechanism, not one per category" contract (task ruling) at the JS
    level, not just in apply.py. "stt" joined 2026-08-20 alongside tts/
    image_gen/video_gen; x_search was dropped from the wizard entirely
    (план A5/B4) and must not be among the call sites."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert "function renderProviderRowSettings(settings, row, category, current, opts)" in html
    # 1 definition + 4 call sites (tts/stt/image_gen/video_gen).
    assert html.count("renderProviderRowSettings(settings, row,") == 5
    assert "function renderXSearchBlock" not in html


def test_success_and_progress_are_not_descendants_of_the_form():
    """Regression: #progress/#success used to be nested inside <div
    id="step-6"> inside <form id="main"> — the moment /api/submit
    succeeds, the click handler on #done sets `main.hidden = true`, which
    (no author CSS override for a hidden <form>) also hides every
    descendant, including the "Готово!" screen itself. A client who just
    finished setup saw a blank page and never got the bot link. Fixed by
    making #progress/#success top-level siblings of the form again — a
    real structural (parent-chain) check, so it can't be fooled by markup
    that merely mentions both ids near each other in the source text."""
    from hermes_cli.setup_wizard.page import render_page

    parser = _StructureParser()
    parser.feed(render_page())
    for field_id in ("progress", "success"):
        ancestors = parser.ancestor_ids_by_id[field_id]
        assert "main" not in ancestors, f"#{field_id} is nested inside <form id='main'>: {ancestors}"


def test_done_button_is_type_button_not_implicit_submit():
    """Regression: every field on steps 2-5 lives in the same <form
    id="main">. With #done as the form's own type="submit" button, HTML
    implicit submission meant pressing Enter in ANY of those fields
    (Telegram token, allowed_users, proxy, provider api key/model, any
    dynamic tool_env field) fired a live POST /api/submit from wherever
    the client happened to be. #done must be a plain button now — the
    click handler drives submission, and the form's own submit listener is
    insurance only (preventDefault, no side effects)."""
    from hermes_cli.setup_wizard.page import render_page

    parser = _StructureParser()
    parser.feed(render_page())
    assert parser.attrs_by_id["done"].get("type") == "button"


def test_fallback_field_errors_have_matching_err_elements():
    """FIELD_STEP maps fallback.name/fallback.api_key to step 4, but
    showFieldErrors() only highlights a field (or jumps to its step) when
    the matching #err_<fieldId> element actually exists — same contract as
    the static #err_provider_name/#err_provider_api_key next to the
    provider fields. renderAdvancedFallback() must build both
    #err_fallback_name and #err_fallback_api_key, or a 422 on either
    fallback field silently falls through to the generic #form-error
    banner instead of surfacing on the right field/step. These two are
    built client-side (document.createElement), not part of the static
    markup, so — like test_tool_env_payload_namespaces_field_ids_per_category
    above — this reads render_page()'s own <script> output, not the .py
    source file."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    start = html.index("function renderAdvancedFallback() {")
    end = html.index("\n  }\n", start)
    body = html[start:end]
    assert 'fallbackNameErr.id = "err_fallback_name"' in body
    assert 'fallbackKeyErr.id = "err_fallback_api_key"' in body


def _function_body(html: str, name: str) -> str:
    """Same "\\n  }\\n" top-level-close convention already used above for
    renderAdvancedFallback — every render*Block()/append*() function in
    page.py's ``_JS`` is indented 2 spaces at its own level, so its own
    closing brace is the first line that is exactly two spaces + "}"
    after the function's opening. Reads render_page()'s emitted <script>
    output (the served artifact), not the .py source file — same
    distinction the docstring above already draws."""
    start = html.index("function %s(" % name)
    end = html.index("\n  }\n", start)
    return html[start:end]


def test_stt_voice_image_gen_video_gen_selects_use_the_out_of_catalog_guard():
    """Finding 4: a saved provider that isn't among the rendered rows (an
    OAuth-only row the structural rule hid, or a plugin that failed to
    load) used to make stt_choice/voice_choice fall straight back to a
    real, submittable default ("local"/"edge") — an untouched resubmit
    then silently overwrote stt.provider/tts.provider. image_gen_choice/
    video_gen_choice already degraded to a safe empty-string no-op via
    their "off" option, but still lied to the client by showing
    "Выключена" for a provider that is actually configured. All four now
    route through the same pickPreselected()/appendOutOfCatalogNote()
    mechanism browser_choice/search_choice already used — this is a
    per-block wiring check, not just the file-wide substring check
    test_out_of_catalog_backend_note_mechanism_present already does."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for name in ("renderVoiceBlock", "renderSTTBlock", "renderImageGenBlock", "renderVideoGenBlock"):
        body = _function_body(html, name)
        assert "pickPreselected(" in body, name
        assert "appendOutOfCatalogNote(settings," in body, name


def test_provider_select_rowbykey_first_duplicate_wins_in_all_four_blocks():
    """Finding 17: a duplicate provider_key in the live catalog (upstream
    has real ones — see tools_view.py's module docstring on
    ``requires_nous_auth`` rows carrying the same tts_provider/stt_provider
    as a BYOK row before the nous rule strips them) used to let the LAST
    row silently win the rowByKey lookup while <select> keeps the FIRST
    matching <option> selected by value — settings panel and dropdown
    would then disagree about which row is active. All four remaining
    provider-select category blocks (tts/stt/image_gen/video_gen —
    x_search dropped from the wizard entirely, план A5/B4) must skip a row
    whose key already has an entry, so the FIRST row wins for both the
    option list and the settings lookup."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for name in (
        "renderVoiceBlock",
        "renderSTTBlock",
        "renderImageGenBlock",
        "renderVideoGenBlock",
    ):
        body = _function_body(html, name)
        guard_idx = body.index("if (rowByKey.hasOwnProperty(key)) return;")
        assign_idx = body.index("rowByKey[key] = row;")
        assert guard_idx < assign_idx, name


def test_provider_row_settings_and_search_block_prefer_russian_env_prompt():
    """Finding 9: env.prompt is catalog English ("OpenAI API key", "Camofox
    server URL", …) written for `hermes tools`' English CLI — wrong for
    this Russian wizard. tools_view.py now stamps env.prompt_ru from
    RU_ENV_PROMPTS; renderProviderRowSettings() (shared by tts/stt/
    image_gen/video_gen) and renderSearchBlock()'s generic env field must
    read prompt_ru, with a fallback to a generic Russian label — never to
    the raw English env.prompt string."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    settings_body = _function_body(html, "renderProviderRowSettings")
    assert "env.prompt_ru" in settings_body
    assert "env.prompt ||" not in settings_body
    assert "env.prompt)" not in settings_body

    search_body = _function_body(html, "renderSearchBlock")
    assert search_body.count("env.prompt_ru ||") == 2
    assert "env.prompt ||" not in search_body


def test_advanced_block_headings_consume_title_ru_with_owner_approved_fallback():
    """Finding 16: tools_view.wizard_tool_blocks() already sends a
    ``title_ru`` per block (from TITLES_RU) — the client used to ignore it
    and print its own hardcoded heading instead, and the two had already
    drifted (TITLES_RU["image_gen"] == "Генерация картинок" vs page.py's
    "Генерация изображений"). Every advanced-block heading must now read
    ``block.title_ru`` first; the hardcoded string survives only as the
    fallback for a category the server omitted, so it must equal
    TITLES_RU's current value exactly — page.py is the text source of
    truth (per the project's owner-approved wording), and TITLES_RU is
    kept in sync with it, not the other way around."""
    from hermes_cli.setup_wizard.page import render_page
    from hermes_cli.setup_wizard.tools_view import TITLES_RU, WIZARD_TOOL_CATEGORIES

    html = render_page()
    assert set(TITLES_RU) >= set(WIZARD_TOOL_CATEGORIES)
    for category, title in TITLES_RU.items():
        if category not in WIZARD_TOOL_CATEGORIES:
            continue
        fallback = '(block && block.title_ru) || "%s"' % title
        assert fallback in html, category


def test_install_status_shows_pending_note_and_blocked_reason():
    """Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"):
    appendInstallStatus() replaced appendInstallControl() — no button, no
    orphan-guard/in-flight-install race to worry about at all any more
    (installing now happens unattended at submit time). What's left to
    check is that a not-yet-installed row with an install hook gets an
    honest note, and — when tools_view.py flagged the row install_blocked
    — that reason is folded into the same note (see appendRowCaveats()
    for the SEPARATE place the same reason surfaces, in the row's own
    <option> label, before the client even picks it)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "appendInstallStatus")
    assert "row.installed" in body
    assert "install_blocked" in body
    assert "install_blocked_reason_ru" in body
    assert "appendMutedNote" in body


# ---- Return-mode clear-signal contract (finding 5/7): "не тронул -> не ---
# очистилось" / "явно выключил -> очистилось" for each of the five
# controls that can now send an explicit `null`. String-matching the
# branch text (as most tests above do) can't tell "returns null" apart
# from "returns an empty string" — these actually RUN the exact function
# body render_page() emitted (extracted the same way _function_body reads
# every other test's target above) under Node, with a minimal byId()/
# state stand-in for the real DOM/fetch state. This is executing the
# served artifact, not "reading source as text": the assertions are on a
# real return value from real control flow, for real (untouched vs.
# deliberate) inputs. (requires_node itself is hoisted next to the imports
# at the top of this file — see the comment there.)


def _call_extracted_fn(html: str, fn_name: str, *, dom: dict, call: str, extra: str = "") -> object:
    # _function_body() (see its own docstring) stops at the START of the
    # function's closing "\n  }\n" — exactly right for the substring
    # assertions every other test in this file does, but it means the
    # slice itself is missing its own closing brace; every caller here
    # actually EXECUTES the body, so put it back.
    body = _function_body(html, fn_name) + "\n}\n"
    script = (
        "function byId(id) {\n"
        "  var DOM = %s;\n"
        "  if (!Object.prototype.hasOwnProperty.call(DOM, id) || DOM[id] === null) return null;\n"
        "  return { value: DOM[id] };\n"
        "}\n"
        'var CAMOFOX_VALUE = "camofox";\n'
        'var CAMOFOX_ENV_VAR = "CAMOFOX_URL";\n'
        'var VOICE_DEFAULT_NAME = "ru-RU-SvetlanaNeural";\n'
        # Camofox's address is no longer an input the client fills in; it
        # comes from the catalog's `auto_default`. A caller that needs it
        # supplies its own toolBlockFor() through `extra`; this stub is the
        # "catalog said nothing" default so unrelated callers still run.
        "if (typeof toolBlockFor === 'undefined') { var toolBlockFor = function () { return null; }; }\n"
        "function autoDefaultFor(row, envKey) {\n"
        "  var envs = (row && row.env_vars) || [];\n"
        "  for (var i = 0; i < envs.length; i++) {\n"
        "    if (envs[i] && envs[i].key === envKey) return envs[i].auto_default || '';\n"
        "  }\n"
        "  return '';\n"
        "}\n"
        "%s\n"
        "%s\n"
        "console.log(JSON.stringify(%s));\n"
    ) % (json.dumps(dom), extra, body, call)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@requires_node
def test_camofox_url_payload_untouched_states_never_clear():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()

    # Never used Camofox — browser select stays on its default "off".
    out = _call_extracted_fn(
        html,
        "camofoxUrlPayload",
        dom={"browser_choice": "off", "camofox_url": None},
        extra='var state = { current: { browser_backend: "off", camofox_url: "" } };',
        call="camofoxUrlPayload()",
    )
    assert out == ""

    # Camofox is active and still selected — untouched resubmit writes the
    # standard address through, not null. The client is never asked for it
    # (owner ruling): it comes from the catalog's own `auto_default`, so
    # there is no #camofox_url input to read any more.
    out = _call_extracted_fn(
        html,
        "camofoxUrlPayload",
        dom={"browser_choice": "camofox"},
        extra=(
            'var state = { current: { browser_backend: "off", camofox_url: "http://localhost:9377" } };\n'
            "var toolBlockFor = function () {\n"
            '  return { rows: [{ env_vars: [{ key: "CAMOFOX_URL", auto_default: "http://localhost:9377" }] }] };\n'
            "};"
        ),
        call="camofoxUrlPayload()",
    )
    assert out == "http://localhost:9377"

    # Catalog handed us no address this time (probe unavailable): fall back
    # to whatever is already saved rather than clearing a working setup.
    out = _call_extracted_fn(
        html,
        "camofoxUrlPayload",
        dom={"browser_choice": "camofox"},
        extra=(
            'var state = { current: { browser_backend: "off", camofox_url: "http://localhost:9377" } };\n'
            "var toolBlockFor = function () { return { rows: [] }; };"
        ),
        call="camofoxUrlPayload()",
    )
    assert out == "http://localhost:9377"

    # An out-of-catalog manual browser_backend the client hasn't touched —
    # renders as select.value === "" (see pickPreselected()) — must stay
    # the ordinary no-op even though Camofox was previously active; this
    # is NOT a deliberate pick of anything.
    out = _call_extracted_fn(
        html,
        "camofoxUrlPayload",
        dom={"browser_choice": "", "camofox_url": None},
        extra='var state = { current: { browser_backend: "off", camofox_url: "http://localhost:9377" } };',
        call="camofoxUrlPayload()",
    )
    assert out == ""


@requires_node
def test_camofox_url_payload_explicit_chromium_clears_an_active_camofox():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    out = _call_extracted_fn(
        html,
        "camofoxUrlPayload",
        dom={"browser_choice": "off", "camofox_url": None},
        extra='var state = { current: { browser_backend: "off", camofox_url: "http://localhost:9377" } };',
        call="camofoxUrlPayload()",
    )
    assert out is None


@requires_node
def test_camofox_url_payload_explicit_browser_use_clears_an_active_camofox():
    """Finding 6 (owner-approved fix): the old version only cleared
    CAMOFOX_URL for the "off" (Chromium) pick — switching to "Browser Use"
    instead left a stale saved CAMOFOX_URL in place, so
    tools/browser_camofox.py's is_camofox_mode() stayed true and Browser
    Use never actually took effect. Any deliberate pick of a row other
    than Camofox must clear it now, not just "off"."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    out = _call_extracted_fn(
        html,
        "camofoxUrlPayload",
        dom={"browser_choice": "browser-use", "camofox_url": None},
        extra='var state = { current: { browser_backend: "off", camofox_url: "http://localhost:9377" } };',
        call="camofoxUrlPayload()",
    )
    assert out is None


@requires_node
@pytest.mark.parametrize(
    "fn_name,select_id,current_key",
    [
        ("imageGenProviderChoiceValue", "image_gen_choice", "image_gen_provider"),
        ("videoGenProviderChoiceValue", "video_gen_choice", "video_gen_provider"),
        # extractBackendChoiceValue shares the exact same "off + rows"
        # clear-signal contract — see renderExtractBlock()'s own comment
        # on why "Чтение страниц" (unlike "Поиск в интернете") has a real,
        # legitimate "never configured" state.
        ("extractBackendChoiceValue", "extract_choice", "extract_backend"),
    ],
)
def test_image_video_gen_choice_untouched_states_never_clear(fn_name, select_id, current_key):
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()

    # Never configured — select defaults to "off".
    out = _call_extracted_fn(
        html,
        fn_name,
        dom={select_id: "off"},
        extra='var state = { current: { %s: "" } };' % current_key,
        call="%s()" % fn_name,
    )
    assert out == ""

    # Still on the saved provider — untouched resubmit writes it through.
    out = _call_extracted_fn(
        html,
        fn_name,
        dom={select_id: "fal"},
        extra='var state = { current: { %s: "fal" } };' % current_key,
        call="%s()" % fn_name,
    )
    assert out == "fal"


@requires_node
@pytest.mark.parametrize(
    "fn_name,select_id,current_key",
    [
        ("imageGenProviderChoiceValue", "image_gen_choice", "image_gen_provider"),
        ("videoGenProviderChoiceValue", "video_gen_choice", "video_gen_provider"),
        ("extractBackendChoiceValue", "extract_choice", "extract_backend"),
    ],
)
def test_image_video_gen_choice_explicit_off_clears_a_saved_provider(fn_name, select_id, current_key):
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    out = _call_extracted_fn(
        html,
        fn_name,
        dom={select_id: "off"},
        extra='var state = { current: { %s: "fal" } };' % current_key,
        call="%s()" % fn_name,
    )
    assert out is None


@requires_node
def test_tts_voice_payload_untouched_states_never_clear():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()

    # Never customized — select stays on "default".
    out = _call_extracted_fn(
        html,
        "ttsVoicePayload",
        dom={"voice_choice": "default"},
        extra='var state = { current: { tts_voice: "" } };',
        call="ttsVoicePayload()",
    )
    assert out == ""

    # Already on the default voice explicitly (== VOICE_DEFAULT_NAME) —
    # not a "custom" save to clear.
    out = _call_extracted_fn(
        html,
        "ttsVoicePayload",
        dom={"voice_choice": "default"},
        extra='var state = { current: { tts_voice: "ru-RU-SvetlanaNeural" } };',
        call="ttsVoicePayload()",
    )
    assert out == ""

    # Still on "custom" with the saved name prefilled — untouched resubmit
    # writes the same name through, not null.
    out = _call_extracted_fn(
        html,
        "ttsVoicePayload",
        dom={"voice_choice": "custom", "tts_voice": "ru-RU-DmitryNeural"},
        extra='var state = { current: { tts_voice: "ru-RU-DmitryNeural" } };',
        call="ttsVoicePayload()",
    )
    assert out == "ru-RU-DmitryNeural"


@requires_node
def test_tts_voice_payload_explicit_default_overwrites_a_saved_custom_voice():
    """Finding 2 (owner-approved fix, reversed from an earlier design):
    this used to send `null` — a clear signal apply.py turned into
    `del cfg["tts"]["edge"]["voice"]`, which handed the agent
    DEFAULT_CONFIG's English baseline voice instead of Светлана (see
    apply.py's own docstring). The client now sends the literal default
    voice name explicitly instead."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    out = _call_extracted_fn(
        html,
        "ttsVoicePayload",
        dom={"voice_choice": "default"},
        extra='var state = { current: { tts_voice: "ru-RU-DmitryNeural" } };',
        call="ttsVoicePayload()",
    )
    assert out == "ru-RU-SvetlanaNeural"


# ---- Plan B1 (spec 7, 2026-08-23 — wizard redesign): sidebar rail, step ---
# reorder (Прокси before Telegram), and the collapsed/verbatim cert
# disclosure. See docs/product/plans/2026-08-23-wizard-redesign-plan.md
# and the approved mockup at
# docs/product/specs/assets/2026-08-23-wizard-approved-mockup.html.


def test_logo_is_inline_svg_not_an_external_reference():
    """Инвариант «без CDN и внешних запросов»: логотип XDataPlus — это
    инлайновый SVG (путь скопирован из эталонного макета), а не картинка
    по ссылке или внешний ресурс."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert html.count('aria-label="XDataPlus"') == 1
    assert "<img" not in html
    logo_id_idx = html.index('id="rail-logo"')
    svg_start = html.index("<svg", logo_id_idx)
    svg_end = html.index("</svg>", svg_start) + len("</svg>")
    svg_chunk = html[svg_start:svg_end]
    assert "<path" in svg_chunk
    assert "http://" not in svg_chunk
    assert "https://" not in svg_chunk
    assert "<image" not in svg_chunk
    assert "xlink:href" not in svg_chunk


def test_rail_logo_shown_only_on_login_and_success():
    """Спека 7 / план B1: логотип виден на экране входа и на экране
    успеха, но убран с полотна во время самих рабочих шагов (2-6) —
    setRailMode() гасит #rail-logo только в режиме "steps"."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("function setRailMode(mode) {")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert 'setHidden("rail-logo", isSteps);' in body
    # The login screen's static markup never hides it either.
    logo_idx = html.index('id="rail-logo"')
    tag = html[logo_idx : logo_idx + 40]
    assert "hidden" not in tag


def test_cert_disclosure_full_text_only_in_rail_reveal():
    """Спека 6 §5 / план B1, updated by spec 8, §8.3: there is no separate
    login screen any more to always-show the self-signed-certificate
    disclosure in full, so its ONE home now is the rail's collapsed
    ``#cert-detail`` — revealed verbatim (same ``_header_intro`` call, not
    a rewording) behind the compact "подробнее" link."""
    from hermes_cli.setup_wizard.page import _header_intro, render_page

    html = render_page()
    full_text = _header_intro(None)

    # Exactly one copy of the verbatim sentence now — the collapsed one
    # inside the rail's "подробнее" reveal — never a second, reworded
    # summary elsewhere on the page.
    assert html.count(full_text) == 1

    detail_idx = html.index('id="cert-detail"')
    detail_tag = html[max(0, detail_idx - 20) : detail_idx + 40]
    assert "hidden" in detail_tag
    detail_end = html.index("</div>", detail_idx)
    assert full_text in html[detail_idx:detail_end]

    # The compact line actually visible while a step is open is short and
    # distinct from the full paragraph — not the same sentence repeated.
    assert 'id="rail-foot-cert-line" hidden' in html
    assert "соединение зашифровано" in html
    assert 'id="cert-toggle">подробнее<' in html


def test_cert_toggle_reveals_the_hidden_detail_without_navigating():
    """The "подробнее" link must be a client-side reveal (toggles
    #cert-detail's `hidden`), never a real navigation away from the page —
    critical since #cert-detail carries the ONLY copy of the disclosure
    text visible while a step is open."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    idx = html.index("var certToggle = byId(")
    body_end = html.index("\n  }", idx)
    body = html[idx:body_end]
    assert "e.preventDefault();" in body
    assert "detail.hidden = !detail.hidden;" in body


def test_step_order_proxy_before_telegram():
    """План B1: шаг 2 = Прокси, шаг 3 = Telegram (раньше было наоборот) —
    и содержимое [data-step] секций, и порядок в STEPS/FIELD_STEP должны
    совпадать."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    step2_idx = html.index('data-step="2"')
    step3_idx = html.index('data-step="3"')
    step4_idx = html.index('data-step="4"')
    step2_chunk = html[step2_idx:step3_idx]
    step3_chunk = html[step3_idx:step4_idx]
    assert 'id="proxy"' in step2_chunk
    assert 'id="telegram_token"' not in step2_chunk
    assert 'id="telegram_token"' in step3_chunk
    assert 'id="allowed_users"' in step3_chunk
    assert 'id="proxy"' not in step3_chunk

    steps_idx = html.index("var STEPS = [")
    steps_end = html.index("];", steps_idx)
    steps_body = html[steps_idx:steps_end]
    assert steps_body.index('label: "Прокси"') < steps_body.index('label: "Telegram"')

    field_step_idx = html.index("var FIELD_STEP = {")
    field_step_end = html.index("};", field_step_idx)
    field_step_body = html[field_step_idx:field_step_end]
    assert "proxy: 2," in field_step_body
    assert "telegram_token: 3," in field_step_body
    assert "allowed_users: 3," in field_step_body


def test_every_step_has_its_own_heading_and_one_line_of_meaning():
    """Спека: у каждого шага — заголовок (h2.screen-title) и одна строка
    смысла (p.screen-sub); раньше внутри формы не было ни одного <h2>.

    Spec 8, §8.3 removed the login screen (there is no [data-step]-less
    "step 1" any more) — every step lives on a [data-step] wrapper now.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for step_n in (2, 3, 4, 5, 6):
        step_idx = html.index(f'data-step="{step_n}"')
        window = html[step_idx : step_idx + 400]
        assert 'class="screen-title"' in window, step_n
        assert 'class="screen-sub"' in window, step_n


def test_rail_present_and_not_nested_inside_main_progress_or_success():
    """Боковая колонка (.rail) — это независимый сосед #main/#progress/
    #success внутри .canvas, а не их потомок, и не потомок боковой
    колонки сам."""
    from hermes_cli.setup_wizard.page import render_page

    parser = _StructureParser()
    parser.feed(render_page())
    assert "rail" in parser.attrs_by_id
    for field_id in ("main", "progress", "success"):
        ancestors = parser.ancestor_ids_by_id.get(field_id, [])
        assert "rail" not in ancestors, field_id


# ---- Plan B5: step 6 summary / honest progress stages / success screen --

def test_launch_button_reads_as_an_action_not_generic_done():
    """Спека: кнопка на шаге 6 называется по действию, а не «Готово» —
    и остаётся единственной кнопкой с id="done" (submit-путь не менялся,
    только надпись)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert '<button type="button" id="done" class="accent">Запустить агента</button>' in html
    assert html.count('id="done"') == 1


def test_step_six_shows_pre_launch_summary_and_duration_warning():
    """B5 п.1/2: сводка (#summary-rows) и честное предупреждение «до пяти
    минут» — оба внутри шага 6, до кнопки запуска, текст из макета
    дословно."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    step6_idx = html.index('data-step="6"')
    form_close_idx = html.rindex("</form>")
    step6_chunk = html[step6_idx:form_close_idx]
    assert 'id="summary-rows"' in step6_chunk
    assert "до пяти минут" in step6_chunk
    assert "Не закрывайте страницу." in step6_chunk
    summary_idx = step6_chunk.index('id="summary-rows"')
    note_idx = step6_chunk.index("до пяти минут")
    done_idx = step6_chunk.index('id="done"')
    # Order matters: summary, then the warning, then the button — a client
    # reads what they are about to launch before being told how long it
    # takes and before the button that starts it.
    assert summary_idx < note_idx < done_idx


def test_summary_rows_wire_edit_links_to_the_right_step():
    """B5 п.1: каждая строка сводки ведёт «изменить» на СВОЙ шаг через
    уже существующий goToStep() — не на случайный/общий."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "renderSummary")
    assert 'summaryRow("Бот", summaryBotValue(), 3)' in body
    assert 'summaryRow("Пишет боту", summaryAllowedUsersValue(), 3)' in body
    assert 'summaryRow("Прокси", summaryProxyValue(), 2)' in body
    assert 'summaryRow("Модель", summaryModelValue(), 4)' in body
    assert 'summaryRow("Дополнительно", summaryAdvancedValue(), 5)' in body


def test_go_to_step_renders_the_summary_on_entering_step_six():
    """Сводка должна пересобираться при КАЖДОМ входе на шаг 6 — и через
    "Далее" с шага 5, и через клик по прогресс-бару в режиме возврата.
    goToStep() — единственная точка входа на любой шаг (см. её же
    комментарий), поэтому вызов живёт там, а не в одном конкретном
    обработчике."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "goToStep")
    assert "if (n === 6) renderSummary();" in body


def test_summary_functions_never_read_secret_fields():
    """B5: «секреты в сводке не показывать даже частично» — структурная
    страховка поверх поведенческих тестов ниже: ни одна из функций,
    формирующих значения строк сводки, не читает provider_api_key /
    fallback_api_key / telegram_token напрямую (только не-секретные поля
    и честные is_set/checked-сигналы)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for name in ("summaryModelValue", "summaryAllowedUsersValue", "summaryAdvancedValue", "summaryProxyValue"):
        body = _function_body(html, name)
        assert "provider_api_key" not in body, name
        assert "fallback_api_key" not in body, name
        assert "telegram_token" not in body, name


@requires_node
def test_mask_proxy_credentials_hides_password_but_keeps_host():
    """B5: прокси может нести user:pass прямо в строке — маскируется
    ТОЛЬКО этот сегмент (secret), схема и host:port остаются видимыми,
    чтобы клиент мог свериться, что это тот прокси."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "maskProxyCredentials") + "\n}\n"
    script = (
        "%s\n"
        'console.log(JSON.stringify(maskProxyCredentials('
        '"socks5://myuser:MySecretPass1@203.0.113.5:1080")));\n'
    ) % body
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert "MySecretPass1" not in out
    assert "myuser" not in out
    assert out == "socks5://···@203.0.113.5:1080"


@requires_node
def test_mask_proxy_credentials_leaves_a_proxy_with_no_userinfo_untouched():
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    body = _function_body(html, "maskProxyCredentials") + "\n}\n"
    script = '%s\nconsole.log(JSON.stringify(maskProxyCredentials("http://203.0.113.5:8080")));\n' % body
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == "http://203.0.113.5:8080"


def _call_summary_bot_value(html: str, telegram_check, telegram_token: str) -> str:
    """``telegram_check`` mirrors ``state.telegramCheck`` (spec B2's
    autocheck result — see runTelegramCheck()/renderTelegramVerdict() in
    page.py): ``None`` (never checked / invalidated by an edit), or a
    dict like ``{"ok": True, "username": "test_raf3_bot"}`` /
    ``{"ok": False}``."""
    body = _function_body(html, "summaryBotValue") + "\n}\n"
    dom = {"telegram_token": telegram_token}
    script = (
        "function byId(id) {\n"
        "  var DOM = %s;\n"
        "  if (!Object.prototype.hasOwnProperty.call(DOM, id) || DOM[id] === null) return null;\n"
        "  return { value: DOM[id], textContent: DOM[id] };\n"
        "}\n"
        "var state = { current: {}, telegramCheck: %s };\n"
        "%s\n"
        "console.log(JSON.stringify(summaryBotValue()));\n"
    ) % (json.dumps(dom), json.dumps(telegram_check), body)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@requires_node
def test_summary_bot_value_reports_unverified_without_echoing_the_token():
    """B5: токен впечатан, но эта сессия его ещё не проверяла (change/blur
    check не прошёл, или ещё не выполнялся) — сводка честно говорит «не
    проверен», а не подглядывает в само значение токена."""
    from hermes_cli.setup_wizard.page import render_page

    out = _call_summary_bot_value(render_page(), None, "123456:SUPER-SECRET-VALUE")
    assert "SUPER-SECRET-VALUE" not in out
    assert "123456" not in out
    assert out == "сохранённый бот — имя покажем после запуска"


@requires_node
def test_summary_bot_value_shows_username_only_after_a_real_check_succeeded():
    """Обратная сторона предыдущего теста: имя бота показывается ТОЛЬКО
    когда автопроверка токена (спека B2) уже подтвердила его в этой
    сессии — не запрос к серверу, честное переиспользование уже
    показанного результата (``state.telegramCheck``)."""
    from hermes_cli.setup_wizard.page import render_page

    out = _call_summary_bot_value(render_page(), {"ok": True, "username": "test_raf3_bot"}, "")
    assert out == "@test_raf3_bot"


@requires_node
def test_summary_bot_value_ignores_a_failed_check():
    """A failed check (state.telegramCheck.ok === false) must not surface
    a username — even if some earlier successful check briefly held one,
    ok:false is the authoritative "this isn't verified" signal."""
    from hermes_cli.setup_wizard.page import render_page

    out = _call_summary_bot_value(render_page(), {"ok": False}, "123456:abc")
    assert out == "сохранённый бот — имя покажем после запуска"


@requires_node
def test_summary_bot_value_honest_when_nothing_is_set_at_all():
    from hermes_cli.setup_wizard.page import render_page

    out = _call_summary_bot_value(render_page(), None, "")
    assert out == "не указан"


def test_botlink_href_has_fixed_telegram_prefix():
    """B5 п.5 / инвариант: ссылка на бота на экране успеха собирается с
    фиксированным префиксом https://t.me/ — сервер отдаёт только имя
    (data.bot_username), никогда готовую ссылку целиком."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    assert 'link.href = "https://t.me/" + data.bot_username;' in html


def test_progress_stage_list_present_with_four_stages_install_hidden_by_default():
    """B5 п.4: список стадий (сохранение → [установка инструментов] →
    перезапуск → ожидание ответа бота) вместо одной застывшей строки
    «Сохраняем…». Owner ruling 2026-08-24 ("Установка инструментов —
    кнопки нет") added "Устанавливаем инструменты" between "apply" and
    "restart" — present in the markup but ``hidden`` by default, since
    whether it actually runs is a per-submission fact
    (pendingToolInstallNames()) decided only once the client is on step
    6, not something the static HTML can know in advance."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    for stage_id in ("stage-apply", "stage-install", "stage-restart", "stage-liveness"):
        assert f'id="{stage_id}"' in html
    assert 'id="stage-install" hidden' in html
    assert 'class="stages"' in html
    assert 'byId("progress-stage")' not in html
    assert "BASE_STAGE_ORDER" in html
    assert "currentStageOrder" in html
    assert "setStageOrder" in html
    assert "renderProgressStages" in html


def test_progress_stages_never_mark_restart_or_liveness_done_from_a_timer():
    """Честность прогресса: единственный setTimeout-переход двигает
    ТОЛЬКО apply -> [install|restart] (локальная запись конфига — быстрая
    и без сети); ни install (когда есть), ни restart, ни liveness никогда
    не помечаются "done" по таймеру — только по факту (успешный ответ или
    data.stage из /api/submit) — см. renderProgressStages(currentStageOrder.length,
    currentStageOrder.length) на успехе."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    start_body = _function_body(html, "startProgressStages")
    assert "renderProgressStages(1, 1);" in start_body
    assert "renderProgressStages(2" not in start_body
    assert "renderProgressStages(3" not in start_body
    # The one place every stage THIS submission carried is confirmed
    # "done" is the /api/submit success branch, not a timer — the length
    # varies (3 or 4) per submission, so this is never a literal
    # "renderProgressStages(3, 3)" any more (see setStageOrder()).
    submit_idx = html.index('jsonFetch("/api/submit"')
    success_idx = html.index(
        "renderProgressStages(currentStageOrder.length, currentStageOrder.length);", submit_idx
    )
    ok_idx = html.rindex("if (data.ok) {", submit_idx, success_idx)
    assert ok_idx < success_idx


def test_success_screen_points_back_at_itself_not_closed():
    """B5 п.5, updated by spec 8, §5: the wizard no longer self-extinguishes
    after a successful submit, so the success screen must not claim the
    page is "закрыта" any more — instead it tells the client they can come
    back to the SAME address with the SAME login/password from the email.
    #key-check-notice is preserved, and the logo is NOT duplicated in the
    content area — it already shows in the rail via setRailMode("success")."""
    from hermes_cli.setup_wizard.page import render_page, _LOGO_SVG

    html = render_page()
    success_idx = html.index('id="success"')
    success_end = html.index("</section>", success_idx)
    success_chunk = html[success_idx:success_end]
    assert "закрыта" not in success_chunk
    assert "логином и паролем" in success_chunk
    assert "письм" in success_chunk
    assert 'id="key-check-notice"' in success_chunk
    assert _LOGO_SVG not in success_chunk
    assert html.count(_LOGO_SVG) == 1


def test_step4_section_titles_read_bigger_than_a_field_label_but_smaller_than_a_step_title():
    """Owner feedback п.5 (live VM walkthrough): "Провайдер модели" (the
    label above step 4's provider picker) and "Запасной провайдер" (the
    heading renderAdvancedFallback() builds) used to read as plain field
    labels — too small to register as section headers. Both now share
    `.section-title`, sized strictly between a plain field `label`
    (0.92rem) and a whole step's own `h2.screen-title` (1.55rem) — bigger
    than a label, but never as large as the step heading itself."""
    import re

    from hermes_cli.setup_wizard.page import render_page

    html = render_page()

    # #provider_group_label carries the class in the static markup (the
    # actual <label> tag, not this test's own docstring mentioning the id
    # or a source comment elsewhere in the file).
    assert '<label id="provider_group_label" class="section-title" for="provider_group">' in html

    # renderAdvancedFallback()'s dynamically-built <h3> carries it too.
    body = html[html.index("function renderAdvancedFallback") :]
    assert 'heading.className = "section-title";' in body[: body.index("\n  }\n")]

    # Exactly one CSS rule defines the shared size, and its selector list
    # covers both the static label's class AND .tool-block h3 (the
    # renderAdvancedFallback() heading's OTHER ancestor-based selector, in
    # case a future edit stops setting className directly).
    rule_idx = html.index(".section-title {")
    # The selector list is whatever precedes "{" back to the previous "}".
    selector_start = html.rindex("}", 0, rule_idx) + 1
    selector = html[selector_start:rule_idx]
    assert ".section-title" in selector
    assert ".tool-block h3" in selector

    rule_end = html.index("}", rule_idx)
    rule_body = html[rule_idx:rule_end]
    size_match = re.search(r"font-size:\s*([\d.]+)rem", rule_body)
    assert size_match, rule_body
    section_title_size = float(size_match.group(1))

    label_rule_idx = html.index("label { display: block;")
    label_rule_end = html.index("}", label_rule_idx)
    label_size_match = re.search(r"font-size:\s*([\d.]+)rem", html[label_rule_idx:label_rule_end])
    assert label_size_match

    screen_title_rule_idx = html.index("h2.screen-title {")
    screen_title_rule_end = html.index("}", screen_title_rule_idx)
    screen_title_size_match = re.search(
        r"font-size:\s*([\d.]+)rem", html[screen_title_rule_idx:screen_title_rule_end]
    )
    assert screen_title_size_match

    assert float(label_size_match.group(1)) < section_title_size < float(screen_title_size_match.group(1))
