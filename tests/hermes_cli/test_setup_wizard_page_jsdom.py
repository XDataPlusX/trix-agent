"""Real-DOM execution tests for the setup wizard's autocheck race conditions
(findings 2/3/4/5 from the post-redesign review, docs/product/... n/a — see
commit history around 2026-08-24).

``test_setup_wizard_page.py`` proves markup/JS-source structure by reading
``render_page()``'s output as text (or, for a handful of pure functions,
extracting one function's body and running it against a hand-rolled DOM
stand-in via ``node -e``). Neither approach can see an in-flight-request
race: the bug only exists in how TWO overlapping async callbacks interact
with a shared ``state``/seq variable while the DOM is live. This file goes
one step further and actually boots the real page — full markup, the real
``<script>``, a real (jsdom) DOM, and a controllable ``fetch`` stub whose
promises this file resolves in whatever order a scenario needs — so the
race is reproduced/disproven by EXECUTING the code, not by pattern-matching
its source.

Requires ``node`` + the ``jsdom`` package (present transitively via the
repo's root ``node_modules`` — pulled in by the ``web`` workspace's
vitest — but never a declared top-level dependency of THIS package, so a
checkout that never ran `npm install` at the repo root, or lint-only CI
lanes, must not fail here). ``requires_jsdom`` skips (not fails) in either
case.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NODE_MODULES = _REPO_ROOT / "node_modules"


def _node_env() -> dict:
    """Inherit the real environment (PATH etc. — node may live anywhere,
    e.g. nvm/asdf/homebrew shims, not just /usr/bin) and point module
    resolution at the repo root's node_modules regardless of which
    directory the driver script itself was written to (a pytest tmp_path,
    not the repo tree)."""
    env = os.environ.copy()
    env["NODE_PATH"] = str(_NODE_MODULES)
    return env


def _jsdom_available() -> bool:
    if shutil.which("node") is None:
        return False
    if not _NODE_MODULES.is_dir():
        return False
    r = subprocess.run(
        ["node", "-e", "require('jsdom')"],
        env=_node_env(),
        cwd=str(_REPO_ROOT),
        capture_output=True,
        timeout=10,
    )
    return r.returncode == 0


requires_jsdom = pytest.mark.skipif(not _jsdom_available(), reason="node + jsdom not available")


def test_jsdom_actually_runs_in_ci():
    """The skip above is right locally and a lie in CI. This says which.

    Skipping is correct for a fresh checkout that never ran ``npm install``
    at the repo root: nobody should need a JS toolchain to run one Python
    test. But the Python lanes (``.github/workflows/tests.yml``,
    ``tests-os.yml``) install uv and nothing else — no ``setup-node``, no
    ``npm ci`` — and ``node_modules`` is not tracked in git. So
    ``require('jsdom')`` fails there, every test in this file skips, and the
    lane reports green.

    That hole is not small. This file is the only place the setup wizard's
    browser behavior is EXECUTED rather than pattern-matched: the
    required-field gate, the absence of a preselected timezone, the warning
    that counts already-scheduled jobs, the ordering races between concurrent
    ``fetch`` calls. Spec 11's status document cites these tests as its
    evidence that the form works. Not running them turns that evidence into
    an assumption, and nothing anywhere says so.

    So: silent locally, loud in CI. Fixed either by adding node + ``npm ci``
    to the Python lanes, or — if that is judged too expensive — by moving
    this file to the JS lane, which ``CLAUDE.md`` already prescribes for
    tests that assert about JS artifacts. Both are decisions for a human;
    this test only refuses to let the choice stay invisible.
    """
    if os.environ.get("CI") != "true":
        pytest.skip("local checkout — jsdom is genuinely optional here")
    assert _jsdom_available(), (
        "jsdom is unavailable in CI, so every test in this file just skipped "
        "and this lane is green over zero browser coverage. Install node + "
        "run `npm ci` in the Python lane, or move this file to the JS lane."
    )

# ---------------------------------------------------------------------------
# The driver: boots the real render_page() markup/script in jsdom with a
# queue-based fetch stub (nothing auto-resolves — every request sits in
# `pending` until a scenario explicitly resolves it, which is what makes
# out-of-order resolution testable at all), loads a real /api/form
# catalog, and then runs one named scenario. Each scenario dispatches real
# DOM events (click/input/blur) — the exact same entry points a human using
# a mouse and keyboard would hit — never calls an internal page.py function
# directly (they are not exported; the closure is the whole point of what's
# being tested: two DOM-driven callbacks racing over shared state).
# ---------------------------------------------------------------------------
_DRIVER_JS = r"""
'use strict';
const { JSDOM } = require('jsdom');
const fs = require('fs');

const htmlPath = process.argv[2];
const catalogPath = process.argv[3];
const scenario = process.argv[4];

const html = fs.readFileSync(htmlPath, 'utf8');
const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'));

function makeFetchStub() {
  const pending = [];
  function fetchStub(url, opts) {
    const method = (opts && opts.method) || 'GET';
    let body = null;
    if (opts && opts.body) {
      try { body = JSON.parse(opts.body); } catch (e) { body = opts.body; }
    }
    return new Promise((resolve, reject) => {
      pending.push({ url, method, body, resolve, reject });
    });
  }
  fetchStub.pending = pending;
  return fetchStub;
}

const fetchStub = makeFetchStub();

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'https://testserver/',
  beforeParse(window) {
    // Set BEFORE the document is parsed, so it is already in place when
    // the page's own inline <script> executes during construction below —
    // jsdom has no built-in fetch, and even if it did, this file needs
    // full control over when/how each request resolves.
    window.fetch = fetchStub;
  },
});
const { window } = dom;
const document = window.document;

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function takePending(matchUrl, matchMethod) {
  const idx = fetchStub.pending.findIndex(
    (p) => p.url === matchUrl && (!matchMethod || p.method === matchMethod)
  );
  if (idx === -1) return null;
  return fetchStub.pending.splice(idx, 1)[0];
}

function resOk(body) {
  return { status: 200, json: () => Promise.resolve(body) };
}

function currentStep() {
  var els = document.querySelectorAll('[data-step]');
  for (var i = 0; i < els.length; i++) {
    if (!els[i].hidden) return Number(els[i].getAttribute('data-step'));
  }
  return null;
}

// Drives the SAME /api/form -> step-2 auto-check sequence a real client
// hits: loadForm() fires the instant the script runs (spec 8, §8.3 — HTTP
// Basic auth means the browser is already authenticated by the time it
// gets this markup, so there is no login step to submit any more) — this
// is the only way to reach the code that populates
// `state.current`/`state.providerGroups` (both plain closure variables,
// not exported), so every scenario below goes through this even when the
// bug it targets has nothing to do with form-loading itself.
async function boot(proxyCheckPayload) {
  await flush();
  const formReq = takePending('/api/form', 'GET');
  formReq.resolve(resOk(catalog));
  await flush();
  await flush();
  // enterStepsMode() -> goToStep(2) -> runProxyCheck() fires the instant
  // the form loads (spec A4's auto-check-on-entry) — every scenario has to
  // answer it before the DOM settles, even scenarios that don't care about
  // step 2 at all.
  const proxyReq = takePending('/api/check/proxy', 'POST');
  if (proxyReq) {
    proxyReq.resolve(resOk(proxyCheckPayload || {
      telegram: true,
      via_proxy: { 'openai-api': true, anthropic: true, openrouter: true },
      direct: { deepseek: true, zai: true, gemini: true },
      providers: {},
    }));
    await flush();
    await flush();
  }
}

// Finding 2: type token A, click "Далее" (check A in flight), edit the
// field to token B before the response lands, THEN let A's answer arrive.
async function scenarioTelegramRace() {
  await boot();
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('step-3-next').click();
  await flush();
  const reqA = takePending('/api/check/telegram', 'POST');
  if (!reqA) throw new Error('expected /api/check/telegram request for token A');
  document.getElementById('telegram_token').value = 'TOKEN_B';
  document.getElementById('telegram_token').dispatchEvent(new window.Event('input', { bubbles: true }));
  await flush();
  reqA.resolve(resOk({ ok: true, username: 'bot_of_TOKEN_A' }));
  await flush();
  await flush();
  await flush();
  return {
    step: currentStep(),
    verdictHidden: document.getElementById('telegram-verdict').hidden,
    verdictText: document.getElementById('telegram-verdict').textContent,
    fieldValue: document.getElementById('telegram_token').value,
  };
}

// Owner feedback п.4 (live VM walkthrough): "было бы круто, если бы там
// тоже высвечивалось сразу, кто это" — drives the real getChat-backed
// lookup end to end: a verified token is a precondition, a single bare id
// triggers it, editing the id clears the note immediately, a negative
// answer (Telegram hasn't seen this user yet) renders as silence — never
// an error — and a comma-separated (ambiguous) value never even attempts
// a lookup. Also proves the XSS invariant: a "name" containing HTML-like
// text must render as literal text, never parsed markup.
async function scenarioTelegramUserNote() {
  await boot();
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('step-3-next').click();
  await flush();
  const tokenReq = takePending('/api/check/telegram', 'POST');
  if (!tokenReq) throw new Error('expected /api/check/telegram request');
  tokenReq.resolve(resOk({ ok: true, username: 'my_trix_bot' }));
  await flush();
  await flush();

  document.getElementById('allowed_users').value = '555';
  document.getElementById('allowed_users').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const userReq = takePending('/api/check/telegram_user', 'POST');
  if (!userReq) throw new Error('expected /api/check/telegram_user request');
  userReq.resolve(resOk({ ok: true, name: 'Иван <b>Hacker</b>', username: 'ivanpetrov' }));
  await flush();
  await flush();

  const noteEl = document.getElementById('telegram-user-note');
  const afterPositive = {
    hidden: noteEl.hidden,
    text: noteEl.textContent,
    hasElementChildren: noteEl.querySelectorAll('*').length > 1, // the one <b> wrapper this code creates itself is fine — anything beyond it means the payload got parsed as markup
    containsRawAngleBrackets: noteEl.innerHTML.indexOf('<b>Hacker</b>') === -1, // the LITERAL payload substring must never appear unescaped in the rendered markup
  };

  // Editing the id must clear the note immediately (before any new
  // network round trip), and a NEGATIVE lookup (e.g. the user hasn't
  // pressed "Старт" yet) must stay silent, not paint an error.
  document.getElementById('allowed_users').value = '556';
  document.getElementById('allowed_users').dispatchEvent(new window.Event('input', { bubbles: true }));
  const afterInputCleared = { hidden: noteEl.hidden };
  document.getElementById('allowed_users').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const userReq2 = takePending('/api/check/telegram_user', 'POST');
  if (!userReq2) throw new Error('expected second /api/check/telegram_user request');
  userReq2.resolve(resOk({ ok: false }));
  await flush();
  await flush();
  const afterNegative = { hidden: noteEl.hidden, text: noteEl.textContent };

  // A comma-separated (ambiguous) value must never even attempt a lookup.
  document.getElementById('allowed_users').value = '556,557';
  document.getElementById('allowed_users').dispatchEvent(new window.Event('input', { bubbles: true }));
  document.getElementById('allowed_users').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const multiReq = takePending('/api/check/telegram_user', 'POST');

  return {
    afterPositive: afterPositive,
    afterInputCleared: afterInputCleared,
    afterNegative: afterNegative,
    multiIdRequestFired: !!multiReq,
  };
}

// Finding 5 (review 2026-08-26, owner-approved fix): the REVERSE order
// scenarioTelegramUserNote above doesn't cover — id typed and blurred
// WHILE the token check is still in flight, not after. Root cause: the
// retry call used to live inside renderTelegramVerdict() itself, which
// reads state.telegramCheck.ok as its precondition — but the CALLER
// (runTelegramCheck()'s `.then`) only assigns state.telegramCheck AFTER
// renderTelegramVerdict() returns, so the retry always saw the token
// field's own "input" handler having just nulled state.telegramCheck.
// Realistic repro: type the token, blur it (check A starts), immediately
// type+blur the id field before A resolves (maybeRunTelegramUserCheck()
// sees no confirmed token yet — same honest silence as always), THEN let
// the token check land with ok:true. Before the fix, the note would
// never appear until the client touched the id field again; after it,
// the id lookup fires automatically the moment the token is confirmed.
async function scenarioTelegramUserNoteRetryAfterLateToken() {
  await boot();
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const tokenReq = takePending('/api/check/telegram', 'POST');
  if (!tokenReq) throw new Error('expected /api/check/telegram request');

  // Id typed and left before the token check above has resolved.
  document.getElementById('allowed_users').value = '555';
  document.getElementById('allowed_users').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const earlyReq = takePending('/api/check/telegram_user', 'POST');
  const noteHiddenWhileTokenInFlight = document.getElementById('telegram-user-note').hidden;

  tokenReq.resolve(resOk({ ok: true, username: 'my_trix_bot' }));
  await flush();
  await flush();
  const retryReq = takePending('/api/check/telegram_user', 'POST');
  if (retryReq) {
    retryReq.resolve(resOk({ ok: true, name: 'Иван', username: 'ivanpetrov' }));
    await flush();
    await flush();
  }

  return {
    earlyLookupFired: !!earlyReq,
    noteHiddenWhileTokenInFlight: noteHiddenWhileTokenInFlight,
    retryLookupFired: !!retryReq,
    noteHiddenAfterRetry: document.getElementById('telegram-user-note').hidden,
    verdictText: document.getElementById('telegram-verdict').textContent,
  };
}

