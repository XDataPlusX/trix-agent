"""The setup wizard's single page (spec §5, §7, §8): one Russian form, no
login screen of its own. ``render_page()`` returns a complete, static,
self-contained HTML document — inline CSS, inline JS, no build step, no
external CDN. Every dynamic bit (provider catalog, tool catalog, search
backends, saved-value prefill) is fetched client-side from ``/api/form``
the instant the script runs — spec 8, §8.3 gates every path behind HTTP
Basic auth (RFC 7617) in front of this module entirely, so a browser never
receives a byte of this markup until it has already authenticated with the
credentials from the machine's own bootstrap email; there is nothing left
here to log into. ``render_page()`` takes only the one optional ``host``
argument (spec §5's "<host>" in the header sentence — see
``app.py::_host_from_request``) and is otherwise pure/deterministic, so
the FastAPI route in ``app.py`` just forwards the request's own ``Host``
header and serves the return value verbatim.

Layout is the hybrid picked in the plan (spec §7.1): four always-visible
required fields (proxy, bot token, Telegram id, the model-provider block —
proxy sits BEFORE both the Telegram fields AND the provider block: it is
what makes a Telegram token OR a model-provider reachable at all from a
data-center machine, so the form asks for it before asking the client for
anything that might need it), plus an "advanced" section (step 5,
"Дополнительно") holding the tool catalog (browser/search/voice/STT/image
generation/video generation — six collapsed rows, plan B4) and the
optional fallback-model block. Spec §7.2 is explicit that
nothing in the provider block is preselected — the wizard never assumes
which provider a returning client meant, even when ``/api/form``'s
``current.provider`` says one is already active; that value is shown as an
informational note, never wired to a preselected ``<option>``/radio.

Plan B1 (2026-08-23, spec 7 — wizard redesign) swapped the Прокси/Telegram
step ORDER on top of that same field-order rationale: Telegram's own
autocheck talks to the network, so putting Прокси first (step 2) means
that check isn't doomed to fail from a still-empty proxy field the way it
was when Telegram was step 2 and Прокси step 3. See _MAIN_FORM_HTML's own
comment on the div swap, and STEPS/FIELD_STEP in the script below.

Owner requirement 3 (2026-08-21): the wizard is ALWAYS the step-by-step
walk (Прокси -> Telegram -> Провайдер -> Дополнительно -> Готово) — there
is no longer a separate one-page "everything at once" layout for
returning clients. (This originally started with a login step — spec 8,
§8.3 later removed it: HTTP Basic auth means the browser is already
authenticated by the time it gets this markup, so the walk now starts
directly on Прокси.) The one-page return-mode summary this file used to
render (behind a `"изменить ▾"` toggle collapsing "advanced") read as a
giant wall of fields the moment a client typed their password, which the
owner rejected outright: "why isn't it interactive, like the first visit?"
A returning client (`current.telegram_token.is_set === true`, spec §12.4's
signal) still walks the same five steps as a first-time client — the only
two differences are (1) every field/select on every step is pre-filled
from `current` (the SAME prefill mechanism first-run already used for its
"Сейчас настроено" hints and preselected `<select>`s — see renderPrefill(),
renderBrowserBlock(), etc. — nothing new was built for this, it now just
also applies on the very first step a returning client sees rather than
only after they'd clicked through), and (2) the progress bar's step
numbers become a real nav — every step is clickable, since a returning
client already has a fully configured agent and there is nothing "ahead"
they haven't set up yet. A first-run client only gets that jump-back
affordance for steps already completed; every step still ahead of them
stays locked behind "Далее" so they can't skip a required field. See
``isStepClickable()`` in the script below.

Owner requirement 1: the provider picker is GROUPED — one top-level entry
per vendor (``/api/form``'s ``provider_groups``, built from
``providers_view.wizard_provider_groups()`` off the upstream
``hermes_cli.models.PROVIDER_GROUPS`` table), never two separate rows for
what is really one provider's two auth methods (the diagnosed bug: OpenAI
showing up as both "ChatGPT" and "OpenAI API"). Picking a multi-variant
group reveals a "способ подключения" radio underneath it; picking a
single-variant group goes straight to that variant's own sub-block. The
submitted ``provider.name`` is always the chosen VARIANT's slug — the
group is a display fold, never sent to the server.

Owner requirement 2: device-code providers (``openai-codex``,
``minimax-oauth`` — ``providers_view.DEVICE_CODE_PROVIDERS``) log in RIGHT
HERE, in the browser — no more "log in from the command line" excuse. The
variant's sub-block has its own "Войти по аккаунту" button that drives
``/api/device/start`` + polls ``/api/device/status`` (see
``device_login.py``) until the login succeeds or fails, then reveals a
model picker fed from the same catalog source ``hermes model``'s own
picker uses (``/api/models`` routes a device-code provider through
``hermes_cli.models.provider_model_ids`` instead of the api-key
``fetch_live_models`` path — see ``app.py``).
"""
from __future__ import annotations

import html


def _header_intro(host: str | None) -> str:
    """Spec §5 header sentence — names the machine when the request's
    ``Host`` header gave us one (see ``app.py::_host_from_request``).

    ``host`` is client-supplied (an HTTP ``Host`` header), so it is always
    run through ``html.escape`` before landing in this f-string — never
    trust it as pre-sanitized HTML.
    """
    where = (
        f"на вашей собственной виртуальной машине ({html.escape(host)})"
        if host
        else "на вашей собственной виртуальной машине"
    )
    return (
        f"Эта страница работает {where}, а не "
        "на нашем сервере. Всё, что вы введёте, сохраняется на этой машине и "
        "используется только для запуска вашего агента; наружу оно уходит "
        "только вашему провайдеру модели и Telegram. Соединение зашифровано "
        "сертификатом, который машина выписала себе сама, — поэтому браузер "
        "показал предупреждение. Для страницы на вашей собственной машине это "
        "ожидаемо."
    )


# Inline XDataPlus wordmark (spec §7/B1) — the exact path data from the
# approved mockup (assets/2026-08-23-wizard-approved-mockup.html), pasted
# verbatim so it renders identically. It is drawn white-on-transparent, so
# it only ever appears on the navy rail — never inline in the light content
# area. `aria-label` names the brand for assistive tech since the shapes
# carry no visible text.
_LOGO_SVG = (
    '<svg class="mark" width="104" height="52" viewBox="0 0 137 68" fill="none" '
    'aria-label="XDataPlus" role="img">'
    '<path d="M7.02167 14.5423C6.69546 14.095 6.68491 13.6259 6.99153 13.1365C7.29816 12.647 7.82024 12.4031 8.56382 12.4031H17.492C19.0899 12.4031 20.321 13.1237 21.1851 14.5626L43.4126 53.4374C43.744 53.8892 43.7538 54.3613 43.4427 54.856C43.1361 55.3507 42.614 55.5969 41.8749 55.5969H32.9422C31.3443 55.5969 30.1133 54.8688 29.2492 53.4148L7.02167 14.5423Z" fill="#0062FF"/>'
    '<path d="M14.4197 65.3186C13.5556 66.7726 12.3246 67.5008 10.7267 67.5008H1.79392C1.05561 67.5008 0.532773 67.2545 0.22615 66.7598C-0.0849927 66.2651 -0.0751988 65.793 0.256285 65.3412L36.0211 2.65954C36.8852 1.2206 38.1163 0.5 39.7142 0.5H48.6424C49.386 0.5 49.9088 0.743212 50.2147 1.2334C50.5213 1.72284 50.5107 2.1927 50.1845 2.63921L14.4197 65.3186Z" fill="#0062FF"/>'
    '<path d="M52.5517 55.563C51.9354 55.563 51.4209 55.3582 51.011 54.9471C50.5997 54.5367 50.3948 54.0232 50.3948 53.4073V14.5935C50.3948 13.9776 50.5997 13.464 51.011 13.0537C51.4216 12.6433 51.9354 12.4377 52.5517 12.4377H68.6535C76.7899 12.4377 83.0361 14.3781 87.3921 18.2597C91.7474 22.1413 93.9261 27.3881 93.9261 34.0008C93.9261 40.6134 91.7474 45.8602 87.3921 49.7418C83.0361 53.6234 76.7899 55.5638 68.6535 55.5638H52.5517V55.563ZM63.9555 45.7058H68.6543C72.2298 45.7058 74.972 44.7104 76.8833 42.718C78.7939 40.7264 79.7499 37.8199 79.7499 34.0008C79.7499 30.1816 78.7947 27.2759 76.8833 25.2835C74.9728 23.2919 72.2298 22.2957 68.6543 22.2957H63.9555V45.7066V45.7058Z" fill="#fff"/>'
    '<path d="M121.391 13.9264H112.441C111.448 13.9264 110.643 14.7311 110.643 15.7237V52.2763C110.643 53.2689 111.448 54.0736 112.441 54.0736H121.391C122.384 54.0736 123.189 53.2689 123.189 52.2763V15.7237C123.189 14.7311 122.384 13.9264 121.391 13.9264Z" fill="#0062FF"/>'
    '<path d="M137 38.4727V29.5273C137 28.5347 136.195 27.7299 135.202 27.7299L98.6302 27.7299C97.6371 27.7299 96.8319 28.5347 96.8319 29.5273V38.4727C96.8319 39.4653 97.6371 40.2701 98.6302 40.2701H135.202C136.195 40.2701 137 39.4653 137 38.4727Z" fill="#0062FF"/>'
    "</svg>"
)


def _rail_address(host: str | None) -> str:
    """The rail foot's machine label (B1) — same client-supplied ``host``
    as ``_header_intro``, so it goes through the identical ``html.escape``
    treatment before landing in the page. A missing ``Host`` header falls
    back to a generic label rather than showing nothing.
    """
    return html.escape(host) if host else "эта машина"


def _rail_html(rail_address: str, header_intro_html: str) -> str:
    """The sidebar column (spec 7 / plan B1): replaces the old horizontal
    step strip. Present on every screen; ``#progress-bar`` (the step list)
    keeps its original id and its original hidden/shown + className
    mechanism (see the script's ``renderProgressBar()``/``isStepClickable()``
    — those are untouched by this pass), just rendered vertically inside
    ``.rail`` instead of as a `<nav>` sibling above the form. ``#rail-logo``
    shows only before the first step is entered and on the success screen
    (JS toggles it — see ``setRailMode()`` in the script below);
    ``#rail-foot-cert-line``'s "подробнее" link reveals ``#cert-detail``,
    which holds ``header_intro_html`` — the self-signed-certificate
    disclosure's only rendering now that there is no separate login
    screen to show it in full (spec 8, §8.3).
    """
    return (
        '<aside class="rail" id="rail">'
        f'<div id="rail-logo">{_LOGO_SVG}</div>'
        '<div class="product">Trix Agent</div>'
        '<p class="product-sub" id="rail-sub">Настройка</p>'
        '<ol id="progress-bar" hidden></ol>'
        '<div class="rail-foot" id="rail-foot">'
        '<p id="rail-foot-default">'
        f"Ваша машина {rail_address}<br>"
        '<span id="rail-foot-cert-line" hidden>соединение зашифровано · '
        '<a href="#" id="cert-toggle">подробнее</a></span>'
        "</p>"
        '<p id="rail-foot-success" hidden>'
        f"Машина {rail_address}<br>обслуживает XDataPlus"
        "</p>"
        '<div id="cert-detail" hidden><p>'
        f"{header_intro_html}"
        "</p></div>"
        "</div>"
        "</aside>"
    )


_CSS = """
:root {
  --ink: #14152b;
  --ink-2: #4e5372;
  --text-dim: #4e5372;
  --blue: #0062ff;
  --blue-d: #0049c0;
  --accent: #0062ff;
  --accent-text: #ffffff;
  --navy: #0d0d27;
  --navy-2: #1b1c3d;
  --border: #dadde5;
  --line: #dadde5;
  --wash: #f5f7fb;
  --bg: #eceef1;
  --panel: #ffffff;
  --text: #14152b;
  --error: #c2263a;
  --ok: #0d7a4a;
  --ok-bg: #e9f6ef;
  --bad: #c2263a;
  --bad-bg: #fdecee;
  --gold: #f5e29e;
  --radius: 0px;
}
* { box-sizing: border-box; }
::selection { background: var(--blue); color: #fff; }
body {
  margin: 0;
  padding: 0 1rem 4rem;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 920px; margin: 0 auto; padding-top: 2rem; }
/* Boковая колонка (индиго-плашка) + рабочая область — вместо старой
   горизонтальной полоски шагов (спека 7, задача B1). На узких экранах
   колонка ложится сверху, а не сбоку. */
.canvas {
  display: grid;
  grid-template-columns: 15rem 1fr;
  background: var(--panel);
  border: 1px solid var(--line);
  box-shadow: 0 14px 38px rgba(20, 21, 43, 0.10);
  min-height: 30rem;
}
@media (max-width: 780px) {
  .canvas { grid-template-columns: 1fr; }
}
.rail {
  background: var(--navy);
  color: #fff;
  padding: 1.6rem 1.4rem 1.4rem;
  display: flex;
  flex-direction: column;
}
.rail .mark { display: block; margin-bottom: 0.25rem; }
.rail .product { font-size: 0.98rem; font-weight: 700; letter-spacing: -0.01em; margin: 0.9rem 0 0.15rem; }
.rail .product-sub { font-size: 0.76rem; color: #9ea3c8; margin: 0 0 1.4rem; }
.rail ol { list-style: none; margin: 0; padding: 0; }
.rail-foot {
  margin-top: auto;
  padding-top: 1.4rem;
  border-top: 1px solid #2a2c50;
  font-size: 0.72rem;
  color: #8c91b8;
  line-height: 1.5;
}
.rail-foot a { color: #b9bde0; }
.rail-foot p { margin: 0; }
header {
  padding: 0 0 1.25rem;
}
header p { color: var(--text-dim); margin: 0; }
.content { padding: 2.1rem 2.4rem 2.2rem; max-width: 41rem; }
section, form { margin-top: 0; }
.panel { padding: 0; }
h2.screen-title { font-size: 1.55rem; font-weight: 800; letter-spacing: -0.03em; line-height: 1.15; margin: 0 0 0.5rem; }
p.screen-sub { color: var(--ink-2); margin: 0 0 1.6rem; max-width: 56ch; }
h2 { font-size: 1.05rem; margin: 0 0 1rem; font-weight: 600; }
label { display: block; font-weight: 650; font-size: 0.92rem; margin-bottom: 0.4rem; }
input[type="text"], input[type="password"], select {
  width: 100%;
  padding: 0.68rem 0.8rem;
  border: 1px solid var(--border);
  border-radius: 0;
  font-size: 1rem;
  background: #fff;
  color: var(--text);
}
input:focus, select:focus {
  outline: 2px solid var(--blue);
  outline-offset: 1px;
  border-color: var(--blue);
}
select { cursor: pointer; }
.field-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 0.5rem 2rem;
  align-items: start;
  padding: 0.85rem 0;
  border-top: 1px solid var(--border);
}
.field-row:first-of-type { border-top: none; }
/* Author-origin rules always beat the UA stylesheet's [hidden]{display:none}
   at equal specificity (origin order, not specificity, decides that fight)
   — so `.field-row { display: grid }` above was silently overriding every
   `someRow.hidden = true` a script set on a field-row (owner-observed bug:
   a single-variant provider group's "способ подключения" row/hint stayed
   visible even though its `hidden` attribute was set). This restores the
   native behavior for any field-row a script hides. */
.field-row[hidden] { display: none; }
.hint { color: var(--text-dim); font-size: 0.9rem; padding-top: 1.9rem; }
/* The push above exists to clear a visible label. A row whose label is
   sr-only has nothing to clear, so keeping it left the hint hanging
   below its own control (owner feedback: "поле и справа текст неровно"). */
.field-row.no-label .hint { padding-top: 0.15rem; }
/* Owner feedback (renderAdvancedFallback, live walkthrough): "странные
   после него пробелы строчные" — every <p class="hint"> on this page is a
   standalone note, never the right column of a .field-row (those are all
   built as <div class="hint">, which keeps the 1.9rem push above intact) —
   but with no page-wide <p> margin reset, a <p class="hint"> stacked the
   browser's own default paragraph margin UNDER that same 1.9rem push,
   opening a much bigger gap than any surrounding .hint/.field-note ever
   gets. One rule fixes every current and future standalone hint paragraph
   at once (provider-signup-hint, the device-login "Откройте …" note,
   key-check-notice on the success screen, and this fallback block's own
   intro line) instead of an inline override repeated at each call site. */
p.hint { margin: 0; padding-top: 0.35rem; }
/* Owner feedback: the saved-secret placeholder used to carry the full
   explanation and overran the input ("...оставьте пустым чт"). The
   placeholder itself is short now; this is where the rest of the
   sentence lives — a caption directly under the field, not a second
   copy of .hint's right-column layout. */
.field-note { color: var(--text-dim); font-size: 0.82rem; margin-top: 0.35rem; }

/* Разделитель внутри шага: «Ваше время» отделено от полей бота, чтобы
   часовой пояс не читался как ещё одна настройка Telegram. */
.field-group {
  border-top: 1px solid var(--line);
  margin-top: 2.1rem;
  padding-top: 1.5rem;
}
.field-group-title {
  color: var(--ink);
  font-size: 1.02rem;
  font-weight: 600;
  margin: 0 0 1.1rem;
}
/* Предупреждение о смене уже отвеченного пояса: заметнее подсказки,
   тише ошибки — это не отказ, а то, чего клиент не мог знать сам. */
.field-warning {
  background: var(--wash);
  border-left: 3px solid var(--gold);
  border-radius: 0 6px 6px 0;
  font-size: 0.88rem;
  margin-top: 0.6rem;
  padding: 0.7rem 0.9rem;
}
.field-warning p { margin: 0 0 0.6rem; }
.field-warning label { cursor: pointer; display: flex; gap: 0.5rem; align-items: center; }
.field-note[hidden] { display: none; }
.field-error {
  color: var(--error);
  font-size: 0.88rem;
  margin-top: 0.3rem;
}
@media (max-width: 700px) {
  .field-row { grid-template-columns: 1fr; }
  .hint { padding-top: 0.15rem; }
  .content { padding: 1.6rem 1.2rem 1.8rem; }
}
button {
  font: inherit;
  font-weight: 650;
  cursor: pointer;
  border-radius: 999px;
  border: 1px solid var(--ink);
  background: #fff;
  color: var(--ink);
  padding: 0.62rem 1.4rem;
}
button.accent, #done {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-text);
  font-weight: 650;
}
button.accent:hover, #done:hover {
  background: var(--blue-d);
  border-color: var(--blue-d);
}
button:disabled { opacity: 0.42; cursor: default; }
#provider-block, #advanced { margin-top: 1.25rem; }
/* #provider-block is a plain <div>, not a <details> — the model-provider
   select is its own always-visible required field row, same as the two
   fields above it, not a click-to-expand affordance (owner feedback: a
   collapsed disclosure summary read as optional/hidden, not required).
   #advanced (спека 7 / план B4) is the "Дополнительно" step's own plain
   <div class="rows"> now — each of the six tool categories is a single
   collapsed .row (see buildCollapsibleRow() in the script), never a
   details/summary wrapper, so the whole step fits one screen without a
   scroll. */
.tool-block { padding: 1rem 0; border-top: 1px solid var(--border); }
.tool-block:first-of-type { border-top: none; padding-top: 0; }
/* Owner feedback (renderAdvancedFallback, live walkthrough): "маленький
   текст выделяется, не понятно, что это header" — at 0.95rem/600 this sat
   almost exactly on top of a plain field <label> (0.92rem/650), so it
   never registered as a section heading. .tool-block only ever gets an h3
   from renderAdvancedFallback() (nothing else in this page creates one) —
   weighted here to read the way h2.screen-title does for a whole step,
   scaled down for a heading inside one. */
/* Owner feedback п.5 (live VM walkthrough, second pass): "Провайдер
   модели" (the static <label id="provider_group_label"> above step 4's
   picker) never got the treatment above at all — it was still the plain
   0.92rem/650 field-label style, so it read as just another field, not
   the section header for the whole provider block sitting above it. Both
   headings are the same KIND of thing (a section title inside one step,
   never a whole step's own h2.screen-title at 1.55rem/800), so they now
   share one rule instead of two independently-tuned ones that could drift
   apart again — .section-title is applied directly to
   #provider_group_label in the markup and via className on the <h3>
   renderAdvancedFallback() builds. Bumped from 1.05rem to 1.2rem so it
   reads unmistakably as a header — still sized well below a step's own
   h2.screen-title (1.55rem), never as large as the step heading itself. */
.tool-block h3, .section-title { font-size: 1.2rem; font-weight: 800; letter-spacing: -0.02em; color: var(--ink); margin: 0 0 0.4rem; }
/* Спека 7 / план B4: "Дополнительно" — каждая категория одной свёрнутой
   строкой (реальная <button>, не div+onclick, ради клавиатурного доступа
   без ручного keydown-обработчика) с текущим состоянием справа и
   шевроном; раскрытие показывает тот же select/настройки, что и раньше,
   в .row-body ниже строки. Разметка/классы — из эталонного макета (экран
   5), поверх переменных этого файла (--text-dim/--line/--wash вместо
   --ink-2/--line/--wash из макета — --line/--wash уже совпадают по
   имени). */
.rows { border-top: 1px solid var(--line); }
.row {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  width: 100%;
  padding: 0.8rem 0.1rem;
  border: 0;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
  background: none;
  color: var(--text);
  font: inherit;
  font-weight: 400;
  text-align: left;
  cursor: pointer;
}
.row:focus-visible { outline: 2px solid var(--blue); outline-offset: -2px; }
.row b { font-weight: 650; font-size: 0.94rem; }
.row .state { margin-left: auto; color: var(--text-dim); font-size: 0.88rem; }
.row .chev { color: var(--text-dim); }
/* Owner feedback: opening a row (e.g. "Браузер") repeated its own title as
   the <select>'s visible <label> right below — "как будто два браузера".
   The row header already names the category; the label inside .row-body
   only needs to exist for the for/id accessibility association, not to be
   seen again. sr-only keeps that association (screen readers still get
   "Браузер") without a second visible heading. */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.row.open { background: var(--wash); padding-left: 0.75rem; padding-right: 0.75rem; }
.row-body { padding: 0.4rem 0.75rem 1.25rem; background: var(--wash); border-bottom: 1px solid var(--line); }
.tool-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.4rem 0;
}
.sub-settings {
  margin-top: 0.6rem;
  padding-left: 0.9rem;
  border-left: 2px solid var(--border);
}
.muted-note { color: var(--text-dim); font-size: 0.88rem; padding: 0.3rem 0; }
.status-ok { color: var(--ok); font-size: 0.85rem; }
.out-of-catalog-note { color: var(--text-dim); font-style: italic; font-size: 0.88rem; }
.device-login-code {
  font-size: 1.6rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  padding: 0.5rem 0;
}
.device-login-status { font-size: 0.9rem; padding: 0.2rem 0; }
#success { text-align: left; padding: 0; }
#success a { color: var(--accent); font-weight: 600; }
/* Same UA-vs-author [hidden] fight as .field-row[hidden] above — an
   author `display: inline-block` on .botlink would otherwise silently
   outrank the browser's own `[hidden] { display: none }` the moment
   doSubmit() sets `link.hidden = true` (no bot_username case). */
.botlink[hidden] { display: none; }
.botlink {
  display: inline-block;
  margin: 0.25rem 0 1.25rem;
  padding: 0.68rem 1.5rem;
  border-radius: 999px;
  background: var(--blue);
  color: #fff !important;
  text-decoration: none;
  font-weight: 650;
}
.general-error {
  color: var(--error);
  margin-top: 0.75rem;
}
.general-error:empty { display: none; }
/* Шаг «Готово» (спека 7 / план B5): сводка перед необратимым шагом
   (.sum/.r), предупреждение о длительности (.note) и честный список
   стадий во время ожидания (.stages) — те же классы и тот же визуальный
   язык, что в эталонном макете (screens 6/7), поверх переменных этого
   файла (--text-dim/--accent/--wash/--gold/--ok вместо --ink-2/--blue/
   --blue-d из макета). */
.sum { border-top: 1px solid var(--line); margin-bottom: 0.25rem; }
.sum .r {
  display: flex;
  gap: 0.9rem;
  align-items: baseline;
  padding: 0.7rem 0.1rem;
  border-bottom: 1px solid var(--line);
  font-size: 0.93rem;
}
.sum .r i { font-style: normal; color: var(--text-dim); width: 9.5rem; flex: none; }
.sum .r a { margin-left: auto; color: var(--accent); font-size: 0.85rem; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; letter-spacing: 0.01em; }
.note {
  font-size: 0.86rem;
  color: var(--text-dim);
  background: var(--wash);
  border-left: 3px solid var(--gold);
  padding: 0.75rem 0.9rem;
  margin: 0 0 1.5rem;
}
.note b { color: var(--text); }
.stages { list-style: none; margin: 0 0 0.25rem; padding: 0; }
.stages li {
  display: flex;
  gap: 0.7rem;
  align-items: center;
  padding: 0.55rem 0;
  font-size: 0.94rem;
  color: #9598ab;
}
.stages li u {
  flex: none;
  width: 19px;
  height: 19px;
  border: 1px solid var(--line);
  display: grid;
  place-items: center;
}
.stages li.done { color: var(--text); }
.stages li.done u { background: var(--ok); border-color: var(--ok); color: #fff; }
.stages li.now { color: var(--text); font-weight: 650; }
.stages li.now u { border-color: var(--accent); border-width: 2px; }
/* Verdict banners (спека: автопроверки прокси/Telegram/ключа выводят один
   явный исход — успех/ошибка — вместо тусклой серой строки подсказки). */
.verdict {
  display: flex;
  gap: 0.55rem;
  align-items: flex-start;
  margin-top: 0.6rem;
  font-size: 0.9rem;
  padding: 0.62rem 0.8rem;
  border-left: 3px solid transparent;
}
.verdict.ok { background: var(--ok-bg); color: var(--ok); border-left-color: var(--ok); }
.verdict.bad { background: var(--bad-bg); color: var(--bad); border-left-color: var(--bad); }
.verdict.wait { background: var(--wash); color: var(--ink-2); border-left-color: var(--line); }
.verdict b { font-weight: 700; }
/* Same UA-vs-author [hidden] fight as .field-row[hidden]/.botlink[hidden]
   above (спека B2) — .verdict sets `display: flex`, which at equal
   specificity silently outranks the browser's own `[hidden] {
   display: none }` origin-order the moment a script sets
   `verdictEl.hidden = true` (telegram-verdict/key-verdict start hidden
   until their first check runs). Without this override a hidden verdict
   box would render as an empty flex row instead of disappearing. */
.verdict[hidden] { display: none; }
/* Reserves the verdict's footprint (спека B2 п.5: "вёрстка не прыгает")
   even while the inner .verdict is still [hidden] — used for the two
   checks that start silent (telegram token, provider key) rather than the
   proxy step's always-visible verdict, which needs no separate slot. */
.verdict-slot { min-height: 2.7rem; margin-top: 0.4rem; }
.verdict-slot .verdict { margin-top: 0; }
/* Rail step list (спека: боковая колонка вместо горизонтальной полоски
   шагов) — тот же механизм, что и раньше (#progress-bar, hidden/shown и
   className через isStepClickable()/renderProgressBar()), только теперь
   отрисовывается вертикально внутри .rail, а не отдельной горизонтальной
   nav-полосой над формой. */
#progress-bar {
  padding: 0 0 1rem;
  font-size: 0.9rem;
  color: #9ea3c8;
}
#progress-bar .step-item {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.5rem 0;
}
#progress-bar .step-num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.3rem;
  height: 1.3rem;
  border-radius: 0;
  border: 1px solid #363a63;
  font-weight: 600;
  font-size: 0.7rem;
  flex: none;
}
#progress-bar .step-item.done { color: #e8eaf6; }
#progress-bar .step-item.done .step-num {
  background: var(--blue);
  border-color: var(--blue);
  color: #fff;
}
#progress-bar .step-item.current { color: #fff; font-weight: 650; }
#progress-bar .step-item.current .step-num {
  border-color: var(--gold);
  border-width: 2px;
  color: var(--gold);
  font-weight: 700;
  background: transparent;
}
/* Owner ruling (return visit): the progress bar is a real nav once
   everything is already configured — every step is a step-item.clickable
   there (see isStepClickable() in the script below). On a first run only
   already-completed steps are clickable; everything still ahead is
   step-item.locked (muted, no pointer) — forward movement stays gated
   behind each step's own "Далее" button so a first-time client can't skip
   required fields by jumping the bar. */
#progress-bar .step-item.clickable { cursor: pointer; }
#progress-bar .step-item.clickable:hover .step-num { border-color: var(--gold); }
#progress-bar .step-item.locked { opacity: 0.45; }
[data-step][hidden] { display: none; }
.step-nav {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding-top: 1rem;
  margin-top: 1.25rem;
  border-top: 1px solid var(--border);
}
.step-nav button.accent { margin-left: auto; }
/* Provider list (спека 7 / план B3, экран 4 эталона) — строка на группу
   вместо голого <select>: имя, description_ru в одну строку, справа
   пометка «рекомендуем»/«нужен прокси». .sel — выбранная строка (тот же
   inset-shadow приём, что и в макете). */
.prov { border-top: 1px solid var(--line); margin-top: 0.4rem; }
.prov .p {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.25rem 0.9rem;
  align-items: start;
  padding: 0.75rem 0.15rem;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
}
.prov .p:focus-visible { outline: 2px solid var(--blue); outline-offset: -2px; }
.prov .p b { font-weight: 650; font-size: 0.95rem; }
.prov .p span.desc { grid-column: 1; font-size: 0.85rem; color: var(--text-dim); }
.prov .p .tag {
  grid-column: 2;
  grid-row: 1;
  font-size: 0.72rem;
  font-weight: 700;
  padding: 0.19rem 0.55rem;
  border-radius: 999px;
  white-space: nowrap;
}
.prov .p .tag.rec { background: #e7effe; color: var(--blue-d); }
.prov .p .tag.off { background: #f1f2f6; color: #6a6f88; }
.prov .p.sel { background: var(--wash); padding-left: 0.75rem; padding-right: 0.75rem; box-shadow: inset 3px 0 0 var(--blue); }
.prov .more { font-size: 0.88rem; padding: 0.75rem 0.15rem; }
.prov .more a { color: var(--blue-d); }
/* Owner feedback (this pass): "как будто нет разделения... добавил
   линий, но всё равно чего-то не хватает" — step 4 reads as one
   undifferentiated block even with .field-row's thin border-top between
   individual rows. These four are the client's own mental groups
   (выбор провайдера / способ подключения / ключ+модель / запасная
   модель) — real vertical space between them, not another border, is
   what actually separates a "group" from a "field". */
#provider-auth-choice, #provider-api-key-block, #provider-device-code-block, #step4-fallback-slot {
  margin-top: 1.75rem;
}
#provider-api-key-block .field-row + .field-row {
  margin-top: 0.85rem;
}
/* "Адрес API" — за раскрытием (спека B3 п.6): значение и поведение поля
   не меняются, меняется только видимость по умолчанию. */
details.reveal summary {
  cursor: pointer;
  color: var(--blue-d);
  font-size: 0.88rem;
  font-weight: 650;
  list-style: none;
  padding: 0.6rem 0 0;
}
details.reveal summary::-webkit-details-marker { display: none; }
details.reveal[open] summary { padding-bottom: 0.2rem; }
"""