// Owner feedback п.3 (live VM walkthrough): "я ввёл токен, он проверил,
// всё окей — почему, когда я ввожу Telegram id, он снова начинает
// проверять токен?" Root cause: #telegram_token carried an unguarded
// "change" AND "blur" listener, each independently issuing a live check.
// Tabbing out of the field fires both, back to back, in the same tick —
// this scenario reproduces exactly that (dispatching both events with no
// `await` between them, the same order a real Tab key produces) and
// counts how many /api/check/telegram requests actually went out.
async function scenarioTelegramTabOutDoesNotDoubleFire() {
  await boot();
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('telegram_token').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const requests = fetchStub.pending.filter((p) => p.url === '/api/check/telegram' && p.method === 'POST');
  return { requestCount: requests.length };
}

// Same double-fire class, same fix shape, for the "parity pair" the task
// asks to check: #provider_api_key's own "change"/"blur" listeners.
async function scenarioProviderKeyTabOutDoesNotDoubleFire() {
  await boot();
  const moreLink = document.querySelector('#provider_group .more a');
  if (moreLink) moreLink.click();
  const apiKeyGroup = catalog.provider_groups.find(
    (g) => g.variants.length === 1 && g.variants[0].kind === 'api_key'
  );
  if (!apiKeyGroup) throw new Error('need at least one single-variant api_key group in the real catalog');
  const row = document.querySelector('.p[data-group-id="' + apiKeyGroup.group_id + '"]');
  if (!row) throw new Error('row not found for group_id ' + apiKeyGroup.group_id);
  row.click();
  await flush();

  document.getElementById('provider_api_key').value = 'KEY_A';
  document.getElementById('provider_api_key').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.getElementById('provider_api_key').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const requests = fetchStub.pending.filter((p) => p.url === '/api/check/key' && p.method === 'POST');
  return { requestCount: requests.length };
}

// Finding 3: type a key for provider A (check A in flight), switch to
// provider B before the response lands, THEN let A's answer arrive.
async function scenarioKeyRace() {
  await boot();
  const singleApiKeyGroups = catalog.provider_groups.filter(
    (g) => g.variants.length === 1 && g.variants[0].kind === 'api_key'
  );
  if (singleApiKeyGroups.length < 2) throw new Error('need at least 2 single-variant api_key groups in the real catalog');
  const groupA = singleApiKeyGroups[0];
  const groupB = singleApiKeyGroups[1];

  // Both rows must exist in the DOM regardless of "recommended" status —
  // renderProviderGroupOptions() only renders non-recommended groups once
  // "Показать остальные N" has been expanded.
  const moreLink = document.querySelector('#provider_group .more a');
  if (moreLink) moreLink.click();

  const rowA = document.querySelector('.p[data-group-id="' + groupA.group_id + '"]');
  if (!rowA) throw new Error('row A not found for group_id ' + groupA.group_id);
  rowA.click();

  document.getElementById('provider_api_key').value = 'KEY_A';
  document.getElementById('provider_api_key').dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  const keyReqA = takePending('/api/check/key', 'POST');
  if (!keyReqA) throw new Error('expected /api/check/key request for group A');

  // Picking A collapsed the picker down to just A's own row (owner
  // feedback, this pass — see renderProviderGroupOptions()'s
  // providerPickerOpen branch): reopen it via "Выбрать другого
  // провайдера" before B's row is reachable again. providerListExpanded
  // stayed true from the earlier "Показать остальные" click, so the
  // reopened list already includes every group — no second expand click
  // needed.
  const changeLink = document.querySelector('#provider_group .more a');
  if (!changeLink) throw new Error('expected a "Выбрать другого провайдера" link after picking group A');
  changeLink.click();

  const rowB = document.querySelector('.p[data-group-id="' + groupB.group_id + '"]');
  if (!rowB) throw new Error('row B not found for group_id ' + groupB.group_id);
  rowB.click();
  await flush();

  keyReqA.resolve(resOk({ checked: true, reachable: true, ok: true, message: '' }));
  await flush();
  await flush();
  await flush();

  return {
    keyVerdictHidden: document.getElementById('key-verdict').hidden,
    keyVerdictText: document.getElementById('key-verdict').textContent,
    keyFieldValue: document.getElementById('provider_api_key').value,
    pendingAfter: fetchStub.pending.map((p) => p.url),
    groupA: groupA.group_id,
    groupB: groupB.group_id,
  };
}

// Finding 4: a returning client's saved token never echoes into the field
// (secrets are never echoed) — blur on the empty field (e.g. from the
// client's own click on "Далее") must not paint a false error.
async function scenarioSavedTokenBlur() {
  await boot();
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  return {
    verdictHidden: document.getElementById('telegram-verdict').hidden,
    verdictText: document.getElementById('telegram-verdict').textContent,
  };
}

// Owner feedback п.2 (live VM walkthrough): "до того как я не ввёл ничего,
// ничего не проверять не надо" — blur on a genuinely empty, first-run
// #telegram_token (no saved token at all) must stay silent, same as the
// saved-token case above; the ONE place that still has to catch "empty and
// never saved" is "Далее" itself (the client's actual attempt to leave the
// step), which must keep showing the real "Вставьте токен бота" prompt and
// refuse to advance.
async function scenarioEmptyTokenBlurThenNext() {
  await boot();
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const afterBlur = {
    verdictHidden: document.getElementById('telegram-verdict').hidden,
    verdictText: document.getElementById('telegram-verdict').textContent,
  };

  document.getElementById('step-3-next').click();
  await flush();
  const afterNextClick = {
    step: currentStep(),
    verdictHidden: document.getElementById('telegram-verdict').hidden,
    verdictText: document.getElementById('telegram-verdict').textContent,
  };

  return { afterBlur: afterBlur, afterNextClick: afterNextClick };
}

// Finding 7 (review 2026-08-26, owner-approved fix): a stale (superseded)
// response landing after a NEWER check has already started must not
// clobber the "something is in flight" guard the newer check owns.
// Repro (matches the review's own reproduction): type token A, blur (check
// A starts), edit to token B and blur again (check B starts, A is now
// stale/superseded), then let A's late answer land. Before the fix,
// telegramCheckSeqInFlight was unconditionally reset to -1 the instant
// EITHER response arrived, so a further blur (the client tabbing through
// again while B is still genuinely in flight) saw "nothing in flight" and
// fired a redundant THIRD request for token B on top of the one already
// running.
async function scenarioTelegramStaleResponseDoesNotClobberInFlightGuard() {
  await boot();
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const reqA = takePending('/api/check/telegram', 'POST');
  if (!reqA) throw new Error('expected check A');

  document.getElementById('telegram_token').value = 'TOKEN_B';
  document.getElementById('telegram_token').dispatchEvent(new window.Event('input', { bubbles: true }));
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const reqB = takePending('/api/check/telegram', 'POST');
  if (!reqB) throw new Error('expected check B');

  // A's late, superseded answer lands while B is still genuinely in flight.
  reqA.resolve(resOk({ ok: true, username: 'bot_of_TOKEN_A' }));
  await flush();
  await flush();

  // A further blur (unchanged token B, still in flight) must NOT fire a
  // redundant third request.
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const reqC = takePending('/api/check/telegram', 'POST');

  return {
    redundantThirdProbeFired: !!reqC,
    fieldValueStillTokenB: document.getElementById('telegram_token').value === 'TOKEN_B',
  };
}

// Finding 6 (review 2026-08-26, owner-approved fix): a real MOUSE click on
// "Далее" fires blur on #proxy BEFORE the click event itself
// (mousedown -> blur -> click). If the client had just finished typing, the
// blur handler sees a still-pending debounce and runs an IMMEDIATE check;
// the click handler's own unconditional runProxyCheck() call then fires a
// SECOND full round trip through the client's proxy for the exact same
// value. Dispatched here the same order a real click produces (blur, then
// the click — jsdom's synthetic .click() doesn't itself fire a real blur
// on whatever else had focus, so this dispatches it explicitly).
async function scenarioProxyBlurThenClickDoesNotDoubleFire() {
  await boot();
  document.getElementById('proxy').value = 'socks5://u:p@host:1080';
  document.getElementById('proxy').dispatchEvent(new window.Event('input', { bubbles: true }));
  await flush();
  document.getElementById('proxy').dispatchEvent(new window.Event('blur', { bubbles: true }));
  document.getElementById('step-2-next').click();
  await flush();
  const requests = fetchStub.pending.filter((p) => p.url === '/api/check/proxy' && p.method === 'POST');
  return { requestCount: requests.length };
}

// Finding 8 (review 2026-08-26, owner-approved fix): telegramUserLastCheckedKey
// is set BEFORE the fetch (the dedup guard), but a genuine network failure
// (OUR OWN request never completing — never a definite Telegram answer) must
// not stick that key — without resetting it, a client whose lookup hit a
// transient error could never retry the SAME id again without first editing
// the field away and back.
async function scenarioTelegramUserLookupNetworkFailureAllowsRetry() {
  await boot();
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const tokenReq = takePending('/api/check/telegram', 'POST');
  tokenReq.resolve(resOk({ ok: true, username: 'my_trix_bot' }));
  await flush();
  await flush();

  document.getElementById('allowed_users').value = '555';
  document.getElementById('allowed_users').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const userReq1 = takePending('/api/check/telegram_user', 'POST');
  if (!userReq1) throw new Error('expected first telegram_user lookup');
  userReq1.reject(new Error('network down'));
  await flush();
  await flush();

  // Same id, blurred again (nothing edited in between) — must retry, not
  // be silently skipped by the dedup key a failed attempt left behind.
  document.getElementById('allowed_users').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const userReq2 = takePending('/api/check/telegram_user', 'POST');

  return { retryFiredAfterNetworkFailure: !!userReq2 };
}

// Finding 12 (review 2026-08-26, owner-approved fix): both the Telegram
// token check and the provider key check run THROUGH whatever proxy is
// typed on step 2 — a verdict earned through the OLD proxy must not keep
// blocking a re-check (via maybeRunTelegramCheck()'s `state.telegramCheck`
// guard / maybeRunProviderKeyCheck()'s `keyCheckSettled` guard) once the
// client changes the proxy and comes back.
async function scenarioProxyChangeResetsTokenAndKeyVerdicts() {
  await boot();
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const tokenReq = takePending('/api/check/telegram', 'POST');
  tokenReq.resolve(resOk({ ok: true, username: 'my_trix_bot' }));
  await flush();
  await flush();

  const apiKeyGroup = catalog.provider_groups.find(
    (g) => g.variants.length === 1 && g.variants[0].kind === 'api_key'
  );
  if (!apiKeyGroup) throw new Error('need at least one single-variant api_key group in the real catalog');
  const moreLink = document.querySelector('#provider_group .more a');
  if (moreLink) moreLink.click();
  const row = document.querySelector('.p[data-group-id="' + apiKeyGroup.group_id + '"]');
  if (!row) throw new Error('row not found for group_id ' + apiKeyGroup.group_id);
  row.click();

  document.getElementById('provider_api_key').value = 'KEY_A';
  document.getElementById('provider_api_key').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const keyReq = takePending('/api/check/key', 'POST');
  if (!keyReq) throw new Error('expected /api/check/key request');
  keyReq.resolve(resOk({ checked: true, reachable: true, ok: true }));
  await flush();
  await flush();

  const beforeProxyEdit = {
    telegramVerdictHidden: document.getElementById('telegram-verdict').hidden,
    keyVerdictHidden: document.getElementById('key-verdict').hidden,
  };

  document.getElementById('proxy').value = 'socks5://u:p@host:1080';
  document.getElementById('proxy').dispatchEvent(new window.Event('input', { bubbles: true }));
  await flush();

  const afterProxyEdit = {
    telegramVerdictHidden: document.getElementById('telegram-verdict').hidden,
    keyVerdictHidden: document.getElementById('key-verdict').hidden,
  };

  // Blurring each field again (values unchanged) must re-fire a check now
  // that the OLD proxy's verdict was invalidated.
  document.getElementById('telegram_token').dispatchEvent(new window.Event('blur', { bubbles: true }));
  document.getElementById('provider_api_key').dispatchEvent(new window.Event('blur', { bubbles: true }));
  await flush();
  const secondTokenReq = takePending('/api/check/telegram', 'POST');
  const secondKeyReq = takePending('/api/check/key', 'POST');

  return {
    telegramVerdictVisibleBeforeProxyEdit: beforeProxyEdit.telegramVerdictHidden === false,
    keyVerdictVisibleBeforeProxyEdit: beforeProxyEdit.keyVerdictHidden === false,
    telegramVerdictHiddenAfterProxyEdit: afterProxyEdit.telegramVerdictHidden,
    keyVerdictHiddenAfterProxyEdit: afterProxyEdit.keyVerdictHidden,
    secondTokenCheckFired: !!secondTokenReq,
    secondKeyCheckFired: !!secondKeyReq,
  };
}

// Owner feedback п.1 (live VM walkthrough): step 2 no longer autochecks the
// instant it is entered — boot() answers no /api/check/proxy request at all
// any more (see its own comment). This scenario establishes a genuine
// "недоступен"/"нужен прокси" state the honest way (a real "Далее" click),
// then proves three things about the "input" listener that replaced the old
// always-check-on-entry behavior: (1) a stale bad verdict does not survive
// an edit (Finding 5's original bug, still guarded against), (2) nothing is
// checked yet the instant the client starts typing, and (3) the debounced
// real check DOES eventually fire once they stop typing — the owner's other
// half of "проверять... когда что-то там вставилось".
async function scenarioProxyInputResets() {
  await boot();
  document.getElementById('step-2-next').click();
  await flush();
  const badReq = takePending('/api/check/proxy', 'POST');
  if (!badReq) throw new Error('expected /api/check/proxy request from "Далее"');
  badReq.resolve(resOk({
    telegram: false,
    via_proxy: { 'openai-api': false, anthropic: false, openrouter: false },
    direct: { deepseek: true, zai: true, gemini: true },
    providers: {},
  }));
  await flush();
  await flush();
  const before = document.getElementById('proxy-verdict').textContent;

  document.getElementById('proxy').value = 'socks5://u:p@host:1080';
  document.getElementById('proxy').dispatchEvent(new window.Event('input', { bubbles: true }));
  await flush();
  const afterInput = document.getElementById('proxy-verdict').textContent;
  const afterInputClass = document.getElementById('proxy-verdict').className;
  const tooSoon = takePending('/api/check/proxy', 'POST');

  // Real timers — this drives an actual node subprocess, not a mocked
  // clock — long enough to clear the debounce window page.py schedules.
  await new Promise((resolve) => setTimeout(resolve, 700));
  const debouncedReq = takePending('/api/check/proxy', 'POST');

  return {
    before: before,
    afterInput: afterInput,
    afterInputClass: afterInputClass,
    tooSoonPending: !!tooSoon,
    debouncedRequestFired: !!debouncedReq,
  };
}

// Finding 4 (review 2026-08-26, owner-approved fix): a RETURNING client
// (saved TELEGRAM_BOT_TOKEN — the catalog this scenario is given sets
// current.telegram_token.is_set true) gets a fully clickable progress bar
// and can jump straight to step 4 ("Провайдер") without ever running step
// 2's own explicit "Далее" check — the ONLY path that used to populate
// state.providerReachabilityByGroup at all, now that step 2 no longer
// autochecks on entry (scenarioProxyInputResets's own comment above).
// Proves: (1) the map is genuinely empty before step 4 is ever entered —
// no tags, matching reachabilityTag()'s own "null map -> silence, never a
// fabricated tag" contract, (2) landing on step 4 fires exactly one
// background /api/check/proxy request, (3) resolving it live-refreshes
// the "нужен прокси" tags without navigating away or touching step 2's
// own (still-hidden) verdict UI, and (4) re-entering step 4 a second time
// does NOT re-fire the request — the one-time-backfill guard holds.
async function scenarioStep4ReturnModeBackfillsReachability() {
  await boot();
  const unreachableGroup = catalog.provider_groups[0];
  if (!unreachableGroup) throw new Error('need at least one provider group in the real catalog');

  const providerNavItem = document.querySelectorAll('.step-item')[2]; // "Провайдер" — see STEPS
  const beforeClick = {
    offTags: document.querySelectorAll('.tag.off').length,
    pendingProxyReq: !!fetchStub.pending.find((p) => p.url === '/api/check/proxy'),
  };
  providerNavItem.click();
  await flush();

  const proxyReq = takePending('/api/check/proxy', 'POST');
  const afterClickStep = currentStep();
  const step2Hidden = document.querySelector('[data-step="2"]').hidden;

  if (proxyReq) {
    const providers = {};
    providers[unreachableGroup.group_id] = false;
    proxyReq.resolve(resOk({ telegram: true, via_proxy: {}, direct: {}, providers: providers }));
  }
  await flush();
  await flush();

  const afterFillOffTags = document.querySelectorAll('.tag.off').length;

  // Leave step 4 and come back — same clickable progress-bar path — to
  // prove the guard is a ONE-TIME backfill, not a check-on-every-entry.
  document.querySelectorAll('.step-item')[0].click(); // back to "Прокси"
  await flush();
  document.querySelectorAll('.step-item')[2].click(); // "Провайдер" again
  await flush();
  const secondEntryProxyReq = takePending('/api/check/proxy', 'POST');

  return {
    offTagsBeforeAnyEntry: beforeClick.offTags,
    proxyRequestPendingBeforeEntry: beforeClick.pendingProxyReq,
    stepAfterNavClick: afterClickStep,
    backgroundProxyRequestFired: !!proxyReq,
    step2StillHiddenAfterBackfill: step2Hidden,
    offTagsAfterFill: afterFillOffTags,
    secondEntryFiredAnotherRequest: !!secondEntryProxyReq,
  };
}

// Owner feedback (this pass): picking a provider group used to leave the
// ENTIRE list rendered underneath — recommended rows, every expanded
// "Показать остальные" row, all of it — pushing the key/model fields the
// pick just revealed far down the page. renderProviderGroupOptions() now
// collapses to just the chosen row + a "Выбрать другого провайдера" link
// the instant a group is picked, and reopens the full list (still
// highlighting the same choice) on demand.
async function scenarioProviderPickCollapses() {
  await boot();
  const beforeRows = document.querySelectorAll('#provider_group .p').length;
  const firstRow = document.querySelector('#provider_group .p');
  if (!firstRow) throw new Error('expected at least one recommended provider row');
  const groupId = firstRow.dataset.groupId;
  firstRow.click();
  await flush();

  const afterPickRows = Array.from(document.querySelectorAll('#provider_group .p'));
  const changeLink = document.querySelector('#provider_group .more a');

  // Reopening must restore the full list AND keep the earlier pick
  // highlighted — not silently forget it.
  changeLink.click();
  await flush();
  const afterReopenRows = document.querySelectorAll('#provider_group .p').length;
  const reopenedChosenRow = document.querySelector('#provider_group .p[data-group-id="' + groupId + '"]');

  return {
    groupId: groupId,
    beforeRows: beforeRows,
    afterPickRowCount: afterPickRows.length,
    afterPickRowGroupId: afterPickRows.length ? afterPickRows[0].dataset.groupId : null,
    afterPickRowSelected: afterPickRows.length ? afterPickRows[0].classList.contains('sel') : null,
    changeLinkText: changeLink ? changeLink.textContent : null,
    afterReopenRows: afterReopenRows,
    reopenedChosenRowSelected: reopenedChosenRow ? reopenedChosenRow.classList.contains('sel') : null,
  };
}

// Owner feedback (this pass): step 5's six category rows must behave like
// an accordion (opening one folds every other) and must never survive a
// step change still open ("наслоение" — a still-open "Браузер" row
// bleeding into whatever the client does next).
async function scenarioAdvancedRowsCollapse() {
  await boot();
  const step5Item = Array.from(document.querySelectorAll('#progress-bar .step-item'))
    .find((it) => it.textContent.indexOf('Дополнительно') !== -1);
  if (!step5Item) throw new Error('step 5 nav item not found');
  step5Item.click();
  await flush();
  if (currentStep() !== 5) throw new Error('expected step 5, got ' + currentStep());

  const browserRow = document.querySelector('#advanced-browser .row');
  const browserBody = document.querySelector('#advanced-browser .row-body');
  const searchRow = document.querySelector('#advanced-search .row');
  const searchBody = document.querySelector('#advanced-search .row-body');
  if (!browserRow || !searchRow) throw new Error('expected browser/search collapsible rows');

  browserRow.click();
  await flush();
  const afterOpenBrowser = {
    browserOpen: browserRow.classList.contains('open'),
    browserBodyHidden: browserBody.hidden,
  };

  searchRow.click();
  await flush();
  const afterOpenSearch = {
    browserOpen: browserRow.classList.contains('open'),
    browserBodyHidden: browserBody.hidden,
    searchOpen: searchRow.classList.contains('open'),
    searchBodyHidden: searchBody.hidden,
  };

  document.getElementById('step-5-next').click();
  await flush();
  const afterLeave = {
    step: currentStep(),
    searchOpen: searchRow.classList.contains('open'),
    searchBodyHidden: searchBody.hidden,
  };

  return { afterOpenBrowser: afterOpenBrowser, afterOpenSearch: afterOpenSearch, afterLeave: afterLeave };
}

// Owner feedback (this pass): a 422 from "Готово" must leave the client
// looking at something that actually explains the failure — not just a
// navigation to the right step with an invisible/off-screen message. This
// drives the REAL doSubmit() 422 branch (click #done, resolve
// /api/submit with a 422) for two shapes: a FIELD_MAP-mapped path
// (telegram_token) and a PATH_STEP-only path with no dedicated element
// (tool_env). scrollIntoView is stubbed to record what it was called on
// (jsdom's own implementation is a documented no-op).
async function scenarioSubmit422Visibility(errors) {
  await boot();
  const calls = [];
  window.HTMLElement.prototype.scrollIntoView = function () {
    calls.push(this.id || ('.' + Array.from(this.classList || []).join('.')));
  };

  document.getElementById('done').click();
  await flush();
  const submitReq = takePending('/api/submit', 'POST');
  if (!submitReq) throw new Error('expected /api/submit request');
  submitReq.resolve({ status: 422, json: () => Promise.resolve({ errors: errors }) });
  await flush();
  await flush();
  await flush();

  return {
    step: currentStep(),
    scrollCalls: calls,
    telegramErrHidden: document.getElementById('err_telegram_token').hidden,
    telegramErrText: document.getElementById('err_telegram_token').textContent,
    timezoneErrHidden: document.getElementById('err_timezone').hidden,
    timezoneErrText: document.getElementById('err_timezone').textContent,
    formErrorText: document.getElementById('form-error').textContent,
  };
}

// Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"): there is
// no "Установить" button any more — installing a chosen tool is now the
// final "Готово" step's own submit-time stage. Two things this scenario
// proves by actually EXECUTING doSubmit(), not by reading source: (1) the
// "Устанавливаем инструменты" stage li is shown BEFORE the request is even
// sent, decided from state.tools + the live <select> value alone (no
// server round-trip needed to know a stage is coming) — via
// pendingToolInstallNames()/setStageOrder() — whenever the currently
// selected row still needs installing; (2) a failed install the server
// reports back (tool_install_failures) renders on the SUCCESS screen as an
// honest per-tool note, without turning the submission itself into a
// failure (ok stays true, #success — not #progress/#form-error — is what
// shows).
async function scenarioInstallStagePending() {
  await boot();
  document.getElementById('done').click();
  await flush();
  const submitReq = takePending('/api/submit', 'POST');
  if (!submitReq) throw new Error('expected /api/submit request');

  const stageInstallHiddenBeforeResponse = document.getElementById('stage-install').hidden;

  submitReq.resolve(resOk({
    ok: true,
    bot_username: 'trixbot',
    key_checked: true,
    tool_install_failures: [
      { name: 'Local Browser', message: 'На этой машине не найден Node.js.' },
    ],
  }));
  await flush();
  await flush();
  await flush();

  const notice = document.getElementById('tool-install-notice');
  return {
    stageInstallHiddenBeforeResponse: stageInstallHiddenBeforeResponse,
    successHidden: document.getElementById('success').hidden,
    formErrorText: document.getElementById('form-error').textContent,
    installNoticeHidden: notice.hidden,
    installNoticeText: notice.textContent,
  };
}

// Companion to the scenario above: when the currently selected row is
// ALREADY installed (catalog fixture's own "installed": true — see the
// Python test), nothing needs installing THIS submission — the stage must
// stay hidden the whole time, and a success response with an EMPTY
// tool_install_failures must leave the notice hidden too (never an empty,
// visible box).
async function scenarioInstallStageAbsent() {
  await boot();
  document.getElementById('done').click();
  await flush();
  const submitReq = takePending('/api/submit', 'POST');
  if (!submitReq) throw new Error('expected /api/submit request');

  const stageInstallHiddenBeforeResponse = document.getElementById('stage-install').hidden;

  submitReq.resolve(resOk({ ok: true, bot_username: 'trixbot', key_checked: true, tool_install_failures: [] }));
  await flush();
  await flush();
  await flush();

  return {
    stageInstallHiddenBeforeResponse: stageInstallHiddenBeforeResponse,
    stageInstallHiddenAfterSuccess: document.getElementById('stage-install').hidden,
    installNoticeHidden: document.getElementById('tool-install-notice').hidden,
  };
}

// Finding 9 (review 2026-08-26, owner-approved fix): pendingToolInstallNames()
// only ever checked the "web" category — app.py's own server-side twin
// (_pending_tool_installs) already checks "web_extract" too (search's
// split-off sibling — see that function's own docstring). Picking a
// "Чтение страниц" ("web_extract") row with a pending post_setup hook
// must show the install stage BEFORE the request is sent, same honesty
// contract as browser/tts/image_gen/video_gen already get.
async function scenarioInstallStagePendingForWebExtract() {
  await boot();
  var select = document.getElementById('extract_choice');
  if (!select) throw new Error('expected #extract_choice to exist');
  select.value = 'tavily';
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();

  document.getElementById('done').click();
  await flush();
  const submitReq = takePending('/api/submit', 'POST');
  if (!submitReq) throw new Error('expected /api/submit request');

  const stageInstallHiddenBeforeResponse = document.getElementById('stage-install').hidden;

  submitReq.resolve(resOk({ ok: true, bot_username: 'trixbot', key_checked: true, tool_install_failures: [] }));
  await flush();
  await flush();
  await flush();

  return { stageInstallHiddenBeforeResponse: stageInstallHiddenBeforeResponse };
}

// Finding 11 (review 2026-08-26, owner-approved fix): two "web" rows
// sharing one web_backend ("firecrawl" — cloud vs self-hosted, see the
// fixture's own comment) used to let rowByValue's unguarded overwrite
// (settings-panel rendering) disagree with searchEnvPayload()'s
// `rows.filter(...)[0]` (submission) — the settings panel showed the
// LAST row's field (self-hosted, FIRECRAWL_API_URL) while the payload
// silently submitted the FIRST row's key (FIRECRAWL_API_KEY). Both must
// now agree — whichever row is FIRST wins for both.
async function scenarioFirecrawlDuplicateBackendSubmitsConsistentEnv() {
  await boot();
  var select = document.getElementById('search_choice');
  if (!select) throw new Error('expected #search_choice to exist');
  select.value = 'firecrawl';
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();

  var input = document.getElementById('search_env_value');
  var renderedFieldType = input ? input.type : null;
  if (input) {
    input.value = 'test-value';
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  }
  await flush();

  document.getElementById('done').click();
  await flush();
  const submitReq = takePending('/api/submit', 'POST');
  if (!submitReq) throw new Error('expected /api/submit request');

  return {
    renderedFieldType: renderedFieldType,
    submittedSearchEnvKey: (submitReq.body && submitReq.body.search_env && submitReq.body.search_env.key) || null,
  };
}