_MAIN_FORM_HTML = """
<form id="main" hidden>
  <div id="form-error" class="general-error" role="alert"></div>

  <!-- Step order (plan B1): 2 = Прокси, 3 = Telegram — swapped from the
       original layout because Telegram's own check needs a working proxy
       to succeed on a data-center machine; asking for the proxy first
       means that check isn't doomed to fail from a still-empty field. The
       div/button ids stay POSITIONAL ("step-2-*"/"step-3-*"), not
       content-named — only what lives inside each wrapper changed. -->
  <div id="step-2" data-step="2" hidden>
    <h2 class="screen-title">Доступ в интернет</h2>
    <p class="screen-sub">Проверяем, до кого ваша машина дотягивается сама, а до кого — только через прокси.</p>
    <!-- Owner feedback п.1 (live VM walkthrough): "надо проверять прокси
         только тогда, когда что-то там вставилось" — the check no longer
         fires the instant this step is entered (that used to run
         unconditionally against a still-empty field on every single visit,
         proxy typed or not). It now fires after the client actually types
         something (debounced — see the "input" listener below) and always,
         unconditionally, on "Далее" (whatever the field holds, even empty —
         that's the one case that still MUST run, since an empty field can
         legitimately mean either "no proxy needed" or "forgot to fill it
         in", and only a real check tells them apart). Starts `hidden`, same
         reserved-space `.verdict-slot` pattern telegram/key already use —
         nothing has been checked yet, so there is nothing honest to show. -->
    <div class="verdict-slot"><div id="proxy-verdict" class="verdict" hidden></div></div>
    <div class="field-row">
      <div class="field">
        <label for="proxy">Прокси</label>
        <input id="proxy" name="proxy" type="text" autocomplete="off">
        <div id="err_proxy" class="field-error" hidden></div>
      </div>
      <div class="hint">Формат socks5://user:pass@host:port или http://host:port. Можно оставить пустым — машина уже проверила, нужен ли он.</div>
    </div>
    <p class="note">Проверка идёт и по провайдерам моделей: те, до кого машина не дотянется, будут помечены на следующих шагах.</p>

    <div class="step-nav">
      <button type="button" id="step-2-next" class="accent">Далее</button>
    </div>
  </div>

  <div id="step-3" data-step="3" hidden>
    <h2 class="screen-title">Бот в Telegram</h2>
    <p class="screen-sub">Через этого бота вы будете писать агенту, а он — вам.</p>
    <div class="field-row">
      <div class="field">
        <label for="telegram_token">Токен бота Telegram</label>
        <input id="telegram_token" name="telegram_token" type="password" autocomplete="off">
        <!-- Owner feedback: the old placeholder ("Сохранён — оставьте
             пустым, чтобы не менять") overran the field and rendered
             clipped ("...оставьте пустым чт"). applySecretPlaceholderEl()
             now writes a short placeholder that always fits and reveals
             this note (hidden unless is_set) with the full explanation
             instead. -->
        <div id="telegram_token_saved_note" class="field-note" hidden></div>
        <!-- Spec B2: no "Проверить" button — the check fires on change/blur
             and again (blocking) on "Далее". The wrapping .verdict-slot
             reserves height even while #telegram-verdict itself is hidden,
             so the first check response doesn't shove the id field below
             it down the page. -->
        <div class="verdict-slot"><div id="telegram-verdict" class="verdict" hidden></div></div>
        <div id="err_telegram_token" class="field-error" hidden></div>
      </div>
      <div class="hint">Где взять: откройте @BotFather в Telegram, отправьте команду /newbot и скопируйте выданный токен.</div>
    </div>

    <div class="field-row">
      <div class="field">
        <label for="allowed_users">Ваш Telegram id</label>
        <input id="allowed_users" name="allowed_users" type="text" autocomplete="off">
        <!-- Owner feedback п.4 (live VM walkthrough): "было бы круто, если
             бы там тоже высвечивалось сразу, кто это". Only ever shows a
             POSITIVE confirmation ("Это Имя (@username)") — see
             maybeRunTelegramUserCheck()'s own comment for why a failed
             lookup renders as silence, never as an error next to this
             field. Starts hidden — nothing has been confirmed yet. -->
        <div id="telegram-user-note" class="field-note" hidden></div>
        <div id="err_allowed_users" class="field-error" hidden></div>
      </div>
      <div class="hint">Как узнать: напишите @userinfobot — он пришлёт ваш id в ответ. Можно указать несколько через запятую.</div>
    </div>

    <p class="note"><b>Откройте бота и нажмите «Старт».</b> Пока вы этого не сделали, Telegram запрещает боту написать вам первым.</p>

    <!-- Часовой пояс (спека 11). Живёт на шаге Telegram, а не в
         «Дополнительно»: шаг 2 — единственный, который спрашивает про
         самого клиента (его бот, его id), и пояс из того же ряда. В
         «Дополнительно» лежит выбор возможностей, чей пропуск клиент
         понимает («этого у меня не будет»); пропуск пояса означает
         «расписание будет врать на несколько часов, и я не узнаю» —
         другой класс, потому и место другое.

         Собственный подзаголовок обязателен: под заголовком «Бот в
         Telegram» поле читалось бы как настройка бота. -->
    <div class="field-group">
      <h3 class="field-group-title">Ваше время</h3>
      <div class="field-row">
        <div class="field">
          <label for="timezone">Часовой пояс</label>
          <!-- Поиск над списком: поясов почти шестьсот, пролистать их
               нельзя. Фильтрует опции, но НИКОГДА не меняет выбранное
               значение — см. renderTimezone(). -->
          <input id="timezone_search" type="text" autocomplete="off"
                 placeholder="Найти: город, страна или Europe/Moscow">
          <select id="timezone" name="timezone"></select>
          <div id="timezone-warning" class="field-warning" hidden>
            <p id="timezone-warning-text"></p>
            <label for="timezone_ack">
              <input type="checkbox" id="timezone_ack">
              <span>Понимаю, меняем</span>
            </label>
          </div>
          <div id="err_timezone" class="field-error" hidden></div>
        </div>
        <div class="hint">По этому времени срабатывают напоминания и задачи по расписанию. «Напомни в 9 утра» — это девять утра здесь.</div>
      </div>
    </div>

    <div class="step-nav">
      <button type="button" id="step-3-back">Назад</button>
      <button type="button" id="step-3-next" class="accent">Далее</button>
    </div>
  </div>

  <div id="step-4" data-step="4" hidden>
    <h2 class="screen-title">Чей мозг у агента</h2>
    <p class="screen-sub">Модель отвечает за то, как агент думает. За неё платите вы напрямую провайдеру.</p>
    <div id="provider-block">
      <label id="provider_group_label" class="section-title" for="provider_group">Провайдер модели</label>
      <!-- Spec B3 (screen 4 of the approved mockup): a row per vendor
           instead of a bare <select> — description_ru, a "рекомендуем"/
           "нужен прокси" tag, and .sel highlighting all need markup a
           native <option> can't carry. renderProviderGroupOptions() builds
           the rows entirely in JS (see there for the group/rest partition
           and reachability-tag priority); this stays an empty shell. -->
      <div id="provider_group" class="prov" role="group" aria-labelledby="provider_group_label"></div>
      <div id="provider-current-hint" class="hint" style="padding-top:0.35rem"></div>
      <div id="err_provider_name" class="field-error" hidden></div>
      <p class="hint" id="provider-signup-hint">Чей ключ/аккаунт будете использовать для модели.</p>

      <!-- Owner feedback (live walkthrough): squeezed into a two-column
           .field-row next to a hint sentence, this read as a minor field
           ("почему там chat GPT только по подписке, а не ещё через API?" —
           the client never registered the API-key radio as a real,
           equally-weighted choice). Full-width now, laid out exactly like
           the "Провайдер модели" picker above it (label on its own line,
           options below, hint below that) — same visual register for two
           decisions of the same kind, not a smaller one. -->
      <div id="provider-auth-choice" hidden>
        <label id="provider-auth-choice-label">Способ подключения</label>
        <div id="provider-auth-options" class="prov" role="group" aria-labelledby="provider-auth-choice-label"></div>
        <p class="hint">У этого провайдера несколько способов подключения — выберите один.</p>
      </div>

      <div id="provider-api-key-block" hidden>
        <div class="field-row">
          <div class="field">
            <label for="provider_api_key">Ключ API провайдера</label>
            <input id="provider_api_key" name="provider_api_key" type="password" autocomplete="off">
            <div id="provider_api_key_saved_note" class="field-note" hidden></div>
            <!-- Spec B2: ключ проверяется сам после ввода (change/blur) —
                 см. runProviderKeyCheck() в скрипте. Не блокирует «Далее»
                 (ключ может быть непроверяемым — сервер отвечает
                 checked:false), но результат клиент должен видеть. Тот же
                 reserved-space приём, что и у telegram-verdict выше. -->
            <div class="verdict-slot"><div id="key-verdict" class="verdict" hidden></div></div>
            <div id="err_provider_api_key" class="field-error" hidden></div>
          </div>
          <div class="hint" id="provider-key-hint">Ключ выдаёт сам провайдер после регистрации по ссылке слева.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="provider_model">Модель</label>
            <!-- Spec B3: "получить список моделей" button is gone — the
                 live catalog is fetched automatically once a key is typed
                 (see fetchLiveModelsForRow(), called from
                 runProviderKeyCheck()). The three old controls (select +
                 button + free-text field) collapse into this one select:
                 "Ввести вручную…" (CUSTOM_MODEL_VALUE) is itself an
                 <option> that reveals provider_model_custom instead of the
                 field sitting there permanently. -->
            <select id="provider_model" name="provider_model">
              <option value="">по умолчанию</option>
              <option value="__custom_model__">Ввести вручную…</option>
            </select>
            <input id="provider_model_custom" name="provider_model_custom" type="text" autocomplete="off" placeholder="имя модели" hidden>
            <div id="models-fetch-error" class="verdict bad" hidden></div>
          </div>
          <div class="hint">Список моделей запрашивается у провайдера сам, как только введён ключ. Нет нужной модели в списке — выберите «Ввести вручную».</div>
        </div>
        <details id="provider-base-url-details" class="reveal">
          <summary>Адрес API — изменить</summary>
          <div class="field-row">
            <div class="field">
              <label for="provider_base_url">Адрес API</label>
              <input id="provider_base_url" name="provider_base_url" type="text" autocomplete="off">
            </div>
            <div class="hint">Обычно менять не нужно — подставляется автоматически для выбранного провайдера.</div>
          </div>
        </details>
      </div>

      <!-- Owner feedback (live walkthrough): a .field-row split the button
           into a left column shaped for a text input and the explanation
           into an unrelated right column — "как-то не посередине... не
           совместимо". A button (not a field) and its explanation read as
           one thing stacked in a single column, the same way .note/.hint
           sit directly under the control they describe everywhere else on
           this page — not as two halves of a field-row grid built for a
           label+input pair. -->
      <div id="provider-device-code-block" hidden>
        <button type="button" id="device-login-start">Войти по аккаунту</button>
        <p class="hint">Вход выполняется через сайт провайдера — код нужно ввести на открывшейся странице. Дождитесь «Вход выполнен» здесь.</p>
        <div id="device-login-info" hidden>
          <p class="hint">Откройте <a id="device-login-url" href="#" target="_blank" rel="noopener"></a> и введите код:</p>
          <p id="device-login-code" class="device-login-code"></p>
          <p id="device-login-status" class="device-login-status hint"></p>
        </div>
        <div id="err_device_login" class="field-error" hidden></div>
        <div id="device-model-block" hidden>
          <div class="field-row">
            <div class="field">
              <label for="provider_model_device">Модель</label>
              <select id="provider_model_device" name="provider_model_device">
                <option value="">по умолчанию</option>
                <option value="__custom_model__">Ввести вручную…</option>
              </select>
              <input id="provider_model_device_custom" name="provider_model_device_custom" type="text" autocomplete="off" placeholder="имя модели" hidden>
            </div>
            <div class="hint">Список моделей запрашивается автоматически после входа. Нет нужной модели в списке — выберите «Ввести вручную».</div>
          </div>
        </div>
      </div>
    </div>

    <div id="step4-fallback-slot"></div>

    <div class="step-nav">
      <button type="button" id="step-4-back">Назад</button>
      <button type="button" id="step-4-next" class="accent">Далее</button>
    </div>
  </div>

  <div id="step-5" data-step="5" hidden>
    <h2 class="screen-title">Что ещё умеет агент</h2>
    <p class="screen-sub">Здесь можно расширить возможности агента. Всё необязательно — можно пропустить и настроить позже.</p>
    <div id="advanced" class="rows">
      <div id="advanced-browser"></div>
      <div id="advanced-search"></div>
      <div id="advanced-extract"></div>
      <div id="advanced-voice"></div>
      <div id="advanced-stt"></div>
      <div id="advanced-image-gen"></div>
      <div id="advanced-video-gen"></div>
    </div>
    <div id="advanced-fallback" class="tool-block"></div>

    <div class="step-nav">
      <button type="button" id="step-5-back">Назад</button>
      <button type="button" id="step-5-next" class="accent">Далее</button>
    </div>
  </div>

  <div id="step-6" data-step="6" hidden>
    <h2 class="screen-title">Всё готово к запуску</h2>
    <p class="screen-sub">Проверьте напоследок. После нажатия агент перезапустится с этими настройками.</p>
    <div class="sum" id="summary-rows"></div>
    <p class="note">Запуск занимает <b>до пяти минут</b>: настройки сохранятся, агент перезапустится, и мы дождёмся, пока бот ответит. Не закрывайте страницу.</p>
    <div class="step-nav">
      <button type="button" id="step-6-back">Назад</button>
      <button type="button" id="done" class="accent">Запустить агента</button>
    </div>
  </div>
</form>

<section id="progress" hidden>
  <h2 class="screen-title">Запускаем агента</h2>
  <p class="screen-sub">Обычно это занимает две-три минуты, иногда до пяти. Не закрывайте страницу.</p>
  <ul class="stages" id="progress-stages">
    <li id="stage-apply"><u></u><span>Сохраняем настройки</span></li>
    <li id="stage-install" hidden><u></u><span>Устанавливаем инструменты</span></li>
    <li id="stage-restart"><u></u><span>Перезапуск агента</span></li>
    <li id="stage-liveness"><u></u><span>Ждём ответа бота</span></li>
  </ul>
</section>

<section id="success" hidden>
  <p class="screen-title" style="margin-bottom:0.35rem">Агент запущен</p>
  <p class="screen-sub" id="success-text">Готово! Напишите вашему боту: </p>
  <a id="botlink" class="botlink" href="#" target="_blank" rel="noopener"></a>
  <div class="sum">
    <div class="r"><i>Захотите что-то поменять</i><b>откройте этот же адрес и войдите тем же логином и паролем, что пришли письмом</b></div>
  </div>
  <p id="key-check-notice" class="hint" hidden></p>
  <div id="tool-install-notice" hidden></div>
  <div id="apply-warning-notice" hidden></div>
</section>
"""