// Owner ruling after looking at the live VM: choosing Camofox in the
// "Браузер" row must NOT show the server address anywhere — not as an
// input, not as text. A bare local port means nothing to a non-technical
// client; Camofox just works once picked. Mutation-tested companion to
// test_camofox_address_is_never_asked_for_or_shown_but_still_submitted
// (which only proves this about the page's *source* — that string never
// appears in the HTML/JS text). It does not prove that choosing Camofox
// in a live DOM actually stays silent — a mutation that reintroduces the
// note (or a differently-worded one) would still leave every source-text
// assertion green. This test drives the real render->expand->select
// sequence and reads the live DOM.
async function scenarioCamofoxAddressHidden() {
  await boot();
  const step5Item = Array.from(document.querySelectorAll('#progress-bar .step-item'))
    .find((it) => it.textContent.indexOf('Дополнительно') !== -1);
  if (!step5Item) throw new Error('step 5 nav item not found');
  step5Item.click();
  await flush();
  if (currentStep() !== 5) throw new Error('expected step 5, got ' + currentStep());

  const browserRow = document.querySelector('#advanced-browser .row');
  if (!browserRow) throw new Error('expected browser collapsible row');
  browserRow.click();
  await flush();

  const select = document.getElementById('browser_choice');
  if (!select) throw new Error('expected #browser_choice select');
  select.value = 'camofox';
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();

  const settings = document.querySelector('#advanced-browser .sub-settings');
  return {
    selectedValue: select.value,
    camofoxUrlInputPresent: !!document.getElementById('camofox_url'),
    settingsText: settings ? settings.textContent : null,
  };
}

// Spec 8, §8.3: there is no cookie session any more — a 401/403 on a
// background request AFTER the page has already loaded means the
// machine's Basic-auth credentials changed, or a request failed the
// Host/Origin CSRF guard. handleAuthLost() (jsonFetch()'s own 401/403
// branch) must show the lost-access message in #form-error, leave the
// current step's markup alone (no rebuild, no navigation to some other
// screen), and do the same thing cleanly a SECOND time — never
// concatenating a duplicate message onto the first one. This exercises
// the real callback, not a source-text match, which is exactly what a
// harmless refactor (switch statement, extracted comparison, different
// quoting) could no longer fake past.
async function scenarioTelegramVerdictIsReadableBeforeAdvancing() {
  await boot();
  document.getElementById('step-2-next').click();
  await flush();
  const proxyReq2 = takePending('/api/check/proxy', 'POST');
  if (proxyReq2) {
    proxyReq2.resolve(resOk({ telegram: true, via_proxy: {}, direct: {}, providers: {} }));
    await flush();
    await flush();
  }
  if (currentStep() !== 3) throw new Error('expected step 3, got ' + currentStep());

  // Клиент печатает токен и сразу жмёт «Далее» — поле не теряло фокус,
  // устоявшегося вердикта нет.
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('step-3-next').click();
  await flush();
  const req = takePending('/api/check/telegram', 'POST');
  if (!req) throw new Error('expected /api/check/telegram request');
  req.resolve(resOk({ ok: true, username: 'my_trix_bot' }));
  await flush();
  await flush();

  const verdict = document.getElementById('telegram-verdict');
  const afterFirstClick = {
    step: currentStep(),
    verdictHidden: verdict.hidden,
    verdictText: (verdict.textContent || '').replace(/\s+/g, ' ').trim(),
    nextDisabled: document.getElementById('step-3-next').disabled,
  };

  // Второе нажатие — вердикт уже устоялся, уходим без новой проверки.
  document.getElementById('step-3-next').click();
  await flush();
  await flush();
  const secondRequest = takePending('/api/check/telegram', 'POST');

  return {
    afterFirstClick: afterFirstClick,
    stepAfterSecondClick: currentStep(),
    secondClickRefetched: !!secondRequest,
  };
}

async function scenarioAuthLostOnBackgroundRequest() {
  await boot();
  // boot() only answers step 2's own auto-check — actually advance to
  // step 3 for real (clicking "Далее" re-fires /api/check/proxy; answer
  // it the same way boot() did) so the scenario starts from a step the
  // client would actually be looking at, not step 2 by coincidence.
  document.getElementById('step-2-next').click();
  await flush();
  const proxyReq2 = takePending('/api/check/proxy', 'POST');
  if (proxyReq2) {
    proxyReq2.resolve(resOk({ telegram: true, via_proxy: {}, direct: {}, providers: {} }));
    await flush();
    await flush();
  }
  if (currentStep() !== 3) throw new Error('expected step 3, got ' + currentStep());

  // First background failure: /api/check/telegram answers 401.
  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('step-3-next').click();
  await flush();
  const reqA = takePending('/api/check/telegram', 'POST');
  if (!reqA) throw new Error('expected /api/check/telegram request');
  reqA.resolve({ status: 401, json: () => Promise.resolve({}) });
  await flush();
  await flush();

  const afterFirst = {
    step: currentStep(),
    mainHidden: document.getElementById('main').hidden,
    formErrorText: document.getElementById('form-error').textContent,
    loginFormPresent: !!document.getElementById('login-form'),
  };

  // Second background failure, same step, this time 403 (CSRF guard) —
  // must not double up the message, hide the form, or jump the wizard
  // anywhere.
  document.getElementById('telegram_token').value = 'TOKEN_B';
  document.getElementById('step-3-next').click();
  await flush();
  const reqB = takePending('/api/check/telegram', 'POST');
  if (!reqB) throw new Error('expected second /api/check/telegram request');
  reqB.resolve({ status: 403, json: () => Promise.resolve({}) });
  await flush();
  await flush();

  return {
    afterFirst: afterFirst,
    afterSecond: {
      step: currentStep(),
      mainHidden: document.getElementById('main').hidden,
      formErrorText: document.getElementById('form-error').textContent,
    },
  };
}

// Spec 8, §8.3.6: a locked-out IP gets 429 (HTML body, no
// WWW-Authenticate) — jsonFetch() must treat it the same honest way as
// 401/403 above (handleRateLimited()), not let res.json() choke on the
// HTML body and surface an endpoint-specific lie ("Токен недействителен.",
// etc.) instead of "too many attempts, wait". Mirrors
// scenarioAuthLostOnBackgroundRequest exactly, just with a 429 answer.
async function scenarioRateLimitedOnBackgroundRequest() {
  await boot();
  document.getElementById('step-2-next').click();
  await flush();
  const proxyReq2 = takePending('/api/check/proxy', 'POST');
  if (proxyReq2) {
    proxyReq2.resolve(resOk({ telegram: true, via_proxy: {}, direct: {}, providers: {} }));
    await flush();
    await flush();
  }
  if (currentStep() !== 3) throw new Error('expected step 3, got ' + currentStep());

  document.getElementById('telegram_token').value = 'TOKEN_A';
  document.getElementById('step-3-next').click();
  await flush();
  const reqA = takePending('/api/check/telegram', 'POST');
  if (!reqA) throw new Error('expected /api/check/telegram request');
  // 429's real body is HTML, not JSON (_rate_limited_body in app.py) — a
  // broken fix that still calls res.json() on this would reject with a
  // SyntaxError instead of the intended "rate_limited" Error.
  reqA.resolve({ status: 429, json: () => Promise.reject(new Error('body is not JSON')) });
  await flush();
  await flush();

  return {
    step: currentStep(),
    mainHidden: document.getElementById('main').hidden,
    formErrorText: document.getElementById('form-error').textContent,
  };
}

// Owner feedback (live walkthrough, п.1): "почему там chat GPT только по
// подписке, а не ещё через API?" — the client never registered
// #provider-auth-choice as the same kind of decision as the group picker
// above it. onProviderGroupChange() now builds the SAME clickable .prov .p
// cards renderProviderRow() does (a native radio's own .change event no
// longer exists at all — see that function's own comment). Proves, by
// actually clicking through the real catalog's "openai" group: (1) the
// choice appears and is NOT silently pre-selected on either variant
// (spec §7.2 still holds), (2) both api_key and device_code sub-blocks stay
// hidden until a card is actually picked, (3) picking one card shows its
// sub-block, hides the other, and highlights only that one card.
async function scenarioAuthChoiceCards() {
  await boot();
  const moreLink = document.querySelector('#provider_group .more a');
  if (moreLink) moreLink.click();
  const openaiRow = document.querySelector('.p[data-group-id="openai"]');
  if (!openaiRow) throw new Error('expected an "openai" group row in the real catalog');
  openaiRow.click();
  await flush();

  const authChoice = document.getElementById('provider-auth-choice');
  const cards = Array.from(document.querySelectorAll('#provider-auth-options .p'));
  const beforePick = {
    authChoiceHidden: authChoice.hidden,
    cardCount: cards.length,
    anySelected: cards.some((c) => c.classList.contains('sel')),
    apiBlockHidden: document.getElementById('provider-api-key-block').hidden,
    deviceBlockHidden: document.getElementById('provider-device-code-block').hidden,
  };

  const apiKeyCard = cards.find((c) => c.dataset.variantName === 'openai-api');
  if (!apiKeyCard) throw new Error('expected an "openai-api" card among the rendered cards');
  apiKeyCard.click();
  await flush();

  return {
    beforePick: beforePick,
    afterPick: {
      apiKeyCardSelected: apiKeyCard.classList.contains('sel'),
      otherCardSelected: cards.filter((c) => c !== apiKeyCard).some((c) => c.classList.contains('sel')),
      apiBlockHidden: document.getElementById('provider-api-key-block').hidden,
      deviceBlockHidden: document.getElementById('provider-device-code-block').hidden,
    },
  };
}

// Owner feedback (live walkthrough, п.2): "Сейчас настроено: auto..." —
// model.provider round-trips to the literal string "auto" after `hermes
// logout` (hermes_cli/auth.py's _reset_config_provider()) and providerRowFor
// finds no catalog row named "auto", so the OLD code printed that raw
// literal as if it meant something. Proves updateProviderCurrentHint()
// stays silent for "auto" and still reports a REAL configured provider
// honestly — a fix that just always returned "" would pass the first half
// and fail the second.
async function scenarioProviderCurrentHint() {
  await boot();
  return { hintText: document.getElementById('provider-current-hint').textContent };
}

// Owner feedback (live walkthrough, п.3): the signup hint used to repeat
// row.description_ru verbatim — text already shown a few lines up in the
// picked row's own .desc span — with the URL appended as bare text, never
// a real link. Drives a real single-variant group pick (catalog.__target__,
// set by the Python test to a row confirmed to carry BOTH a description_ru
// and a signup_url) and reads the actual rendered #provider-signup-hint.
async function scenarioSignupHintLinkOnly() {
  await boot();
  const target = catalog.__target__;
  const moreLink = document.querySelector('#provider_group .more a');
  if (moreLink) moreLink.click();
  const row = document.querySelector('.p[data-group-id="' + target + '"]');
  if (!row) throw new Error('expected a group row for ' + target);
  row.click();
  await flush();

  const hintEl = document.getElementById('provider-signup-hint');
  const link = hintEl.querySelector('a');
  return {
    hintText: hintEl.textContent,
    linkHref: link ? link.href : null,
    linkText: link ? link.textContent : null,
  };
}

// Owner feedback (live walkthrough, п.5): "Запасных провайдеров нет
// chatgpt?" — reads the REAL rendered <select id="fallback_name"> options
// (built from the live catalog, not a hand-picked stand-in) so the
// assertion is that no device_code provider's slug ever appears as an
// <option>, and reads #advanced-fallback's own text for the renamed
// heading and the honest OAuth-unsupported note.
async function scenarioFallbackBlockContents() {
  await boot();
  const select = document.getElementById('fallback_name');
  return {
    optionValues: Array.from(select.options).map((o) => o.value),
    blockText: document.getElementById('advanced-fallback').textContent,
  };
}

(async () => {
  try {
    let result;

// ---- Часовой пояс (спека 11) ------------------------------------------
//
// Каталог этих сценариев приходит из настоящего /api/form (см. питонову
// сторону), поэтому список поясов, current.timezone и cron_jobs — те же
// данные, что увидит живой клиент. Каждый сценарий диспетчеризует
// НАСТОЯЩИЕ события DOM: клик, change, input. Ни одна внутренняя функция
// страницы не вызывается напрямую — они и не экспортированы.

// Довести клиента до шага Telegram так же, как это делает человек: через
// «Далее» на шаге «Прокси». Сценарии пояса живут именно на третьем шаге,
// и проверять «остался на месте» имеет смысл только оттуда.
async function gotoTelegramStep() {
  await boot();
  document.getElementById('step-2-next').click();
  await flush();
  const proxyReq = takePending('/api/check/proxy', 'POST');
  if (proxyReq) {
    proxyReq.resolve(resOk({ telegram: true, via_proxy: {}, direct: {}, providers: {} }));
    await flush();
    await flush();
  }
  if (currentStep() !== 3) throw new Error('expected step 3, got ' + currentStep());
}

function timezoneSnapshot() {
  const sel = document.getElementById('timezone');
  const warn = document.getElementById('timezone-warning');
  return {
    step: currentStep(),
    value: sel ? sel.value : null,
    optionCount: sel ? sel.options.length : 0,
    errHidden: document.getElementById('err_timezone').hidden,
    errText: document.getElementById('err_timezone').textContent,
    warningHidden: warn ? warn.hidden : null,
    warningText: warn ? warn.textContent : '',
  };
}

// Токен в этих каталогах уже сохранён (current.telegram_token.is_set),
// поэтому "Далее" на шаге Telegram идёт быстрым путём и не ходит в сеть:
// единственное, что может его задержать, — наше новое поле.
async function scenarioTimezoneRequiredBlocksNext() {
  await gotoTelegramStep();
  document.getElementById('step-3-next').click();
  await flush();
  return timezoneSnapshot();
}

async function scenarioTimezonePickAdvances() {
  await gotoTelegramStep();
  const sel = document.getElementById('timezone');
  sel.value = 'Europe/Moscow';
  sel.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  const afterPick = timezoneSnapshot();
  document.getElementById('step-3-next').click();
  await flush();
  return { afterPick: afterPick, after: timezoneSnapshot() };
}

async function scenarioTimezonePrefilledFromSavedAnswer() {
  await gotoTelegramStep();
  return timezoneSnapshot();
}

async function scenarioTimezoneChangeWarnsAndAckGatesNext() {
  await gotoTelegramStep();
  const before = timezoneSnapshot();
  const sel = document.getElementById('timezone');
  sel.value = 'Europe/Moscow';
  sel.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  const afterChange = timezoneSnapshot();

  document.getElementById('step-3-next').click();
  await flush();
  const afterBlockedNext = timezoneSnapshot();

  const ack = document.getElementById('timezone_ack');
  ack.checked = true;
  ack.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  document.getElementById('step-3-next').click();
  await flush();
  const afterAck = timezoneSnapshot();

  return {
    before: before,
    afterChange: afterChange,
    afterBlockedNext: afterBlockedNext,
    afterAck: afterAck,
  };
}

async function scenarioTimezoneChangeBackToSavedDropsWarning() {
  await gotoTelegramStep();
  const sel = document.getElementById('timezone');
  const saved = sel.value;
  sel.value = 'Europe/Moscow';
  sel.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  const changed = timezoneSnapshot();
  sel.value = saved;
  sel.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  return { changed: changed, back: timezoneSnapshot() };
}

async function scenarioTimezoneSearchFilters() {
  await gotoTelegramStep();
  const all = document.getElementById('timezone').options.length;
  const search = document.getElementById('timezone_search');
  search.value = 'Vladivostok';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  await flush();
  const sel = document.getElementById('timezone');
  const names = Array.from(sel.options).map(function (o) { return o.value; });
  return {
    allCount: all,
    filteredCount: sel.options.length,
    hasVladivostok: names.indexOf('Asia/Vladivostok') !== -1,
    hasMoscow: names.indexOf('Europe/Moscow') !== -1,
  };
}

async function scenarioTimezoneSearchKeepsTheChosenValue() {
  await gotoTelegramStep();
  const sel = document.getElementById('timezone');
  sel.value = 'Europe/Moscow';
  sel.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  const search = document.getElementById('timezone_search');
  search.value = 'Vladivostok';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  await flush();
  const whileFiltered = document.getElementById('timezone').value;
  search.value = '';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  await flush();
  return { whileFiltered: whileFiltered, afterClearing: document.getElementById('timezone').value };
}

async function scenarioTimezoneReachesThePayload() {
  await gotoTelegramStep();
  const sel = document.getElementById('timezone');
  sel.value = 'Asia/Vladivostok';
  sel.dispatchEvent(new window.Event('change', { bubbles: true }));
  await flush();
  document.getElementById('done').click();
  await flush();
  const submitReq = takePending('/api/submit', 'POST');
  if (!submitReq) throw new Error('expected /api/submit request');
  return { timezone: submitReq.body.timezone };
}

    if (scenario === 'telegram_verdict_readable') result = await scenarioTelegramVerdictIsReadableBeforeAdvancing();
    else if (scenario === 'telegram_race') result = await scenarioTelegramRace();
    else if (scenario === 'key_race') result = await scenarioKeyRace();
    else if (scenario === 'telegram_user_note') result = await scenarioTelegramUserNote();
    else if (scenario === 'telegram_user_note_retry_after_late_token') result = await scenarioTelegramUserNoteRetryAfterLateToken();
    else if (scenario === 'telegram_tab_out_no_double_fire') result = await scenarioTelegramTabOutDoesNotDoubleFire();
    else if (scenario === 'telegram_stale_response_no_clobber') result = await scenarioTelegramStaleResponseDoesNotClobberInFlightGuard();
    else if (scenario === 'proxy_blur_then_click_no_double_fire') result = await scenarioProxyBlurThenClickDoesNotDoubleFire();
    else if (scenario === 'telegram_user_lookup_network_failure_allows_retry') result = await scenarioTelegramUserLookupNetworkFailureAllowsRetry();
    else if (scenario === 'proxy_change_resets_token_and_key_verdicts') result = await scenarioProxyChangeResetsTokenAndKeyVerdicts();
    else if (scenario === 'provider_key_tab_out_no_double_fire') result = await scenarioProviderKeyTabOutDoesNotDoubleFire();
    else if (scenario === 'saved_token_blur') result = await scenarioSavedTokenBlur();
    else if (scenario === 'empty_token_blur_then_next') result = await scenarioEmptyTokenBlurThenNext();
    else if (scenario === 'step4_return_mode_backfills_reachability') result = await scenarioStep4ReturnModeBackfillsReachability();
    else if (scenario === 'provider_pick_collapses') result = await scenarioProviderPickCollapses();
    else if (scenario === 'advanced_rows_collapse') result = await scenarioAdvancedRowsCollapse();
    else if (scenario === 'camofox_address_hidden') result = await scenarioCamofoxAddressHidden();
    else if (scenario === 'submit_422_field_mapped') {
      result = await scenarioSubmit422Visibility({ telegram_token: 'Токен недействителен.' });
    } else if (scenario === 'submit_422_path_only') {
      result = await scenarioSubmit422Visibility({ tool_env: 'Неизвестный ключ инструмента.' });
    }
    else if (scenario === 'timezone_required_blocks_next') result = await scenarioTimezoneRequiredBlocksNext();
    else if (scenario === 'timezone_pick_advances') result = await scenarioTimezonePickAdvances();
    else if (scenario === 'timezone_prefilled') result = await scenarioTimezonePrefilledFromSavedAnswer();
    else if (scenario === 'timezone_change_warns') result = await scenarioTimezoneChangeWarnsAndAckGatesNext();
    else if (scenario === 'timezone_change_back') result = await scenarioTimezoneChangeBackToSavedDropsWarning();
    else if (scenario === 'timezone_search_filters') result = await scenarioTimezoneSearchFilters();
    else if (scenario === 'timezone_search_keeps_value') result = await scenarioTimezoneSearchKeepsTheChosenValue();
    else if (scenario === 'timezone_payload') result = await scenarioTimezoneReachesThePayload();
    else if (scenario === 'submit_422_timezone') {
      result = await scenarioSubmit422Visibility({ timezone: 'Выберите часовой пояс.' });
    }
    else if (scenario === 'proxy_input_resets') result = await scenarioProxyInputResets();
    else if (scenario === 'install_stage_pending') result = await scenarioInstallStagePending();
    else if (scenario === 'install_stage_absent') result = await scenarioInstallStageAbsent();
    else if (scenario === 'install_stage_pending_web_extract') result = await scenarioInstallStagePendingForWebExtract();
    else if (scenario === 'firecrawl_duplicate_backend_consistent_env') result = await scenarioFirecrawlDuplicateBackendSubmitsConsistentEnv();
    else if (scenario === 'auth_lost_on_background_request') result = await scenarioAuthLostOnBackgroundRequest();
    else if (scenario === 'rate_limited_on_background_request') result = await scenarioRateLimitedOnBackgroundRequest();
    else if (scenario === 'auth_choice_cards') result = await scenarioAuthChoiceCards();
    else if (scenario === 'provider_current_hint') result = await scenarioProviderCurrentHint();
    else if (scenario === 'signup_hint_link_only') result = await scenarioSignupHintLinkOnly();
    else if (scenario === 'fallback_block_contents') result = await scenarioFallbackBlockContents();
    else throw new Error('unknown scenario ' + scenario);
    console.log(JSON.stringify(result));
    process.exit(0);
  } catch (err) {
    console.error((err && err.stack) || String(err));
    process.exit(1);
  }
})();
"""


def _run_scenario(tmp_path: Path, html: str, catalog: dict, scenario: str) -> dict:
    html_path = tmp_path / "page.html"
    catalog_path = tmp_path / "catalog.json"
    driver_path = tmp_path / "driver.js"
    html_path.write_text(html, encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    driver_path.write_text(_DRIVER_JS, encoding="utf-8")

    result = subprocess.run(
        ["node", str(driver_path), str(html_path), str(catalog_path), scenario],
        cwd=str(_REPO_ROOT),
        env=_node_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "scenario %r failed:\nstdout=%s\nstderr=%s" % (
        scenario,
        result.stdout,
        result.stderr,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _synthetic_row(name: str, **overrides) -> dict:
    row = {
        "name": name,
        "badge": "",
        "tag": "",
        "env_vars": [],
        "post_setup": None,
        "recommended": True,
        "installed": True,
        "backend_key": None,
        "web_backend": None,
        "provider_key": None,
        "beta": False,
        "beta_note_ru": None,
        "voices": None,
        "default_voice": None,
        "install_blocked": False,
        "install_blocked_reason_ru": None,
    }
    row.update(overrides)
    return row


def _catalog_with_pending_browser_install(base_catalog: dict, *, installed: bool) -> dict:
    """A real ``/api/form`` catalog (``base_catalog``, for its providers/
    provider_groups) with its ENTIRE ``tools`` array replaced by one
    fully controlled, ``post_setup``-free "recommended" row per category
    — except "browser", whose row carries the one ``post_setup`` hook
    this scenario cares about, toggled installed/not-installed by
    ``installed``.

    Every category, not just "browser", has to be pinned this way: some
    OTHER category's own DEFAULT selection can carry a genuinely
    not-yet-installed ``post_setup`` hook on the machine actually running
    this test (e.g. "Local Whisper"/``faster_whisper`` for "stt", which —
    unlike "image_gen"/"video_gen" — has no "off" state and is always
    actively selected — see tools_view.py's own comment on this). Left
    unpinned, that real fact would make
    ``test_install_stage_hidden_when_selected_row_is_already_installed``
    flaky/environment-dependent instead of proving what it claims to
    prove: THIS row's installed state is what decides the stage.
    """
    catalog = dict(base_catalog)
    catalog["tools"] = [
        {
            "category": "browser",
            "title_ru": "Браузер",
            "rows": [
                _synthetic_row(
                    "Local Browser",
                    post_setup="agent_browser",
                    installed=installed,
                    backend_key="off",
                )
            ],
        },
        {"category": "web", "title_ru": "Поиск", "rows": [_synthetic_row("DuckDuckGo", web_backend="ddgs")]},
        {"category": "tts", "title_ru": "Голос", "rows": [_synthetic_row("Microsoft Edge TTS", provider_key="edge")]},
        {
            "category": "stt",
            "title_ru": "Распознавание речи",
            "rows": [_synthetic_row("Local Whisper", provider_key="local")],
        },
        {"category": "image_gen", "title_ru": "Изображения", "rows": [_synthetic_row("Off", provider_key="off")]},
        {"category": "video_gen", "title_ru": "Видео", "rows": [_synthetic_row("Off", provider_key="off")]},
    ]
    return catalog


def _catalog_with_pending_extract_install(base_catalog: dict) -> dict:
    """Finding 9 (review 2026-08-26, owner-approved fix)'s own fixture —
    same shape/reasoning as ``_catalog_with_pending_browser_install``
    above (every OTHER category pinned to a ``post_setup``-free row so
    only the row under test can possibly contribute a pending install),
    except here it's "web_extract" ("Чтение страниц") that carries the
    one ``post_setup`` hook, and "browser"/"web" are the pinned ones.
    Latent in the real catalog today (no exa/firecrawl/parallel/tavily
    row has a ``post_setup`` hook yet — see the finding's own comment in
    page.py), so a synthetic row is the only way to exercise this at all.
    """
    catalog = dict(base_catalog)
    catalog["tools"] = [
        {
            "category": "browser",
            "title_ru": "Браузер",
            "rows": [_synthetic_row("Local Browser", backend_key="off")],
        },
        {"category": "web", "title_ru": "Поиск", "rows": [_synthetic_row("DuckDuckGo", web_backend="ddgs")]},
        {
            "category": "web_extract",
            "title_ru": "Чтение страниц",
            "rows": [_synthetic_row("Tavily", web_backend="tavily", post_setup="install_tavily", installed=False)],
        },
        {"category": "tts", "title_ru": "Голос", "rows": [_synthetic_row("Microsoft Edge TTS", provider_key="edge")]},
        {
            "category": "stt",
            "title_ru": "Распознавание речи",
            "rows": [_synthetic_row("Local Whisper", provider_key="local")],
        },
        {"category": "image_gen", "title_ru": "Изображения", "rows": [_synthetic_row("Off", provider_key="off")]},
        {"category": "video_gen", "title_ru": "Видео", "rows": [_synthetic_row("Off", provider_key="off")]},
    ]
    return catalog


def _catalog_with_duplicate_firecrawl_web_backend(base_catalog: dict) -> dict:
    """Finding 11 (review 2026-08-26, owner-approved fix)'s own fixture:
    two DIFFERENT "web" rows sharing one ``web_backend`` value
    ("firecrawl") — the real catalog shape whenever a self-hosted
    Firecrawl instance answers the liveness probe alongside the
    always-rendered cloud Firecrawl plugin row (tools_view.py). The cloud
    row (``FIRECRAWL_API_KEY``) renders FIRST, the self-hosted row
    (``FIRECRAWL_API_URL``) SECOND — matching plugin-injection order in
    the real catalog.
    """
    catalog = dict(base_catalog)
    catalog["tools"] = [
        {"category": "browser", "title_ru": "Браузер", "rows": [_synthetic_row("Local Browser", backend_key="off")]},
        {
            "category": "web",
            "title_ru": "Поиск",
            "rows": [
                _synthetic_row(
                    "Firecrawl",
                    web_backend="firecrawl",
                    env_vars=[{"key": "FIRECRAWL_API_KEY", "prompt_ru": "Ключ"}],
                ),
                _synthetic_row(
                    "Firecrawl Self-Hosted",
                    web_backend="firecrawl",
                    badge="free · self-hosted",
                    env_vars=[{"key": "FIRECRAWL_API_URL", "prompt_ru": "Адрес"}],
                ),
            ],
        },
        {"category": "tts", "title_ru": "Голос", "rows": [_synthetic_row("Microsoft Edge TTS", provider_key="edge")]},
        {
            "category": "stt",
            "title_ru": "Распознавание речи",
            "rows": [_synthetic_row("Local Whisper", provider_key="local")],
        },
        {"category": "image_gen", "title_ru": "Изображения", "rows": [_synthetic_row("Off", provider_key="off")]},
        {"category": "video_gen", "title_ru": "Видео", "rows": [_synthetic_row("Off", provider_key="off")]},
    ]
    return catalog


@requires_jsdom
def test_install_stage_shown_before_response_and_failure_reported_on_success(logged_in, tmp_path):
    """Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"):
    (1) the wait screen's own stage list already knows — before
    /api/submit is even sent — whether an install will run this
    submission (the default-selected "Local Browser" row here is not yet
    installed), via pendingToolInstallNames()/setStageOrder(); (2) a
    reported tool_install_failures entry lands on the SUCCESS screen as an
    honest note, and does NOT turn the submission itself into a failure
    (ok stays true, #success — not #progress/#form-error — is what
    shows)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    base_catalog = logged_in.get("/api/form").json()
    catalog = _catalog_with_pending_browser_install(base_catalog, installed=False)

    out = _run_scenario(tmp_path, html, catalog, "install_stage_pending")
    assert out["stageInstallHiddenBeforeResponse"] is False, out
    assert out["successHidden"] is False, out
    assert out["formErrorText"] == "", out
    assert out["installNoticeHidden"] is False, out
    assert "Local Browser" in out["installNoticeText"], out
    assert "Node.js" in out["installNoticeText"], out


@requires_jsdom
def test_install_stage_hidden_when_selected_row_is_already_installed(logged_in, tmp_path):
    """Honest-stages invariant (spec: never draw a stage that won't run):
    an already-installed row means nothing will actually run at submit
    time — the stage must stay hidden the whole time, and an empty
    tool_install_failures must not paint a visible-but-empty notice."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    base_catalog = logged_in.get("/api/form").json()
    catalog = _catalog_with_pending_browser_install(base_catalog, installed=True)

    out = _run_scenario(tmp_path, html, catalog, "install_stage_absent")
    assert out["stageInstallHiddenBeforeResponse"] is True, out
    assert out["stageInstallHiddenAfterSuccess"] is True, out
    assert out["installNoticeHidden"] is True, out


@requires_jsdom
def test_install_stage_shown_for_a_pending_web_extract_row(logged_in, tmp_path):
    """Finding 9 (review 2026-08-26, owner-approved fix): pendingToolInstallNames()
    used to only check the "web" ("Поиск в интернете") category —
    app.py's server-side twin (_pending_tool_installs) already checks
    "web_extract" ("Чтение страниц") too. Picking an extract-capable row
    with a pending post_setup hook (synthetic here — no real
    exa/firecrawl/parallel/tavily row has one yet, so this is otherwise
    unreachable) must show the install stage before the request is even
    sent, same as every other category already does."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    base_catalog = logged_in.get("/api/form").json()
    catalog = _catalog_with_pending_extract_install(base_catalog)

    out = _run_scenario(tmp_path, html, catalog, "install_stage_pending_web_extract")
    assert out["stageInstallHiddenBeforeResponse"] is False, out


@requires_jsdom
def test_firecrawl_duplicate_web_backend_submits_the_rendered_rows_env(logged_in, tmp_path):
    """Finding 11 (review 2026-08-26, owner-approved fix): two "web" rows
    sharing one web_backend ("firecrawl") must never let the settings
    panel show one row's field while the submitted payload names a
    DIFFERENT row's env var. Before the fix, rowByValue's unguarded
    overwrite picked the LAST row (self-hosted, FIRECRAWL_API_URL) for
    rendering while searchEnvPayload() picked the FIRST (cloud,
    FIRECRAWL_API_KEY) for submission — a self-hosted instance address
    typed into what LOOKED like a URL field would silently land in
    FIRECRAWL_API_KEY instead. Both must now agree."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    base_catalog = logged_in.get("/api/form").json()
    catalog = _catalog_with_duplicate_firecrawl_web_backend(base_catalog)

    out = _run_scenario(tmp_path, html, catalog, "firecrawl_duplicate_backend_consistent_env")
    assert out["renderedFieldType"] == "password", out
    assert out["submittedSearchEnvKey"] == "FIRECRAWL_API_KEY", out


@requires_jsdom
def test_telegram_user_note_shows_name_only_after_verified_token_and_single_id(logged_in, tmp_path):
    """Owner feedback п.4 (live VM walkthrough): once the token is
    verified and a single, unambiguous id is typed, the wizard shows "Это
    <имя>" next to the id field. Also proves: editing the id clears the
    note immediately (never a stale name for a since-edited id), a
    negative lookup (Telegram hasn't seen this user yet — the same
    "нажмите «Старт»" precondition the field's own hint already states)
    renders as complete silence rather than an error, a comma-separated
    (ambiguous) value never triggers a lookup at all, and the name is
    rendered as literal text — never parsed as markup — even when it
    contains HTML-like characters."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "telegram_user_note")

    positive = out["afterPositive"]
    assert positive["hidden"] is False
    assert "Иван" in positive["text"]
    assert "@ivanpetrov" in positive["text"]
    assert positive["hasElementChildren"] is False
    assert positive["containsRawAngleBrackets"] is True

    assert out["afterInputCleared"]["hidden"] is True

    negative = out["afterNegative"]
    assert negative["hidden"] is True
    assert negative["text"] == ""

    assert out["multiIdRequestFired"] is False


@requires_jsdom
def test_telegram_user_note_retries_after_token_confirms_late(logged_in, tmp_path):
    """Finding 5 (review 2026-08-26, owner-approved fix): the id typed
    and left BEFORE the token check resolves must still get its "Это
    <имя>" lookup once the token is confirmed — without the client
    touching the id field again. Before the fix, the retry call lived
    inside renderTelegramVerdict() itself and always read the token
    field's own "input" handler having just nulled state.telegramCheck
    (the SAME renderTelegramVerdict() call is what's about to assign a
    fresh value, but only after returning) — the retry branch was
    unreachable. The existing
    test_telegram_user_note_shows_name_only_after_verified_token_and_single_id
    only drives the OTHER order (token confirms, then id is typed) and
    never caught this."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "telegram_user_note_retry_after_late_token")

    assert out["earlyLookupFired"] is False, out
    assert out["noteHiddenWhileTokenInFlight"] is True, out
    assert out["verdictText"] == "Подключён бот @my_trix_bot", out
    assert out["retryLookupFired"] is True, out
    assert out["noteHiddenAfterRetry"] is False, out


@requires_jsdom
def test_telegram_token_tab_out_issues_exactly_one_check(logged_in, tmp_path):
    """Owner feedback п.3 (live VM walkthrough): "я ввёл токен, он
    проверил, всё окей — почему, когда я ввожу Telegram id, он снова
    начинает проверять токен?" Root cause: #telegram_token's "change" AND
    "blur" listeners each independently called runTelegramCheck() — one
    Tab out of the field (as happens the instant the client moves on to
    the very next field, "Ваш Telegram id") fired both, issuing two live
    /api/check/telegram requests for the identical value. Exactly one must
    go out now."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "telegram_tab_out_no_double_fire")
    assert out["requestCount"] == 1, out


@requires_jsdom
def test_provider_key_tab_out_issues_exactly_one_check(logged_in, tmp_path):
    """Owner feedback п.3, parity pair: #provider_api_key carried the same
    unguarded "change"/"blur" double-fire — fixed the same way."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "provider_key_tab_out_no_double_fire")
    assert out["requestCount"] == 1, out


@requires_jsdom
def test_telegram_stale_response_does_not_clobber_in_flight_guard(logged_in, tmp_path):
    """Finding 7 (review 2026-08-26, owner-approved fix): reproduces the
    review's own "проба" — token A's check starts, gets superseded by
    token B's own check, and A's late answer must not reset
    telegramCheckSeqInFlight while B is still genuinely running. Before the
    fix, ANY settling response (stale or not) unconditionally cleared the
    sentinel, so a further blur while B was still in flight fired a
    redundant THIRD request for the same value."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "telegram_stale_response_no_clobber")
    assert out["redundantThirdProbeFired"] is False, out
    assert out["fieldValueStillTokenB"] is True, out


@requires_jsdom
def test_proxy_mouse_click_blur_then_click_issues_exactly_one_check(logged_in, tmp_path):
    """Finding 6 (review 2026-08-26, owner-approved fix): a real mouse
    click on "Далее" blurs the currently-focused #proxy field BEFORE the
    click event fires (mousedown -> blur -> click). If the client had just
    finished typing, the blur handler's own immediate check and the click
    handler's own unconditional check both fired — two full network round
    trips (Telegram + every provider) through the client's proxy for one
    click. Exactly one must go out now."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "proxy_blur_then_click_no_double_fire")
    assert out["requestCount"] == 1, out


@requires_jsdom
def test_telegram_user_lookup_network_failure_allows_immediate_retry(logged_in, tmp_path):
    """Finding 8 (review 2026-08-26, owner-approved fix): a genuine network
    failure on the id lookup (never a definite Telegram answer either way)
    must not leave telegramUserLastCheckedKey pointing at the failed
    attempt — the client must be able to retry the SAME id on the very
    next blur, not just after editing the field away and back."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "telegram_user_lookup_network_failure_allows_retry")
    assert out["retryFiredAfterNetworkFailure"] is True, out


@requires_jsdom
def test_proxy_change_resets_stale_token_and_key_verdicts(logged_in, tmp_path):
    """Finding 12 (review 2026-08-26, owner-approved fix): both the
    Telegram token check and the provider key check run THROUGH whatever
    proxy is typed on step 2 — changing it must invalidate a verdict
    earned through the OLD one, both visibly (the stale verdict hides) and
    behaviorally (a subsequent blur on the unchanged field actually
    re-checks, instead of maybeRunTelegramCheck()/maybeRunProviderKeyCheck()'s
    own "already settled" guards silently skipping it)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "proxy_change_resets_token_and_key_verdicts")
    assert out["telegramVerdictVisibleBeforeProxyEdit"] is True, out
    assert out["keyVerdictVisibleBeforeProxyEdit"] is True, out
    assert out["telegramVerdictHiddenAfterProxyEdit"] is True, out
    assert out["keyVerdictHiddenAfterProxyEdit"] is True, out
    assert out["secondTokenCheckFired"] is True, out
    assert out["secondKeyCheckFired"] is True, out


@requires_jsdom
def test_telegram_check_input_race_does_not_advance_on_stale_answer(logged_in, tmp_path):
    """Finding 2: editing the token field after "Далее" fired a check must
    invalidate that check IN FLIGHT, not just an already-rendered verdict.

    Repro (matches the review's own "проба A"): type token A, click
    "Далее" (check A starts), edit the field to token B before the
    response lands, then let A's answer arrive. Before the fix,
    telegramCheckSeq was only bumped inside runTelegramCheck() itself — the
    "input" listener cleared the verdict but left the sequence number
    alone, so A's late answer still matched and the wizard advanced to
    step 4 showing A's bot name while token B (never checked) sat in the
    field, about to be submitted unverified."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "telegram_race")
    assert out["step"] != 4, "must not advance on a stale (pre-edit) verdict: %r" % out
    assert out["verdictHidden"] is True
    assert "TOKEN_A" not in out["verdictText"]
    assert out["fieldValue"] == "TOKEN_B"


@requires_jsdom
def test_provider_key_check_race_does_not_leak_across_a_provider_switch(logged_in, tmp_path):
    """Finding 3: switching providers mid-check must invalidate the
    in-flight key check (and the live-models fetch it would trigger), not
    just the already-rendered verdict.

    Repro (matches the review's own "проба G"): type a key for provider A
    (check A starts), switch to provider B before the response lands
    (clearing the key field, per spec B3 п.8), then let A's answer arrive.
    Before the fix, neither onProviderChange() nor the key field's own
    "input" listener bumped keyCheckSeq/modelsFetchSeq, so A's late "Ключ
    принят" answer still matched and painted a green verdict — plus fired
    a live /api/models fetch — over provider B's EMPTY key field."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "key_race")
    assert out["keyVerdictHidden"] is True, out
    assert "Ключ принят" not in out["keyVerdictText"], out
    assert out["keyFieldValue"] == ""
    # The fixed code's seq guard returns before ever calling
    # fetchLiveModelsForRow() for the stale answer — no /api/models leak.
    assert "/api/models" not in out["pendingAfter"], out


@requires_jsdom
def test_saved_token_field_blur_does_not_show_a_false_error(logged_in_with_saved_env, tmp_path):
    """Finding 4: a returning client's saved Telegram token never echoes
    into #telegram_token (secrets are never echoed — see
    applySecretPlaceholder). blur on the empty field — which also fires
    from the client's own click on "Далее" — must not paint a red "Вставьте
    токен бота" over a bot that is already configured and working."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in_with_saved_env.get("/api/form").json()
    assert catalog["current"]["telegram_token"]["is_set"] is True

    out = _run_scenario(tmp_path, html, catalog, "saved_token_blur")
    assert out["verdictHidden"] is True
    assert out["verdictText"] == ""


@requires_jsdom
def test_step4_return_mode_jump_backfills_reachability_markers(logged_in_with_saved_env, tmp_path):
    """Finding 4 (review 2026-08-26, owner-approved fix): a returning
    client (saved TELEGRAM_BOT_TOKEN — `logged_in_with_saved_env`, same
    fixture `test_saved_token_field_blur_does_not_show_a_false_error`
    uses) gets a fully clickable progress bar and can reach step 4
    ("Провайдер") by clicking straight there, skipping step 2's own
    "Далее" entirely — which used to be the ONLY thing that ever filled
    `state.providerReachabilityByGroup`, now that step 2 no longer
    autochecks on entry (owner feedback п.1). Reproduces the review's own
    table: before this fix, `.tag.off` was `[]` on this path even though a
    provider genuinely needed the VM's proxy; after it, a silent
    background check fires on entering step 4 and the markers actually
    appear — without ever touching step 2's own (still-hidden) verdict
    UI, and without re-firing on a second visit to step 4."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in_with_saved_env.get("/api/form").json()
    assert catalog["current"]["telegram_token"]["is_set"] is True

    out = _run_scenario(tmp_path, html, catalog, "step4_return_mode_backfills_reachability")
    assert out["offTagsBeforeAnyEntry"] == 0, out
    assert out["proxyRequestPendingBeforeEntry"] is False, out
    assert out["stepAfterNavClick"] == 4, out
    assert out["backgroundProxyRequestFired"] is True, out
    assert out["step2StillHiddenAfterBackfill"] is True, out
    assert out["offTagsAfterFill"] >= 1, out
    assert out["secondEntryFiredAnotherRequest"] is False, out


@requires_jsdom
def test_first_run_empty_token_blur_stays_silent_but_next_still_shows_the_error(logged_in, tmp_path):
    """Owner feedback п.2 (live VM walkthrough): "до того как я не ввёл
    ничего, ничего не проверять не надо" — on a FIRST-run client (no saved
    token at all), blur on the still-empty field must NOT paint "Вставьте
    токен бота" any more (that used to fire the instant focus left an
    untouched field, before the client had typed a single character).
    "Далее" is the one place that still must catch it — the client's own
    attempt to leave the step with nothing entered — so this scenario
    proves BOTH halves in one flow, not just the removal."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()
    assert not catalog["current"].get("telegram_token", {}).get("is_set")

    out = _run_scenario(tmp_path, html, catalog, "empty_token_blur_then_next")
    assert out["afterBlur"]["verdictHidden"] is True
    assert out["afterBlur"]["verdictText"] == ""

    assert out["afterNextClick"]["verdictHidden"] is False
    assert "Вставьте токен бота" in out["afterNextClick"]["verdictText"]