# NB on the guard below (spec §7.2 "nothing preselected"): every <option>
# the client builds for the provider <select> is created with
# document.createElement and never has its `.selected` property (or the
# matching HTML attribute) set — that <select>'s value is left to the
# browser's own default (nothing chosen) until the client picks one.
# Recommended-but-not-required options in the ADVANCED blocks (browser,
# search, voice, STT, image generation, video generation) DO get a default: their
# logic sets `select.value = ...` directly (see pickPreselected below and
# its callers) rather than touching `.selected` on an <option>, and lives
# in functions that never mention "provider" — kept deliberately separate
# from providerRowFor/buildProviderOptions so the two concepts never
# appear near each other in this file.
_JS = """
(function () {
  "use strict";

  function byId(id) { return document.getElementById(id); }

  var state = {
    providers: [],
    providerGroups: [],
    // "web" (Поиск и извлечение страниц) is a regular member of `tools`
    // now — see toolBlockFor("web") in renderSearchBlock() below. There
    // is no separate `search` state any more.
    tools: [],
    current: {},
    // Часовой пояс (спека 11). Группы поясов приходят из /api/form —
    // второго, возможно разошедшегося списка внутри страницы нет.
    timezones: [],
    // Сколько задач по расписанию уже заведено. Три значения, и третье
    // существует отдельно от нуля: число, 0, либо null — «проверить не
    // удалось». Предупреждение о смене пояса говорит именно то, что
    // здесь лежит, и никогда не выдаёт незнание за отсутствие задач.
    cronJobs: 0,
    chosenProviderRow: null,
    // Contract with step 4 (spec B3, a later commit): /api/check/proxy's
    // own `providers` object (catalog group_id -> reachable-from-here
    // bool), captured by runProxyCheck() the moment the "Прокси" step's
    // autocheck answers. Step 4's provider list reads this to grey out an
    // unreachable provider instead of letting the client pick one blind
    // and find out five minutes later on "Готово". null until the first
    // check on step 2 completes.
    providerReachabilityByGroup: null,
    // Spec B3: which group_id's row is currently highlighted (.sel) in the
    // provider list — set the instant a row is clicked, read back by
    // renderProviderGroupOptions() so a reachability-driven re-render (see
    // runProxyCheck()) doesn't lose the client's own choice.
    chosenGroupId: null,
    // Spec B3: "Показать остальные N →" starts collapsed — every group is
    // still rendered (owner decision — all providers stay reachable, just
    // folded), this only remembers that the client asked to see the tail.
    providerListExpanded: false,
    // Owner feedback (this pass): before this, the full provider list
    // stayed rendered underneath a chosen row forever — 20+ rows sitting
    // below the key/model fields the client actually came here to fill
    // in. true = the picker (recommended rows, "Показать
    // остальные", every .p row) is what renderProviderGroupOptions() draws;
    // false = a picked group (state.chosenGroupId set) collapses down to
    // just that ONE row + a "выбрать другого провайдера" link. Starts
    // true (nothing chosen yet); pick() flips it false the instant a group
    // is clicked, the reopen link flips it back.
    providerPickerOpen: true,
    // Owner feedback (this pass): step 5's six category rows must never
    // show more than one open .row-body at a time, and none should stay
    // open across a step change ("наслоение" — a still-open "Браузер" row
    // bleeding into whatever the client does next). Populated once by
    // buildCollapsibleRow() (called from renderAdvanced(), itself called
    // once from loadForm()) — {row, close} per category. See
    // closeAllCollapsibleRows() and buildCollapsibleRow() below.
    collapsibleRows: [],
    // Last /api/check/telegram verdict for whatever token currently sits
    // in #telegram_token this session — {ok:true, username} on success,
    // {ok:false} otherwise, null before the first check (or after an edit
    // invalidates the previous one — see the telegram_token "input"
    // listener below). summaryBotValue() (step 6) reads this instead of
    // re-parsing rendered verdict text.
    telegramCheck: null,
    // Owner requirement 3: the wizard is ALWAYS the step wizard now — this
    // is no longer a mode switch between two different layouts, only a
    // record that enterStepsMode() has run (kept for showFieldErrors()'s
    // own guard, and any future caller that wants to know the wizard's
    // fields are on screen). null until loadForm()'s /api/form answer
    // decides — see enterStepsMode() below.
    mode: null,
    // Set once, in loadForm(), right after /api/form answers — a client
    // who already has telegram_token.is_set (spec §12.4's return-mode
    // signal) is RETURNING: same five steps as a first run, but every
    // step's progress-bar entry is clickable (they have nothing left
    // "ahead" to skip past) instead of only the completed ones. See
    // isStepClickable() below.
    returning: false,
    // The step (2-6) currently visible. There is no separate login step
    // any more (spec 8, §8.3 — HTTP Basic auth means a browser only ever
    // gets this markup once it's already authenticated), so the wizard
    // starts directly on step 2 the instant loadForm() resolves.
    currentStep: 2,
  };

  function setHidden(id, hidden) { byId(id).hidden = hidden; }

  // ---- Rail (spec 7 / plan B1: sidebar replaces the old horizontal step
  // strip). #progress-bar keeps its original id and its original
  // hidden/shown + className mechanism (renderProgressBar()/
  // isStepClickable(), both unchanged by this pass) — only its container
  // moved, from a <nav> above the form to an <ol> inside .rail. The logo
  // shows only before the first step is entered and on the success screen
  // (never while a step is open), the rail's own subtitle/foot text differ
  // by screen, and the compact foot line's "подробнее" reveals the
  // self-signed-certificate paragraph (see #cert-detail in _rail_html) —
  // that reveal is the ONLY place this text renders now that there is no
  // separate login screen to show it in full (spec 8, §8.3).
  function setRailMode(mode) {
    var isSuccess = mode === "success";
    var isSteps = mode === "steps";
    setHidden("rail-logo", isSteps);
    byId("rail-sub").textContent = isSuccess ? "Настроен" : "Настройка";
    setHidden("rail-foot-default", isSuccess);
    setHidden("rail-foot-success", !isSuccess);
    setHidden("rail-foot-cert-line", !isSteps);
    if (!isSteps) setHidden("cert-detail", true);
  }
  // The wizard has exactly one screen sequence now — there is no separate
  // login mode to start in (spec 8, §8.3: HTTP Basic auth means a browser
  // never sees this markup until it is already authenticated) — so the
  // rail renders in "steps" dress from the very first paint, before
  // loadForm() below has even resolved.
  setRailMode("steps");

  var certToggle = byId("cert-toggle");
  if (certToggle) {
    certToggle.addEventListener("click", function (e) {
      e.preventDefault();
      var detail = byId("cert-detail");
      detail.hidden = !detail.hidden;
    });
  }

  // Finding 19/11 pairing (spec 6/7): every OTHER fetch in this file
  // (models, device start/status, telegram/proxy checks, install) went
  // through jsonFetch() treating any response as ordinary JSON — a 401 or
  // 403 was invisible here and surfaced as a wrong, endpoint-specific lie
  // ("Проверка не пройдена.", "Прокси не отвечает", a silently empty model
  // list...). Centralized here so every caller gets the same honest
  // outcome in one place.
  //
  // There is no cookie session any more to expire (spec 8, §8.3 — the
  // browser resends its cached HTTP Basic credentials on every request, no
  // server-side session to go stale), so a 401/403 on a call made AFTER
  // the page itself already loaded means one of two things: the machine's
  // login/password changed underneath the client (an operator reset it
  // through an admin-only command), or this particular request failed the
  // Host/Origin CSRF guard. Neither has a form to show any more —
  // handleAuthLost() below just says so honestly and points at reloading
  // the page, which makes the browser re-prompt for credentials itself.
  function isAuthError(err) {
    return String(err && err.message) === "auth_lost";
  }

  function handleAuthLost() {
    stopDevicePoll();
    setSubmitInFlight(false);
    setHidden("progress", true);
    setHidden("main", false);
    byId("form-error").textContent =
      "Доступ пропал — возможно, изменились логин или пароль. Обновите страницу: браузер спросит их заново.";
  }

  // Spec 8, §8.3.6: a locked-out IP gets 429 with an HTML body
  // (_rate_limited_body in app.py, deliberately WITHOUT WWW-Authenticate —
  // see that function's own docstring), never JSON. Left unhandled here it
  // hits the exact same "every response is ordinary JSON" trap 401/403 were
  // fixed for above: res.json() fails to parse the HTML, and the endpoint's
  // OWN catch block paints a wrong, specific lie ("Прокси не отвечает.",
  // "Проверка не пройдена.") instead of the true reason — too many failed
  // login attempts from this machine, wait it out. Same centralized
  // treatment as auth_lost: one honest message, decided once in jsonFetch(),
  // never duplicated per caller.
  function isRateLimitedError(err) {
    return String(err && err.message) === "rate_limited";
  }

  function handleRateLimited() {
    stopDevicePoll();
    setSubmitInFlight(false);
    setHidden("progress", true);
    setHidden("main", false);
    byId("form-error").textContent =
      "Слишком много неудачных попыток входа с этой машины. Подождите немного и обновите страницу.";
  }

  // Finding 6 (this review pass): a client-side ceiling for the three
  // interactive autochecks below (proxy/telegram/key) — NOT used on every
  // jsonFetch call, since /api/submit is legitimately a multi-minute
  // request (save settings, restart the gateway, wait for the bot to
  // answer — see its own comment further down) and must never be aborted
  // by a blanket timeout. Without this, a hung request (the browser<->VM
  // connection itself dropping, not just a slow server — check_reachability
  // already bounds its own worst case to ~5s) left "Далее" disabled and
  // the step-2 progress-bar entry locked, with no "Назад" on step 2 and no
  // way out short of reloading the page. AbortSignal.timeout is absent in
  // very old runtimes only — degrades to no client-side ceiling there
  // rather than throwing, same as before this fix.
  function checkTimeoutSignal() {
    return (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function")
      ? AbortSignal.timeout(30000)
      : undefined;
  }

  function jsonFetch(path, options) {
    var opts = Object.assign({}, options || {});
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    return fetch(path, opts).then(function (res) {
      if (res.status === 401 || res.status === 403) {
        handleAuthLost();
        throw new Error("auth_lost");
      }
      if (res.status === 429) {
        handleRateLimited();
        throw new Error("rate_limited");
      }
      return res;
    });
  }

  // ---- Verdict banners (spec B2: автопроверки прокси/Telegram/ключа) ----
  //
  // One shared renderer for the three autochecks below (proxy step,
  // Telegram token, provider key) — each writes into its own reserved
  // <div class="verdict" id="..."> (see .verdict-slot in the CSS above for
  // the two that start [hidden]). Every piece of TEXT this touches is
  // either a fixed Russian string this file wrote itself, or landed
  // through .textContent (never .innerHTML) — the only .innerHTML writes
  // here are the two static, argument-free icon constants below.
  var VERDICT_OK_ICON = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M3 8.5l3.2 3.2L13 5" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var VERDICT_BAD_ICON = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M8 4v5" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"/><circle cx="8" cy="12" r="1.15" fill="currentColor"/></svg>';

  // kind: "ok" | "bad" | "wait". linkStep/linkText (optional) append a
  // clickable "перейти на шаг «Прокси»"-style action that calls goToStep()
  // — used by the Telegram checks whose fix lives on a different step.
  function setVerdict(id, kind, boldText, restText, linkStep, linkText) {
    var el = byId(id);
    if (!el) return;
    el.hidden = false;
    el.className = "verdict " + kind;
    el.innerHTML = "";
    if (kind === "ok" || kind === "bad") {
      var icon = document.createElement("span");
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = kind === "ok" ? VERDICT_OK_ICON : VERDICT_BAD_ICON;
      el.appendChild(icon);
    }
    var span = document.createElement("span");
    var b = document.createElement("b");
    b.textContent = boldText;
    span.appendChild(b);
    if (restText) span.appendChild(document.createTextNode(restText));
    if (linkStep) {
      span.appendChild(document.createTextNode(" "));
      var a = document.createElement("a");
      a.href = "#";
      a.textContent = linkText || "Перейти на шаг «Прокси»";
      a.addEventListener("click", function (e) {
        e.preventDefault();
        goToStep(linkStep);
      });
      span.appendChild(a);
    }
    el.appendChild(span);
  }

  // ---- Step wizard --------------------------------------------------------
  //
  // Owner requirement 3 (2026-08-21): EVERY client — first run or return
  // visit — walks Прокси -> Telegram -> Провайдер -> Дополнительно ->
  // Готово, one section visible at a time (plan B1 swapped the Прокси/
  // Telegram order — see the module docstring's own note on why). There
  // used to be a login step ahead of this walk (spec 6); spec 8, §8.3
  // removed it — HTTP Basic auth means the browser is already
  // authenticated before it gets any of this markup, so the wizard now
  // starts directly on Прокси. The internal step identifiers below (`n`,
  // still 2-6, matching the [data-step] wrapper divs and FIELD_STEP) are
  // untouched by that removal; only STEPS' own display numbering (`num`)
  // was renumbered to start at 1 for what the client actually sees. There
  // is no more "show everything at once" layout for a returning client (spec
  // §12.4's return-mode signal — current.telegram_token.is_set, decided in
  // loadForm() once /api/form answers — now only changes which steps the
  // progress bar lets you click, via state.returning; see
  // isStepClickable() below). A returning step's fields still arrive
  // pre-filled, through the SAME renderPrefill()/renderBrowserBlock()/etc.
  // mechanism a first-run client's step 5 always used — that prefill never
  // depended on a single-page layout, so it needed no changes to work here.
  //
  // Critically, NOTHING here recreates an <input> on a step change — every
  // step's fields are part of the static HTML from the very start (see
  // _MAIN_FORM_HTML), goToStep() only flips `.hidden` on the [data-step]
  // wrapper divs. A value typed on step 2 is still sitting in its <input>
  // when step 5 is reached, because that <input> node never left the DOM.

  // Plan B1: positions 2/3 swapped from the original layout — Прокси now
  // comes before Telegram (the network check on the Telegram step needs a
  // working proxy to succeed at all on a data-center machine). See
  // _MAIN_FORM_HTML's own comment on the matching div swap.
  // `n` is the internal step id (matches [data-step] on the wrapper divs,
  // and FIELD_STEP below) — unchanged by the login step's removal. `num`
  // is only the digit shown in the rail circle, renumbered to start at 1
  // now that there is nothing ahead of Прокси any more.
  var STEPS = [
    { n: 2, num: 1, label: "Прокси" },
    { n: 3, num: 2, label: "Telegram" },
    { n: 4, num: 3, label: "Провайдер" },
    { n: 5, num: 4, label: "Дополнительно" },
    { n: 6, num: 5, label: "Готово" },
  ];

  // Maps a field's <input> id (FIELD_MAP's VALUES, below) to the step it
  // lives on — used by showFieldErrors() to jump to the earliest step
  // carrying a 422 error, so a "Готово"-time validation failure surfaces
  // on a visible field instead of silently failing on a hidden one.
  var FIELD_STEP = {
    proxy: 2,
    telegram_token: 3,
    allowed_users: 3,
    timezone: 3,
    provider_name: 4,
    provider_api_key: 4,
    fallback_name: 4,
    fallback_api_key: 4,
  };

  // Owner requirement 3: the progress bar's own step numbers are a real
  // nav now, not just a decoration.
  //
  // A RETURNING client (state.returning) already has a fully configured
  // agent by the time they log back in — nothing further along the bar is
  // "not set up yet" the way it is on a first run, so every remaining step
  // is clickable and they can jump straight to whichever one they came
  // back to change.
  //
  // A FIRST-RUN client must still complete each step's required fields in
  // order — only a step strictly BEFORE the current one (i.e. already
  // walked through once via "Далее") is clickable; the current step and
  // everything still ahead stay locked, so the only way to reach them is
  // through the step's own "Далее" button, never by skipping ahead on the
  // bar.
  // submitInFlight (set by setSubmitInFlight(), declared in the Submit
  // section below — hoisted, so the forward reference here is fine) guards
  // against a client hiding step 6's #progress/#success mid-flight
  // (POST /api/submit can run for minutes: save, restart the gateway, wait
  // for the bot to come alive) by clicking an earlier step on the bar.
  function isStepClickable(n) {
    if (submitInFlight) return false;
    if (n < 2 || n === state.currentStep) return false;
    if (state.returning) return true;
    return n < state.currentStep;
  }

  function renderProgressBar() {
    var nav = byId("progress-bar");
    nav.innerHTML = "";
    STEPS.forEach(function (step) {
      var item = document.createElement("span");
      var clickable = isStepClickable(step.n);
      var classes = ["step-item"];
      if (step.n === state.currentStep) classes.push("current");
      else if (step.n < state.currentStep) classes.push("done");
      if (clickable) classes.push("clickable");
      else if (step.n > state.currentStep) classes.push("locked");
      item.className = classes.join(" ");
      var num = document.createElement("span");
      num.className = "step-num";
      num.textContent = step.n < state.currentStep ? "✓" : String(step.num);
      var label = document.createElement("span");
      label.textContent = step.label;
      item.appendChild(num);
      item.appendChild(label);
      if (clickable) {
        item.setAttribute("role", "button");
        item.setAttribute("tabindex", "0");
        item.addEventListener("click", function () { goToStep(step.n); });
        item.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            goToStep(step.n);
          }
        });
      }
      nav.appendChild(item);
    });
  }

  function goToStep(n) {
    // Owner feedback: fold every open step-5 category row on ANY step
    // change (leaving step 5 for good, or just re-entering it) — a row
    // left open from a previous visit must never "наслаиваться" onto
    // whatever the client does next. No-op when nothing is open.
    closeAllCollapsibleRows();
    state.currentStep = n;
    document.querySelectorAll("[data-step]").forEach(function (el) {
      el.hidden = Number(el.getAttribute("data-step")) !== n;
    });
    renderProgressBar();
    // Owner feedback п.1 (live VM walkthrough): step 2 ("Прокси") used to
    // autocheck the instant the step was entered — every path into it
    // (first arrival via enterStepsMode(), "Назад" from step 3, a
    // progress-bar jump) ran a live probe against whatever the field
    // already held, proxy typed or not. That is exactly the "checks before
    // I've typed anything" behavior the owner flagged — removed on
    // purpose, no replacement call here. The real triggers now live next
    // to the field itself: a debounced check after the client types
    // something (see #proxy's own "input" listener) and an unconditional
    // one on "Далее" (step-2-next's own handler) — see either for why.
    // Плана B5: сводка на шаге 6 читает уже заполненную форму — рендерится
    // при каждом входе на шаг, будь то "Далее" с шага 5 или клик по
    // прогресс-бару в режиме возврата (оба пути идут через goToStep(),
    // единственную точку входа на любой шаг).
    if (n === 6) renderSummary();
    // Finding 4 (review 2026-08-26, owner-approved fix): removing step 2's
    // autocheck-on-entry above (the "n === 2" branch this comment used to
    // sit next to) also silently removed the ONLY thing that ever filled
    // state.providerReachabilityByGroup for a client who never lands on
    // step 2 at all — a RETURNING client (state.returning, saved
    // TELEGRAM_BOT_TOKEN) gets a fully clickable progress bar and can jump
    // straight to step 4 ("Провайдер") from the header nav, same
    // goToStep() entry point as everything else. Without a fill, every
    // "нужен прокси" tag reachabilityTag() would otherwise draw (its own
    // "null map -> no tag, never a fabricated one" contract stays intact —
    // see that function's own comment) just doesn't exist yet, so a
    // provider that genuinely needs the VM's proxy shows as an ordinary,
    // unflagged option.
    //
    // This does NOT reintroduce the owner's "не проверять прокси, пока
    // ничего не введено" complaint: runProxyCheck() only ever paints a
    // VISIBLE "Проверяем…" verdict into #proxy-verdict, which lives inside
    // step 2's own [data-step="2"] container — hidden here since n is 4,
    // not 2 (see the data-step loop above). The fetch runs, and once it
    // resolves, renderProviderGroupOptions() (already part of
    // runProxyCheck()'s own .then) refreshes step 4's tags live — this is
    // a silent background fill, never a step-2 UI change. The
    // `state.providerReachabilityByGroup === null` guard makes this a
    // one-time backfill: the ordinary "Далее" path through step 2 already
    // populates the map before step 4 is ever reached, so this never
    // double-fires the network round trip finding 6 is about.
    if (n === 4 && state.providerReachabilityByGroup === null) runProxyCheck();
  }

  function wireStepNav(backId, nextId, nextStep) {
    var back = backId ? byId(backId) : null;
    if (back) back.addEventListener("click", function () { goToStep(state.currentStep - 1); });
    var next = nextId ? byId(nextId) : null;
    if (next) next.addEventListener("click", function () { goToStep(nextStep); });
  }

  function wireStepsNavigation() {
    // Steps 2 (Прокси) and 3 (Telegram) do NOT use the generic
    // wireStepNav()-driven "Далее" — both gate on their own autocheck (see
    // the step-2-next/step-3-next handlers below) instead of jumping
    // straight to the next step. Step 2 has no "Назад" (it is the first
    // step of the wizard).
    wireStepNav("step-4-back", "step-4-next", 5);
    // Commit 4 polish (owner review): "Далее" and "Пропустить всё" both
    // called goToStep(6) verbatim — two buttons for one action. Step 5's
    // "Дополнительно" is always fully expanded in the stepper (see
    // enterStepsMode()'s comment above — there is no separate collapsed
    // state to "skip" out of), so "Далее" alone already covers it; the
    // redundant "Пропустить всё" button is gone.
    wireStepNav("step-5-back", "step-5-next", 6);
    wireStepNav("step-6-back", null, null);
    var step3Back = byId("step-3-back");
    if (step3Back) step3Back.addEventListener("click", function () { goToStep(2); });
  }
  wireStepsNavigation();

  // ---- Прокси (step 2) — spec A4/B2: no button, no "never blocks either
  // way" rule any more. The check itself never blocks — it is what ANSWERS
  // "do you need a proxy" — but "Далее" (below) reruns it with whatever
  // the client just typed and refuses to advance while Telegram stays
  // unreachable, since without Telegram the product plain doesn't work.
  var proxyCheckSeq = 0;

  // Finding 6/7 (review 2026-08-26, owner-approved fix): mirrors
  // telegramCheckSeqInFlight's own contract (below) — set the moment a
  // check's fetch is issued, cleared once it settles. Starts at -1 for
  // the identical reason: proxyCheckSeq ALSO starts at 0, and 0 is a
  // legitimate "no check has ever run yet" value for it.
  var proxyCheckSeqInFlight = -1;

  // Finding 6 (review 2026-08-26, owner-approved fix): a real MOUSE click
  // on "Далее" fires blur on #proxy (whichever field last had focus)
  // BEFORE the click event itself — mousedown -> blur -> click, in that
  // order, in the same tick. If the client had just finished typing, the
  // blur handler below sees a still-pending debounce and runs an
  // IMMEDIATE check for the CURRENT value; the click handler's own
  // unconditional runProxyCheck() call then fires a SECOND full round
  // trip through the client's proxy (Telegram + every provider) for the
  // exact same value. Holds the in-flight promise so a second call for
  // the SAME proxyCheckSeq (no edit happened in between — see the
  // "input" listener's own proxyCheckSeq++) gets handed the one already
  // running instead of starting another.
  var proxyCheckInFlightPromise = null;

  // Owner feedback п.1 (live VM walkthrough): the debounce timer that
  // schedules a real check after the client stops typing into #proxy (see
  // the "input" listener below) — module-level so a fresh keystroke, an
  // early blur, or "Далее" can all cancel/pre-empt whatever is still
  // pending instead of leaving a redundant check to fire later on top of
  // one already running.
  var proxyCheckDebounceTimer = null;

  function clearProxyCheckDebounceTimer() {
    if (proxyCheckDebounceTimer) {
      clearTimeout(proxyCheckDebounceTimer);
      proxyCheckDebounceTimer = null;
    }
  }

  // Спека A4: если ответившее провайдера через прокси (via_proxy) не
  // покрывает все три «закрытых из РФ» сервиса, честно называем именно те,
  // что недоступны — не молчим и не выдумываем список заранее. Ключи здесь
  // — те же group_id/via_proxy-имена, что отдаёт /api/check/proxy; их
  // display-имена собственные для этого текста, не хосты (хосты клиент
  // не хардкодит нигде).
  var VIA_PROXY_DISPLAY_NAMES = { "openai-api": "OpenAI", "anthropic": "Anthropic", "openrouter": "OpenRouter" };

  function unavailableForeignProviders(viaProxy) {
    var names = [];
    Object.keys(VIA_PROXY_DISPLAY_NAMES).forEach(function (key) {
      if (viaProxy && viaProxy[key] === false) names.push(VIA_PROXY_DISPLAY_NAMES[key]);
    });
    return names;
  }

  // Two verdicts from the approved mockup (screens 2/2а), plus a third,
  // honest variant for a case the mockup doesn't show: Telegram answered
  // THROUGH a proxy the client already typed — "доступен напрямую" would
  // be a lie there, since a proxy is in fact doing the work.
  //
  // Finding 1 (this review pass — regression of a previously-closed
  // finding): proxy_invalid MUST be checked first. Without this branch, a
  // malformed proxy ("1.2.3.4:1080", no scheme) fell straight into the
  // `else` below and told the client "нужен прокси" — the exact opposite
  // of true, since they had already typed one; the real problem (wrong
  // format) was never named, and step 2 has no "Назад" to escape from.
  function renderProxyVerdict(data, proxyValueUsed) {
    if (data.proxy_invalid) {
      setVerdict("proxy-verdict", "bad", "Неверный формат прокси.", " Нужно socks5://user:pass@host:port или http://host:port.");
      return;
    }
    if (data.telegram) {
      if (proxyValueUsed) {
        setVerdict("proxy-verdict", "ok", "Прокси работает — Telegram доступен.", "");
        return;
      }
      var missing = unavailableForeignProviders(data.via_proxy || {});
      var tail = missing.length
        ? " Зарубежные провайдеры моделей отсюда закрыты: " + missing.join(", ") + " — будут работать только через прокси."
        : "";
      setVerdict("proxy-verdict", "ok", "Telegram доступен напрямую — прокси не нужен.", tail);
    } else {
      setVerdict("proxy-verdict", "bad", "Telegram отсюда недоступен — нужен прокси.", " Без него бот не заработает.");
    }
  }

  // Returns a Promise resolving to the /api/check/proxy answer (or a
  // synthetic {telegram:false} on our own request failing) — step-2-next
  // reads the resolved value's `.telegram` to decide whether to advance.
  // `proxyCheckSeq` discards a stale response if the client fires a second
  // check (e.g. edits the field then immediately clicks "Далее") before
  // the first one returns.
  function runProxyCheck() {
    // A debounced check that was still waiting to fire is superseded by
    // this one, whichever triggered it (the debounce timer itself, an
    // early blur, or "Далее") — nothing left to cancel later.
    clearProxyCheckDebounceTimer();
    // Finding 6 (review 2026-08-26, owner-approved fix): a caller that
    // arrives while a check for the CURRENT proxyCheckSeq is already in
    // flight (the mousedown-triggered blur, immediately followed by
    // "Далее"'s own click — see proxyCheckInFlightPromise's own comment)
    // gets handed that SAME promise instead of issuing a second live
    // round trip through the client's proxy. proxyCheckSeq only changes
    // on an actual edit (the "input" listener's own ++), so this can
    // never dedup two checks for genuinely DIFFERENT values.
    if (proxyCheckInFlightPromise && proxyCheckSeqInFlight === proxyCheckSeq) {
      return proxyCheckInFlightPromise;
    }
    var proxyValue = (byId("proxy").value || "").trim();
    var seq = ++proxyCheckSeq;
    proxyCheckSeqInFlight = seq;
    setVerdict("proxy-verdict", "wait", "Проверяем…", "");
    var promise = jsonFetch("/api/check/proxy", { method: "POST", body: JSON.stringify({ proxy: proxyValue }), signal: checkTimeoutSignal() })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        // Finding 7 (review 2026-08-26, owner-approved fix): only clear
        // the in-flight sentinel when IT is the check settling — a stale
        // response arriving after a newer check has already started
        // (seq !== proxyCheckSeq) must not clobber the sentinel the
        // newer check just set, or the very next blur/click would see
        // "nothing in flight" and fire a redundant request while the
        // actually-current check is still running.
        if (seq === proxyCheckSeqInFlight) proxyCheckSeqInFlight = -1;
        if (seq !== proxyCheckSeq) return null;
        // Contract with step 4 (see state.providerReachabilityByGroup's
        // own comment) — captured on every check, not just the first.
        state.providerReachabilityByGroup = data.providers || {};
        renderProxyVerdict(data, !!proxyValue);
        // Spec B3: refresh the provider list's "нужен прокси"/"недоступен"
        // tags now that reachability is known — a no-op the very first
        // time this runs before /api/form's provider_groups have loaded
        // (goToStep(2) never fires before enterStepsMode(), which only
        // runs after loadForm() resolves, so state.providerGroups is
        // already populated by the time step 2 is ever reachable).
        renderProviderGroupOptions();
        return data;
      })
      .catch(function (err) {
        if (seq === proxyCheckSeqInFlight) proxyCheckSeqInFlight = -1;
        if (isAuthError(err) || isRateLimitedError(err)) return null;
        setVerdict("proxy-verdict", "bad", "Не удалось выполнить проверку.", " Проверьте соединение и попробуйте снова.");
        return { telegram: false };
      })
      .then(function (result) {
        if (proxyCheckInFlightPromise === promise) proxyCheckInFlightPromise = null;
        return result;
      });
    proxyCheckInFlightPromise = promise;
    return promise;
  }

  byId("step-2-next").addEventListener("click", function () {
    var btn = byId("step-2-next");
    btn.disabled = true;
    // Owner feedback п.1: "Далее" is the one place that MUST always run a
    // real check, empty field included — the client's one remaining
    // safety net against submitting a proxy-less setup they actually
    // needed a proxy for. runProxyCheck() itself clears any still-pending
    // debounce timer, so this never double-fires against the debounced
    // check below.
    runProxyCheck().then(function (data) {
      btn.disabled = false;
      if (data && data.telegram) goToStep(3);
      // Else: stay on step 2 — the verdict just re-rendered above already
      // spells out why ("Telegram отсюда недоступен — нужен прокси.").
    });
  });

  // Owner feedback п.1 (live VM walkthrough): "надо проверять прокси
  // только тогда, когда что-то там вставилось" — #proxy used to autocheck
  // the instant step 2 was entered (goToStep()'s old n===2 branch, removed
  // above) and every keystroke here only queued a passive "проверим при
  // переходе «Далее»" note, never an actual check — a client who typed a
  // working proxy and just sat on step 2 (no "Далее" click yet) had no way
  // to learn it actually worked. An edit that leaves the field non-empty
  // now schedules the REAL check — debounced (see
  // proxyCheckDebounceTimer's own comment), never on every character, per
  // the owner's own "не на каждый символ" — while an edit that empties the
  // field shows nothing at all (Finding 5's original bug this listener
  // fixed still holds: a stale "недоступен"/"нужен прокси" verdict from
  // the OLD value must not survive an edit — an empty field has nothing
  // honest to say until "Далее" runs the unconditional check). proxyCheckSeq
  // still bumps on every keystroke so an in-flight check for a value the
  // client has since changed can't land late and paint a verdict for text
  // no longer in the field.
  byId("proxy").addEventListener("input", function () {
    proxyCheckSeq++;
    clearProxyCheckDebounceTimer();
    // Finding 12 (review 2026-08-26, owner-approved fix): the Telegram
    // token and provider key checks both run THROUGH whatever proxy is
    // typed here (see runTelegramCheck()/runProviderKeyCheck() — both
    // read #proxy's own value into their request body). A verdict earned
    // through the OLD proxy must not keep blocking a re-check once the
    // client changes it and comes back to step 3/4 — without resetting
    // these, maybeRunTelegramCheck()'s `if (state.telegramCheck) return;`
    // and maybeRunProviderKeyCheck()'s `if (keyCheckSettled) return;`
    // would both treat the stale, wrong-proxy verdict as still current
    // and silently skip the blur/change that should have re-verified it.
    // Same reset shape each field's OWN "input" listener already uses.
    setHidden("telegram-verdict", true);
    state.telegramCheck = null;
    telegramCheckSeq++;
    setHidden("telegram-user-note", true);
    setHidden("key-verdict", true);
    keyCheckSeq++;
    keyCheckSettled = false;
    var value = (byId("proxy").value || "").trim();
    if (!value) {
      setHidden("proxy-verdict", true);
      return;
    }
    setVerdict("proxy-verdict", "wait", "Прокси изменён.", " Проверяем…");
    proxyCheckDebounceTimer = setTimeout(function () {
      proxyCheckDebounceTimer = null;
      runProxyCheck();
    }, 600);
  });

  // A blur with a debounced check still queued means the client typed
  // something and then tabbed/clicked away — that is itself "done
  // typing", so run the check now instead of making them wait out the
  // rest of the window. No-op when nothing is queued (empty field, or the
  // debounced check already fired/was superseded) — this must never
  // become a second "check on entry" for an untouched field.
  byId("proxy").addEventListener("blur", function () {
    if (!proxyCheckDebounceTimer) return;
    runProxyCheck();
  });

  // ---- Telegram (step 3) — spec B2: blocking autocheck, no button. Runs
  // on change/blur (below) and again — authoritatively — on "Далее"; the
  // step never advances without a confirmed-good token, EXCEPT the one
  // case where a returning client left the (never-echoed) secret field
  // untouched — see step-3-next's own comment.
  var telegramCheckSeq = 0;

  function telegramProxyLink(text) {
    // Every "не добрались"-shaped failure below points back at step 2 —
    // spec's table: proxy missing, proxy present but not working, and a
    // malformed proxy all send the client to the same place to fix it.
    return { linkStep: 2, linkText: text || "Перейти на шаг «Прокси»" };
  }

  // Renders the verdict for one /api/check/telegram answer and returns
  // {ok, username} — used both by state.telegramCheck (gates "Далее") and
  // by the summary step's summaryBotValue().
  function renderTelegramVerdict(data, hasProxy) {
    if (data && data.ok === true) {
      var el = byId("telegram-verdict");
      el.hidden = false;
      el.className = "verdict ok";
      el.innerHTML = "";
      var icon = document.createElement("span");
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = VERDICT_OK_ICON;
      var span = document.createElement("span");
      span.appendChild(document.createTextNode("Подключён бот "));
      var b = document.createElement("b");
      // XSS: the bot's @username comes straight from Telegram's own
      // getMe answer — textContent only, never innerHTML/string concat.
      b.textContent = "@" + data.username;
      span.appendChild(b);
      el.appendChild(icon);
      el.appendChild(span);
      // Finding 5 (review 2026-08-26, owner-approved fix): the retry call
      // that used to sit right here (owner feedback п.4 — "the token just
      // went from unverified to verified, retry the id lookup in case the
      // client already typed an id before finishing the token") was dead
      // on arrival: maybeRunTelegramUserCheck() reads state.telegramCheck.ok
      // as its precondition, but this function's OWN return value is what
      // the caller (runTelegramCheck()'s `.then`) assigns to
      // state.telegramCheck — AFTER renderTelegramVerdict() has already
      // returned. Calling it here, mid-render, always saw the PREVIOUS
      // (usually null, from #telegram_token's own "input" handler)
      // state.telegramCheck — the retry branch could never fire. Moved to
      // runTelegramCheck() itself, right after the assignment it depends
      // on; see that function's own comment.
      return { ok: true, username: data.username };
    }
    // The token is no longer confirmed-good (or never was) — any "Это
    // <имя>" note on screen was earned by a PREVIOUS, now-superseded
    // verification and must not survive it.
    setHidden("telegram-user-note", true);
    if (data && data.proxy_invalid) {
      var link = telegramProxyLink();
      setVerdict("telegram-verdict", "bad", "Неверный формат прокси на шаге «Прокси»: нужно socks5://… или http://….", "", link.linkStep, link.linkText);
      return { ok: false };
    }
    if (data && data.network) {
      var text = hasProxy
        ? "Telegram не отвечает через указанный прокси. Проверьте адрес на шаге «Прокси»."
        : "С этой машины не удалось связаться с Telegram. Скорее всего нужен прокси —";
      var netLink = telegramProxyLink(hasProxy ? "Перейти на шаг «Прокси»" : "вернитесь на шаг «Прокси» и укажите его");
      setVerdict("telegram-verdict", "bad", text, "", netLink.linkStep, netLink.linkText);
      return { ok: false };
    }
    // Reached Telegram and got a definite "no" — the token itself is
    // wrong (401/404, or ok:false in the JSON body). This falls out of
    // the two structural flags above already being ruled out, never a
    // text match against the server's own reply (spec: differentiate by
    // network/proxy_invalid).
    setVerdict(
      "telegram-verdict",
      "bad",
      "Telegram не признаёт этот токен.",
      " Проверьте, что скопировали его целиком, без пробелов. Получить заново: @BotFather → /mybots → ваш бот → API Token."
    );
    return { ok: false };
  }

  // Returns a Promise resolving to {ok, username?} — step-3-next reads
  // `.ok` to decide whether to advance.
  function runTelegramCheck() {
    var token = (byId("telegram_token").value || "").trim();
    if (!token) {
      // Finding 4 (this review pass): a returning client's saved token
      // never echoes into this field (secrets are never echoed at all —
      // see applySecretPlaceholder) — an empty field here is normal for
      // them, not a missing token. Without this guard, blur firing from
      // the client's own click on "Далее" painted a red "Вставьте токен
      // бота" over a bot that already works, on EVERY visit to step 3.
      // Same "don't fabricate, don't complain" rule the key field's own
      // empty-value branch already uses (runProviderKeyCheck): hide,
      // don't claim anything, since no live probe actually ran.
      if (state.current.telegram_token && state.current.telegram_token.is_set) {
        setHidden("telegram-verdict", true);
        state.telegramCheck = null;
        return Promise.resolve(state.telegramCheck);
      }
      setVerdict("telegram-verdict", "bad", "Вставьте токен бота — его выдаёт @BotFather по команде /newbot.", "");
      state.telegramCheck = { ok: false };
      return Promise.resolve(state.telegramCheck);
    }
    var proxyValue = (byId("proxy").value || "").trim();
    var seq = ++telegramCheckSeq;
    telegramCheckSeqInFlight = seq;
    setVerdict("telegram-verdict", "wait", "Проверяем…", "");
    return jsonFetch("/api/check/telegram", { method: "POST", body: JSON.stringify({ token: token, proxy: proxyValue }), signal: checkTimeoutSignal() })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        // Finding 7 (review 2026-08-26, owner-approved fix): only clear
        // the in-flight sentinel when IT is the check settling — a stale
        // response landing after a NEWER check has already started
        // (seq !== telegramCheckSeq, checked right below) must not
        // clobber the sentinel the newer check just set, or the next
        // blur would see "nothing in flight" and fire a redundant
        // request while the actually-current one is still running.
        if (seq === telegramCheckSeqInFlight) telegramCheckSeqInFlight = -1;
        if (seq !== telegramCheckSeq) return state.telegramCheck || { ok: false };
        state.telegramCheck = renderTelegramVerdict(data, !!proxyValue);
        // Finding 5 (review 2026-08-26, owner-approved fix): the retry
        // belongs HERE, after state.telegramCheck is actually assigned —
        // see renderTelegramVerdict()'s own comment on why calling it from
        // inside the render function itself was always a no-op.
        // maybeRunTelegramUserCheck() already no-ops when .ok is false (a
        // failed/expired token), so this is safe to call unconditionally.
        maybeRunTelegramUserCheck();
        return state.telegramCheck;
      })
      .catch(function (err) {
        if (seq === telegramCheckSeqInFlight) telegramCheckSeqInFlight = -1;
        if (isAuthError(err) || isRateLimitedError(err)) return state.telegramCheck || { ok: false };
        // Our OWN request failed to complete — indistinguishable from
        // "Telegram itself is unreachable" from here, so the same
        // network-shaped, non-token-blaming verdict applies.
        state.telegramCheck = renderTelegramVerdict({ ok: false, network: true }, !!proxyValue);
        return state.telegramCheck;
      });
  }

  // Owner feedback п.3 (live VM walkthrough): "я ввёл токен, он проверил,
  // всё окей — почему, когда я ввожу Telegram id, он снова начинает
  // проверять токен?" Root cause: #telegram_token carried BOTH a "change"
  // AND a "blur" listener, each independently calling runTelegramCheck()
  // unconditionally. Tabbing out of the token field into the very next
  // field ("Ваш Telegram id") fires both events back to back, in the same
  // tick — well before the first request's fetch could possibly settle —
  // so leaving the token field for the id field fired a SECOND live
  // /api/check/telegram round trip on top of the first, and the verdict
  // visibly flashed "Проверяем…" again right as the client's attention
  // moved to typing the id. telegramCheckSeqInFlight (set the moment a
  // check's fetch is issued, cleared once it settles — success, failure,
  // or an auth/rate-limit bypass, all matter, since an unresolved "in
  // flight" flag would otherwise wedge every check thereafter) plus the
  // already-settled state.telegramCheck (nulled by the "input" listener on
  // every keystroke) together answer "is there anything left to check for
  // the CURRENT value" — a genuine edit still reruns the check on the next
  // blur; a second trigger for a value that is already settled or already
  // in flight does not. Starts at -1, never 0 — telegramCheckSeq ALSO
  // starts at 0, and 0 is a legitimate "no check has ever run yet" value
  // for it, so 0 cannot double as this flag's "nothing in flight" sentinel
  // without the very first check silently matching it and never firing.
  var telegramCheckSeqInFlight = -1;

  function maybeRunTelegramCheck() {
    if (state.telegramCheck) return;
    if (telegramCheckSeqInFlight === telegramCheckSeq) return;
    // Owner feedback п.2: "до того как я не ввёл ничего, ничего не
    // проверять не надо" — an empty field (first visit, nothing typed yet;
    // or a returning client whose saved token never echoes back — see
    // applySecretPlaceholder) has nothing to check. Blur naturally fires
    // the moment focus leaves an untouched field (tabbing straight through
    // on arrival, or a returning client whose token stays empty on
    // purpose) — showing "Вставьте токен бота" THERE, before any attempt
    // to advance, is exactly the premature check the owner flagged. Далее
    // still runs runTelegramCheck() directly and unconditionally, so an
    // empty+unsaved field is still caught the moment the client actually
    // tries to leave step 3.
    var token = (byId("telegram_token").value || "").trim();
    if (!token) return;
    runTelegramCheck();
  }

  // A fresh keystroke invalidates whatever verdict is currently shown —
  // without this, editing a token after a successful check would leave
  // "Подключён бот @old_name" on screen (and state.telegramCheck.ok=true
  // in summaryBotValue()) for a token that no longer matches what is
  // actually in the field.
  //
  // Finding 2 (this review pass): `telegramCheckSeq++` here too, not just
  // in runTelegramCheck() — without it, this only invalidates an ALREADY
  // FINISHED verdict, not a check still in flight. Repro: type token A,
  // click "Далее" (check in flight), then edit the field to token B before
  // the response lands — the in-flight response's `seq` still equals
  // `telegramCheckSeq`, so it renders token A's "Подключён бот" verdict
  // and advances to step 4 while token B (never checked) is what actually
  // gets submitted.
  byId("telegram_token").addEventListener("input", function () {
    setHidden("telegram-verdict", true);
    state.telegramCheck = null;
    telegramCheckSeq++;
    // The token is being edited — whatever "Это <имя>" note is showing
    // was earned by whatever token was there BEFORE this keystroke.
    setHidden("telegram-user-note", true);
  });
  byId("telegram_token").addEventListener("change", maybeRunTelegramCheck);
  byId("telegram_token").addEventListener("blur", maybeRunTelegramCheck);

  // ---- "Кто это" lookup for #allowed_users (owner feedback п.4, live VM
  // walkthrough): "было бы круто, если бы там тоже высвечивалось сразу,
  // кто это, чтобы было понятно, что ты не ошибся". Purely informational,
  // never blocking "Далее", never shown as an error — see
  // validate.check_telegram_user's own docstring for why Telegram
  // genuinely can't resolve a user who hasn't started the bot yet (the
  // SAME "нажмите «Старт»" precondition the note right below this field
  // already asks for), which makes a failed lookup meaningless as
  // evidence the id itself is wrong.
  //
  // Requires a token this session has actually verified — getChat needs a
  // real, working bot token to call at all — and exactly one bare integer
  // id: the field legitimately accepts several, comma-separated (see its
  // own hint text), and showing a name for "the first one" would silently
  // claim something about ids nobody asked about.
  var telegramUserCheckSeq = 0;
  // "<token>:<id>" the last lookup was issued for, set the moment the
  // request starts (before the fetch resolves) — the same immediate-dedupe
  // shape maybeRunTelegramCheck()/maybeRunProviderKeyCheck() use to stop
  // "change" and "blur" firing back to back on the same tab-out from
  // issuing two requests for the identical value.
  var telegramUserLastCheckedKey = null;
  var _SINGLE_TELEGRAM_ID_RE = /^-?[0-9]+$/;

  function renderTelegramUserNote(data) {
    var el = byId("telegram-user-note");
    if (!data || data.ok !== true) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = "";
    el.appendChild(document.createTextNode("Это "));
    var b = document.createElement("b");
    // XSS: name/username come straight from Telegram's own getChat answer
    // — textContent only, never innerHTML/string concat (same rule
    // renderTelegramVerdict()'s own @username uses above).
    var pieces = [];
    if (data.name) pieces.push(data.name);
    if (data.username) pieces.push("@" + data.username);
    b.textContent = pieces.join(" ");
    el.appendChild(b);
  }

  function maybeRunTelegramUserCheck() {
    var token = (byId("telegram_token").value || "").trim();
    if (!token || !state.telegramCheck || !state.telegramCheck.ok) {
      setHidden("telegram-user-note", true);
      return;
    }
    var raw = (byId("allowed_users").value || "").trim();
    if (!_SINGLE_TELEGRAM_ID_RE.test(raw)) {
      // Empty, or several ids separated by commas — nothing unambiguous
      // to look up.
      setHidden("telegram-user-note", true);
      return;
    }
    var key = token + ":" + raw;
    if (key === telegramUserLastCheckedKey) return;
    telegramUserLastCheckedKey = key;
    var seq = ++telegramUserCheckSeq;
    var proxyValue = (byId("proxy").value || "").trim();
    jsonFetch("/api/check/telegram_user", { method: "POST", body: JSON.stringify({ token: token, user_id: raw, proxy: proxyValue }), signal: checkTimeoutSignal() })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (seq !== telegramUserCheckSeq) return;
        renderTelegramUserNote(data);
      })
      .catch(function (err) {
        if (isAuthError(err) || isRateLimitedError(err)) return;
        if (seq !== telegramUserCheckSeq) return;
        // Owner feedback п.4: a failed lookup (network error, Telegram
        // hasn't seen this user yet, anything at all) is never an error —
        // it only means "can't confirm right now". Never paint anything
        // scary next to a field the client may have typed correctly.
        setHidden("telegram-user-note", true);
        // Finding 8 (review 2026-08-26, owner-approved fix): OUR OWN
        // request failed to complete (network blip, timeout — never a
        // definite Telegram answer either way), so this token+id pair was
        // never actually resolved. telegramUserLastCheckedKey was set
        // BEFORE the fetch (dedup guard above) and, unlike a real
        // negative/positive answer, must not stick — without this, a
        // client whose lookup hit a transient network error could never
        // retry the SAME id again (the dedup guard would keep skipping
        // it) without editing the field to a different value and back.
        telegramUserLastCheckedKey = null;
      });
  }

  byId("allowed_users").addEventListener("input", function () {
    setHidden("telegram-user-note", true);
    telegramUserCheckSeq++;
    telegramUserLastCheckedKey = null;
  });
  byId("allowed_users").addEventListener("change", maybeRunTelegramUserCheck);
  byId("allowed_users").addEventListener("blur", maybeRunTelegramUserCheck);

  byId("timezone").addEventListener("change", function () {
    setHidden("err_timezone", true);
    updateTimezoneWarning();
  });

  byId("timezone_ack").addEventListener("change", function () {
    setHidden("err_timezone", true);
  });

  byId("timezone_search").addEventListener("input", function () {
    renderTimezone(byId("timezone_search").value);
  });

  byId("step-3-next").addEventListener("click", function () {
    // Часовой пояс закрывает ПЕРЕХОД, а не весь обработчик. Ворота в
    // начале выглядели дешевле (проверка не ходит в сеть), но меняли
    // поведение шага: клиент, не дошедший до пояса, переставал получать
    // по «Далее» вердикт по своему боту — а имя бота здесь единственное
    // подтверждение, что подключён тот, кого хотели. Проверка токена
    // выполняется как прежде; удерживается только сам уход на шаг 4,
    // и обе ошибки при этом могут быть видны одновременно.
    var typed = (byId("telegram_token").value || "").trim();
    if (!typed && state.current.telegram_token && state.current.telegram_token.is_set) {
      // Owner principle: never ask for what is already saved and working.
      // A returning client who left this (never-echoed) secret field
      // untouched already has a verified token from when it was first
      // saved — re-demanding it here on every visit would be exactly the
      // "лишняя кнопка" friction the whole autocheck redesign exists to
      // remove.
      if (!timezoneStepOk()) return;
      goToStep(4);
      return;
    }
    // Уже проверено и прочитано — уходим сразу. state.telegramCheck
    // хранит УСТОЯВШИЙСЯ вердикт и обнуляется на любом "input" в поле,
    // так что он не равен ok ровно тогда, когда результат ещё не был
    // показан человеку: поле не теряло фокус, либо токен только что
    // правили. Клиент, который ушёл с поля (Tab, клик мимо) и прочитал
    // "Подключён бот @имя", здесь не платит ни лишним кликом, ни
    // ожиданием.
    if (state.telegramCheck && state.telegramCheck.ok) {
      if (!timezoneStepOk()) return;
      goToStep(4);
      return;
    }
    // Проверка выполняется прямо сейчас, по этому нажатию. Показываем
    // вердикт и ОСТАЁМСЯ на шаге: имя бота — единственное подтверждение,
    // что человек подключил именно того бота, которого хотел, и раньше
    // оно исчезало в тот же миг, что и появлялось (найдено владельцем на
    // живой машине 2026-08-25). Второе нажатие уйдёт по быстрому пути
    // выше — state.telegramCheck к тому моменту уже устоялся.
    var btn = byId("step-3-next");
    btn.disabled = true;
    runTelegramCheck().then(function () {
      btn.disabled = false;
    });
  });

  // Moves the (already-rendered, by renderAdvanced() -> renderAdvancedFallback())
  // "Запасная модель" block from its default home inside #advanced (step
  // 5's container) to the bottom of step 4 (Провайдер), per spec: "+
  // Запасная модель" belongs with the rest of the provider block in the
  // stepper — first run AND return visits alike, now that both walk the
  // same steps. A DOM move, never a rebuild — appendChild() relocates the
  // existing node (and whatever value its <input>s already carry) rather
  // than recreating it, and is a no-op if already moved (idempotent —
  // enterStepsMode() only ever runs once per page load, but this stays
  // cheap to call defensively).
  function relocateFallbackBlockForSteps() {
    var slot = byId("step4-fallback-slot");
    var block = byId("advanced-fallback");
    if (slot && block && block.parentNode !== slot) {
      slot.appendChild(block);
    }
  }

  // Owner requirement 3: the ONLY entry point into the main form now —
  // every client, first run or return visit, lands on step 2 (Прокси) and
  // walks the same five steps. `isReturning` (spec §12.4's signal, decided by the
  // caller in loadForm()) only changes state.returning, which
  // renderProgressBar()'s isStepClickable() reads to decide whether every
  // step is jumpable (returning — nothing left unconfigured to protect)
  // or only the completed ones (first run — required fields must still be
  // walked in order). Every field on every step is pre-filled from
  // `current` on a return visit through the pre-existing prefill
  // mechanism (renderPrefill()/renderProviderGroupOptions()/
  // renderBrowserBlock()/etc., all called from loadForm() before this
  // runs) — nothing here re-renders anything, it only decides what is
  // visible and what is clickable.
  function enterStepsMode(isReturning) {
    state.mode = "steps";
    state.returning = !!isReturning;
    // <form id="main"> is rendered `hidden` and stays that way until the
    // form data has actually loaded. Раньше его раскрывал обработчик
    // успешного входа через HTML-форму; вместе с формой (спека 8 §8.3)
    // уехал и этот вызов, а на путь успеха его никто не вернул — так что
    // рельс и шапка рисовались, а справа было пусто. Три оставшихся
    // setHidden("main", false) сидели только в обработчиках ОШИБОК, то
    // есть форма показывалась при неудачной загрузке и не показывалась
    // при удачной. Найдено владельцем на живой машине 2026-08-25.
    setHidden("main", false);
    setHidden("progress-bar", false);
    setRailMode("steps");
    // Step 5 in the stepper IS the "Дополнительно" step — #advanced is a
    // plain <div class="rows"> now (план B4), always visible the moment
    // step 5 itself is; each category collapses/expands on its own via
    // buildCollapsibleRow(), never the whole section at once.
    relocateFallbackBlockForSteps();
    goToStep(2);
  }

  // ---- Form data ------------------------------------------------------

  function loadForm() {
    return jsonFetch("/api/form").then(function (res) { return res.json(); }).then(function (data) {
      state.providers = data.providers || [];
      state.providerGroups = data.provider_groups || [];
      state.tools = data.tools || [];
      state.current = data.current || {};
      state.timezones = data.timezones || [];
      // `undefined` (ключа нет вовсе) и `null` («не смогли прочитать») —
      // разные вещи, и обе не равны нулю. Отсутствующий ключ трактуем как
      // незнание: придумать за сервер, что задач нет, мы не вправе.
      state.cronJobs = data.cron_jobs === undefined ? null : data.cron_jobs;
      renderProviderGroupOptions();
      renderTimezone();
      renderPrefill();
      renderAdvanced();
      // Return-mode signal (spec §12.4): a client who already has a saved
      // Telegram token is a RETURNING client — owner requirement 3: they
      // still walk the same step wizard as a first run (enterStepsMode()
      // is the only entry point now), just with every step pre-filled and
      // the whole progress bar clickable. Decided exactly once per
      // /api/form response, after every field the prefill could care
      // about has already been rendered above.
      var isReturning = !!(state.current.telegram_token && state.current.telegram_token.is_set);
      enterStepsMode(isReturning);
    });
  }

  // Spec 8, §8.3: there is no login screen to submit any more — HTTP
  // Basic auth means the browser already proved itself before this
  // markup (and this script) ever reached it, so loading the wizard's
  // own data starts the instant the script runs. A failure here is
  // either a genuine loss of access (handleAuthLost() already ran, via
  // jsonFetch()) or an ordinary "couldn't reach the server" — the same
  // honest fallback message either way.
  loadForm().catch(function (err) {
    if (!isAuthError(err) && !isRateLimitedError(err)) {
      setHidden("main", false);
      byId("form-error").textContent = "Не удалось загрузить форму настройки. Обновите страницу и попробуйте снова.";
    }
  });

  // ---- Model-provider block (spec §7.2 — nothing chosen by default) --
  //
  // Owner requirement 1: the primary picker is GROUPED (one row per
  // vendor — "OpenAI" is a single entry, not two) — group_id/display_name/
  // variants come from /api/form's provider_groups. Picking a group with
  // more than one variant reveals a
  // "способ подключения" radio; a single-variant group skips straight to
  // that one variant's own sub-block. Either way the actual VARIANT row
  // (state.chosenProviderRow) is what api_key/device_code rendering and
  // buildPayload() key off — group_id is a display concept only and is
  // never sent to the server.

  function providerRowFor(name) {
    // Flat lookup by variant slug — used for the "Сейчас настроено" hint,
    // the return-mode prefill, and the (still-flat, ungrouped)
    // "запасная модель" picker in renderAdvancedFallback().
    for (var i = 0; i < state.providers.length; i++) {
      if (state.providers[i].name === name) return state.providers[i];
    }
    return null;
  }

  function providerGroupFor(groupId) {
    for (var i = 0; i < state.providerGroups.length; i++) {
      if (state.providerGroups[i].group_id === groupId) return state.providerGroups[i];
    }
    return null;
  }

  // Spec B3 п.2: absent/empty is honest silence, not a guess — a group the
  // proxy check never covered (only openai/anthropic/openrouter/deepseek/
  // zai/google get an entry — see app.py's _reachability_providers_by_group)
  // gets no tag at all. `true` also means no tag (reachable is not itself
  // newsworthy). A proxy already typed and STILL failing through it is a
  // stronger, more honest claim than "нужен прокси" (which promises a
  // proxy would fix it) — see /api/check/proxy's own via_proxy/direct split.
  function reachabilityTag(groupId) {
    var map = state.providerReachabilityByGroup;
    if (!map || !Object.prototype.hasOwnProperty.call(map, groupId)) return null;
    if (map[groupId]) return null;
    var hasProxy = !!(byId("proxy").value || "").trim();
    return hasProxy ? "недоступен" : "нужен прокси";
  }

  // One .p row (mockup screen 4). The unreachable tag always wins over
  // "рекомендуем" when both would apply (e.g. OpenAI is recommended AND,
  // without a proxy, unreachable) — what to DO about it right now outranks
  // a general endorsement the client can't act on yet.
  function renderProviderRow(group) {
    var row = document.createElement("div");
    row.className = "p" + (state.chosenGroupId === group.group_id ? " sel" : "");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.dataset.groupId = group.group_id;

    var name = document.createElement("b");
    name.textContent = group.display_name;
    row.appendChild(name);

    var offText = reachabilityTag(group.group_id);
    if (offText) {
      var offTag = document.createElement("span");
      offTag.className = "tag off";
      offTag.textContent = offText;
      row.appendChild(offTag);
    } else if (group.recommended) {
      var recTag = document.createElement("span");
      recTag.className = "tag rec";
      recTag.textContent = "рекомендуем";
      row.appendChild(recTag);
    }

    // Spec A1: description_ru only — the upstream catalog's English
    // `description` must never reach this markup.
    if (group.description_ru) {
      var desc = document.createElement("span");
      desc.className = "desc";
      desc.textContent = group.description_ru;
      row.appendChild(desc);
    }

    function pick() {
      state.chosenGroupId = group.group_id;
      // Owner feedback (this pass): picking a group used to leave the
      // WHOLE list (recommended + every expanded row) rendered underneath
      // — the client had to scroll past 20+ provider rows to reach the
      // key/model fields their choice had just revealed. A full rebuild
      // (not the old lightweight re-highlight) is what lets
      // renderProviderGroupOptions() switch into the collapsed
      // "one chosen row + изменить" view below.
      state.providerPickerOpen = false;
      renderProviderGroupOptions();
      onProviderGroupChange(group.group_id);
    }
    row.addEventListener("click", pick);
    row.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        pick();
      }
    });
    return row;
  }

  // Owner feedback (live walkthrough): a fresh install's config.yaml carries
  // model.provider: "auto" (DEFAULT_CONFIG's own un-set default, never a
  // real catalog entry) — providerRowFor("auto") finds no row and this used
  // to fall back to the raw literal, rendering the meaningless "Сейчас
  // настроено: auto." "auto" now gets the same silence an empty value
  // already got. The second sentence ("Ничего не выбрано автоматически —
  // выберите провайдера заново...") explained a mechanic that is obvious
  // from the picker itself (nothing is highlighted) and is dropped —
  // what's left states the one fact this hint actually needs to: what is
  // live right now for a returning client, before they touch the list.
  function updateProviderCurrentHint() {
    var currentName = (state.current.provider || {}).name || "";
    var hint = byId("provider-current-hint");
    if (currentName && currentName !== "auto") {
      var row = providerRowFor(currentName);
      hint.textContent = "Сейчас настроено: " + (row ? row.display_name : currentName) + ".";
    } else {
      hint.textContent = "";
    }
  }

  // Owner decision (spec B3 п.1): every group renders — nothing is hidden
  // from the client, only folded. Recommended groups (server-sorted first
  // already — providers_view.wizard_provider_groups()) always show; the
  // rest collapse behind "Показать остальные N →" until state
  // .providerListExpanded flips true. Called again whenever the proxy
  // step's reachability answer changes (see runProxyCheck()) so the
  // "нужен прокси"/"недоступен" tags refresh without losing whichever row
  // the client already picked.
  //
  // Owner feedback (this pass): once a group IS picked
  // (state.chosenGroupId set) and the client hasn't explicitly asked to
  // change it (state.providerPickerOpen), the picker collapses to just
  // that one row — the full list (recommended rows, "Показать
  // остальные", every unreachable/off row) is dead weight once a choice
  // is made; it was staying rendered underneath the key/model fields,
  // pushing them far down the page. "Выбрать другого провайдера" reopens
  // the full picker without losing the current pick — it's still
  // highlighted (.sel) the moment it reopens.
  function renderProviderGroupOptions() {
    var list = byId("provider_group");
    clearContainer(list);

    var chosenGroup = state.chosenGroupId ? providerGroupFor(state.chosenGroupId) : null;
    if (chosenGroup && !state.providerPickerOpen) {
      list.appendChild(renderProviderRow(chosenGroup));
      var change = document.createElement("div");
      change.className = "more";
      var changeLink = document.createElement("a");
      changeLink.href = "#";
      changeLink.textContent = "Выбрать другого провайдера";
      changeLink.addEventListener("click", function (e) {
        e.preventDefault();
        state.providerPickerOpen = true;
        renderProviderGroupOptions();
      });
      change.appendChild(changeLink);
      list.appendChild(change);
      updateProviderCurrentHint();
      return;
    }

    var recommended = state.providerGroups.filter(function (g) { return g.recommended; });
    var rest = state.providerGroups.filter(function (g) { return !g.recommended; });

    recommended.forEach(function (g) { list.appendChild(renderProviderRow(g)); });

    if (state.providerListExpanded) {
      rest.forEach(function (g) { list.appendChild(renderProviderRow(g)); });
    } else if (rest.length) {
      var more = document.createElement("div");
      more.className = "more";
      var a = document.createElement("a");
      a.href = "#";
      a.textContent = "Показать остальные " + rest.length + " →";
      a.addEventListener("click", function (e) {
        e.preventDefault();
        state.providerListExpanded = true;
        renderProviderGroupOptions();
      });
      more.appendChild(a);
      list.appendChild(more);
    }

    updateProviderCurrentHint();
  }

  // Picking a group renders its "способ подключения" radios (skipped
  // entirely for a single-variant group — see wizard_provider_groups()'s
  // own contract: a group reduced to one surviving variant still carries
  // exactly one entry in `variants`) and always clears whatever variant
  // was previously chosen. Nothing is auto-selected here (spec §7.2), even
  // for a single-variant group — the client still has to see the
  // api_key/device_code sub-block appear as a direct result of THIS
  // choice, not a silent side effect of switching groups.
  function onProviderGroupChange(groupId) {
    var group = providerGroupFor(groupId);
    var authChoice = byId("provider-auth-choice");
    var authOptions = byId("provider-auth-options");
    clearContainer(authOptions);

    if (!group) {
      authChoice.hidden = true;
      onProviderChange(null);
      return;
    }

    if (group.variants.length <= 1) {
      authChoice.hidden = true;
      onProviderChange(group.variants[0] || null);
      return;
    }

    authChoice.hidden = false;
    // Same clickable-card pattern as renderProviderRow() above — a native
    // radio button (read by nothing else — grep confirms) but visually
    // registered, per the owner, as a minor checkbox list rather than the
    // SAME kind of decision as the provider picker right above it. Reusing
    // .prov .p (card, .sel highlight, keyboard-selectable) makes the two
    // choices look like the two choices they are.
    group.variants.forEach(function (variant) {
      var row = document.createElement("div");
      row.className = "p";
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.dataset.variantName = variant.name;
      var label = document.createElement("b");
      label.textContent = variant.auth_label;
      row.appendChild(label);

      function pick() {
        var rows = authOptions.querySelectorAll(".p");
        for (var i = 0; i < rows.length; i++) rows[i].classList.remove("sel");
        row.classList.add("sel");
        onProviderChange(variant);
      }
      row.addEventListener("click", pick);
      row.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          pick();
        }
      });
      authOptions.appendChild(row);
    });
    onProviderChange(null);
  }

  // Spec B3 п.5: the old select + button + free-text trio collapses into
  // one <select> — "Ввести вручную…" is itself an <option> that reveals
  // the (otherwise hidden) free-text field next to it, rather than that
  // field sitting there permanently. Both provider_model and
  // provider_model_device carry this sentinel as their LAST <option> in
  // the static markup (see _MAIN_FORM_HTML) — resetModelSelect()/
  // addModelOptions() below both key off it staying last.
  var CUSTOM_MODEL_VALUE = "__custom_model__";

  // Clears every fetched/fallback <option> a previous provider/live-probe
  // added, but keeps the two static ones ("" and CUSTOM_MODEL_VALUE) —
  // the one full-reset case (onProviderChange, a genuinely different
  // catalog); addModelOptions()'s own live-probe merge below must NOT
  // reset, only add.
  function resetModelSelect(select) {
    for (var i = select.options.length - 1; i >= 0; i--) {
      var value = select.options[i].value;
      if (value !== "" && value !== CUSTOM_MODEL_VALUE) select.remove(i);
    }
  }

  function addModelOptions(select, models) {
    // Additive only — never clears existing <option>s first. The one
    // caller that DOES need a full reset (switching providers in
    // onProviderChange, a genuinely different catalog) calls
    // resetModelSelect() itself before this; the live /api/models probe
    // must not, since an empty result there is "the provider has no live
    // catalog for this key", not "forget the offline fallback_models
    // catalog that's already showing". New options are inserted BEFORE
    // the "Ввести вручную…" sentinel so it stays the last entry.
    var existing = {};
    var customOpt = null;
    for (var i = 0; i < select.options.length; i++) {
      existing[select.options[i].value] = true;
      if (select.options[i].value === CUSTOM_MODEL_VALUE) customOpt = select.options[i];
    }
    (models || []).forEach(function (model) {
      if (existing[model]) return;
      var opt = document.createElement("option");
      opt.value = model;
      opt.textContent = model;
      if (customOpt) select.insertBefore(opt, customOpt);
      else select.appendChild(opt);
      existing[model] = true;
    });
  }

  // Shows/hides the free-text field next to a model <select> based on
  // whether "Ввести вручную…" is the current pick.
  function syncModelCustomVisibility(select, custom) {
    if (!select || !custom) return;
    custom.hidden = select.value !== CUSTOM_MODEL_VALUE;
  }

  // Spec B3 п.7: "по умолчанию" on its own doesn't say which model that
  // IS — reuse the exact same source renderKeyVerdict()'s own "Модель по
  // умолчанию: …" already trusts (row.fallback_models[0]), so the two
  // never disagree.
  function defaultModelOptionLabel(row) {
    var defaultModel = (row && row.fallback_models && row.fallback_models[0]) || "";
    return defaultModel ? "по умолчанию (" + defaultModel + ")" : "по умолчанию";
  }

  function onProviderChange(row) {
    // Finding 3 (this review pass): invalidate any in-flight key check /
    // model fetch from the PREVIOUS provider, not just the already-landed
    // verdict below. Without this, a key check started right before the
    // switch (its response still in flight) lands afterward with a
    // matching seq and renders "Ключ принят" for the NEW provider's
    // (empty) key field — the client sees a green verdict for a field
    // they never touched and can submit it blind (see keyCheckSeq/
    // modelsFetchSeq's own declarations below for the invariant this
    // keeps: every check's seq must be bumped by anything that changes
    // WHICH check would now be the current one).
    keyCheckSeq++;
    keyCheckSettled = false;
    modelsFetchSeq++;
    state.chosenProviderRow = row;
    var apiBlock = byId("provider-api-key-block");
    var deviceBlock = byId("provider-device-code-block");
    var signupHint = byId("provider-signup-hint");
    // Spec B2: whatever key-verdict was showing belonged to the PREVIOUS
    // provider's env var — carrying it over to a freshly chosen row would
    // be a straight-up false claim ("Ключ принят" for a key we haven't
    // even asked about yet).
    setHidden("key-verdict", true);
    setHidden("models-fetch-error", true);
    // Spec B3 п.8 (bug found in review): a key typed for the PREVIOUS
    // provider must never survive a switch to a different one — every
    // other field this branch touches (model, base_url) already resets on
    // a genuine catalog switch; the key field was the one exception,
    // which meant it could be submitted under the NEW provider's env_var
    // (buildPayload() reads whatever sits in this input, not which
    // provider it was typed for). Cleared unconditionally, including when
    // row is null or device_code (kinds that don't even show this field).
    byId("provider_api_key").value = "";

    if (!row) {
      apiBlock.hidden = true;
      deviceBlock.hidden = true;
      resetDeviceLoginUI();
      signupHint.textContent = "Чей ключ/аккаунт будете использовать для модели.";
      return;
    }

    if (row.kind === "device_code") {
      apiBlock.hidden = true;
      deviceBlock.hidden = false;
      resetDeviceLoginUI();
      // Genuinely switching catalogs here — mirrors the api_key branch's
      // own reset below (review finding: this block was missing entirely,
      // so a stale model list from an earlier device_code variant survived
      // a switch to a different one). Cleared unconditionally every time
      // this branch runs — loadDeviceModels() (below, or the return-mode
      // branch right under this) repopulates it fresh whenever a login is
      // already known-good.
      var deviceModelSelect = byId("provider_model_device");
      resetModelSelect(deviceModelSelect);
      deviceModelSelect.options[0].textContent = defaultModelOptionLabel(row);
      deviceModelSelect.value = "";
      var deviceModelCustom = byId("provider_model_device_custom");
      if (deviceModelCustom) deviceModelCustom.value = "";
      syncModelCustomVisibility(deviceModelSelect, deviceModelCustom);
      // Same field-shadowing fix as the api_key branch's modelSelect.onchange
      // below — assignment, not addEventListener, for the same idempotency
      // reason (onProviderChange re-runs on every render).
      if (deviceModelCustom) {
        deviceModelSelect.onchange = function () {
          if (deviceModelSelect.value !== CUSTOM_MODEL_VALUE) deviceModelCustom.value = "";
          syncModelCustomVisibility(deviceModelSelect, deviceModelCustom);
          if (deviceModelSelect.value === CUSTOM_MODEL_VALUE) deviceModelCustom.focus();
        };
      }
      // Return-mode: this exact variant is already the active provider AND
      // still has a live credential right now (device_login_is_valid() on
      // the server, surfaced as current.provider.device_login_ok) — don't
      // force a returning client through the OAuth dance again just
      // because they touched the radio/row.
      var currentProviderDC = state.current.provider || {};
      if (currentProviderDC.name === row.name && currentProviderDC.device_login_ok) {
        showDeviceLoginSuccess("✓ Вход выполнен (сохранённая сессия)");
        loadDeviceModels(row);
      }
    } else {
      deviceBlock.hidden = true;
      resetDeviceLoginUI();
      apiBlock.hidden = false;
      // Round-trip fix (review finding "Important 3"): when the chosen
      // provider is the SAME one already configured (state.current.provider
      // .name), prefer what the wizard itself wrote last time over the
      // catalog's generic default — otherwise re-selecting your own active
      // provider silently reverts a custom base_url/model back to the
      // catalog default the instant you touch the row, even though
      // buildPayload() would then submit that reverted value as if it were
      // deliberate. Only a straight prefill: the client still sees its own
      // value and can edit or clear it like any other field.
      var currentProvider = state.current.provider || {};
      var isCurrentProvider = !!currentProvider.name && currentProvider.name === row.name;
      var baseUrlValue = (isCurrentProvider && currentProvider.base_url) || row.base_url || "";
      byId("provider_base_url").value = baseUrlValue;
      // Spec B3 п.6: "Адрес API" lives behind "изменить" now — but a
      // returning client with a genuinely non-default base_url should
      // still SEE it without hunting for the toggle.
      var baseUrlDetails = byId("provider-base-url-details");
      if (baseUrlDetails) baseUrlDetails.open = !!(isCurrentProvider && currentProvider.base_url);
      var modelSelect = byId("provider_model");
      // Genuinely switching catalogs here (a different provider's model
      // list) — this IS the one place a full reset is correct, unlike
      // the live-probe merge in fetchLiveModelsForRow() below.
      resetModelSelect(modelSelect);
      modelSelect.options[0].textContent = defaultModelOptionLabel(row);
      addModelOptions(modelSelect, row.fallback_models);
      var modelCustom = byId("provider_model_custom");
      var currentModel = (isCurrentProvider && currentProvider.model) || "";
      var modelIsListed = false;
      for (var oi = 0; oi < modelSelect.options.length; oi++) {
        if (modelSelect.options[oi].value === currentModel) { modelIsListed = true; break; }
      }
      if (currentModel && modelIsListed) {
        modelSelect.value = currentModel;
        if (modelCustom) modelCustom.value = "";
      } else if (currentModel) {
        // A round-tripped model the fallback/live catalog doesn't list —
        // still legitimate (spec §7.2 free-form entry), shown through the
        // "Ввести вручную…" branch instead of silently losing it.
        modelSelect.value = CUSTOM_MODEL_VALUE;
        if (modelCustom) modelCustom.value = currentModel;
      } else {
        modelSelect.value = "";
        if (modelCustom) modelCustom.value = "";
      }
      syncModelCustomVisibility(modelSelect, modelCustom);
      // Field-shadowing fix (review): modelFieldValue() below prefers
      // provider_model_custom over the <select> whenever the free-text
      // field is non-empty. The round-trip prefill just above can leave
      // that field holding last session's model — if the client then
      // picks a different entry from the <select>, that pick would
      // silently lose to the stale custom text unless we clear it here.
      // Assignment (not addEventListener), same idempotency reason as
      // the provider row above: onProviderChange re-runs on every render
      // and must not stack a new handler each time.
      if (modelCustom) {
        modelSelect.onchange = function () {
          if (modelSelect.value !== CUSTOM_MODEL_VALUE) modelCustom.value = "";
          syncModelCustomVisibility(modelSelect, modelCustom);
          if (modelSelect.value === CUSTOM_MODEL_VALUE) modelCustom.focus();
        };
      }
    }
    renderSignupHint(row);
  }

  // Owner feedback (live walkthrough): this used to repeat row.description_ru
  // verbatim — text the client had already just read in the .desc span of
  // the provider row above (renderProviderRow()) — with the signup URL
  // tacked on as bare text, not a link. Now shows only what the row above
  // does NOT carry: the registration link itself, rendered safely (same
  // scheme-checked posture as setVerificationLink() below — our own catalog
  // only ever produces https:// signup_url values, this is defense in
  // depth, not a case expected to actually trigger).
  function renderSignupHint(row) {
    var el = byId("provider-signup-hint");
    clearContainer(el);
    if (!row || !row.signup_url) return;
    if (isHttpUrl(row.signup_url)) {
      el.appendChild(document.createTextNode("Регистрация: "));
      var link = document.createElement("a");
      link.href = row.signup_url;
      link.textContent = row.signup_url;
      link.target = "_blank";
      link.rel = "noopener";
      el.appendChild(link);
    } else {
      el.textContent = "Регистрация: " + row.signup_url;
    }
  }

  // ---- Auto-check the API key on step 4 (spec B2) — via the existing
  // /api/check/key, never blocking "Далее" (a vendor with no live probe on
  // the server side answers `checked:true, reachable:false, message:""`,
  // which is NOT a pass; see renderKeyVerdict()'s own comment on why that
  // case gets its own honest, non-claiming "wait" verdict instead of
  // silence OR a fabricated "принят").
  var keyCheckSeq = 0;

  // Same double-fire class as telegram_token's own fix above (owner
  // feedback п.3 — the "parity pair" the task asks to look for): this
  // field carried an unguarded "change" AND "blur" listener too, each
  // independently issuing a fresh live probe. keyCheckSeqInFlight (set
  // while a probe is outstanding, cleared once it settles) plus
  // keyCheckSettled (true once a response has actually rendered for the
  // value currently in the field, reset on every edit or a switch to a
  // different vendor) together stop a second trigger for an
  // already-verified-or-in-flight value from firing a redundant round
  // trip. Starts at -1, never 0 — same reasoning as
  // telegramCheckSeqInFlight's own comment: keyCheckSeq ALSO starts at 0,
  // so 0 cannot double as "nothing in flight" without the very first
  // check silently matching it and never firing at all.
  var keyCheckSeqInFlight = -1;
  var keyCheckSettled = false;

  function renderKeyVerdict(data, row) {
    if (!data || !data.checked) {
      setHidden("key-verdict", true);
      return;
    }
    if (data.reachable && data.ok) {
      var defaultModel = (row && row.fallback_models && row.fallback_models[0]) || "";
      setVerdict("key-verdict", "ok", "Ключ принят.", defaultModel ? " Модель по умолчанию: " + defaultModel : "");
      return;
    }
    if (data.message) {
      // Covers BOTH a reachable-but-rejected key (401/403) and our own
      // probe request failing outright (network error) — check_provider_key
      // already gives each its own honest Russian message; reuse it
      // verbatim instead of collapsing both into one generic string.
      setVerdict("key-verdict", "bad", data.message, "");
      return;
    }
    // Reached here with data.checked true but neither branch above fired
    // (reachable false, message empty) — no live probe exists for this
    // vendor at all (see CREDENTIAL_PROBES in credential_probes.py). Never
    // claim "принят" for a key nothing actually contacted a server with.
    setVerdict("key-verdict", "wait", "Ключ сохранён — этот провайдер не проверяется автоматически.", "");
  }

  function runProviderKeyCheck() {
    var row = state.chosenProviderRow;
    if (!row || row.kind !== "api_key") return;
    var value = (byId("provider_api_key").value || "").trim();
    if (!value) {
      // A returning client's saved key never echoes into this field (see
      // applySecretPlaceholderEl) — an empty field here means "keep what's
      // already saved", not "no key at all", so there is nothing to check
      // and nothing honest to say about it yet.
      setHidden("key-verdict", true);
      return;
    }
    var envVar = row.env_var || null;
    var proxyValue = (byId("proxy").value || "").trim();
    var seq = ++keyCheckSeq;
    keyCheckSeqInFlight = seq;
    var rowAtCallTime = row;
    jsonFetch("/api/check/key", { method: "POST", body: JSON.stringify({ env_var: envVar, value: value, proxy: proxyValue }), signal: checkTimeoutSignal() })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        // Finding 7 (review 2026-08-26, owner-approved fix): only clear
        // the in-flight sentinel when IT is the check settling — same
        // reasoning as runTelegramCheck()'s own fix (its comment has the
        // full rationale): a stale response landing after a NEWER check
        // has already started must not clobber the sentinel the newer
        // check just set.
        if (seq === keyCheckSeqInFlight) keyCheckSeqInFlight = -1;
        if (seq !== keyCheckSeq) return;
        keyCheckSettled = true;
        renderKeyVerdict(data, rowAtCallTime);
        // Spec B3 п.4: no more manual "fetch models" button — the
        // live catalog loads by itself once the key round-trip answers,
        // whatever that answer was (accepted, rejected, or "this vendor
        // has no live probe at all" — fetchLiveModelsForRow() hits
        // /api/models directly and doesn't care which of those three this
        // was; a wrong key just gets an empty/short live list back).
        fetchLiveModelsForRow(rowAtCallTime);
      })
      .catch(function (err) {
        if (seq === keyCheckSeqInFlight) keyCheckSeqInFlight = -1;
        if (isAuthError(err) || isRateLimitedError(err)) return;
        setHidden("key-verdict", true);
      });
  }

  // Owner feedback п.3 (parity pair — see telegram_token's own
  // maybeRunTelegramCheck() for the original finding): skip a redundant
  // recheck when the value is already checked/checking, and skip entirely
  // on an empty field — nothing typed (or a returning client's un-echoed
  // saved key) means nothing to check yet.
  function maybeRunProviderKeyCheck() {
    var row = state.chosenProviderRow;
    if (!row || row.kind !== "api_key") return;
    if (keyCheckSettled) return;
    if (keyCheckSeqInFlight === keyCheckSeq) return;
    var value = (byId("provider_api_key").value || "").trim();
    if (!value) return;
    runProviderKeyCheck();
  }

  // Finding 3 (this review pass): same in-flight-invalidation gap as
  // telegram_token's own "input" listener (Finding 2) — a fresh keystroke
  // must discard a check already in flight for the OLD value, not just an
  // already-rendered verdict. Repro: type a key, let the check start, edit
  // the field before the response lands — without this bump the stale
  // response's seq still matches and paints "Ключ принят" over whatever
  // is now in the field.
  byId("provider_api_key").addEventListener("input", function () {
    setHidden("key-verdict", true);
    keyCheckSeq++;
    keyCheckSettled = false;
    // Same reasoning for the live-models fetch a PREVIOUS keystroke's
    // check may have already kicked off (fetchLiveModelsForRow merges
    // into the <select> rather than replacing it, so a stale response
    // landing after this edit would silently mix an old key's model list
    // into the one the client is about to see for the new key).
    modelsFetchSeq++;
  });
  byId("provider_api_key").addEventListener("change", maybeRunProviderKeyCheck);
  byId("provider_api_key").addEventListener("blur", maybeRunProviderKeyCheck);

  // Spec B3 п.4: replaces the old manual "fetch models" button —
  // called from runProviderKeyCheck() above once a typed key's check
  // round-trip answers, never from a click. modelsFetchSeq discards a
  // stale response the same way keyCheckSeq/telegramCheckSeq/
  // proxyCheckSeq already do elsewhere in this file (fast edits can fire
  // more than one of these before the first reply lands).
  var modelsFetchSeq = 0;

  function fetchLiveModelsForRow(row) {
    if (!row || row.kind !== "api_key") return;
    var seq = ++modelsFetchSeq;
    setHidden("models-fetch-error", true);
    jsonFetch("/api/models", {
      method: "POST",
      body: JSON.stringify({
        provider: row.name,
        api_key: byId("provider_api_key").value,
        base_url: byId("provider_base_url").value,
        // Read live, at request time — a RU-hosted server may need the
        // proxy field to reach the provider's own model-catalog endpoint
        // (OpenAI/OpenRouter/Anthropic among others) at all.
        proxy: byId("proxy").value,
      }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (seq !== modelsFetchSeq) return;
        // Merge, never replace: an empty live result (key not entered
        // yet, provider has no live model-list endpoint, transient
        // network failure — fetch_live_models() degrades to [] on any
        // of these, per its own docstring) must not wipe out the
        // catalog's offline fallback_models this <select> already
        // carries from onProviderChange.
        addModelOptions(byId("provider_model"), data.models);
      })
      .catch(function (err) {
        if (isAuthError(err) || isRateLimitedError(err)) return;
        if (seq !== modelsFetchSeq) return;
        // Спека B3: ошибку показывать честно, не молча — the offline
        // fallback_models list (already in the <select> from
        // onProviderChange) stays usable either way.
        setVerdict("models-fetch-error", "bad", "Не удалось получить список моделей у провайдера.", " Можно выбрать «Ввести вручную» в списке моделей.");
      });
  }

  // ---- Device-code login (owner requirement 2): the "Войти по аккаунту"
  // button drives /api/device/start + /api/device/status end to end, right
  // here in the browser — no more "log in from the command line" excuse.
  // Poll interval is fixed at 3s per the task brief; DeviceLoginManager on
  // the server owns the real 15-minute timeout, this loop just stops
  // polling once it sees a terminal state (ok/error).

  var devicePollTimer = null;

  function stopDevicePoll() {
    if (devicePollTimer) {
      clearInterval(devicePollTimer);
      devicePollTimer = null;
    }
  }

  function resetDeviceLoginUI() {
    stopDevicePoll();
    setHidden("device-login-info", true);
    setHidden("device-model-block", true);
    var btn = byId("device-login-start");
    btn.disabled = false;
    btn.textContent = "Войти по аккаунту";
    byId("device-login-status").textContent = "";
    var err = byId("err_device_login");
    err.hidden = true;
    err.textContent = "";
  }

  function showDeviceLoginSuccess(message) {
    setHidden("device-login-info", false);
    byId("device-login-status").textContent = message;
    var btn = byId("device-login-start");
    btn.disabled = false;
    btn.textContent = "Войти повторно";
  }

  function loadDeviceModels(row) {
    setHidden("device-model-block", false);
    jsonFetch("/api/models", {
      method: "POST",
      // provider_model_ids() (the device-code catalog path on the server)
      // never touches the network, so proxy is a no-op here — sent anyway
      // for a uniform request shape, harmless either way.
      body: JSON.stringify({ provider: row.name, api_key: "", base_url: "", proxy: byId("proxy").value }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) { addModelOptions(byId("provider_model_device"), data.models); })
      .catch(function () {});
  }

  // The server (DeviceLoginManager) is ONE active login per PROCESS, not
  // per browser tab/session — a status poll started for THIS login can
  // race a different login started elsewhere (another tab, another admin,
  // or this same tab switching providers and back). login_id pins each
  // poll loop to the exact login it was started for; a status response
  // for a different login_id (or, defensively, a different provider) is
  // ignored rather than misapplied to the wrong sub-block.
  //
  // Only a display-layer safeguard — the actual write-side fix (a stale
  // login can never persist tokens after being superseded) lives entirely
  // server-side, in DeviceLoginManager's generation gate.
  function isHttpUrl(url) {
    return /^https?:\\/\\//i.test(String(url || ""));
  }

  function setVerificationLink(url) {
    var link = byId("device-login-url");
    if (isHttpUrl(url)) {
      link.href = url;
      link.textContent = url;
    } else {
      // Never wire an unexpected scheme (javascript:, data:, ...) into
      // href — show it as inert text instead. Our own server only ever
      // returns https:// URLs; this is defense in depth, not a case that
      // should ever actually trigger.
      link.removeAttribute("href");
      link.textContent = String(url || "");
    }
  }

  // Finding 14: DeviceLoginManager.retire() (server, called after a
  // successful /api/submit — see device_login.py) raises the login's
  // generation without touching self._status, so a login the client never
  // confirmed stays "pending" forever server-side. Without its own
  // deadline this loop would then poll indefinitely — mirror the server's
  // own 15-minute cap here so an abandoned login visibly gives up instead
  // of running silently in the background for the rest of the page's life.
  var DEVICE_POLL_TIMEOUT_MS = 15 * 60 * 1000;

  function startDevicePoll(row, loginId) {
    stopDevicePoll();
    var startedAt = Date.now();
    devicePollTimer = setInterval(function () {
      if (Date.now() - startedAt > DEVICE_POLL_TIMEOUT_MS) {
        stopDevicePoll();
        byId("device-login-status").textContent = "";
        var timeoutErr = byId("err_device_login");
        timeoutErr.textContent = "Время ожидания входа истекло. Нажмите «Войти по аккаунту» ещё раз.";
        timeoutErr.hidden = false;
        var timeoutBtn = byId("device-login-start");
        timeoutBtn.disabled = false;
        timeoutBtn.textContent = "Войти по аккаунту";
        return;
      }
      jsonFetch("/api/device/status")
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data.login_id && data.login_id !== loginId) return;
          if (data.provider && data.provider !== row.name) return;
          if (data.state === "ok") {
            stopDevicePoll();
            showDeviceLoginSuccess("✓ Вход выполнен");
            loadDeviceModels(row);
          } else if (data.state === "error") {
            stopDevicePoll();
            byId("device-login-status").textContent = "";
            var err = byId("err_device_login");
            err.textContent = data.error || "Не удалось войти. Попробуйте ещё раз.";
            err.hidden = false;
            var btn = byId("device-login-start");
            btn.disabled = false;
            btn.textContent = "Войти по аккаунту";
          }
          // "pending" (or any other state) — keep polling silently.
        })
        .catch(function (err) {
          // Second instance of the same "poll never stops" defect: a lost
          // access error already showed the generic banner (jsonFetch
          // threw after handling it) — the interval itself must stop too,
          // there is nothing left worth polling for.
          if (isAuthError(err) || isRateLimitedError(err)) stopDevicePoll();
        });
    }, 3000);
  }

  byId("device-login-start").addEventListener("click", function () {
    var row = state.chosenProviderRow;
    if (!row) return;
    var btn = byId("device-login-start");
    btn.disabled = true;
    var err = byId("err_device_login");
    err.hidden = true;
    err.textContent = "";
    jsonFetch("/api/device/start", {
      method: "POST",
      // Read live, at request time — the device-code exchange talks
      // straight to the provider's own OAuth endpoint (auth.openai.com,
      // MiniMax's portal), which a RU-hosted server can need the proxy
      // field for just as much as an API-key provider's catalog call.
      body: JSON.stringify({ provider: row.name, proxy: byId("proxy").value }),
    })
      .then(function (res) {
        if (res.status !== 200) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            throw new Error(data.error || "Не удалось начать вход. Попробуйте ещё раз.");
          });
        }
        return res.json();
      })
      .then(function (data) {
        setHidden("device-login-info", false);
        setVerificationLink(data.verification_url);
        byId("device-login-code").textContent = data.user_code;
        byId("device-login-status").textContent = "Ожидаем подтверждение…";
        btn.textContent = "Входим…";
        startDevicePoll(row, data.login_id);
      })
      .catch(function (thrown) {
        if (isAuthError(thrown) || isRateLimitedError(thrown)) return;
        btn.disabled = false;
        btn.textContent = "Войти по аккаунту";
        err.textContent = String((thrown && thrown.message) || "Не удалось начать вход. Попробуйте ещё раз.");
        err.hidden = false;
      });
  });

  // ---- Return-mode prefill (non-secret by value, secrets by neutral placeholder only) -----

  function applySecretPlaceholderEl(input, secretState) {
    // Neutral by design (owner feedback): never echo any fragment of the
    // real saved value into the DOM. A returning client only ever learns
    // "something is saved", never a hint of what it is — the server-side
    // redacted preview that used to sit in this placeholder is unused now.
    //
    // Owner feedback (this pass): the previous placeholder spelled out the
    // full explanation and overran the input, rendering visibly clipped
    // mid-word. The placeholder itself now only needs to fit; the rest of
    // the sentence moves to a caption right under the field
    // (byId(id + "_saved_note") — appendSecretField() creates one for
    // every dynamic secret field, and the two static secret fields carry
    // the matching element in _MAIN_FORM_HTML itself).
    if (input && secretState && secretState.is_set) {
      input.placeholder = "Сохранён — не меняем";
      var note = byId(input.id + "_saved_note");
      if (note) {
        note.textContent = "Сохранён — оставьте поле пустым, чтобы не менять сохранённое значение.";
        note.hidden = false;
      }
    }
  }

  // For the two static, always-in-document fields (telegram_token,
  // provider_api_key) — byId() resolves them fine, they're part of the
  // page's own static HTML. Every secret field built dynamically inside
  // a per-category `.sub-settings` container (browser, search, voice, STT,
  // image generation, video generation) goes through applySecretPlaceholderEl()
  // with the element reference appendSecretField() just returned instead
  // — a plain habit carried over from the pre-redesign code, not a
  // requirement here (unlike that older code's detached-fragment case,
  // `.sub-settings` is already attached to the live document by the time
  // a field is added to it — see appendSubSettings()).
  function applySecretPlaceholder(inputId, secretState) {
    applySecretPlaceholderEl(byId(inputId), secretState);
  }

  // ---- Часовой пояс (спека 11) ---------------------------------------
  //
  // Список приходит из /api/form (`timezones`) — все пояса, какие знает
  // рантайм, разложенные по группам с Россией первой. Второго списка
  // внутри страницы нет намеренно: разошедшиеся копии одного каталога уже
  // стоили этой ветке отдельного разбора.

  function timezoneOptionLabel(zone) {
    return zone.label + " (" + zone.offset + ")";
  }

  function timezoneMatches(zone, needle) {
    if (!needle) return true;
    var hay = (zone.label + " " + zone.name + " " + zone.offset).toLowerCase();
    return hay.indexOf(needle) !== -1;
  }

  // Перерисовать список, необязательно отфильтровав его.
  //
  // Выбранное значение переживает любой фильтр: клиент, набравший что-то в
  // поиске и передумавший искать, иначе отправил бы форму с пустым полем и
  // не понял бы, куда делся его ответ. Если фильтр прячет выбранное — оно
  // всё равно остаётся в списке отдельной группой.
  function renderTimezone(filter) {
    var sel = byId("timezone");
    if (!sel) return;
    var keep = sel.value || "";
    var needle = (filter || "").trim().toLowerCase();
    sel.innerHTML = "";

    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "— выберите —";
    sel.appendChild(placeholder);

    var keepRendered = false;
    var groups = state.timezones || [];
    for (var i = 0; i < groups.length; i++) {
      var group = groups[i];
      var zones = group.zones || [];
      var optgroup = document.createElement("optgroup");
      optgroup.label = group.title;
      var shown = 0;
      for (var j = 0; j < zones.length; j++) {
        var zone = zones[j];
        if (!timezoneMatches(zone, needle)) continue;
        var option = document.createElement("option");
        option.value = zone.name;
        option.textContent = timezoneOptionLabel(zone);
        optgroup.appendChild(option);
        shown++;
        if (zone.name === keep) keepRendered = true;
      }
      if (shown) sel.appendChild(optgroup);
    }

    if (keep && !keepRendered) {
      var kept = document.createElement("optgroup");
      kept.label = "Выбрано";
      var keptOption = document.createElement("option");
      keptOption.value = keep;
      keptOption.textContent = keep;
      kept.appendChild(keptOption);
      sel.appendChild(kept);
    }

    sel.value = keep;
  }

  function pluralRu(n, one, few, many) {
    var mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 14) return many;
    var mod10 = n % 10;
    if (mod10 === 1) return one;
    if (mod10 >= 2 && mod10 <= 4) return few;
    return many;
  }

  // Текст предупреждения о смене уже отвеченного пояса.
  //
  // Три исхода счётчика задач различаются НА ВИДУ, потому что «задач нет»
  // и «проверить не удалось» — разные утверждения. Выдать второе за первое
  // значило бы промолчать о задачах, которые на самом деле есть.
  function timezoneWarningText(jobs) {
    if (jobs === null || jobs === undefined) {
      return (
        "Проверить, есть ли у вас задачи по расписанию, не удалось. " +
        "Если они есть — они останутся на прежнем времени: смена пояса их не переносит. " +
        "По новому поясу будут работать те, что вы заведёте дальше."
      );
    }
    var word = pluralRu(jobs, "задача", "задачи", "задач");
    var saved = pluralRu(jobs, "Она была сохранена", "Они были сохранены", "Они были сохранены");
    var stay = pluralRu(jobs, "останется", "останутся", "останутся");
    return (
      "У вас заведено " + jobs + " " + word + " по расписанию. " +
      saved + " по прежнему поясу и " + stay + " на своём времени — " +
      "смена пояса их не переносит. По новому поясу будут работать те, " +
      "что вы заведёте дальше. Старые проще пересоздать: попросите агента в Telegram."
    );
  }

  // Предупреждение показывается ровно тогда, когда клиенту есть что
  // потерять: пояс уже был отвечен ИЛИ задачи уже заведены под системным
  // временем, и выбор отличается от нынешнего. На первой установке (задач
  // нет) его не видно вовсе.
  function updateTimezoneWarning() {
    var warning = byId("timezone-warning");
    if (!warning) return;
    var chosen = (byId("timezone").value || "").trim();
    var saved = ((state.current || {}).timezone || "").trim();
    var jobs = state.cronJobs;
    var somethingToLose = jobs === null || jobs === undefined || (typeof jobs === "number" && jobs > 0);
    var show = !!chosen && chosen !== saved && somethingToLose;
    if (!show) {
      // Снимаем и отметку: иначе клиент, вернувшийся к прежнему поясу и
      // снова передумавший, прошёл бы дальше с галочкой, которой не ставил
      // для ЭТОГО изменения.
      byId("timezone_ack").checked = false;
      setHidden("timezone-warning", true);
      return;
    }
    byId("timezone-warning-text").textContent = timezoneWarningText(jobs);
    setHidden("timezone-warning", false);
  }

  function setTimezoneError(message) {
    var el = byId("err_timezone");
    el.textContent = message;
    el.hidden = false;
  }

  // Ворота шага. Пустое поле — не выбор: пустой ключ `timezone` означает
  // системное время машины, а она стоит у хостера.
  function timezoneStepOk() {
    var chosen = (byId("timezone").value || "").trim();
    if (!chosen) {
      setTimezoneError("Выберите часовой пояс — от него зависит время напоминаний.");
      return false;
    }
    if (!byId("timezone-warning").hidden && !byId("timezone_ack").checked) {
      setTimezoneError("Отметьте «Понимаю, меняем» — уже заведённые задачи останутся на прежнем времени.");
      return false;
    }
    setHidden("err_timezone", true);
    return true;
  }

  function renderPrefill() {
    var cur = state.current || {};
    applySecretPlaceholder("telegram_token", cur.telegram_token);
    byId("allowed_users").value = cur.allowed_users || "";
    byId("proxy").value = cur.proxy || "";
    var providerCur = cur.provider || {};
    applySecretPlaceholder("provider_api_key", providerCur.api_key);
    // Часовой пояс: сохранённый ответ возвращается выбранным, пустой —
    // остаётся пустым. Преселекта нет по решению владельца: мастер не
    // отвечает за клиента (то же правило, что и в блоке провайдера).
    byId("timezone").value = cur.timezone || "";
    updateTimezoneWarning();
  }

  // ---- Advanced: search / tool blocks / fallback model ----------------
  //
  // No expand/collapse toggle any more — step 5 ("Дополнительно") is
  // always fully rendered and visible, first run or return visit alike
  // (see enterStepsMode()).

  // Return-mode preselect (review 9d #2): a saved value already means a
  // real choice was made — defaulting the <select> back to the catalog's
  // "recommended" option regardless would silently re-submit (and
  // overwrite) whatever the client actually has configured the moment
  // they touch "Готово", even before they've edited anything on step 5.
  // Preselect from the saved value when it names one of the rendered
  // options; fall back to the recommended option only when nothing is
  // saved (first run) or the saved value doesn't match any rendered
  // option (an out-of-wizard-scope backend the client set by hand —
  // safest to fall back rather than silently land on the first option).
  function pickPreselected(options, savedValue) {
    // {value, outOfCatalog}. Round 2 fix: a saved value that fails to
    // match any rendered option must NOT silently fall back to
    // "recommended" — that would submit a DIFFERENT value than what is
    // actually configured (e.g. `web.search_backend: searxng` while the
    // local SearXNG instance isn't reachable right now — tools_view.py's
    // self-hosted liveness rule hides that row until it answers again).
    // apply_settings() treats an empty string as a
    // no-op for every one of these fields, so "" is the one value that
    // leaves a manually-configured backend untouched. Callers render a
    // visible note (see appendOutOfCatalogNote below) whenever
    // outOfCatalog is true so the client can see why nothing lit up.
    if (savedValue) {
      for (var i = 0; i < options.length; i++) {
        if (options[i].value === savedValue) return { value: savedValue, outOfCatalog: false };
      }
      return { value: "", outOfCatalog: true };
    }
    for (var j = 0; j < options.length; j++) {
      if (options[j].recommended) return { value: options[j].value, outOfCatalog: false };
    }
    return { value: "", outOfCatalog: false };
  }

  function appendOutOfCatalogNote(container, savedValue) {
    var note = document.createElement("div");
    note.className = "tool-row out-of-catalog-note";
    note.textContent = "настроено вручную: " + savedValue;
    container.appendChild(note);
  }

  // Owner feedback (redesign): a mutually exclusive choice — browser
  // backend, search backend, voice mode, STT provider, image generation,
  // video generation — is now always exactly one <select>, never a radio
  // group. The chosen
  // option's own settings render BELOW the select, in a `.sub-settings`
  // container that is rebuilt on every change — never before a choice is
  // made (so e.g. Camofox's install button + server-address field never
  // show while Chromium, the default, is selected).

  function clearContainer(el) {
    el.innerHTML = "";
  }

  // The address of a local service the client should never be asked for:
  // tools_view.py stamps `auto_default` on any *_URL env var whose value
  // is already known (Camofox's fixed port, a SearXNG/Firecrawl instance
  // that answered a liveness probe). Its presence IS the instruction not
  // to render an input for it.
  function autoDefaultFor(row, envKey) {
    var envs = (row && row.env_vars) || [];
    for (var i = 0; i < envs.length; i++) {
      if (envs[i] && envs[i].key === envKey) return envs[i].auto_default || "";
    }
    return "";
  }

  function appendMutedNote(container, text) {
    var note = document.createElement("div");
    note.className = "muted-note";
    note.textContent = text;
    container.appendChild(note);
  }

  function buildSelectRow(container, id, labelText, rightHint) {
    var row = document.createElement("div");
    // `no-label`: the right-hand hint is normally pushed down by the
    // height of a visible label so it lines up with the control beside
    // it. This row's label is sr-only (see below), so that push would
    // indent the hint away from nothing and leave the two columns
    // visibly out of line (owner feedback after the live run).
    row.className = "field-row no-label";
    var field = document.createElement("div");
    field.className = "field";
    // Owner feedback: every caller here builds the settings that live
    // INSIDE an already-titled buildCollapsibleRow() — a second, visible
    // "Браузер"/"Поиск и извлечение страниц"/etc. label right below the
    // row's own header read as two of the same category stacked. sr-only
    // keeps the for/id association (and the accessible name) without
    // repeating the heading on screen.
    var label = document.createElement("label");
    label.className = "sr-only";
    label.setAttribute("for", id);
    label.textContent = labelText;
    var select = document.createElement("select");
    select.id = id;
    field.appendChild(label);
    field.appendChild(select);
    row.appendChild(field);
    var hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = rightHint || "";
    row.appendChild(hint);
    container.appendChild(row);
    return select;
  }

  function addSelectOption(select, value, text) {
    var opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    select.appendChild(opt);
  }

  function placeholderOption() {
    var opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "настроено вручную — см. ниже";
    return opt;
  }

  function appendSubSettings(container) {
    var settings = document.createElement("div");
    settings.className = "sub-settings";
    container.appendChild(settings);
    return settings;
  }

  // Renders the settings container once and wires it to re-render on every
  // change — the one place progressive disclosure actually happens: a
  // fresh select().onchange assignment each renderAdvanced() pass, never
  // addEventListener (idempotency reason identical to the provider
  // <select> above — renderAdvanced() re-runs whenever /api/form reloads).
  function wireSelect(select, renderSettingsFn) {
    renderSettingsFn();
    select.onchange = renderSettingsFn;
  }

  // Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"): there
  // is no "Установить" button any more — a tool this row names installs on
  // its own, unattended, as part of the final "Готово" step's own install
  // stage (see #stage-install / pendingToolInstallNames() below), same as
  // the console wizard has always worked (tools_config.py runs a chosen
  // provider's post_setup hook right after the provider is picked, no
  // separate manual step). This just reports current status: an
  // already-installed row gets the same "✓ установлено" pill it always
  // had; a not-yet-installed row with an install hook gets an honest note
  // that picking it means an install attempt at submit time — including,
  // when install_blocked_reason_ru says so, that the attempt is likely to
  // fail on this machine right now (see tools_view.py's
  // _row_install_blocked_reason).
  function appendInstallStatus(container, row) {
    if (row.installed === true) {
      var ok = document.createElement("span");
      ok.className = "status-ok";
      ok.textContent = "✓ установлено";
      container.appendChild(ok);
      return;
    }
    if (!row.post_setup) return;
    var text = "Установится автоматически на последнем шаге, при запуске агента.";
    if (row.install_blocked && row.install_blocked_reason_ru) {
      text = "Попробует установиться на последнем шаге, но, скорее всего, не получится: " + row.install_blocked_reason_ru + ".";
    }
    appendMutedNote(container, text);
  }

  function extractEnvField(row, keySuffix) {
    var vars = row.env_vars || [];
    for (var i = 0; i < vars.length; i++) {
      if (vars[i].key && vars[i].key.indexOf(keySuffix) !== -1) return vars[i];
    }
    return null;
  }

  function toolBlockFor(category) {
    for (var i = 0; i < state.tools.length; i++) {
      if (state.tools[i].category === category) return state.tools[i];
    }
    return null;
  }

  // ---- Generic provider-select row settings (owner ruling 2026-08-20):
  // shared by "Генерация изображений"/"Генерация видео"/the non-Edge part
  // of "Голосовые ответы"/"Распознавание речи" — every row in these
  // categories carries at most one env var (enforced server-side, see
  // tools_view.py's test_every_provider_select_row_has_at_most_one_env_var),
  // so one field + one install control covers all of them. The field's id
  // is namespaced per CATEGORY ("tool_env_value__" + category), never a
  // bare "tool_env_value" — renderVoiceBlock/renderSTTBlock/
  // renderImageGenBlock/renderVideoGenBlock all render into the SAME page
  // at once (inside #advanced's row bodies), so a shared literal id would
  // collide across categories. toolEnvPayload() (below) reads this same
  // id convention back out at submit time. -------------------------------

  function renderProviderRowSettings(settings, row, category, current, opts) {
    opts = opts || {};
    var addedSomething = false;
    var envs = (row && row.env_vars) || [];
    if (envs.length) {
      var env = envs[0];
      var curEnv = ((current && current.tool_env) || {})[env.key] || null;
      var isUrlField = /_URL$/.test(env.key || "");
      var fieldId = "tool_env_value__" + category;
      // Finding 9: env.prompt is catalog English ("OpenAI API key",
      // "Camofox server URL", …) — tools_view.py stamps a Russian
      // translation onto env.prompt_ru by key (RU_ENV_PROMPTS); a key
      // missing from that dict falls back to a generic Russian label,
      // never to the raw English string.
      var promptText = (opts.envLabel) || env.prompt_ru || (isUrlField ? "Адрес" : "Ключ");
      var input;
      if (isUrlField) {
        input = appendTextField(settings, fieldId, promptText, opts.hint || "", env.default || "");
        input.value = (curEnv && curEnv.url) || env.default || "";
      } else {
        input = appendSecretField(settings, fieldId, promptText, opts.hint || "");
        if (curEnv) applySecretPlaceholderEl(input, curEnv);
      }
      input.setAttribute("data-env-key", env.key);
      addedSomething = true;
    }
    var controlRow = document.createElement("div");
    controlRow.className = "tool-row";
    appendInstallStatus(controlRow, row);
    if (controlRow.childNodes.length) {
      settings.appendChild(controlRow);
      addedSomething = true;
    }
    if (!addedSomething) {
      appendMutedNote(settings, opts.emptyHint || "Ничего настраивать не нужно.");
    }
  }

  // ---- Свёрнутые строки категорий (план B4, спека 7 «Шаг 5.
  // Дополнительно»): владелец — «опять лист целый», восемь развёрнутых
  // блоков сразу требовали прокрутки на шаге, который целиком
  // необязателен. Каждая из шести категорий теперь ОДНА строка с
  // текущим состоянием и шевроном; раскрытие показывает тот же select +
  // настройки, что рендерился раньше, внутри .row-body. Разметка/классы
  // — из эталонного макета (экран 5).
  //
  // A real <button> (not a bare div+onclick) — native Enter/Space
  // handling and focus semantics come for free, no manual keydown
  // listener needed (спека требование: доступность с клавиатуры).
  //
  // While the row is OPEN, the state column reads "Настраиваем" instead
  // of the live select value — the value is already visible right there
  // in the open body, so echoing it in the header too would just be
  // noise; the header goes back to reflecting the select's CURRENT value
  // the moment the row closes (computeState() is invoked fresh on every
  // close, reading the same <select> the settings panel/buildPayload()
  // itself reads — see selectStateText() below — so header and body can
  // never disagree about what is actually selected).
  function buildCollapsibleRow(container, title, computeState) {
    var row = document.createElement("button");
    row.type = "button";
    row.className = "row";
    row.setAttribute("aria-expanded", "false");
    var label = document.createElement("b");
    label.textContent = title;
    var stateSpan = document.createElement("span");
    stateSpan.className = "state";
    var chev = document.createElement("span");
    chev.className = "chev";
    chev.setAttribute("aria-hidden", "true");
    chev.textContent = "›";
    row.appendChild(label);
    row.appendChild(stateSpan);
    row.appendChild(chev);

    var body = document.createElement("div");
    body.className = "row-body";
    body.hidden = true;

    function refreshState() {
      stateSpan.textContent = row.classList.contains("open") ? "Настраиваем" : computeState();
    }

    // Owner feedback (this pass): closes THIS row without touching any
    // other — used both by the accordion click handler below (to fold
    // every OTHER row before opening this one) and by
    // closeAllCollapsibleRows() (to fold this one when the client leaves
    // the step). Safe to call on an already-closed row (no-op past the
    // first two lines).
    function close() {
      if (!row.classList.contains("open")) return;
      row.classList.remove("open");
      row.setAttribute("aria-expanded", "false");
      chev.textContent = "›";
      body.hidden = true;
      refreshState();
    }

    row.addEventListener("click", function () {
      var wasOpen = row.classList.contains("open");
      // Owner feedback: at most one category open at a time — opening
      // "Поиск" while "Браузер" was still expanded left both bodies
      // stacked underneath each other. Folding every other row here,
      // before this row's own toggle, gives that accordion behavior
      // without a separate "which row is open" flag to keep in sync.
      state.collapsibleRows.forEach(function (entry) {
        if (entry.row !== row) entry.close();
      });
      var open = !wasOpen;
      row.classList.toggle("open", open);
      row.setAttribute("aria-expanded", open ? "true" : "false");
      chev.textContent = open ? "⌄" : "›";
      body.hidden = !open;
      refreshState();
    });

    container.appendChild(row);
    container.appendChild(body);

    state.collapsibleRows.push({ row: row, close: close });
    return { body: body, refreshState: refreshState };
  }

  // Owner feedback: leaving step 5 (or arriving at it fresh) must never
  // carry over a still-open category row from a previous visit — "если
  // юзер выбирает браузер, а потом кликает дальше... наслоение идёт".
  // Called from goToStep() on every navigation (idempotent — a no-op when
  // nothing is open).
  function closeAllCollapsibleRows() {
    state.collapsibleRows.forEach(function (entry) { entry.close(); });
  }

  // Reads the collapsed row's "текущее состояние" straight off the SAME
  // <select> the settings panel and buildPayload() already read — the
  // two can never diverge because there is only one source (спека
  // требование 2). Trailing catalog decoration ("(рекомендуется)", "(по
  // умолчанию)", …) is stripped so the row reads as a plain value, not a
  // repeat of the option's own hint text. An empty <select> (the catalog
  // never arrived for this category) falls back to an honest neutral
  // line, never a made-up value.
  function selectStateText(select, unavailableText) {
    if (!select || !select.options.length) return unavailableText;
    var opt = select.options[select.selectedIndex];
    if (!opt) return unavailableText;
    return (opt.textContent || "").replace(/\\s*\\([^)]*\\)\\s*$/, "").trim();
  }

  // ---- Браузер: one <select>, Camofox's own switch (CAMOFOX_URL, not a
  // browser.backend value — see apply.py) folded in as a sentinel option
  // whose settings (status note + server-address field) appear only
  // once it is the chosen value. ----------------------------------------

  var CAMOFOX_VALUE = "camofox";
  var CAMOFOX_ENV_VAR = "CAMOFOX_URL";
  var BROWSER_LABELS = {
    off: "Chromium (встроенный, по умолчанию)",
    "browser-use": "Browser Use",
  };

  // Owner ruling 2026-08-24, item 5 ("Установка инструментов — кнопки
  // нет"): a row that is beta and/or whose install is doomed on THIS
  // machine right now (see tools_view.py's rows[].beta/install_blocked)
  // must say so IN the picker, before the client selects it — installing
  // now happens unattended, on the final "Готово" step, minutes after the
  // client leaves this select. Appended to the option's own label text
  // (not hidden in a sub-panel the client only sees after picking).
  function appendRowCaveats(label, row) {
    var text = label;
    if (row && row.beta) text += " (бета)";
    if (row && row.install_blocked && row.install_blocked_reason_ru) {
      text += " — " + row.install_blocked_reason_ru;
    }
    return text;
  }

  function renderBrowserBlock() {
    var container = byId("advanced-browser");
    clearContainer(container);
    var block = toolBlockFor("browser");

    var rows = (block && block.rows) || [];
    var options = [];
    var rowByValue = {};
    rows.forEach(function (row) {
      if (row.backend_key) {
        options.push({
          value: row.backend_key,
          label: appendRowCaveats(BROWSER_LABELS[row.backend_key] || row.name, row),
          recommended: !!row.recommended,
        });
        rowByValue[row.backend_key] = row;
      } else if (extractEnvField(row, "CAMOFOX_URL")) {
        options.push({
          value: CAMOFOX_VALUE,
          label: appendRowCaveats("Camofox — анти-детект", row),
          recommended: !!row.recommended,
        });
        rowByValue[CAMOFOX_VALUE] = row;
      }
    });

    var current = state.current || {};
    // Priority fix (review): an explicit NON-"off" backend (e.g.
    // "browser-use") is a real, later choice and must always win over a
    // stale saved CAMOFOX_URL from an earlier session — apply_settings()
    // never clears CAMOFOX_URL on its own (see apply.py), so a client who
    // tried Camofox once and later switched to Browser Use would
    // otherwise see Camofox silently preselected here, and an untouched
    // resubmit would downgrade browser.backend back to "off" underneath
    // them. Only when the saved backend is "off" (or unset) does a saved
    // CAMOFOX_URL mean "Camofox is the thing actually active" — "off" is
    // exactly the value apply_settings() writes alongside camofox_url
    // when Camofox is chosen (see buildPayload below), so this is a
    // real signal, not a guess.
    var backend = current.browser_backend || "";
    var savedValue = (backend && backend !== "off")
      ? backend
      : (current.camofox_url ? CAMOFOX_VALUE : backend);
    var picked = pickPreselected(options, savedValue);

    // Finding 16: title_ru comes from /api/form (tools_view.TITLES_RU) —
    // the hardcoded string is only a fallback for a block the server
    // didn't send at all, never a second, independently-maintained copy
    // of the heading text.
    var rowUI = buildCollapsibleRow(container, (block && block.title_ru) || "Браузер", function () {
      if (picked.outOfCatalog && select.value === "") return "настроено вручную";
      return selectStateText(select, "каталог недоступен");
    });

    var select = buildSelectRow(rowUI.body, "browser_choice", "Браузер", "Чем агент открывает и читает веб-страницы.");
    options.forEach(function (o) { addSelectOption(select, o.value, o.label); });
    if (picked.outOfCatalog) select.insertBefore(placeholderOption(), select.firstChild);
    select.value = picked.value;

    var settings = appendSubSettings(rowUI.body);

    function renderSettings() {
      clearContainer(settings);
      var value = select.value;
      if (picked.outOfCatalog && value === "") {
        appendOutOfCatalogNote(settings, current.browser_backend);
        return;
      }
      if (value === "off") {
        appendMutedNote(settings, "Работает из коробки — ничего настраивать не нужно.");
        return;
      }
      var row = rowByValue[value];
      if (!row) return;
      var controlRow = document.createElement("div");
      controlRow.className = "tool-row";
      appendInstallStatus(controlRow, row);
      if (controlRow.childNodes.length) settings.appendChild(controlRow);
      // Owner ruling (live-VM review): the address itself is never shown,
      // not even as a note — it means nothing to a non-technical client
      // and Camofox just works once picked. camofoxUrlPayload() (below)
      // still reads the catalog's auto_default and writes it to
      // CAMOFOX_URL on submit; appendInstallStatus() above and the
      // "(бета)" caveat already on the option's own label (see
      // appendRowCaveats) are the only feedback this choice needs.
    }
    wireSelect(select, renderSettings);
    rowUI.refreshState();
  }

  // ---- Поиск в интернете: a regular tools block ("web" in /api/form's
  // `tools`), same generic shape as browser/tts/image_gen — every live
  // search-backend row renders (ddgs, brave-free, exa, firecrawl,
  // parallel, tavily, xai, searxng), not just a hardcoded two-backend
  // subset. A "self-hosted" row (SearXNG, Firecrawl Self-Hosted) only
  // appears at all when tools_view.py's liveness probe found something
  // listening — see that module's docstring — in which case its env_vars
  // entry already carries a `default` (the address that answered) for
  // this code to prefill. ddgs comes back `recommended` structurally (its
  // badge text never says the word).
  //
  // Owner question (verbatim): "поиск и извлечение страниц это разные
  // тулзы или нет?" — yes: `web_search`/`web_extract` are two separate
  // model tools with separate settings, and several search backends
  // (DuckDuckGo, Brave, SearXNG, Grok) cannot read a page's content at
  // all. This block only ever writes `search_backend`/`search_env` (the
  // "finds pages" capability) — see renderExtractBlock() right below for
  // the SEPARATE "reads a page's text" capability, `extract_backend`/
  // `extract_env`. Splitting the two blocks (not just the two payload
  // fields) is the actual fix for the client seeing them mashed into one
  // picker with an English error when a search-only backend was asked to
  // extract.

  function renderSearchBlock() {
    var container = byId("advanced-search");
    clearContainer(container);
    var block = toolBlockFor("web");

    var current = state.current || {};
    var rows = (block && block.rows) || [];
    var options = rows.map(function (row) {
      return {
        value: row.web_backend,
        label: row.name + (row.recommended ? " (рекомендуется)" : ""),
        recommended: !!row.recommended,
      };
    });
    var rowByValue = {};
    // Finding 11 (review 2026-08-26, owner-approved fix): same "first row
    // with a given key wins" rule Finding 17 already established for
    // tts/video_gen's own provider_key collisions, applied here for
    // web_backend. Two DIFFERENT rows can legitimately share one
    // web_backend value — "Firecrawl" (cloud, FIRECRAWL_API_KEY) and
    // "Firecrawl Self-Hosted" (FIRECRAWL_API_URL) both resolve to
    // web.search_backend: "firecrawl" — and an unguarded overwrite here
    // let the LAST one win for the settings panel while
    // searchEnvPayload() below (unchanged: `rows.filter(...)[0]`, always
    // FIRST) submitted the FIRST row's env var — a self-hosted instance
    // URL silently landing in FIRECRAWL_API_KEY. First one in wins for
    // both now, consistently, matching Finding 17's own reasoning.
    rows.forEach(function (row) {
      var key = row.web_backend;
      if (rowByValue.hasOwnProperty(key)) return;
      rowByValue[key] = row;
    });
    var picked = pickPreselected(options, current.search_backend);

    var rowUI = buildCollapsibleRow(container, (block && block.title_ru) || "Поиск в интернете", function () {
      if (picked.outOfCatalog && select.value === "") return "настроено вручную";
      return selectStateText(select, "каталог недоступен");
    });

    var select = buildSelectRow(rowUI.body, "search_choice", "Поиск в интернете", "Находит страницы и ссылки в интернете по запросу.");
    options.forEach(function (o) { addSelectOption(select, o.value, o.label); });
    if (picked.outOfCatalog) select.insertBefore(placeholderOption(), select.firstChild);
    select.value = picked.value;

    var settings = appendSubSettings(rowUI.body);

    function renderSettings() {
      clearContainer(settings);
      var value = select.value;
      if (picked.outOfCatalog && value === "") {
        appendOutOfCatalogNote(settings, current.search_backend);
        return;
      }
      var row = rowByValue[value];
      if (!row) {
        appendMutedNote(settings, "Работает без ключа — ничего настраивать не нужно.");
        return;
      }
      var envs = row.env_vars || [];
      var addedSomething = false;
      if (envs.length) {
        // Only the first env var of the chosen row is ever submittable —
        // apply.py's search_env mechanism writes exactly one key/value
        // pair (matches _current_search_env's own "first env var" read).
        var env = envs[0];
        var curEnv = current.search_env || {};
        var isSameField = curEnv.env_var === env.key;
        var isUrlField = /_URL$/.test(env.key || "");
        var input;
        if (isUrlField) {
          // A local address, not a credential — same plain-text
          // convention CAMOFOX_URL/HASS_URL already use. env.default is
          // the address tools_view.py's liveness probe just found
          // listening (self-hosted rows only render at all when it
          // answers) — the already-saved value (if this row is already
          // active) wins over that probe default when both exist.
          input = appendTextField(settings, "search_env_value", env.prompt_ru || "Адрес", "", env.default || "");
          input.value = (isSameField && curEnv.url) || env.default || "";
        } else {
          input = appendSecretField(settings, "search_env_value", env.prompt_ru || "Ключ", "Нужен, только если выбран этот источник.");
          if (isSameField) applySecretPlaceholderEl(input, curEnv);
        }
        addedSomething = true;
      } else if (row.post_setup === "xai_grok") {
        // Polish (owner review, 2026-08-20): this row carries no env_vars
        // AND its post_setup hook (tools_config._run_post_setup's
        // "xai_grok" branch) drives interactive CLI prompts over stdin —
        // meaningless, and effectively a dead "Установить" button, from a
        // headless web request. That leaves an empty settings panel with
        // nothing actionable in it, same failure shape as every other
        // "chose it, nothing happened" gap this pass is fixing. The row's
        // own readiness (tools_config.provider_readiness_status's
        // "xai_grok" branch) already falls back to a plain XAI_API_KEY —
        // exactly the key step 4's provider block writes when xAI is
        // chosen as the model provider — so point the client there
        // instead of offering the broken button.
        appendMutedNote(
          settings,
          "Использует ключ провайдера xAI (Grok) — настройте провайдера на шаге 4."
        );
        addedSomething = true;
      }
      if (row.post_setup !== "xai_grok") {
        var controlRow = document.createElement("div");
        controlRow.className = "tool-row";
        appendInstallStatus(controlRow, row);
        if (controlRow.childNodes.length) {
          settings.appendChild(controlRow);
          addedSomething = true;
        }
      }
      if (!addedSomething) {
        appendMutedNote(settings, "Работает без ключа — ничего настраивать не нужно.");
      }
    }
    wireSelect(select, renderSettings);
    rowUI.refreshState();
  }

  // ---- Чтение страниц: the SEPARATE "web_extract" tools block —
  // server-derived from the SAME "web" catalog rows (tools_view.py
  // filters to only the ones that can actually open a link and pull its
  // text: firecrawl, tavily, exa, parallel), not a client-side guess.
  // Shape mirrors "Генерация изображений"/"Генерация видео" (an explicit
  // "off" option first, nothing preselected unless a row is actually
  // `recommended`) rather than "Поиск в интернете"'s — unlike search,
  // no extract row is a always-on no-key default, so "not configured yet"
  // is a real, common, and entirely legitimate state: the agent simply
  // won't be able to open links and read their text, search keeps working
  // regardless. Nothing here should read as broken or block "Далее".

  function renderExtractBlock() {
    var container = byId("advanced-extract");
    clearContainer(container);
    var block = toolBlockFor("web_extract");

    var current = state.current || {};
    var rows = (block && block.rows) || [];
    var options = rows.map(function (row) {
      return {
        value: row.web_backend,
        label: row.name + (row.recommended ? " (рекомендуется)" : ""),
        recommended: !!row.recommended,
      };
    });
    var rowByValue = {};
    // Finding 11 (review 2026-08-26, owner-approved fix): same fix/reason
    // as renderSearchBlock()'s own rowByValue above — "Firecrawl
    // Self-Hosted" is extract-capable too, so this block hits the exact
    // same "two rows, one web_backend" collision.
    rows.forEach(function (row) {
      var key = row.web_backend;
      if (rowByValue.hasOwnProperty(key)) return;
      rowByValue[key] = row;
    });

    var savedValue = current.extract_backend || "";
    // Same "off + rows" shape as image_gen/video_gen above: an empty
    // saved value means "never configured", which defaults to a plain
    // "off" (never a silent guess at a paid backend) instead of the
    // recommended-row fallback the helper below normally falls back to.
    var picked = savedValue ? pickPreselected(options, savedValue) : { value: "off", outOfCatalog: false };

    var rowUI = buildCollapsibleRow(container, (block && block.title_ru) || "Чтение страниц", function () {
      if (picked.outOfCatalog && select.value === "") return "настроено вручную";
      return selectStateText(select, "каталог недоступен");
    });

    var select = buildSelectRow(rowUI.body, "extract_choice", "Чтение страниц", "Открывает конкретную ссылку и достаёт из неё текст.");
    addSelectOption(select, "off", "Выключено (по умолчанию)");
    options.forEach(function (o) { addSelectOption(select, o.value, o.label); });
    if (picked.outOfCatalog) select.insertBefore(placeholderOption(), select.firstChild);
    select.value = picked.value;

    var settings = appendSubSettings(rowUI.body);

    function renderSettings() {
      clearContainer(settings);
      var value = select.value;
      if (picked.outOfCatalog && value === "") {
        appendOutOfCatalogNote(settings, current.extract_backend);
        return;
      }
      if (value === "off") {
        appendMutedNote(settings, "Агент не будет открывать ссылки и читать из них текст — поиск при этом продолжит работать.");
        return;
      }
      var row = rowByValue[value];
      if (!row) return;
      var envs = row.env_vars || [];
      var addedSomething = false;
      if (envs.length) {
        // Same "first env var of the row is the only submittable one"
        // contract as search_env above (matches extract_env's own
        // "first env var" read on the server).
        var env = envs[0];
        var curEnv = current.extract_env || {};
        var isSameField = curEnv.env_var === env.key;
        var isUrlField = /_URL$/.test(env.key || "");
        var input;
        if (isUrlField) {
          input = appendTextField(settings, "extract_env_value", env.prompt_ru || "Адрес", "", env.default || "");
          input.value = (isSameField && curEnv.url) || env.default || "";
        } else {
          input = appendSecretField(settings, "extract_env_value", env.prompt_ru || "Ключ", "Нужен, только если выбран этот источник.");
          if (isSameField) applySecretPlaceholderEl(input, curEnv);
        }
        addedSomething = true;
      }
      var controlRow = document.createElement("div");
      controlRow.className = "tool-row";
      appendInstallStatus(controlRow, row);
      if (controlRow.childNodes.length) {
        settings.appendChild(controlRow);
        addedSomething = true;
      }
      if (!addedSomething) {
        appendMutedNote(settings, "Работает без ключа — ничего настраивать не нужно.");
      }
    }
    wireSelect(select, renderSettings);
    rowUI.refreshState();
  }

  function extractBackendChoiceValue() {
    var select = byId("extract_choice");
    var value = select ? select.value : "";
    if (value !== "off") return value;
    // Same clear-signal contract as imageGenProviderChoiceValue(): an
    // untouched/never-configured "off" (the default the select starts
    // on) stays a "" no-op; only a DELIBERATE pick back to "off", after a
    // backend was actually saved, sends the explicit `null` that clears
    // it server-side.
    var current = state.current || {};
    return current.extract_backend ? null : "";
  }

  // Generalized "key of the chosen web_extract row" payload — extract_env's
  // own sibling of searchEnvPayload() above. null when the chosen row has
  // no submittable env var, the field is empty, OR "off" was (re)picked —
  // same return-mode no-op contract as every other optional field here.
  function extractEnvPayload() {
    var block = toolBlockFor("web_extract");
    var rows = (block && block.rows) || [];
    var backend = extractBackendChoiceValue();
    var row = rows.filter(function (r) { return r.web_backend === backend; })[0];
    var envs = (row && row.env_vars) || [];
    if (!envs.length) return null;
    var input = byId("extract_env_value");
    var value = input ? input.value : "";
    if (!value) return null;
    return { key: envs[0].key, value: value };
  }

  // ---- Голосовые ответы: Edge stays the two-way "default voice" /
  // "custom voice name" split it always had (apply_settings() writes the
  // flat `tts_voice` string only for Edge — see apply.py) — every OTHER
  // tts-category row (owner ruling 2026-08-20: KittenTTS, Piper, OpenAI
  // TTS, ElevenLabs, Mistral, Google Gemini TTS, DeepInfra) is now an
  // extra option in the SAME select, sharing renderProviderRowSettings()
  // with stt/image_gen/video_gen below. Selecting a non-Edge option
  // also submits tool_provider.tts = that row's provider_key (see
  // ttsProviderChoiceValue()) so the choice actually activates. ---------

  var VOICE_DEFAULT_NAME = "ru-RU-SvetlanaNeural";
  var EDGE_PROVIDER_KEY = "edge";

  function renderVoiceBlock() {
    var container = byId("advanced-voice");
    clearContainer(container);
    var block = toolBlockFor("tts");

    var rows = (block && block.rows) || [];
    var otherRows = rows.filter(function (row) { return row.provider_key !== EDGE_PROVIDER_KEY; });
    var rowByKey = {};
    var otherOptions = [];
    otherRows.forEach(function (row) {
      var key = row.provider_key;
      // Finding 17: a duplicate provider_key would otherwise stamp out two
      // <option>s with the same value — select.value picks the FIRST one,
      // but rowByKey[key] = row here would let the LAST one win for the
      // settings panel, silently mismatching what the select shows. First
      // one in wins for both, consistently.
      if (rowByKey.hasOwnProperty(key)) return;
      rowByKey[key] = row;
      otherOptions.push({ value: key, label: row.name });
    });

    var current = state.current || {};
    var currentVoice = current.tts_voice || "";
    var isCustom = !!currentVoice && currentVoice !== VOICE_DEFAULT_NAME;
    var savedTts = current.tts_provider || EDGE_PROVIDER_KEY;
    var nonEdgeSaved = savedTts !== EDGE_PROVIDER_KEY ? savedTts : "";
    // Finding 4: a saved non-Edge choice that isn't among the rendered
    // rows (e.g. hidden by the OAuth-only rule, or a plugin that failed to
    // load) used to fall straight through to "default"/"custom" — Edge —
    // and an untouched resubmit would silently overwrite it with "edge".
    // The catalog-mismatch guard below (shared with browser/search) makes
    // that visible instead of silent.
    var picked = pickPreselected(otherOptions, nonEdgeSaved);

    var rowUI = buildCollapsibleRow(container, (block && block.title_ru) || "Голосовые ответы", function () {
      if (picked.outOfCatalog && select.value === "") return "настроено вручную";
      return selectStateText(select, "каталог недоступен");
    });

    var select = buildSelectRow(rowUI.body, "voice_choice", "Голосовые ответы", "Каким голосом агент озвучивает ответы.");
    addSelectOption(select, "default", "Голос Светлана (по умолчанию, рекомендуется)");
    addSelectOption(select, "custom", "Свой голос");
    otherOptions.forEach(function (o) { addSelectOption(select, o.value, o.label); });
    if (picked.outOfCatalog) select.insertBefore(placeholderOption(), select.firstChild);

    if (nonEdgeSaved && !picked.outOfCatalog) {
      select.value = picked.value;
    } else if (picked.outOfCatalog) {
      select.value = "";
    } else {
      select.value = isCustom ? "custom" : "default";
    }

    var settings = appendSubSettings(rowUI.body);

    function renderSettings() {
      clearContainer(settings);
      var value = select.value;
      if (picked.outOfCatalog && value === "") {
        appendOutOfCatalogNote(settings, current.tts_provider);
        return;
      }
      if (value === "default") {
        appendMutedNote(settings, "Используется голос Светлана — ничего настраивать не нужно.");
        return;
      }
      if (value === "custom") {
        var input = appendTextField(settings, "tts_voice", "Имя голоса", "Имя голоса Edge TTS, например ru-RU-SvetlanaNeural.");
        input.value = isCustom ? currentVoice : "";
        return;
      }
      var row = rowByKey[value];
      if (!row) return;
      renderProviderRowSettings(settings, row, "tts", current);
    }
    wireSelect(select, renderSettings);
    rowUI.refreshState();
  }

  function ttsProviderChoiceValue() {
    var select = byId("voice_choice");
    var value = select ? select.value : "";
    return value === "default" || value === "custom" ? EDGE_PROVIDER_KEY : value;
  }

  // ---- Распознавание речи: same "always has an active default provider"
  // shape as "Голосовые ответы" above (never an "off" state, unlike
  // image_gen/video_gen) — Local Whisper is the recommended default
  // (free, no API key), every other row (Groq, OpenAI, ElevenLabs,
  // DeepInfra, and any registered plugin — Nexara today, see
  // tools_view.py::_stt_registry_rows()) shares renderProviderRowSettings()
  // with tts/image_gen/video_gen. Unlike "Голосовые ответы" there
  // is no Edge-shaped default/custom split — Local Whisper needs nothing
  // beyond its own install button (post_setup="faster_whisper"). ---------

  var STT_DEFAULT_KEY = "local";

  function renderSTTBlock() {
    var container = byId("advanced-stt");
    clearContainer(container);
    var block = toolBlockFor("stt");

    var rows = (block && block.rows) || [];
    var rowByKey = {};
    var options = [];
    rows.forEach(function (row) {
      var key = row.provider_key || row.name;
      // Finding 17: first row with a given key wins — see the identical
      // guard in renderVoiceBlock() above for why.
      if (rowByKey.hasOwnProperty(key)) return;
      rowByKey[key] = row;
      options.push({ value: key, label: row.name + (row.recommended ? " (рекомендуется)" : ""), recommended: !!row.recommended });
    });

    var current = state.current || {};
    var savedStt = current.stt_provider || STT_DEFAULT_KEY;
    // Finding 4: a saved choice missing from the rendered catalog (a
    // plugin that failed to load, e.g.) used to fall straight back to
    // STT_DEFAULT_KEY ("local") — and since "local" almost always renders
    // (it ships built in, not plugin-dependent), an untouched resubmit
    // would silently overwrite it. The catalog-mismatch guard below (same
    // mechanism as browser/search) makes that visible instead of silent.
    var picked = pickPreselected(options, savedStt);

    var rowUI = buildCollapsibleRow(container, (block && block.title_ru) || "Распознавание речи", function () {
      if (picked.outOfCatalog && select.value === "") return "настроено вручную";
      return selectStateText(select, "каталог недоступен");
    });

    var select = buildSelectRow(rowUI.body, "stt_choice", "Распознавание речи", "Чем агент расшифровывает голосовые сообщения.");
    options.forEach(function (o) { addSelectOption(select, o.value, o.label); });
    if (picked.outOfCatalog) select.insertBefore(placeholderOption(), select.firstChild);
    select.value = picked.value;

    var settings = appendSubSettings(rowUI.body);

    function renderSettings() {
      clearContainer(settings);
      // Finding 20 (kept as-is): an entirely empty catalog (registry
      // import failure — see _stt_registry_rows()) means there was never
      // a real choice to preserve or lose — say so plainly, distinct from
      // the out-of-catalog "you have something we can't show" case below.
      if (!rows.length) {
        appendMutedNote(settings, "Каталог недоступен, настройки не будут изменены.");
        return;
      }
      var value = select.value;
      if (picked.outOfCatalog && value === "") {
        appendOutOfCatalogNote(settings, current.stt_provider);
        return;
      }
      var row = rowByKey[value];
      if (!row) return;
      renderProviderRowSettings(settings, row, "stt", current);
    }
    wireSelect(select, renderSettings);
    rowUI.refreshState();
  }

  function sttProviderChoiceValue() {
    var select = byId("stt_choice");
    return select ? select.value : "";
  }

  // ---- Генерация изображений: every row of the "image_gen" catalog
  // block renders now (owner ruling 2026-08-20) — FAL/DeepInfra/Krea/
  // OpenAI/OpenRouter through renderProviderRowSettings()'s generic env
  // field (form.tool_env, apply.py), plus a distinct informational hint
  // for "OpenAI (Codex auth)" (structural: recognized by its provider_key
  // — the plugin identity marker tools_view.py already stamps on the row
  // — never by display name). That row carries neither
  // env_vars nor a post_setup hook: it activates through the ChatGPT/
  // Codex OAuth login already completed in step 4 ("Провайдер"), nothing
  // left to configure here — see tools_view.py's module docstring for
  // why the OAuth-only structural rule can't (and shouldn't) hide it. ---

  var CODEX_AUTH_PROVIDER_KEY = "openai-codex";

  function renderImageGenBlock() {
    var container = byId("advanced-image-gen");
    clearContainer(container);
    var block = toolBlockFor("image_gen");

    var rows = (block && block.rows) || [];
    var options = [];
    var rowByKey = {};
    rows.forEach(function (row) {
      var key = row.provider_key || row.name;
      // Finding 17: first row with a given key wins.
      if (rowByKey.hasOwnProperty(key)) return;
      rowByKey[key] = row;
      options.push({ value: key, label: row.name + (row.recommended ? " (рекомендуется)" : ""), recommended: !!row.recommended });
    });

    var current = state.current || {};
    var savedValue = current.image_gen_provider || "";
    // Finding 4: unlike stt/voice, "off" already means an empty string on
    // submit (imageGenProviderChoiceValue() below), which apply.py treats
    // as a no-op — so falling back to "off" when the saved choice isn't in
    // the catalog was never a silent-overwrite bug the way stt/voice were.
    // Still routed through the same catalog-mismatch guard as browser/
    // search, for the same reason: showing "Выключена" while something is
    // actually configured (just hidden — e.g. by the OAuth-only rule) is
    // misleading even though it doesn't lose data.
    var picked = savedValue ? pickPreselected(options, savedValue) : { value: "off", outOfCatalog: false };

    var rowUI = buildCollapsibleRow(container, (block && block.title_ru) || "Генерация изображений", function () {
      if (picked.outOfCatalog && select.value === "") return "настроено вручную";
      return selectStateText(select, "каталог недоступен");
    });

    var select = buildSelectRow(rowUI.body, "image_gen_choice", "Генерация изображений", "Рисует картинки по просьбе в чате.");
    addSelectOption(select, "off", "Выключена (по умолчанию)");
    options.forEach(function (o) { addSelectOption(select, o.value, o.label); });
    if (picked.outOfCatalog) select.insertBefore(placeholderOption(), select.firstChild);
    select.value = picked.value;

    var settings = appendSubSettings(rowUI.body);

    function renderSettings() {
      clearContainer(settings);
      var value = select.value;
      if (picked.outOfCatalog && value === "") {
        appendOutOfCatalogNote(settings, current.image_gen_provider);
        return;
      }
      if (value === "off") {
        appendMutedNote(settings, "Ничего настраивать не нужно.");
        return;
      }
      var row = rowByKey[value];
      if (!row) return;
      if (row.provider_key === CODEX_AUTH_PROVIDER_KEY) {
        appendMutedNote(settings, "Работает после входа по аккаунту ChatGPT (шаг «Провайдер»).");
        return;
      }
      renderProviderRowSettings(settings, row, "image_gen", current);
    }
    wireSelect(select, renderSettings);
    rowUI.refreshState();
  }

  function imageGenProviderChoiceValue() {
    var select = byId("image_gen_choice");
    var value = select ? select.value : "";
    if (value !== "off") return value;
    // Finding 7's clear signal: apply.py treats "" as a no-op, so an "off"
    // pick only means anything when a provider was actually saved before —
    // an untouched/never-configured "off" (the default the select starts
    // on) must stay a no-op too, never a destructive null.
    var current = state.current || {};
    return current.image_gen_provider ? null : "";
  }

  // ---- Генерация видео: same "off + rows" shape as "Генерация
  // изображений" — FAL/DeepInfra (owner ruling 2026-08-20). ------------

  function renderVideoGenBlock() {
    var container = byId("advanced-video-gen");
    clearContainer(container);
    var block = toolBlockFor("video_gen");

    var rows = (block && block.rows) || [];
    var options = [];
    var rowByKey = {};
    rows.forEach(function (row) {
      var key = row.provider_key || row.name;
      // Finding 17: first row with a given key wins.
      if (rowByKey.hasOwnProperty(key)) return;
      rowByKey[key] = row;
      options.push({ value: key, label: row.name + (row.recommended ? " (рекомендуется)" : ""), recommended: !!row.recommended });
    });

    var current = state.current || {};
    var savedValue = current.video_gen_provider || "";
    // Finding 4: same reasoning as renderImageGenBlock() above — "off"
    // already submits an empty (no-op) string, so this was never a
    // silent-overwrite bug, but the note keeps the panel honest about a
    // choice that's actually configured but hidden from the catalog.
    var picked = savedValue ? pickPreselected(options, savedValue) : { value: "off", outOfCatalog: false };

    var rowUI = buildCollapsibleRow(container, (block && block.title_ru) || "Генерация видео", function () {
      if (picked.outOfCatalog && select.value === "") return "настроено вручную";
      return selectStateText(select, "каталог недоступен");
    });

    var select = buildSelectRow(rowUI.body, "video_gen_choice", "Генерация видео", "Снимает короткие видео по просьбе в чате.");
    addSelectOption(select, "off", "Выключена (по умолчанию)");
    options.forEach(function (o) { addSelectOption(select, o.value, o.label); });
    if (picked.outOfCatalog) select.insertBefore(placeholderOption(), select.firstChild);
    select.value = picked.value;

    var settings = appendSubSettings(rowUI.body);

    function renderSettings() {
      clearContainer(settings);
      var value = select.value;
      if (picked.outOfCatalog && value === "") {
        appendOutOfCatalogNote(settings, current.video_gen_provider);
        return;
      }
      if (value === "off") {
        appendMutedNote(settings, "Ничего настраивать не нужно.");
        return;
      }
      var row = rowByKey[value];
      if (!row) return;
      renderProviderRowSettings(settings, row, "video_gen", current);
    }
    wireSelect(select, renderSettings);
    rowUI.refreshState();
  }

  function videoGenProviderChoiceValue() {
    var select = byId("video_gen_choice");
    var value = select ? select.value : "";
    if (value !== "off") return value;
    // Same clear-signal reasoning as imageGenProviderChoiceValue() above.
    var current = state.current || {};
    return current.video_gen_provider ? null : "";
  }

  // Категории x_search/homeassistant ушли из мастера целиком — план A5/B4
  // (owner ruling 2026-08-23): редкие, настраиваются позже через
  // консольную утилиту. Их серверные пути записи/очистки
  // (tool_provider.x_search, hass) остаются для CLI, но клиент больше не
  // рендерит блоки и не отправляет эти поля — см. buildPayload() ниже.

  function appendSecretField(container, id, labelText, hintText) {
    var row = document.createElement("div");
    row.className = "field-row";
    var field = document.createElement("div");
    field.className = "field";
    var label = document.createElement("label");
    label.setAttribute("for", id);
    label.textContent = labelText;
    var input = document.createElement("input");
    input.type = "password";
    input.id = id;
    input.autocomplete = "off";
    field.appendChild(label);
    field.appendChild(input);
    // Owner feedback: applySecretPlaceholderEl() reveals this note (by
    // "<id>_saved_note") instead of overflowing the placeholder — see its
    // own comment. Every dynamically-built secret field gets one; the two
    // static secret fields (telegram_token, provider_api_key) carry the
    // matching element in _MAIN_FORM_HTML itself.
    var savedNote = document.createElement("div");
    savedNote.id = id + "_saved_note";
    savedNote.className = "field-note";
    savedNote.hidden = true;
    field.appendChild(savedNote);
    var hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = hintText;
    row.appendChild(field);
    row.appendChild(hint);
    container.appendChild(row);
    return input;
  }

  function appendTextField(container, id, labelText, hintText, placeholder) {
    var row = document.createElement("div");
    row.className = "field-row";
    var field = document.createElement("div");
    field.className = "field";
    var label = document.createElement("label");
    label.setAttribute("for", id);
    label.textContent = labelText;
    var input = document.createElement("input");
    input.type = "text";
    input.id = id;
    input.autocomplete = "off";
    if (placeholder) input.placeholder = placeholder;
    field.appendChild(label);
    field.appendChild(input);
    var hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = hintText;
    row.appendChild(field);
    row.appendChild(hint);
    container.appendChild(row);
    return input;
  }

  function fallbackRowFor(name) {
    for (var i = 0; i < state.providers.length; i++) {
      if (state.providers[i].name === name) return state.providers[i];
    }
    return null;
  }

  function renderAdvancedFallback() {
    var container = byId("advanced-fallback");
    container.innerHTML = "";
    var heading = document.createElement("h3");
    // Owner feedback (п.5, live walkthrough): "Не запасная модель, а
    // запасной провайдер надо написать" — the heading said "модель", the
    // field label right under it said "провайдер" — two different words
    // for the config this block actually writes (fallback.name/
    // fallback.api_key, a PROVIDER route, not a bare model name). Every
    // user-facing string in this block now says "запасной провайдер"
    // consistently (heading, hint, secret-field label/hint below).
    heading.textContent = "Запасной провайдер";
    // Owner feedback п.5 (CSS): shares .section-title with
    // #provider_group_label (see that rule's own comment) so both step-4
    // section headers read as the same kind of thing.
    heading.className = "section-title";
    container.appendChild(heading);
    var hint = document.createElement("p");
    hint.className = "hint";
    // Раньше здесь было только «Используется, если основной провайдер
    // недоступен. Необязательно.» — клиент видел поле и не понимал,
    // зачем оно и чем он рискует, оставив его пустым. Называем
    // последствие прямо: отказ основного провайдера (кончились
    // средства, провайдер лёг) — это молчащий бот до ручного
    // вмешательства, а вмешаться клиент может только через мастер.
    hint.textContent = "Если основной провайдер откажет — закончатся средства или он станет недоступен, — бот перестанет отвечать, пока вы не вмешаетесь. Запасной провайдер с отдельным ключом снимает этот риск. Поле необязательное.";
    container.appendChild(hint);
    // Owner feedback (п.5, live walkthrough): "Запасных провайдеров нет
    // chatgpt?" — device_code providers (ChatGPT, MiniMax OAuth) are
    // filtered out of the <select> below on purpose, not by oversight.
    // Checked against the runtime: resolve_provider_client()'s
    // openai-codex/minimax-oauth branches (agent/auxiliary_client.py) read
    // a STORED OAuth token from the credential store and ignore any
    // api_key entirely — an OAuth fallback genuinely works at runtime
    // (agent/agent_init.py's init-time fallback path already resolves one
    // this way), and the CLI's own fallback picker already lets an admin
    // pick one, via the SAME device-code login flow the main model picker
    // uses. What is missing is a device-login sub-block for the fallback
    // SLOT on THIS screen — there is nowhere here to actually complete
    // that login, so listing the option would be a control with no way to
    // ever work. Say so, rather than leave the client to guess why
    // ChatGPT/MiniMax OAuth are absent from the list. No CLI invocation or
    // product name in the visible copy — this page is white-labeled
    // "Trix Agent" (test_page_is_russian_and_brandless enforces it) and a
    // shell command is out of scope for someone using a web form anyway.
    var oauthNote = document.createElement("p");
    oauthNote.className = "hint";
    oauthNote.textContent = "В списке — только провайдеры с ключом API. Вход по аккаунту (например, ChatGPT или MiniMax OAuth) мастер для запасного провайдера пока не настраивает.";
    container.appendChild(oauthNote);

    // buildSelectRow()'s sr-only label — same fix as the "Браузер"/"Поиск"
    // category rows below (see that function's own comment): a second
    // visible "Запасной провайдер" label right under this heading would
    // read as two headings stacked, not one heading plus its field.
    var select = buildSelectRow(container, "fallback_name", "Запасной провайдер", "");
    addSelectOption(select, "", "— не использовать —");
    state.providers.forEach(function (p) {
      if (p.kind !== "api_key") return;
      addSelectOption(select, p.name, p.display_name);
    });
    // Spec B3 п.8 (second instance of the same bug found in the main
    // provider block's onProviderChange): a key typed for one fallback
    // provider must not survive a switch to a different one and get
    // submitted under the NEW one's env_var — buildPayload() reads
    // whatever sits in #fallback_api_key without knowing which provider
    // it was typed for. byId() lookup (not a captured reference) because
    // fallbackKeyInput below doesn't exist yet at this point in the
    // render.
    select.onchange = function () {
      var keyInput = byId("fallback_api_key");
      if (keyInput) keyInput.value = "";
    };
    // FIELD_STEP (submit section, below) already maps fallback_name/
    // fallback_api_key to step 4 — but showFieldErrors() only jumps to
    // that step, or highlights the field at all, when the matching
    // `#err_<fieldId>` element actually exists (same pattern as the static
    // #err_provider_name/#err_provider_api_key next to the provider
    // fields). Without these two, a 422 on fallback.name/fallback.api_key
    // silently fell through to the generic #form-error banner and never
    // surfaced on the right field.
    var fallbackNameErr = document.createElement("div");
    fallbackNameErr.id = "err_fallback_name";
    fallbackNameErr.className = "field-error";
    fallbackNameErr.hidden = true;
    select.parentNode.appendChild(fallbackNameErr);

    var fallbackKeyInput = appendSecretField(container, "fallback_api_key", "Ключ запасного провайдера", "Нужен, только если выбран запасной провайдер.");
    var fallbackKeyErr = document.createElement("div");
    fallbackKeyErr.id = "err_fallback_api_key";
    fallbackKeyErr.className = "field-error";
    fallbackKeyErr.hidden = true;
    fallbackKeyInput.parentNode.appendChild(fallbackKeyErr);
  }

  function renderAdvanced() {
    // Defensive reset: buildCollapsibleRow() pushes into
    // state.collapsibleRows every time it runs — if renderAdvanced() were
    // ever called a second time (it isn't today; see loadForm()'s own
    // comment on this), the seven render*Block() calls below would each
    // clearContainer() their own DOM but leave the OLD row entries (now
    // detached from the document) sitting in the registry, so
    // closeAllCollapsibleRows()/the accordion click handler would call
    // .close() on nodes nobody can see any more.
    state.collapsibleRows = [];
    renderBrowserBlock();
    renderSearchBlock();
    renderExtractBlock();
    renderVoiceBlock();
    renderSTTBlock();
    renderImageGenBlock();
    renderVideoGenBlock();
    renderAdvancedFallback();
  }

  // ---- Submit -----------------------------------------------------------

  // Finding 10: true for the ~2-5 minutes a POST /api/submit is in flight
  // (save -> restart the gateway -> wait for the bot to come alive) —
  // isStepClickable() (above) reads this to stop the progress bar from
  // offering a click, and the flag also disables step 6's own "Назад".
  // Neither blocked the client from hiding #progress/#success mid-flight
  // before this — see the finding.
  var submitInFlight = false;

  function setSubmitInFlight(flag) {
    submitInFlight = flag;
    var backBtn = byId("step-6-back");
    if (backBtn) backBtn.disabled = flag;
    renderProgressBar();
  }

  var FIELD_MAP = {
    "telegram_token": "telegram_token",
    "allowed_users": "allowed_users",
    "timezone": "timezone",
    "proxy": "proxy",
    "provider.name": "provider_name",
    "provider.api_key": "provider_api_key",
    "fallback.name": "fallback_name",
    "fallback.api_key": "fallback_api_key",
  };

  // Finding 8 (this review pass, pre-existing since before the step
  // redesign): server-side 422 field paths with no per-field #err_<id>
  // slot to highlight at all — provider.env_var/fallback.env_var (no
  // dedicated element next to provider_api_key/fallback_api_key) and
  // search_env.key/extract_env.key/tool_env/tool_provider (step 5's rows
  // have no per-field error slots at all — see renderAdvanced()'s seven
  // render*Block()s). extract_env.key is search_env.key's sibling, same
  // reason. The client still SEES the message (it lands in the general
  // #form-error banner below, same as always), but showFieldErrors() used
  // to only navigate to the earliest step when FIELD_MAP had a matching
  // element — an error on one of these silently left the client on
  // whatever step they were already on. This is a step-only fallback: it
  // does NOT create a new per-field slot, just tells goToStep() where to
  // land.
  //
  // Finding 1 (review 2026-08-26): search_backend/extract_backend joined
  // this map for the same reason — a 422 on either (unknown backend name)
  // was already reaching the client via the general #form-error banner,
  // but with no step to jump to. Both row-select fields share this gap
  // (paired-defect rule), not just the one the review reproduced.
  var PATH_STEP = {
    "provider.env_var": 4,
    "fallback.env_var": 4,
    "search_backend": 5,
    "search_env.key": 5,
    "extract_backend": 5,
    "extract_env.key": 5,
    "tool_env": 5,
    "tool_provider": 5,
  };

  function clearFieldErrors() {
    document.querySelectorAll(".field-error").forEach(function (el) {
      el.hidden = true;
      el.textContent = "";
    });
    byId("form-error").textContent = "";
  }

  function pydanticGenericMessage(type) {
    return type === "missing" ? "Обязательное поле." : "Неверное значение.";
  }

  function errorsFromResponseBody(data) {
    if (data.errors) return data.errors;
    // Pydantic's 422 shape sends `detail` as a list of {loc, type} items —
    // true for every submit-time validation error today. Any OTHER
    // HTTPException(detail="строка") sends a plain string instead; calling
    // .forEach on that threw a TypeError that landed in the outer .catch
    // and showed the false "Не удалось связаться с сервером" instead of
    // the real reason. Surface a string detail as a general form error
    // instead of crashing on it.
    if (Array.isArray(data.detail)) {
      var out = {};
      data.detail.forEach(function (item) {
        var loc = (item.loc || []).slice(1).join(".");
        out[loc] = pydanticGenericMessage(item.type);
      });
      return out;
    }
    if (typeof data.detail === "string" && data.detail) {
      return { _general: data.detail };
    }
    return {};
  }

  // Owner feedback (this pass): a mapped field's own #err_<id> can sit
  // inside a sub-block the client never opened THIS session (e.g.
  // #provider-api-key-block/#provider-device-code-block start `hidden` and
  // only unhide once a provider row is picked — see onProviderGroupChange()
  // — which a returning client isn't required to touch again). Setting
  // el.hidden=false on the error text itself does nothing visible while an
  // ANCESTOR still carries `hidden` — this walks up to (never including)
  // the [data-step] wrapper goToStep() already owns and clears it on
  // anything still hidden in between, so the highlighted field is
  // guaranteed reachable once the step is shown.
  function revealFieldError(el) {
    var node = el.parentElement;
    while (node && !node.hasAttribute("data-step")) {
      if (node.hidden) node.hidden = false;
      node = node.parentElement;
    }
  }

  function showFieldErrors(errors) {
    var unmapped = [];
    var firstStep = null;
    var stepFirstEl = {};
    Object.keys(errors).forEach(function (path) {
      var fieldId = FIELD_MAP[path];
      var el = fieldId ? byId("err_" + fieldId) : null;
      var step = fieldId ? FIELD_STEP[fieldId] : PATH_STEP[path];
      if (el) {
        el.textContent = errors[path];
        el.hidden = false;
        revealFieldError(el);
        if (step && !stepFirstEl[step]) stepFirstEl[step] = el;
      } else {
        unmapped.push(errors[path]);
      }
      if (step && (firstStep === null || step < firstStep)) firstStep = step;
    });
    if (unmapped.length) byId("form-error").textContent = unmapped.join(" ");
    // A 422 from "Готово" (step 6) can name a field that lives on an
    // earlier, now-hidden step — jump to the EARLIEST step carrying an
    // error so the client actually sees what needs fixing, instead of an
    // error message sitting invisibly behind a step they've already left.
    if (state.mode === "steps" && firstStep !== null) goToStep(firstStep);
    // Owner feedback: goToStep() lands on the right step, but neither it
    // nor this function used to touch scroll position — a client who had
    // scrolled down to reach "Запустить агента" stayed at that same pixel
    // offset after the navigation, which can leave the one thing that
    // explains the failure (the highlighted field, or the banner when no
    // field owns the path) sitting above the fold on the new, shorter
    // step. Bring whichever one is the actual complaint into view instead
    // of trusting the old scroll position to still make sense.
    var focusEl = (firstStep !== null && stepFirstEl[firstStep]) || (unmapped.length ? byId("form-error") : null);
    if (focusEl && typeof focusEl.scrollIntoView === "function") {
      focusEl.scrollIntoView({ block: "center" });
    }
  }

  var STAGE_LABELS = {
    apply: "Сохранение",
    install: "Установка инструментов",
    restart: "Перезапуск шлюза",
    liveness: "Проверка бота",
  };

  function showStageError(stage, message) {
    var label = STAGE_LABELS[stage] || "Настройка";
    byId("form-error").textContent = label + ": " + message;
  }

  // ---- Honest wait-screen progress (spec 7 / plan B5) -------------------
  //
  // POST /api/submit is ONE long blocking request (apply settings, maybe
  // install a tool or two, restart the gateway, poll until the bot
  // answers) with no incremental signal back to the client while it is in
  // flight — no SSE, no websocket, nothing between "request sent" and
  // "response arrived" up to several minutes later. This deliberately
  // does NOT fabricate progress: a stage only gets its checkmark +
  // past-tense "done" text when it is either near-certain or
  // server-confirmed, never from a bare clock guess dressed up as fact.
  //
  //   - "apply" (save config.yaml/.env) is local disk I/O with no network
  //     call — see apply_settings() in app.py. By ~1.5s after the request
  //     starts it has essentially always finished, so advancing past it
  //     on a short timer is a safe, near-certain call.
  //   - "install" (owner ruling 2026-08-24 — app.py's own install stage,
  //     right after "apply") can legitimately run for minutes (Chromium
  //     alone is hundreds of MB) — same treatment as "restart"/"liveness"
  //     below, never marked "done" from elapsed time. It only appears in
  //     the stage list at all when pendingToolInstallNames() (above) says
  //     something will actually be installed THIS submission — an empty
  //     list means the stage stays hidden rather than showing a step that
  //     will never run (see setStageOrder() below).
  //   - "restart" (restart_gateway()) carries its OWN 120s timeout and can
  //     legitimately still be running minutes in. It is never marked
  //     "done" from elapsed time — it just stays "идёт" for the rest of
  //     the wait, because nothing client-visible can honestly confirm it
  //     finished before the response itself does.
  //   - "liveness" (wait_bot_alive()) never gets its own "now" state
  //     during the wait for the same reason — there is no way to tell
  //     "still restarting" from "restarting done, now polling" without a
  //     server signal this protocol doesn't have.
  //
  // The three real, CONFIRMED transitions all come from the response
  // itself: success confirms every stage that was actually in play this
  // submission (renderProgressStages() called with the full length of
  // currentStageOrder in doSubmit()'s success branch — see below), and a
  // stage-tagged error (data.stage from app.py's _run_submit —
  // "apply"/"restart"/"liveness"; the install stage never produces one —
  // see its own comment in app.py) proves every stage strictly before the
  // failed one actually completed, since the server runs them in that
  // exact order.
  var BASE_STAGE_ORDER = ["apply", "restart", "liveness"];
  var STAGE_TEXT = {
    apply: { ahead: "Сохраняем настройки", now: "Сохраняем настройки…", done: "Настройки сохранены" },
    install: { ahead: "Установка инструментов", now: "Устанавливаем инструменты…", done: "Установка завершена" },
    restart: { ahead: "Перезапуск агента", now: "Перезапускаем агента…", done: "Агент перезапущен" },
    liveness: { ahead: "Ждём ответа бота", now: "Ждём ответа бота…", done: "Бот ответил" },
  };
  // Same checkmark glyph as the approved mockup's screen 7 (done stages).
  var STAGE_CHECK_SVG = '<svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M3 8.5l3.2 3.2L13 5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var progressStageTimer = null;
  // Which stages THIS submission carries, in order — decided once, in
  // doSubmit(), before the request is sent (see setStageOrder()). Read by
  // renderProgressStages() below instead of a fixed constant, since
  // whether "install" is in the list varies per submission.
  var currentStageOrder = BASE_STAGE_ORDER;

  // Toggles #stage-install's visibility to match includeInstall and
  // records the stage order doSubmit()'s own renderProgressStages() calls
  // read from now on. Called exactly once per submission, before
  // startProgressStages() — see doSubmit().
  function setStageOrder(includeInstall) {
    currentStageOrder = includeInstall
      ? ["apply", "install", "restart", "liveness"]
      : BASE_STAGE_ORDER;
    var installLi = byId("stage-install");
    if (installLi) installLi.hidden = !includeInstall;
  }

  // doneCount stages are CONFIRMED complete (checkmark + past-tense text);
  // nowIndex (if not already inside doneCount) is the one stage currently
  // shown as "идёт"; everything after stays "впереди" in plain text.
  function renderProgressStages(nowIndex, doneCount) {
    currentStageOrder.forEach(function (key, i) {
      var li = byId("stage-" + key);
      if (!li) return;
      var span = li.querySelector("span");
      var mark = li.querySelector("u");
      if (i < doneCount) {
        li.className = "done";
        if (span) span.textContent = STAGE_TEXT[key].done;
        if (mark) mark.innerHTML = STAGE_CHECK_SVG;
      } else if (i === nowIndex) {
        li.className = "now";
        if (span) span.textContent = STAGE_TEXT[key].now;
        if (mark) mark.innerHTML = "";
      } else {
        li.className = "";
        if (span) span.textContent = STAGE_TEXT[key].ahead;
        if (mark) mark.innerHTML = "";
      }
    });
  }

  function startProgressStages() {
    renderProgressStages(0, 0);
    // The one time-based advance this makes — see the block comment
    // above BASE_STAGE_ORDER for why only this transition is safe to
    // guess. Whatever sits at index 1 (install when present, otherwise
    // restart) simply moves to "идёт" — never "done" — same honesty rule.
    progressStageTimer = setTimeout(function () {
      renderProgressStages(1, 1);
    }, 1500);
  }

  function stopProgressStages() {
    if (progressStageTimer) {
      clearTimeout(progressStageTimer);
      progressStageTimer = null;
    }
  }

  function modelFieldValue() {
    // §7.2 free-form entry (review 9d #3): the offline fallback_models/
    // live /api/models catalog is a convenience list, not the only legal
    // value — a provider can release a model the wizard's snapshot
    // hasn't seen yet. The free-text field wins whenever the client
    // typed something into it; the <select> is the fallback.
    //
    // Owner requirement 2: a device_code variant has its OWN model
    // select/free-text pair (provider_model_device{,_custom}) — it's
    // populated after login via loadDeviceModels(), never shares the
    // api_key block's fields (which stay hidden for a device_code
    // variant, so reading them would just return the empty default).
    var isDeviceCode = state.chosenProviderRow && state.chosenProviderRow.kind === "device_code";
    var customId = isDeviceCode ? "provider_model_device_custom" : "provider_model_custom";
    var selectId = isDeviceCode ? "provider_model_device" : "provider_model";
    var custom = byId(customId);
    if (custom && custom.value.trim()) return custom.value.trim();
    var select = byId(selectId);
    if (!select) return "";
    // "Ввести вручную…" chosen but the free-text field never got typed
    // into (custom.value.trim() above was empty) — the sentinel itself is
    // not a model name; fall back to "по умолчанию" rather than submit it.
    if (select.value === CUSTOM_MODEL_VALUE) return "";
    return select.value;
  }

  // ---- Step 6 summary (spec 7 / plan B5) --------------------------------
  //
  // "Всё готово к запуску" is the one screen where the client can review
  // everything before an expensive-to-undo action (the submit restarts the
  // agent). Every value below reads the form/state that is ALREADY on the
  // page — no new request to the server, per the task brief. Secrets
  // (telegram_token, provider_api_key, fallback_api_key, proxy
  // credentials) are never echoed, not even partially — see
  // maskProxyCredentials() and summaryBotValue()'s own comments.

  function summaryBotValue() {
    // The bot's @username is only known client-side if THIS session's
    // autocheck (runTelegramCheck(), spec B2 — change/blur or "Далее")
    // already succeeded — the wizard never calls Telegram on its own to
    // resolve a username, and /api/form's `current` never carries one
    // (see app.py's _current_state: telegram_token is `{is_set}` only).
    // Reading state.telegramCheck is honest reuse of that confirmed
    // answer, not a new request; anything less certain (token typed/saved
    // but never checked this session, or edited since the last check —
    // see the telegram_token "input" listener clearing it) gets an honest
    // "not verified" line instead of a fabricated name.
    if (state.telegramCheck && state.telegramCheck.ok && state.telegramCheck.username) {
      return "@" + state.telegramCheck.username;
    }
    var typed = (byId("telegram_token").value || "").trim();
    var savedSet = !!(state.current.telegram_token && state.current.telegram_token.is_set);
    // Owner feedback: "токен сохранён, бот не проверен" reads like a
    // warning about something the client did wrong. Nothing is wrong —
    // this is the ordinary return visit where the saved token was never
    // re-entered, so no name to show. Say what happens next instead.
    if (typed || savedSet) return "сохранённый бот — имя покажем после запуска";
    return "не указан";
  }

  function summaryAllowedUsersValue() {
    var typed = (byId("allowed_users").value || "").trim();
    if (typed) return typed;
    var saved = (state.current.allowed_users || "").trim();
    if (saved) return saved;
    return "не указан";
  }

  // Masks only the "user[:pass]@" userinfo segment of a proxy URL — never
  // the scheme or host:port, which are not secrets and are exactly what
  // the client needs to see to confirm "yes, this is the proxy I meant".
  // A password embedded in the URL never appears, not even partially,
  // same rule as every other secret field in this wizard.
  function maskProxyCredentials(raw) {
    var m = /^([a-zA-Z][a-zA-Z0-9+.-]*:\\/\\/)([^@/]+)@(.*)$/.exec(raw);
    if (!m) return raw;
    return m[1] + "···@" + m[3];
  }

  function summaryProxyValue() {
    var typed = (byId("proxy").value || "").trim();
    if (typed) return maskProxyCredentials(typed);
    return "не используется";
  }

  function summaryModelValue() {
    var row = state.chosenProviderRow;
    var providerLabel = "";
    var modelLabel = "";
    if (row) {
      providerLabel = row.display_name || row.name;
      modelLabel = modelFieldValue();
    } else {
      var cur = state.current.provider || {};
      // Second instance of updateProviderCurrentHint()'s own "auto" leak
      // (see that function's comment): model.provider's un-set default is
      // the literal string "auto", not a real catalog slug —
      // providerRowFor("auto") finds nothing and used to fall back to
      // printing "auto" itself on the "Всё готово" summary.
      if (cur.name && cur.name !== "auto") {
        var curRow = providerRowFor(cur.name);
        providerLabel = curRow ? curRow.display_name : cur.name;
        modelLabel = cur.model || "";
      }
    }
    if (!providerLabel) return "не выбран";
    return providerLabel + " · " + (modelLabel || "модель по умолчанию");
  }

  // One entry per "Дополнительно" category (spec: браузер, поиск, чтение
  // страниц, голос, распознавание речи, изображения, видео — x_search/
  // homeassistant are dropped from the wizard per plan A5 and never
  // rendered here).
  // isDefault() names each category's own baseline value — the same
  // sentinel its own render*Block() above already treats as "nothing
  // special chosen" (CAMOFOX_VALUE/off/"default"/STT_DEFAULT_KEY) — so a
  // client who touched nothing sees an honest "по умолчанию", not a
  // padded-out list of every category.
  var SUMMARY_TOOL_CATEGORIES = [
    { select: "browser_choice", isDefault: function (v) { return v === "off" || v === ""; } },
    { select: "search_choice", isDefault: function () { return false; } },
    // "Чтение страниц" has a real "off" (never configured is legitimate —
    // see renderExtractBlock()'s own comment), unlike search's always-on
    // ddgs default — same isDefault shape as image_gen/video_gen below.
    { select: "extract_choice", isDefault: function (v) { return v === "off" || v === ""; } },
    { select: "voice_choice", isDefault: function (v) { return v === "default"; } },
    { select: "stt_choice", isDefault: function (v) { return v === STT_DEFAULT_KEY; } },
    { select: "image_gen_choice", isDefault: function (v) { return v === "off" || v === ""; } },
    { select: "video_gen_choice", isDefault: function (v) { return v === "off" || v === ""; } },
  ];

  function summaryAdvancedValue() {
    var parts = [];
    SUMMARY_TOOL_CATEGORIES.forEach(function (cat) {
      var select = byId(cat.select);
      if (!select || !select.value || cat.isDefault(select.value)) return;
      var opt = select.options ? select.options[select.selectedIndex] : null;
      var text = opt ? opt.textContent : select.value;
      text = text.replace(/\\s*\\(рекомендуется\\)\\s*$/, "").replace(/\\s*\\(по умолчанию\\)\\s*$/, "");
      parts.push(text);
    });
    return parts.length ? parts.join(", ") : "по умолчанию — ничего не включено";
  }

  function summaryRow(label, value, step) {
    var row = document.createElement("div");
    row.className = "r";
    var i = document.createElement("i");
    i.textContent = label;
    var b = document.createElement("b");
    b.textContent = value;
    row.appendChild(i);
    row.appendChild(b);
    var a = document.createElement("a");
    a.href = "#";
    a.textContent = "изменить";
    a.addEventListener("click", function (e) {
      e.preventDefault();
      goToStep(step);
    });
    row.appendChild(a);
    return row;
  }

  function summaryTimezoneValue() {
    var chosen = (byId("timezone").value || "").trim();
    if (!chosen) return "не выбран";
    // Подпись берётся из того же каталога, что нарисовал список, — не
    // разбирается обратно из текста выбранной опции.
    var groups = state.timezones || [];
    for (var i = 0; i < groups.length; i++) {
      var zones = groups[i].zones || [];
      for (var j = 0; j < zones.length; j++) {
        if (zones[j].name === chosen) return timezoneOptionLabel(zones[j]);
      }
    }
    return chosen;
  }

  function renderSummary() {
    var container = byId("summary-rows");
    if (!container) return;
    container.innerHTML = "";
    container.appendChild(summaryRow("Бот", summaryBotValue(), 3));
    container.appendChild(summaryRow("Пишет боту", summaryAllowedUsersValue(), 3));
    container.appendChild(summaryRow("Часовой пояс", summaryTimezoneValue(), 3));
    container.appendChild(summaryRow("Прокси", summaryProxyValue(), 2));
    container.appendChild(summaryRow("Модель", summaryModelValue(), 4));
    container.appendChild(summaryRow("Дополнительно", summaryAdvancedValue(), 5));
  }

  // hass (homeassistant) больше не рендерится мастером и не отправляется —
  // план A5/B4. Клиент просто не включает ключ "hass" в payload вовсе;
  // сервер (apply.py) уже трактует ОТСУТСТВУЮЩИЙ ключ как "не трогать",
  // отдельно от явного `null` ("очистить") — тот же контракт, что и у
  // каждого другого необязательного поля здесь, только теперь клиент
  // никогда не решает за CLI-настроенного клиента судьбу этой секции.

  // Finding 5/6's clear signal for Camofox (owner-approved fix): CAMOFOX_URL
  // — not browser.backend — is the real on/off switch (see apply.py's own
  // docstring), and the only way a client can actually turn Camofox off is
  // by picking anything ELSE in the single "Браузер" select. That pick
  // must send `camofox_url: null` whenever Camofox was the thing actually
  // active AND the client made a real, deliberate OTHER choice — every
  // other shape (Camofox never configured, an out-of-catalog placeholder
  // left untouched, staying on Camofox) stays the ordinary "" no-op, same
  // contract every other optional field here follows.
  //
  // Finding 6: the original version only sent `null` for `value === "off"`
  // (Chromium) — picking "Browser Use" instead left a stale saved
  // CAMOFOX_URL in place, so `is_camofox_mode()` (tools/browser_camofox.py)
  // stayed true and the Browser Use pick never actually took effect. Any
  // deliberately-picked row other than Camofox now clears it, not just
  // "off" — the out-of-catalog placeholder renders as `select.value === ""`
  // (see pickPreselected()), which is excluded here the same way "off" and
  // CAMOFOX_VALUE are, so an untouched return visit still stays a no-op.
  function camofoxUrlPayload() {
    var browserSelect = byId("browser_choice");
    var value = browserSelect ? browserSelect.value : "";
    if (value === CAMOFOX_VALUE) {
      // No input to read any more (owner ruling — the client is never
      // asked for this address): send the catalog's own `auto_default`,
      // which is what turns Camofox on in .env. Falling back to whatever
      // is already saved keeps a return visit a no-op rather than
      // clearing a working address the catalog didn't hand us this time.
      var block = toolBlockFor("browser");
      var rows = (block && block.rows) || [];
      for (var i = 0; i < rows.length; i++) {
        var addr = autoDefaultFor(rows[i], CAMOFOX_ENV_VAR);
        if (addr) return addr;
      }
      return (state.current || {}).camofox_url || "";
    }
    var current = state.current || {};
    var backend = current.browser_backend || "";
    // Same derivation renderBrowserBlock() uses for its own preselect (see
    // that function's own priority-fix comment) — duplicated here rather
    // than shared because this only needs the resulting boolean, not the
    // whole preselect/out-of-catalog dance.
    var camofoxWasActive = !(backend && backend !== "off") && !!current.camofox_url;
    return value && camofoxWasActive ? null : "";
  }

  // Finding 2 (owner-approved fix, reversed from an earlier design):
  // picking "Голос Светлана" after a custom voice name was saved used to
  // send `tts_voice: ""` — a no-op, so the saved custom name silently
  // survived — and a later fix flipped that to `tts_voice: null`, meant
  // as a "clear the saved override" signal. That was itself broken:
  // apply.py's `null` branch DELETED `tts.edge.voice` from config.yaml,
  // and DEFAULT_CONFIG's own baseline for that key is the English
  // "en-US-AriaNeural" (Trix's own template ships the Russian voice
  // explicitly, but load_config() falls back to the upstream agent
  // framework's default the instant the key is absent) — so "Голос
  // Светлана" silently
  // switched the agent to an English voice. The client now sends the
  // literal default voice name explicitly instead of a clear signal;
  // apply.py just writes it like any other real name. Every other shape
  // (already default, "custom" with a name typed or left blank, a
  // non-Edge row picked) stays the ordinary "" no-op.
  function ttsVoicePayload() {
    var select = byId("voice_choice");
    var value = select ? select.value : "";
    var current = state.current || {};
    var savedVoice = current.tts_voice || "";
    var wasCustom = !!savedVoice && savedVoice !== VOICE_DEFAULT_NAME;
    if (value === "default") return wasCustom ? VOICE_DEFAULT_NAME : "";
    var input = byId("tts_voice");
    return input ? input.value : "";
  }

  function buildPayload() {
    var providerRow = state.chosenProviderRow;
    var fallbackName = byId("fallback_name") ? byId("fallback_name").value : "";
    var fallbackRow = fallbackName ? fallbackRowFor(fallbackName) : null;

    return {
      // Spec B2: "токен и ключи обрезаются от пробелов и переносов молча"
      // — a pasted token/key frequently carries a leading/trailing space
      // or newline; trimmed here (submit) exactly like the live checks
      // above (runTelegramCheck/runProviderKeyCheck) already trim before
      // sending, so what got verified is exactly what gets saved.
      telegram_token: (byId("telegram_token").value || "").trim(),
      allowed_users: byId("allowed_users").value,
      proxy: byId("proxy").value,
      // Часовой пояс (спека 11). Пустая строка — законный no-op на
      // стороне сервера ровно тогда, когда ответ уже сохранён; на первой
      // установке до отправки дело не доходит — ворота шага не пустят.
      timezone: (byId("timezone").value || "").trim(),
      provider: {
        name: providerRow ? providerRow.name : "",
        env_var: providerRow ? providerRow.env_var : null,
        // A device_code variant's api_key/base_url fields live in the
        // (hidden, untouched) api-key sub-block — reading them here would
        // risk carrying a STALE value left over from a previously-chosen
        // api_key variant into a payload that must have neither (the
        // account login already happened via /api/device/*; apply.py
        // resolves the real base_url itself when this is empty).
        api_key: providerRow && providerRow.kind === "device_code" ? "" : (byId("provider_api_key").value || "").trim(),
        base_url: providerRow && providerRow.kind === "device_code" ? "" : byId("provider_base_url").value,
        model: modelFieldValue(),
      },
      fallback: fallbackName
        ? {
            name: fallbackName,
            env_var: fallbackRow ? fallbackRow.env_var : null,
            api_key: byId("fallback_api_key") ? (byId("fallback_api_key").value || "").trim() : "",
            base_url: "",
            model: "",
          }
        : null,
      // These five <select>s are only rendered inside #advanced — but
      // they're always preselected from `current`/`recommended` in
      // renderBrowserBlock()/renderSearchBlock()/etc. as soon as
      // /api/form loads (review 9d #2), and #advanced (step 5) is always
      // fully rendered — see enterStepsMode(). Reading their `.value`
      // therefore already reflects "what the client has, or the default
      // if they have nothing" even when the client never scrolled to step
      // 5 — sending it is a same-value, idempotent write in the common
      // case, never a silent downgrade.
      //
      // Camofox mapping (kept from the pre-redesign behavior — see
      // apply.py's own docstring): CAMOFOX_URL, not browser.backend, is
      // the real Camofox on/off switch, so selecting it in the single
      // "Браузер" <select> still submits browser_backend "off" +
      // camofox_url, exactly as when Camofox was its own always-visible
      // row.
      search_backend: searchBackendChoiceValue(),
      search_env: searchEnvPayload(),
      // extract_backend/extract_env are search_backend/search_env's
      // siblings for the SEPARATE "Чтение страниц" capability
      // (renderExtractBlock() above) — same idempotent-resubmit contract,
      // read from extract_choice/extract_env_value the same way.
      extract_backend: extractBackendChoiceValue(),
      extract_env: extractEnvPayload(),
      browser_backend: browserBackendChoiceValue(),
      tts_voice: ttsVoicePayload(),
      camofox_url: camofoxUrlPayload(),
      // Generalized search_env/search_backend for the OTHER provider-
      // select categories (tts/image_gen/video_gen) — same "always
      // reflects current <select> state, idempotent to resubmit"
      // contract as search_backend/browser_backend above (see this
      // object's own comment on that). "hass" and "tool_provider.x_search"
      // are deliberately absent — план A5/B4 dropped the homeassistant/
      // x_search categories from the wizard; omitting the key is the
      // framework's own "не трогать" signal (apply.py/app.py), distinct
      // from an explicit `null` ("очистить").
      tool_env: toolEnvPayload(),
      tool_provider: {
        tts: ttsProviderChoiceValue(),
        stt: sttProviderChoiceValue(),
        image_gen: imageGenProviderChoiceValue(),
        video_gen: videoGenProviderChoiceValue(),
      },
    };
  }

  function browserBackendChoiceValue() {
    var select = byId("browser_choice");
    var value = select ? select.value : "";
    return value === CAMOFOX_VALUE ? "off" : value;
  }

  function searchBackendChoiceValue() {
    var select = byId("search_choice");
    return select ? select.value : "";
  }

  // Generalized "key of the chosen web row" payload — replaces the old
  // firecrawl-only firecrawl_key field. null when the chosen row has no
  // submittable env var, OR the field is empty (return-mode no-op, same
  // contract as every other optional secret field).
  function searchEnvPayload() {
    var block = toolBlockFor("web");
    var rows = (block && block.rows) || [];
    var backend = searchBackendChoiceValue();
    var row = rows.filter(function (r) { return r.web_backend === backend; })[0];
    var envs = (row && row.env_vars) || [];
    if (!envs.length) return null;
    var input = byId("search_env_value");
    var value = input ? input.value : "";
    if (!value) return null;
    return { key: envs[0].key, value: value };
  }

  // Generalized ``searchEnvPayload`` for the OTHER provider-select
  // categories — reads whichever "tool_env_value__<category>" field
  // renderProviderRowSettings() rendered (empty/absent for a category
  // with nothing to submit right now), keyed back to its own env var via
  // the "data-env-key" attribute the same function stamped on it.
  function toolEnvPayload() {
    var categories = ["tts", "stt", "image_gen", "video_gen"];
    var result = [];
    categories.forEach(function (category) {
      var input = byId("tool_env_value__" + category);
      if (!input) return;
      var key = input.getAttribute("data-env-key");
      var value = input.value;
      if (key && value) result.push({ key: key, value: value });
    });
    return result;
  }

  // Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"): the
  // final "Готово" step's own install stage (app.py's ``_run_submit``)
  // installs whichever catalog rows THIS submission's own choices select
  // and that aren't already installed — see that module's
  // ``_pending_tool_installs`` for the server-side twin of this exact
  // matching rule. Mirrored here (not re-derived from server data) so the
  // wait screen can show — or hide — the "Устанавливаем инструменты"
  // stage BEFORE the request is even sent: honest stages means never
  // drawing a stage that won't run (see startProgressStages()'s own
  // comment on this file's larger honesty rule).
  function rowByMatch(category, matcher) {
    var block = toolBlockFor(category);
    var rows = (block && block.rows) || [];
    return rows.filter(matcher)[0] || null;
  }

  function pendingToolInstallNames() {
    var names = [];
    function consider(row) {
      if (row && row.post_setup && row.installed !== true) names.push(row.name);
    }
    var camofoxUrl = camofoxUrlPayload();
    if (camofoxUrl) {
      consider(rowByMatch("browser", function (r) { return !!extractEnvField(r, "CAMOFOX_URL"); }));
    } else {
      var backend = browserBackendChoiceValue();
      consider(rowByMatch("browser", function (r) { return r.backend_key === backend; }));
    }
    consider(rowByMatch("web", function (r) { return r.web_backend === searchBackendChoiceValue(); }));
    // Finding 9 (review 2026-08-26, owner-approved fix): "web_extract"
    // ("Чтение страниц") is app.py's `_pending_tool_installs`'s own
    // SECOND web-split category (search's sibling — see that function's
    // own docstring) — this client-side mirror only ever checked "web"
    // and silently dropped the extract row, breaking the "never draw a
    // stage that won't run" honesty rule in the OTHER direction: no stage
    // drawn while the server's install stage runs anyway. Latent today
    // (no extract-capable backend — exa/firecrawl/parallel/tavily — has
    // a post_setup hook yet), but a future one would surface this
    // immediately. extractBackendChoiceValue() can return `null` (the
    // finding-1 clear signal) or `""` (no-op); neither ever matches a
    // real `web_backend` string, so this is a safe no-op until a real
    // pick is made, same as every other entry here.
    consider(rowByMatch("web_extract", function (r) { return r.web_backend === extractBackendChoiceValue(); }));
    [
      { category: "tts", value: ttsProviderChoiceValue() },
      { category: "stt", value: sttProviderChoiceValue() },
      { category: "image_gen", value: imageGenProviderChoiceValue() },
      { category: "video_gen", value: videoGenProviderChoiceValue() },
    ].forEach(function (entry) {
      if (!entry.value) return;
      consider(rowByMatch(entry.category, function (r) { return r.provider_key === entry.value; }));
    });
    return names;
  }

  // Finding 2: #done used to be the form's own type="submit" button, and
  // EVERY step 2-5 field lives in this same <form> — that turned Enter in
  // any of them (token, allowed_users, proxy, api key, model name, any
  // dynamic tool_env field) into implicit submission of a live
  // /api/submit, mid-form, on whatever step the client happened to be on.
  // #done is now type="button" (see _MAIN_FORM_HTML) and owns the ONE real
  // submit handler; the <form>'s own "submit" listener below is left as
  // pure insurance — e.preventDefault() and nothing else, so if some other
  // implicit-submission path still fires the event, it does not also run
  // doSubmit().
  function doSubmit() {
    clearFieldErrors();
    var doneBtn = byId("done");
    doneBtn.disabled = true;
    setSubmitInFlight(true);
    // Finding 7 (this review pass): hide the form itself while the wait
    // screen is up — without this, step 6's own summary and the now
    // grayed-out "Запустить агента" button stayed visible underneath
    // #progress for the full 2-5 minutes /api/submit can run (save
    // config, restart the gateway, poll for the bot to answer). The
    // approved mockup's screen 7 is its own dedicated wait screen, not an
    // overlay on top of the form. Symmetric with the success branch below
    // (setHidden("main", true) right before setHidden("success", false)),
    // which already hid #main — this just makes the IN-PROGRESS state do
    // the same.
    setHidden("main", true);
    setHidden("progress", false);
    // Decided once, right before the stage list first renders — see
    // pendingToolInstallNames()'s own comment for why this is mirrored
    // client-side rather than asked of the server first: showing (or
    // hiding) "Устанавливаем инструменты" is a rendering decision that has
    // to be made before the request is even sent.
    setStageOrder(pendingToolInstallNames().length > 0);
    startProgressStages();

    jsonFetch("/api/submit", { method: "POST", body: JSON.stringify(buildPayload()) })
      .then(function (res) {
        stopProgressStages();
        setHidden("progress", true);
        // Re-shown here unconditionally — the success branch below hides
        // it again right before revealing #success, so this is only
        // user-visible on the 409/422/generic-error paths, where the
        // client needs the form back to fix whatever failed.
        setHidden("main", false);
        setSubmitInFlight(false);
        doneBtn.disabled = false;
        // Lost access (401 bad/changed Basic credentials, 403 Origin
        // guard) is handled centrally by jsonFetch() -> handleAuthLost()
        // — it never reaches this .then() at all (jsonFetch's own promise
        // rejects with "auth_lost" first), so there is no separate
        // 401/403 branch to keep in sync here any more. Same for a
        // locked-out IP (429) -> handleRateLimited() -> "rate_limited".
        if (res.status === 409) {
          return res.json().then(function (data) { byId("form-error").textContent = data.error; });
        }
        if (res.status === 422) {
          return res.json().then(function (data) { showFieldErrors(errorsFromResponseBody(data)); });
        }
        return res
          .json()
          .catch(function () { return {}; })
          .then(function (data) {
            if (typeof data.ok !== "boolean") {
              // Anything else unrecognized (5xx, a shape the client
              // doesn't know) — an honest generic message with the HTTP
              // code, not a silent "undefined" stage label.
              byId("form-error").textContent = "Не удалось сохранить настройки (код " + res.status + "). Попробуйте ещё раз.";
              return;
            }
            if (data.ok) {
              // Finding 14a: a client who started (but never confirmed) a
              // device-code login, then succeeded here via an already-valid
              // saved session, must not leave that poll running — the
              // server's retire() (called right after this same successful
              // apply) never marks the old login's status terminal, so
              // without this the interval would keep firing against a
              // screen that's about to disappear anyway.
              stopDevicePoll();
              // Finding 10: the rail's step list ("#progress-bar", a
              // sibling of <form id="main">, not a descendant of it) does
              // not hide itself just because #main does — without this a
              // client landed on "Готово!" with a fully checked-off step
              // strip still shown above it.
              // Success genuinely confirms every stage THIS submission
              // carried happened, in order — not a guess, the server
              // would not have reached this response otherwise (see
              // currentStageOrder/setStageOrder() above for why the
              // length varies). #progress is hidden already, so this has
              // no visible effect today; it exists so the stage list is
              // never left in a stale "идёт"/"впереди" state for any
              // future surface that keeps it visible longer.
              renderProgressStages(currentStageOrder.length, currentStageOrder.length);
              setHidden("progress-bar", true);
              setRailMode("success");
              setHidden("main", true);
              setHidden("success", false);
              var link = byId("botlink");
              var successText = byId("success-text");
              if (data.bot_username) {
                successText.textContent = "Готово! Напишите вашему боту: ";
                link.hidden = false;
                link.textContent = "@" + data.bot_username;
                link.href = "https://t.me/" + data.bot_username;
              } else {
                successText.textContent = "Готово! Бот настроен и запущен.";
                link.hidden = true;
              }
              // Spec §10.1 honest disclosure: a false value here means the
              // saved credential was never put through a real live probe
              // this submission (see app.py's docstring on the
              // /api/submit success tuple's key_checked field) — tell the
              // client so a later "bot replied with an error" isn't a
              // mystery.
              var keyNotice = byId("key-check-notice");
              if (data.key_checked === false) {
                keyNotice.textContent = "Ключ провайдера не проверялся автоматически — если бот ответит ошибкой, проверьте ключ командой /setup.";
                keyNotice.hidden = false;
              } else {
                keyNotice.textContent = "";
                keyNotice.hidden = true;
              }
              // Owner ruling 2026-08-24: a failed install does NOT fail
              // the submission (settings are saved, the agent is running)
              // — app.py's /api/submit always returns tool_install_failures
              // (possibly empty). Report each one honestly instead of
              // silently pretending everything installed.
              var installNotice = byId("tool-install-notice");
              var failures = data.tool_install_failures || [];
              if (failures.length) {
                installNotice.innerHTML = "";
                failures.forEach(function (failure) {
                  var p = document.createElement("p");
                  p.className = "hint";
                  p.textContent = "Инструмент «" + failure.name + "» установить не удалось: " + failure.message + " Остальное настроено.";
                  installNotice.appendChild(p);
                });
                installNotice.hidden = false;
              } else {
                installNotice.innerHTML = "";
                installNotice.hidden = true;
              }
              // Finding 2 (review 2026-08-26): same "settings saved, one
              // thing didn't fully apply" posture as tool_install_failures
              // above — apply_settings() (apply.py) can skip a field (today,
              // only an extract backend picked with no usable key) without
              // failing the submission; app.py's success tuple always
              // returns "warnings" (possibly empty) so the client reports it
              // honestly instead of the summary silently claiming a
              // capability the agent doesn't actually have.
              var warningNotice = byId("apply-warning-notice");
              var applyWarnings = data.warnings || [];
              if (applyWarnings.length) {
                warningNotice.innerHTML = "";
                applyWarnings.forEach(function (message) {
                  var p = document.createElement("p");
                  p.className = "hint";
                  p.textContent = message;
                  warningNotice.appendChild(p);
                });
                warningNotice.hidden = false;
              } else {
                warningNotice.innerHTML = "";
                warningNotice.hidden = true;
              }
            } else {
              showStageError(data.stage, data.error);
            }
          });
      })
      .catch(function (err) {
        stopProgressStages();
        setHidden("progress", true);
        setSubmitInFlight(false);
        doneBtn.disabled = false;
        if (!isAuthError(err) && !isRateLimitedError(err)) {
          // An auth-lost or rate-limited error already ran its own handler
          // (handleAuthLost()/handleRateLimited()), which un-hides #main
          // and shows its own message — restoring it here too would just
          // fight that, so this only runs for a genuine "our own request
          // failed" outcome.
          setHidden("main", false);
          byId("form-error").textContent = "Не удалось связаться с сервером. Проверьте соединение и попробуйте снова.";
        }
      });
  }

  byId("done").addEventListener("click", doSubmit);

  // Insurance only (Finding 2) — see doSubmit()'s comment above.
  byId("main").addEventListener("submit", function (e) {
    e.preventDefault();
  });
})();
"""