@requires_jsdom
def test_proxy_field_input_clears_a_stale_verdict_and_debounces_a_real_recheck(logged_in, tmp_path):
    """Finding 5: #proxy's "input" listener must clear a stale
    "недоступен"/"нужен прокси" verdict the instant the client edits the
    field, never let it survive unchanged.

    Owner feedback п.1 (live VM walkthrough, this pass): step 2 no longer
    autochecks on entry (see goToStep()'s own comment) — the "недоступен"
    state here is established the honest way, via a real "Далее" click,
    not a boot()-time freebie. And unlike the OLD "Прокси изменён...
    Проверим при переходе «Далее»" placeholder-only behavior, editing the
    field must eventually fire a REAL check by itself once the client
    stops typing (debounced, not on every character) — the owner's other
    complaint ("надо проверять... когда что-то там вставилось")."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "proxy_input_resets")
    assert "недоступен" in out["before"] or "нужен прокси" in out["before"]
    assert out["afterInput"] != out["before"]
    assert "недоступен" not in out["afterInput"]
    assert "нужен прокси" not in out["afterInput"]
    # Still an honest "wait" state, never claiming success before a real
    # answer has come back.
    assert "wait" in out["afterInputClass"]
    assert out["tooSoonPending"] is False
    assert out["debouncedRequestFired"] is True


@requires_jsdom
def test_picking_a_provider_collapses_the_list_to_just_that_row(logged_in, tmp_path):
    """Owner feedback: "нажимаю Подробнее... после нажатия ничего не
    происходит... приходится снова вниз всё пролистывать" — plus a picked
    provider's list staying fully rendered "очень сильно портит всю
    картинку". Picking a group must collapse #provider_group down to just
    that one row (+ a reopen link) instead of leaving every recommended/
    expanded row rendered underneath the key/model fields the pick just
    revealed. Reopening ("Выбрать другого провайдера") must restore the
    full list without losing the highlighted pick."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "provider_pick_collapses")
    assert out["beforeRows"] > 1, out
    assert out["afterPickRowCount"] == 1, out
    assert out["afterPickRowGroupId"] == out["groupId"], out
    assert out["afterPickRowSelected"] is True, out
    assert out["changeLinkText"] == "Выбрать другого провайдера", out
    assert out["afterReopenRows"] == out["beforeRows"], out
    assert out["reopenedChosenRowSelected"] is True, out


@requires_jsdom
def test_advanced_rows_are_an_accordion_and_close_when_the_step_is_left(logged_in_with_saved_env, tmp_path):
    """Owner feedback: opening "Браузер" then "Поиск" left both .row-body
    panels open at once, and neither folded back up when the client moved
    on to a different step — "наслоение идёт". At most one category row
    may be open at a time, and leaving step 5 (via its own "Далее") must
    fold whatever was still open."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in_with_saved_env.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "advanced_rows_collapse")
    assert out["afterOpenBrowser"]["browserOpen"] is True, out
    assert out["afterOpenBrowser"]["browserBodyHidden"] is False, out
    # Accordion: opening "Поиск" must fold "Браузер" back up.
    assert out["afterOpenSearch"]["browserOpen"] is False, out
    assert out["afterOpenSearch"]["browserBodyHidden"] is True, out
    assert out["afterOpenSearch"]["searchOpen"] is True, out
    assert out["afterOpenSearch"]["searchBodyHidden"] is False, out
    # Leaving the step must fold "Поиск" too.
    assert out["afterLeave"]["step"] == 6, out
    assert out["afterLeave"]["searchOpen"] is False, out
    assert out["afterLeave"]["searchBodyHidden"] is True, out


def _camofox_auto_default(catalog: dict) -> str:
    """Pull Camofox's standard address out of a real ``/api/form`` catalog
    the same way the page itself does — via ``env_vars[].auto_default`` on
    the ``CAMOFOX_URL`` entry (tools_view.py) — instead of hardcoding the
    known-fixed port in this test file. Keeps the assertion honest: it
    checks the DOM shows whatever the catalog actually handed over, not a
    value this test made up independently."""
    for block in catalog.get("tools", []):
        if block.get("category") != "browser":
            continue
        for row in block.get("rows", []):
            for env in row.get("env_vars", []):
                if env.get("key") == "CAMOFOX_URL" and env.get("auto_default"):
                    return env["auto_default"]
    raise AssertionError("fixture catalog has no CAMOFOX_URL auto_default — cannot run this test honestly")


@requires_jsdom
def test_camofox_address_is_never_shown_but_selection_still_works(logged_in_with_saved_env, tmp_path):
    """Owner ruling after looking at the live VM: picking Camofox in the
    "Браузер" row must show NEITHER an ``#camofox_url`` input NOR the
    address as text anywhere in that row's settings — an ordinary user
    gets nothing to read there, just like picking "Chromium" or "Browser
    Use". The pick itself must still register normally (the select's own
    value stays "camofox"); the address keeps flowing to the payload
    silently via ``camofoxUrlPayload()``, covered separately by
    ``test_camofox_url_payload_untouched_states_never_clear``.

    See ``test_camofox_address_is_never_asked_for_or_shown_but_still_submitted``
    in ``test_setup_wizard_page.py`` for the source-text half of this
    contract. This test drives the real render->expand->select sequence
    and reads the live DOM — a mutation that reintroduces the address note
    (under any wording) would still leave every source-text assertion
    green but must turn this one red."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in_with_saved_env.get("/api/form").json()
    address = _camofox_auto_default(catalog)

    out = _run_scenario(tmp_path, html, catalog, "camofox_address_hidden")
    assert out["selectedValue"] == "camofox", out
    assert out["camofoxUrlInputPresent"] is False, out
    assert out["settingsText"] is not None, out
    assert address not in out["settingsText"], out
    assert "стандартный" not in out["settingsText"], out


@requires_jsdom
def test_submit_422_on_a_mapped_field_scrolls_the_highlighted_field_into_view(logged_in, tmp_path):
    """Owner feedback: "он направляет обратно на ту страницу, которую не
    сделал, но не пишет, что не так" — a 422 for telegram_token must land
    on step 3 with #err_telegram_token both populated/visible AND actually
    brought into view (not just technically un-hidden somewhere off the
    current scroll position)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "submit_422_field_mapped")
    assert out["step"] == 3, out
    assert out["telegramErrHidden"] is False, out
    assert out["telegramErrText"] == "Токен недействителен.", out
    assert "err_telegram_token" in out["scrollCalls"], out


@requires_jsdom
def test_submit_422_on_a_path_only_error_scrolls_the_banner_into_view(logged_in, tmp_path):
    """Companion to the mapped-field case above: paths with no dedicated
    `#err_<id>` element (tool_env/tool_provider/search_env.key —
    PATH_STEP-only) still land the client on the right step (5) with the
    message in #form-error, and that banner must be the thing scrolled
    into view — not left wherever the client's old scroll position from
    step 6 happened to put it."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "submit_422_path_only")
    assert out["step"] == 5, out
    assert out["formErrorText"] == "Неизвестный ключ инструмента.", out
    assert "form-error" in out["scrollCalls"], out