def render_page(host: str | None = None) -> str:
    """Return the wizard's complete, static single-page HTML document.

    ``host`` (spec §5) is the machine's own address as seen in the
    request's ``Host`` header — passed straight through
    ``app.py``'s route handler, never resolved or looked up server-side.
    ``None`` (the default, and every non-HTTP caller) renders the
    original host-less sentence.
    """
    # B1 (spec 7): the old top-level "Настройка Trix Agent" <h1> + always-
    # visible cert disclosure is gone — the product name now lives in the
    # rail (see _rail_html). There is no login screen any more (spec 8,
    # §8.3 — HTTP Basic auth gates every path before a single byte of this
    # markup is served, so by the time a browser gets here it is already
    # authenticated), so the full self-signed-certificate disclosure has
    # exactly one home now: the rail's own compact "подробнее" reveal
    # (`#cert-detail`, see `_rail_html`).
    header_intro = _header_intro(host)
    body = (
        '<div class="wrap">'
        '<div class="canvas">'
        f"{_rail_html(_rail_address(host), header_intro)}"
        '<div class="content">'
        # #progress/#success live inside _MAIN_FORM_HTML's own string as
        # siblings AFTER </form> (never as descendants of <form id="main">
        # — hiding the form via setHidden("main", true) must not also hide
        # the "Готово!" screen). No separate _PROGRESS_HTML/_SUCCESS_HTML
        # constants — they're part of the same triple-quoted block.
        f"{_MAIN_FORM_HTML}"
        "</div>"
        "</div>"
        "</div>"
    )
    return (
        "<!doctype html>"
        '<html lang="ru">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Настройка Trix Agent</title>"
        f"<style>{_CSS}</style>"
        "</head>"
        f"<body>{body}<script>{_JS}</script></body>"
        "</html>"
    )