@requires_jsdom
def test_background_401_or_403_shows_auth_lost_message_once_no_rebuild(logged_in, tmp_path):
    """Spec 8, §8.3: there is no cookie session any more — a 401 (changed
    Basic credentials) or 403 (CSRF Host/Origin guard) on a request made
    AFTER the page already loaded must show the "access lost" message via
    handleAuthLost(), and do it honestly: the current step stays visible
    (#main never hidden, no navigation to any other screen), and a SECOND
    background failure of the same kind does not duplicate the message —
    it's a plain textContent assignment, not an append, and this proves
    that by actually triggering it twice through the real jsonFetch()
    code path rather than pattern-matching the source for a comparison
    string (which would pass on a broken refactor just as easily as a
    working one)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "auth_lost_on_background_request")

    first = out["afterFirst"]
    assert first["step"] == 3, out
    assert first["mainHidden"] is False, out
    assert first["loginFormPresent"] is False, out
    assert first["formErrorText"], out
    assert "Обновите страницу" in first["formErrorText"], out

    second = out["afterSecond"]
    # The second failed check must not move the wizard off step 3 either.
    assert second["step"] == 3, out
    assert second["mainHidden"] is False, out
    # Same message, not doubled — proves handleAuthLost() overwrites
    # rather than accumulates.
    assert second["formErrorText"] == first["formErrorText"], out


@requires_jsdom
def test_background_429_shows_rate_limited_message_not_a_json_parse_crash(logged_in, tmp_path):
    """Spec 8, §8.3.6: a locked-out IP's 429 carries an HTML body, not
    JSON. Before jsonFetch() handled 429 the same way it already handles
    401/403, every caller's own `.then(function (res) { return
    res.json(); })` either threw a raw SyntaxError parsing HTML as JSON or
    (for callers with a `.catch` that isn't auth-aware) painted their own
    endpoint-specific lie — never told the client the true reason: too
    many failed logins from this machine.

    This drives the real jsonFetch()/handleRateLimited() code path (not a
    source match), the same way
    test_background_401_or_403_shows_auth_lost_message_once_no_rebuild
    proves the 401/403 path — a `res.json()` call that would reject on an
    HTML body is used as the fetch stub's response specifically so a
    broken fix (one that still tries to parse it as JSON) fails loudly
    instead of silently passing."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()

    out = _run_scenario(tmp_path, html, catalog, "rate_limited_on_background_request")
    assert out["step"] == 3, out
    assert out["mainHidden"] is False, out
    assert out["formErrorText"], out
    assert "попыт" in out["formErrorText"].lower(), out


@requires_jsdom
def test_telegram_verdict_stays_visible_before_the_step_advances(tmp_path, logged_in):
    """Имя бота — единственное подтверждение, что подключён нужный бот.

    Раньше «Далее» запускал проверку и уходил на следующий шаг в тот же
    момент, когда вердикт появлялся: клиент видел «подождите» и сразу
    оказывался на другом шаге, так и не увидев @имя. Найдено владельцем
    на живой машине 2026-08-25.

    Контракт: если проверка выполнялась по этому нажатию, шаг НЕ меняется
    — вердикт остаётся на экране. Второе нажатие уводит дальше и НЕ
    перепроверяет токен заново (вердикт уже устоялся).
    """
    from hermes_cli.setup_wizard.page import render_page

    catalog = logged_in.get("/api/form").json()
    # Часовой пояс уже отвечен (спека 11): без него «Далее» на этом шаге
    # не уводит дальше, и второе нажатие меряло бы новые ворота вместо
    # устоявшегося вердикта, ради которого тест и написан.
    catalog["current"] = dict(catalog["current"], timezone="Europe/Moscow")
    out = _run_scenario(tmp_path, render_page(), catalog, "telegram_verdict_readable")

    first = out["afterFirstClick"]
    assert first["step"] == 3, "шаг сменился до того, как вердикт можно было прочитать"
    assert first["verdictHidden"] is False
    assert "my_trix_bot" in first["verdictText"]
    assert first["nextDisabled"] is False, "кнопка должна вернуться в рабочее состояние"

    assert out["stepAfterSecondClick"] == 4, "второе нажатие обязано уводить дальше"
    assert out["secondClickRefetched"] is False, "второй клик не должен перепроверять токен"


@requires_jsdom
def test_openai_group_offers_both_auth_paths_as_unmissable_cards(logged_in, tmp_path):
    """Owner feedback (live walkthrough, п.1): "Почему там chat GPT только
    по подписке, а не ещё через API?" — the data was always right (the
    "openai" group has always carried both openai-codex and openai-api as
    variants — see providers_view.wizard_provider_groups()); the radio list
    just never registered as a real, equally-weighted choice. Drives a real
    click through the live catalog's "openai" group and proves: both cards
    render, neither is pre-selected (spec §7.2), neither sub-block shows
    until a card is actually picked, and picking one shows only that one
    card's sub-block."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()
    openai_group = next((g for g in catalog["provider_groups"] if g["group_id"] == "openai"), None)
    if openai_group is None or len(openai_group["variants"]) < 2:
        pytest.skip("live catalog has no multi-variant 'openai' group to drive this scenario")

    out = _run_scenario(tmp_path, html, catalog, "auth_choice_cards")

    before = out["beforePick"]
    assert before["authChoiceHidden"] is False, out
    assert before["cardCount"] == len(openai_group["variants"]), out
    assert before["anySelected"] is False, "nothing may be preselected on first entry (spec §7.2): %r" % out
    assert before["apiBlockHidden"] is True, out
    assert before["deviceBlockHidden"] is True, out

    after = out["afterPick"]
    assert after["apiKeyCardSelected"] is True, out
    assert after["otherCardSelected"] is False, out
    assert after["apiBlockHidden"] is False, "picking the API-key card must reveal the key/model block: %r" % out
    assert after["deviceBlockHidden"] is True, out


@requires_jsdom
def test_provider_current_hint_silent_for_auto_but_honest_for_a_real_provider(logged_in, tmp_path):
    """Owner feedback (live walkthrough, п.2): "Сейчас настроено: auto.
    Ничего не выбрано автоматически..." — model.provider round-trips to the
    literal string "auto" after `hermes logout` (auth.py's own
    _reset_config_provider()), and providerRowFor("auto") finds nothing in
    the catalog, so the field used to print that raw literal as if it were
    a real answer. Two catalogs from the SAME real /api/form response,
    differing only in current.provider.name, prove the fix is a real
    guard — not a change that just always returns "" (which would pass an
    "auto" catalog and silently break a genuine returning client)."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()
    if not catalog.get("providers"):
        pytest.skip("live catalog has no providers to drive the 'real provider' half of this test")
    real_row = catalog["providers"][0]

    auto_catalog = json.loads(json.dumps(catalog))
    auto_catalog["current"]["provider"] = {
        "name": "auto", "base_url": "", "model": "",
        "api_key": {"is_set": False}, "device_login_ok": False,
    }
    out_auto = _run_scenario(tmp_path, html, auto_catalog, "provider_current_hint")
    assert out_auto["hintText"] == "", out_auto

    real_catalog = json.loads(json.dumps(catalog))
    real_catalog["current"]["provider"] = {
        "name": real_row["name"], "base_url": "", "model": "",
        "api_key": {"is_set": False}, "device_login_ok": False,
    }
    out_real = _run_scenario(tmp_path, html, real_catalog, "provider_current_hint")
    assert out_real["hintText"] == "Сейчас настроено: %s." % real_row["display_name"], out_real


@requires_jsdom
def test_signup_hint_shows_only_the_link_not_a_repeated_description(logged_in, tmp_path):
    """Owner feedback (live walkthrough, п.3): "Зачем мы повторяем то же
    самое, что пишем при выборе провайдера?" — #provider-signup-hint used
    to repeat row.description_ru verbatim (already shown a few lines up, in
    the picked row's own .desc span) with the signup URL appended as bare
    text, never a real link. Drives a real single-variant group whose row
    is confirmed (from the live catalog) to carry BOTH a description_ru and
    a signup_url, so a regression that brought the description back would
    actually be caught."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()
    target = None
    for g in catalog["provider_groups"]:
        if len(g["variants"]) != 1:
            continue
        v = g["variants"][0]
        if v.get("description_ru") and v.get("signup_url"):
            target = g
            break
    if target is None:
        pytest.skip("no single-variant provider in the live catalog has both description_ru and signup_url")

    catalog["__target__"] = target["group_id"]
    out = _run_scenario(tmp_path, html, catalog, "signup_hint_link_only")

    variant = target["variants"][0]
    assert variant["description_ru"] not in out["hintText"], out
    assert out["hintText"] == "Регистрация: " + variant["signup_url"], out
    assert out["linkHref"] == variant["signup_url"], out
    assert out["linkText"] == variant["signup_url"], out


@requires_jsdom
def test_fallback_block_never_offers_a_device_code_provider_and_says_why(logged_in, tmp_path):
    """Owner feedback (live walkthrough, п.5): "Запасных провайдеров нет
    chatgpt?" — checked against the runtime (resolve_provider_client()'s
    openai-codex/minimax-oauth branches in agent/auxiliary_client.py read a
    stored OAuth token and ignore api_key entirely; the CLI's own fallback
    picker already supports an OAuth fallback via the same device-code
    flow `hermes model` uses) — the gap is that THIS web form has no
    device-login sub-block for the fallback slot, so a device_code option
    here would be a control with no way to ever complete a login. The
    filter (`p.kind !== "api_key"`) is therefore correct; this test proves
    it stays that way AND that the client is told why, instead of being
    left to guess — reading the real rendered <select> against the real
    catalog's own kind field, not a hand-picked stand-in list."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = logged_in.get("/api/form").json()
    device_code_names = {p["name"] for p in catalog["providers"] if p["kind"] != "api_key"}
    if not device_code_names:
        pytest.skip("live catalog has no device_code provider to drive this scenario")

    out = _run_scenario(tmp_path, html, catalog, "fallback_block_contents")

    offered = set(out["optionValues"])
    assert not (offered & device_code_names), (offered, device_code_names)
    assert "Запасной провайдер" in out["blockText"], out["blockText"]
    assert "Запасная модель" not in out["blockText"], out["blockText"]
    assert "только провайдеры с ключом API" in out["blockText"], out["blockText"]


# ---------------------------------------------------------------------------
# Часовой пояс (спека 11) — исполнением, а не чтением исходника
# ---------------------------------------------------------------------------


def _catalog_with_timezone(base_catalog: dict, *, saved: str = "", cron_jobs=0) -> dict:
    """Каталог `/api/form` с заданным сохранённым поясом и числом задач.

    Токен бота помечен сохранённым намеренно: тогда «Далее» на шаге
    Telegram идёт быстрым путём и в сеть не ходит, и единственное, что
    может его задержать, — проверяемое здесь поле. Иначе сценарий мерил
    бы заодно и проверку токена.
    """
    catalog = dict(base_catalog)
    current = dict(catalog.get("current") or {})
    current["telegram_token"] = {"is_set": True, "hint": ""}
    current["timezone"] = saved
    catalog["current"] = current
    catalog["cron_jobs"] = cron_jobs
    return catalog


@requires_jsdom
def test_timezone_is_required_before_leaving_the_telegram_step(logged_in, tmp_path):
    """Пропуск обязан упереться в поле, а не проехать молча.

    Пустой пояс означает системное время машины хостера — беда, которую
    клиент замечает только по тому, что напоминания приходят не вовремя.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "timezone_required_blocks_next")
    assert out["step"] == 3, out
    assert out["errHidden"] is False, out
    assert out["errText"].strip(), out


@requires_jsdom
def test_nothing_is_preselected_until_the_client_answers(logged_in, tmp_path):
    """Владелец: преселекта нет. Мастер не отвечает за клиента."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "timezone_required_blocks_next")
    assert out["value"] == "", out


@requires_jsdom
def test_every_zone_is_offered_in_the_picker(logged_in, tmp_path):
    """Все пояса, а не только российские: клиент может быть откуда угодно.

    Сравнение с рантаймом, а не с числом-снимком.
    """
    from zoneinfo import available_timezones

    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "timezone_required_blocks_next")
    # +1 — пустая строка «— выберите —», которой в базе зон нет.
    assert out["optionCount"] == len(available_timezones()) + 1, out


@requires_jsdom
def test_picking_a_zone_lets_the_client_move_on(logged_in, tmp_path):
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "timezone_pick_advances")
    assert out["afterPick"]["value"] == "Europe/Moscow", out
    assert out["after"]["step"] == 4, out


@requires_jsdom
def test_a_saved_answer_comes_back_selected(logged_in, tmp_path):
    """Возвратный клиент видит свой ответ, а не пустое поле."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(
        logged_in.get("/api/form").json(), saved="Asia/Yekaterinburg"
    )

    out = _run_scenario(tmp_path, html, catalog, "timezone_prefilled")
    assert out["value"] == "Asia/Yekaterinburg", out


@requires_jsdom
def test_changing_an_answered_zone_warns_with_the_real_job_count(logged_in, tmp_path):
    """Предупреждение опирается на проверку, а не на допущение.

    Уже заведённые задачи сохранены в базу с прежним поясом и на новый не
    переезжают. Число берётся из настоящего ответа `/api/form`, поэтому в
    тексте обязано стоять именно оно.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(
        logged_in.get("/api/form").json(), saved="Asia/Yekaterinburg", cron_jobs=3
    )

    out = _run_scenario(tmp_path, html, catalog, "timezone_change_warns")
    assert out["before"]["warningHidden"] is True, out
    assert out["afterChange"]["warningHidden"] is False, out
    assert "3" in out["afterChange"]["warningText"], out


@requires_jsdom
def test_the_change_warning_must_be_acknowledged_before_moving_on(logged_in, tmp_path):
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(
        logged_in.get("/api/form").json(), saved="Asia/Yekaterinburg", cron_jobs=3
    )

    out = _run_scenario(tmp_path, html, catalog, "timezone_change_warns")
    assert out["afterBlockedNext"]["step"] == 3, out
    assert out["afterAck"]["step"] == 4, out


@requires_jsdom
def test_no_warning_when_there_is_nothing_to_lose(logged_in, tmp_path):
    """Задач нет — предупреждать не о чем, и молчание здесь честное."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(
        logged_in.get("/api/form").json(), saved="Asia/Yekaterinburg", cron_jobs=0
    )

    out = _run_scenario(tmp_path, html, catalog, "timezone_change_warns")
    assert out["afterChange"]["warningHidden"] is True, out
    assert out["afterBlockedNext"]["step"] == 4, out


@requires_jsdom
def test_an_unreadable_job_list_warns_without_inventing_a_number(logged_in, tmp_path):
    """Третий исход: проверить не удалось.

    Мастер обязан сказать именно это, а не выдать незнание за отсутствие
    задач и не назвать выдуманное число.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(
        logged_in.get("/api/form").json(), saved="Asia/Yekaterinburg", cron_jobs=None
    )

    out = _run_scenario(tmp_path, html, catalog, "timezone_change_warns")
    assert out["afterChange"]["warningHidden"] is False, out
    assert not any(ch.isdigit() for ch in out["afterChange"]["warningText"]), out


@requires_jsdom
def test_returning_to_the_saved_zone_takes_the_warning_away(logged_in, tmp_path):
    """Клиент передумал — предупреждение обязано уйти вместе с изменением."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(
        logged_in.get("/api/form").json(), saved="Asia/Yekaterinburg", cron_jobs=3
    )

    out = _run_scenario(tmp_path, html, catalog, "timezone_change_back")
    assert out["changed"]["warningHidden"] is False, out
    assert out["back"]["warningHidden"] is True, out


@requires_jsdom
def test_search_narrows_the_list_of_zones(logged_in, tmp_path):
    """Шестьсот пунктов не пролистать — поиск обязан работать."""
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "timezone_search_filters")
    assert out["filteredCount"] < out["allCount"], out
    assert out["hasVladivostok"] is True, out
    assert out["hasMoscow"] is False, out


@requires_jsdom
def test_search_never_silently_drops_the_clients_choice(logged_in, tmp_path):
    """Отфильтровать список — не то же самое, что передумать за клиента.

    Выбранный пояс обязан пережить фильтр, иначе клиент, набравший что-то
    в поиске и передумавший искать, отправил бы форму с пустым полем и не
    понял бы, куда делся ответ.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "timezone_search_keeps_value")
    assert out["whileFiltered"] == "Europe/Moscow", out
    assert out["afterClearing"] == "Europe/Moscow", out


@requires_jsdom
def test_the_chosen_zone_actually_reaches_the_submitted_payload(logged_in, tmp_path):
    """Точка вызора, а не только поле: выбор обязан доехать до сервера.

    Возврат сборки тела на «не передавать» оставил бы все проверки выше
    зелёными, а пояс перестал бы сохраняться молча.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "timezone_payload")
    assert out["timezone"] == "Asia/Vladivostok", out


@requires_jsdom
def test_a_422_on_the_timezone_lands_on_its_own_step_and_field(logged_in, tmp_path):
    """Серверный отказ обязан привести клиента к полю, а не к пустому месту.

    Это же проверяет и запись `timezone` в FIELD_STEP: без неё ошибка
    осела бы общим баннером на чужом шаге.
    """
    from hermes_cli.setup_wizard.page import render_page

    html = render_page()
    catalog = _catalog_with_timezone(logged_in.get("/api/form").json())

    out = _run_scenario(tmp_path, html, catalog, "submit_422_timezone")
    assert out["step"] == 3, out
    assert out["timezoneErrHidden"] is False, out
    assert out["timezoneErrText"] == "Выберите часовой пояс.", out
    assert "err_timezone" in out["scrollCalls"], out
