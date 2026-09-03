"""Support section for the setup wizard (spec 15,
``docs/product/PROMPT-spec15-support-page.md``) — the deterministic half
only. **No model, no chat, no HTTP route for either** — that is
deliberately the next task (the delegating brief's own words: "Без модели
и без чата... места под него не готовь"). This module wires the
already-built ``hermes_cli/trix_support.py`` (checks -> fix-where-possible
-> recheck -> verdict, plus the client-report/feedback-log primitives) onto
one page and two mutating routes.

**Why this is a separate entry point, not a step of the wizard form.**
The brief is explicit that this is a hard requirement, not a style choice:
the wizard's last form step ("Готово" -> ``POST /api/submit`` ->
``_run_submit`` in ``app.py``) re-applies settings, re-runs whatever tool
installs the submission selects, and restarts the gateway. A client who
shows up three weeks after setup because something broke must reach a
repair tool without walking through — and risking re-triggering — that
pipeline. ``GET /support`` is therefore its own page, reachable the
instant Basic auth succeeds, wired independently of the form's own
state/steps.

**This module renders through the wizard's own shell, never a second
design.** An earlier pass read "design isn't the point right now" as
"don't build a design" and shipped this page as a bare, unstyled HTML
document with its own ad-hoc CSS — a different font, different buttons,
nothing in common with the wizard. The client saw two different-looking
programs, and the owner could not even find the one link into this page
on a screenshot (it was a small underlined line in the wizard rail's
footer). The correct reading was always "don't invent a new design —
reuse the one that exists": this page now calls
``hermes_cli.setup_wizard.page.render_shell()`` (the wizard's own
wrap/canvas/rail/content shell plus its one ``_CSS`` constant, factored
out of ``render_page()`` for exactly this reuse — see that function's own
docstring), and reuses ``page.py``'s own rail builder
(``_rail_html``/``_rail_address``/``_header_intro``) for the sidebar
column. Nothing about the wizard's *form* (``_MAIN_FORM_HTML``, its
script, its steps) is touched or imported here — only the shared shell
and stylesheet, which is exactly the part that was supposed to be shared
in the first place — the shell and stylesheet stay the single source of
truth for both surfaces; only the content this module supplies to that
shell (see ``render_support_page``'s own docstring) has since grown a
return path into the wizard and a fuller chat, per later owner feedback
(2026-09-03) — the presentation still matches the product the client
already knows.

**Security is reused, never reinvented** (per the brief). Every route
registered here (``register_support_routes(app)``, called once from
``app.py``'s ``create_app()``) lands on the SAME ``FastAPI`` app instance
``create_app()`` already wraps in, outermost first,
``_ClosedWizardGateMiddleware`` -> ``_BasicAuthMiddleware`` ->
``_OriginGuardMiddleware`` (see ``app.py``'s own module docstring for the
exact order and why). Nothing in this module re-implements auth, lockout,
or Origin checking:

* ``GET /support`` needs no Origin check (GET never mutates) but is still
  behind Basic auth like every other route on this app — the auth
  middleware gates by REQUEST, not by path or method.
* The two mutating routes, ``POST /api/support/run`` and ``POST
  /api/support/feedback``, live under ``/api/`` *specifically* so
  ``_OriginGuardMiddleware`` — keyed on ``request.url.path.startswith
  ("/api/")`` for a mutating HTTP method, see that middleware's own
  docstring — picks them up automatically, the exact same way it already
  covers ``/api/submit``. There is no second Origin-checking code path
  anywhere in this file.
* The exception handler ``app.py`` registers for ``RequestValidationError``
  (never echo submitted values back) is global to the app instance, so it
  already covers a malformed ``POST /api/support/feedback`` body with zero
  extra code here.

**Single-flight lock — same shape as ``app.py``'s ``submit_lock``.**
``trix_support.run_support_pass()`` can legitimately run for minutes in
the worst case (every check failing, both implemented fix-and-recheck
chains firing — see that module's own timeout constants; a rough summed
worst case is on the order of twenty minutes). A second, overlapping run
would restart the gateway again mid-poll and pull the rug out from under
the first run's own liveness wait — the exact failure mode ``app.py``'s
``submit_lock`` documents for ``/api/submit``. Same fix, same shape:
``app.state.support_lock`` (a ``threading.Lock``) makes the
check-and-set atomic even though the actual work runs inside
``asyncio.to_thread``; ``app.state.support_in_flight`` is the flag it
guards. A concurrent second call gets ``409`` — no queueing, no retry —
matching the brief's "Одна кнопка. Никаких... выбора" and "Два
одновременных прогона запускать нельзя."

**No Python-level timeout wrapper added on top of ``run_support_pass()``.**
``app.py`` itself documents its own known mistake (``_run_tool_install_
with_timeout``'s docstring): a ``ThreadPoolExecutor``/
``future.result(timeout=...)`` around a subprocess call stops WAITING on
timeout but never kills the child — the subprocess keeps running.
``trix_support.py`` already avoids this for the one action that needs a
real kill (the doctor subprocess uses ``subprocess.run(..., timeout=...)``,
which genuinely terminates the child — see that module's own docstring).
This module adds **no** additional timeout layer around
``run_support_pass()`` as a whole; doing so would only reintroduce the
exact bug the brief calls out by name, for no benefit — the call is
already bounded by the sum of its own actions' real, individually-enforced
timeouts. The request simply takes as long as that already-bounded pass
takes; nothing here shortens or lengthens it.

**Report shape — client sees only the result.** The JSON body
``POST /api/support/run`` returns carries exactly four fields:
``{"message": <str>, "run_id": <uuid hex>, "resolved": <bool>,
"chat_available": <bool>}``. No check id, no stage name, no internal
error string, no step count ever reaches this response — per the brief's
"Отчёт... только результат" and owner ruling 4. ``write_internal_report()``
(``trix_support.py``, unchanged) is called first, unconditionally, so the
full internal detail is never lost even though the client never sees any
of it. ``resolved``/``chat_available`` are themselves structural, not
internal detail — a client-side rendering decision (skip the да/нет
question and open the chat automatically, or offer the escalation
contact right away) needs to know these two facts to draw the right
screen; neither one is a check id, a stage name, or a log line.

**Verdict wording depends on whether the chat can open — checked BEFORE
the verdict is built, never after (owner, 2026-09-03).** ``message`` for
the two "everything's fine" outcomes is still exactly one of
``trix_support.build_client_report()``'s own ``_MSG_CLIENT_ALL_GOOD``/
``_MSG_CLIENT_FIXED`` sentences — those never named the escalation
contact and don't change here. The THIRD outcome (``not result.ok`` —
some check still failed after its own fix-and-recheck chain) used to
reuse ``build_client_report()``'s ``_MSG_CLIENT_NOT_FIXED`` verbatim,
which names ``SUPPORT_ESCALATION_CONTACT`` directly in the verdict text
— sending the client to a human before the product's own chat ever got a
turn (owner feedback: "это отправляет человека к людям раньше, чем
продукт попробовал сам"). This module now picks between two page-local
sentences instead (``_MSG_SUPPORT_NOT_FIXED_WITH_CHAT``/
``_MSG_SUPPORT_NOT_FIXED_NO_CHAT``, below) based on chat availability —
resolved once, before ``message`` is built, so the verdict text and the
client's actual ability to open a chat can never disagree. ``trix_support.py``'s own
``_MSG_CLIENT_NOT_FIXED`` (with the contact inline) is untouched and
still backs the *other* caller of ``build_client_report()`` —
``app.py``'s post-submit notice on the "Готово" screen, which has no
chat to open at all, so naming the contact directly there remains
correct.

**Chat availability comes from the pass's own ``provider_key`` check, not
a second call (owner, 2026-09-03 follow-up — "не выбрасывай уже
проверенное").** ``run_support_pass()`` already ran a REAL live probe
against the configured provider key, in ``CHECK_ORDER``, as one of the
pass's own checks (``trix_support._check_provider_key`` →
``validate.check_provider_key`` → ``credential_probes.probe_provider_key``
— a genuine HTTP round trip to the provider). Deriving "can the chat open"
from ``support_chat.is_chat_available()`` — a SEPARATE credential
resolution this module used to call unconditionally on every run — threw
that result away and asked essentially the same question again through a
different path, with no guarantee the two would ever agree.
:func:`_provider_key_verdict` reads the ``provider_key`` check's own
``CheckOutcome`` off the ``SupportPassResult`` that already exists and
answers from that alone. ``support_chat.is_chat_available()`` is kept as
the fallback — used only when the pass carries no ``provider_key`` check
at all (should not happen in production; ``CHECK_ORDER`` always includes
it, so this is defense in depth / what a test double with a hand-built
``SupportPassResult`` naturally exercises) — never as the primary source
of truth anymore.

**Verified premise, worth recording here.** The task that asked for this
change described the removed call as "второе обращение к провайдеру" — in
fact ``support_chat.is_chat_available()``'s own docstring says it does
NOT make a live network call (it resolves through
``agent.auxiliary_client.aux_probe_mode()``, which explicitly skips the
one network fetch that mode would otherwise make). So the call being
replaced was a redundant CONFIG/credential resolution, not a redundant
HTTP round trip. That doesn't change the fix: two independent resolutions
of "is a provider usable" can still disagree (different config path,
different provider under ``auxiliary.support.*``), and re-deriving from
the pass's own already-verified result is strictly better — it just isn't
literally saving a network call the way the framing implied.

**The reason a client reads, not just the fact "не всё исправлено"
(spec follow-up, owner: "причина, а не факт").** When the pass's own
``provider_key`` check failed, :func:`_provider_key_verdict` maps its
structural ``reason`` (``credential_probes.probe_provider_key``'s own
``"auth"``/``"billing"``/``"network"``/``"other"``, module docstring
there) to one of the already-reviewed Russian sentences in
``trix_provider_errors.py`` — never new copy. A key the provider rejected
(401/403) and an account with no funds left (402) are both things the
client can fix themselves, so their messages point at where to fix it (the
wizard for the key, the provider's own dashboard for a top-up, per
``client_error_message``/``client_billing_next_step_message``'s own
wording); a provider that couldn't be reached at all is NOT about the key,
so that message names the escalation contact instead, per the owner's own
instruction — see :func:`_provider_key_verdict` for the exact mapping and
its own note on the one bucket (an unclassified non-2xx status) that has
no dedicated ready phrase.

**No chat without a working key (owner ruling, spec follow-up: "пока ключ
не работает, чат не открывается вовсе").** The client-side script already
only calls ``openChat()`` when ``chat_available`` came back ``true`` — but
that is a UI convenience, not a security boundary: nothing previously
stopped a direct ``POST /api/support/chat`` from reaching
``support_chat.run_chat_turn()`` (and therefore attempting a real
completion call through the very key the pass just proved doesn't work)
regardless of what ``/api/support/run`` reported. ``support_run`` now
records the same boolean it returns to the client into
``app.state.support_chat_allowed[run_id]``, and ``support_chat_endpoint``
refuses (with the exact same graceful sentence ``run_chat_turn`` itself
would have used —
``support_chat.chat_unavailable_reply()`` — never a new one) any
``run_id`` that isn't in that map with a ``True`` value, BEFORE calling
``run_chat_turn`` at all. An unknown ``run_id`` (no matching
``/api/support/run`` in this process — e.g. after a wizard-process
restart, same in-memory-only caveat as ``support_chat_history``) is
treated as "not allowed" — there is no evidence it's safe, so the safe
default is refusal, not a bare "current session key doesn't apply" 500.

**Waiting indicator — no new polling route.** The brief asks for
``build_heartbeat_text`` (already Russian, already built to name the deed
rather than a tool) with no step list. Rather than add a new GET route
purely to drive a cosmetic timer (more permanent API surface for no
functional gain), the page embeds a short, precomputed table of heartbeat
strings for minute 0..``_HEARTBEAT_MAX_MINUTES`` at RENDER time (a pure,
cheap, no-I/O call) and a few lines of inline JS advance an index into it
once a minute while a run is in flight, holding on the last entry past the
cap. Every one of those calls passes ``tool_name=None`` — this table can
therefore never name an internal check or tool, structurally, not by
prompt discipline (``build_heartbeat_text`` only ever splices in a "deed"
when it's given a recognized tool name — see its own docstring).

**No Hermes/Nous, no jargon, Russian only.** Every string a client can see
from this module (page copy, both JSON response messages, the escalation
line) is reviewed for this by hand and covered by
``tests/hermes_cli/test_setup_wizard_support_view.py``, in the same style
as ``tests/hermes_cli/test_trix_identity.py`` /
``tests/gateway/test_brand_leak_debrand_l10n.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from hermes_cli import trix_provider_errors, trix_support
from hermes_cli.setup_wizard import support_chat
from hermes_cli.setup_wizard.page import _header_intro, _rail_address, _rail_html, render_shell
from hermes_cli.trix_status import build_heartbeat_text

logger = logging.getLogger(__name__)

_MSG_SUPPORT_IN_PROGRESS = "Проверка уже выполняется, подождите."
_MSG_SUPPORT_RUN_FAILED = "Не удалось выполнить проверку. Попробуйте ещё раз позже."
_MSG_FEEDBACK_RUN_ID_REQUIRED = "Не указан идентификатор проверки."
_MSG_CHAT_EMPTY_MESSAGE = "Пустое сообщение."
_MSG_CHAT_SEND_FAILED = "Не получилось отправить сообщение. Попробуйте ещё раз."

# The "not everything got fixed" verdict, page-local (see the module
# docstring's "Verdict wording..." section for why this isn't
# trix_support._MSG_CLIENT_NOT_FIXED reused as-is): which of these two a
# client sees is decided ONCE, before either string is ever picked -- never
# a fallback the client reaches only after a chat already failed to open.
_MSG_SUPPORT_NOT_FIXED_WITH_CHAT = (
    "Проверка завершена, но починилось не всё. "
    "Давайте разберёмся вместе — расскажите, что не работает."
)
_MSG_SUPPORT_NOT_FIXED_NO_CHAT = (
    "Проверка завершена: часть неполадок исправить самостоятельно не удалось, "
    f"а чат сейчас недоступен. Напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}."
)
# Shown only when a client answers "Нет" to "Всё наладилось?" after an
# ALL-GOOD/FIXED verdict and the chat can't open (owner ruling: a client
# must never be left with neither a chat nor an address) -- the verdict
# text itself has nothing to say in that branch since it already reported
# success.
_MSG_NO_CHAT_AFTER_FEEDBACK = (
    "Разобраться прямо сейчас не получится. "
    f"Напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}."
)


# ---------------------------------------------------------------------------
# provider_key verdict -- the reason a client reads, derived from the pass's
# own already-run check, never a second provider call (module docstring's
# "Chat availability comes from..." / "The reason a client reads..." notes).
# ---------------------------------------------------------------------------


def _provider_key_check_detail(result: trix_support.SupportPassResult) -> Optional[dict]:
    """The raw ``credential_probes.probe_provider_key`` (or
    ``validate.check_provider_key``'s own "nothing to check"/"key empty")
    result dict this pass's own ``provider_key`` check already produced --
    never a fresh call. ``provider_key`` has no entry in
    ``trix_support.FIX_FOR_CHECK``, so it is only ever attempted once per
    pass; its outcome always lives on ``.initial``, never ``.fix``/
    ``.recheck``.
    """
    for check in result.checks:
        if check.check_id == "provider_key":
            return check.initial.detail
    return None


@dataclass(frozen=True)
class _ProviderKeyVerdict:
    """``key_ok=True`` means the pass's own ``provider_key`` check did not
    treat the key as broken (a genuinely working key, OR nothing configured
    to check at all -- ``validate.check_provider_key``'s own deliberate
    "nothing configured is not itself a failure" contract, which this
    module does not override). ``message`` is only ever set when
    ``key_ok`` is ``False`` -- the reason-specific sentence the client
    reads instead of the generic "не всё исправлено" verdict.
    """

    key_ok: bool
    message: Optional[str] = None


def _provider_key_verdict(result: trix_support.SupportPassResult) -> Optional[_ProviderKeyVerdict]:
    """Map this pass's own ``provider_key`` check onto a client-facing
    verdict -- ``None`` when the pass carries no such check at all (defense
    in depth; see the module docstring's "Chat availability comes from..."
    note for when the caller should fall back to
    ``support_chat.is_chat_available()`` instead).

    Every non-``None`` branch below reuses an already-reviewed sentence --
    either ``trix_provider_errors.py``'s (spec 10) or the check's own
    already-Russian ``message`` -- never new copy (task instruction: "бери
    формулировки из trix_provider_errors.py, своих не изобретай").
    """
    detail = _provider_key_check_detail(result)
    if detail is None:
        return None
    if detail.get("ok"):
        return _ProviderKeyVerdict(key_ok=True)

    if not detail.get("checked", True):
        # No live probe ran at all. validate.check_provider_key's own
        # "nothing configured" branch is ok=True and already returned above
        # -- reaching here with checked=False means the key FIELD was
        # empty (trix_support._check_provider_key's "Ключ провайдера не
        # задан." branch). Self-fixable in the wizard, same as a rejected
        # key -- no ready trix_provider_errors.py phrase covers "field left
        # blank" specifically, so this reuses the check's own existing,
        # already-tested Russian sentence and adds the one missing fact
        # (where to fix it) rather than inventing a whole new message.
        return _ProviderKeyVerdict(
            key_ok=False,
            message=(
                (detail.get("message") or "Ключ провайдера не задан.")
                + " Впишите ключ в мастере настройки, на шаге провайдера."
            ),
        )

    reason = detail.get("reason")
    if reason == "auth":
        # credential_probes.probe_provider_key: 401/403 -- the provider
        # itself rejected this key. Self-fixable: client_error_message's
        # own "auth" text already names the fix location ("в мастере
        # настройки"), so no extra link text is added here -- see the
        # module docstring's note on why a deep link to a specific step was
        # not built for this task.
        return _ProviderKeyVerdict(
            key_ok=False,
            message=trix_provider_errors.client_error_message(reason="auth", is_auth=True),
        )
    if reason == "billing":
        # credential_probes.probe_provider_key: 402 -- the account is out
        # of funds. Fixing this happens on the PROVIDER's own dashboard,
        # not in our wizard -- client_error_message(billing)'s own wording
        # already says so ("личный кабинет провайдера"), so no wizard link
        # is added for this case (see _provider_key_verdict's own note
        # above for the case that DOES need one).
        # Joined with a space, not a newline: the page renders this string
        # via plain .textContent into a paragraph with no
        # `white-space: pre-line` (page.py's shared _CSS), so a literal
        # "\n" would just collapse to whitespace in the browser anyway --
        # joining with a space up front keeps the source string honest
        # about how it actually renders.
        return _ProviderKeyVerdict(
            key_ok=False,
            message=(
                trix_provider_errors.client_error_message(reason="billing")
                + " "
                + trix_provider_errors.client_billing_next_step_message()
            ),
        )
    if reason == "network":
        # The provider itself could not be reached to check the key at
        # all -- not about the key (task instruction: "это не про ключ, и
        # адрес поддержки тут уместен"). trix_provider_errors.py has no
        # dedicated phrase for THIS specific situation (its "tls"/
        # "unavailable" sentences describe a live conversation turn
        # failing mid-call, worded around "ход"/"попробуйте повторить" --
        # not a clean match for a key-verification probe) -- reuse the
        # probe's own already-reviewed Russian message instead of writing
        # new copy, and add the one allowed escalation contact per the
        # owner's own instruction for this branch.
        base = detail.get("message") or "Не удалось связаться с провайдером для проверки ключа."
        return _ProviderKeyVerdict(
            key_ok=False,
            message=f"{base} Напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}.",
        )

    # reason in ("other", None): an unclassified non-2xx/401/403/402/429
    # response, or a reason-less failure from a code path this branch
    # doesn't otherwise recognize. No case in trix_provider_errors.py names
    # this precisely either -- its closest analog is the generic
    # "unavailable" sentence (a provider responding abnormally, try again
    # later), which is what this reuses; nothing here is invented copy, but
    # this bucket is the one place this task's report flags as "no exact
    # ready phrase exists" per the task's own instruction to say so rather
    # than sowing text quietly.
    base = trix_provider_errors.client_error_message(reason=None, status_code=detail.get("status_code"))
    return _ProviderKeyVerdict(
        key_ok=False,
        message=f"{base} Напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}.",
    )


# See support_chat.py's own module docstring for why the chat has no lock
# of its own the way /api/support/run does: a chat turn is far cheaper than
# a full check pass, and this dict is scoped per run_id, not process-wide,
# so two different clients (two different runs) never contend on it. A
# bound trims the tail so one very long conversation can't grow the prompt
# sent to the auxiliary model without limit.
_MAX_CHAT_HISTORY_MESSAGES = 12

# Precomputed once, at import time — build_heartbeat_text() is pure and
# does no I/O. tool_name is always None (see module docstring): this table
# can never grow a step name later without a code change here, which is
# the point. Rough worst-case bound for one support pass, from
# trix_support.py's own timeout constants (every check failing, both
# implemented fix-and-recheck chains firing): on the order of twenty
# minutes — the cap below is a comfortable margin over that, not an exact
# figure; the JS holds on the last entry for anything longer.
_HEARTBEAT_MAX_MINUTES = 25
_HEARTBEAT_TEXTS: tuple[str, ...] = tuple(
    build_heartbeat_text(minute, None, lang="ru") for minute in range(_HEARTBEAT_MAX_MINUTES + 1)
)


class _SupportFeedbackBody(BaseModel):
    run_id: str
    helped: bool


class _SupportChatBody(BaseModel):
    run_id: str
    message: str


def _heartbeats_json_for_script() -> str:
    """``_HEARTBEAT_TEXTS`` as a JSON array literal safe to splice into an
    inline ``<script>`` block — ``</script`` inside a JSON string would
    otherwise close the tag early in an HTML parser. None of the current
    Russian strings contain it, but the escape is cheap defense in depth
    and costs nothing to keep even if a future locale string ever did.
    """
    return json.dumps(_HEARTBEAT_TEXTS, ensure_ascii=False).replace("</", "<\\/")


def render_support_page(host: str | None = None) -> str:
    """The support page's full HTML — rendered through the wizard's own
    shell (``render_shell()`` + ``_rail_html()``, both from ``page.py``;
    see this module's docstring for why). Every piece of text is a fixed
    Russian literal; nothing here interpolates request- or client-supplied
    data, so there is no HTML-escaping concern for this page's static
    copy.

    **Flow (owner feedback, 2026-09-03 — "порядок текста сдаётся до
    попытки").** The verdict for "не всё исправлено" no longer names the
    escalation contact itself; it says the product wants to keep trying,
    and the chat opens right under it automatically — no да/нет question
    in between (that question only makes sense for a verdict that
    already claims success). The contact address moves to a permanent
    line under the chat log/input instead of a one-shot escalation
    paragraph, so it is always where the client can see it once a chat is
    open, in every path that opens one:
      - the pass didn't fully succeed and a chat is reachable — the
        server picked ``_MSG_SUPPORT_NOT_FIXED_WITH_CHAT`` and the
        script opens the chat immediately;
      - the pass looked fine but the client answers "Нет" to "Всё
        наладилось?" — the chat opens then too, same permanent line
        under it.
    A chat that genuinely cannot open (``chat_available: false`` — see
    ``support_chat.is_chat_available()`` and this module's own docstring)
    never gets silently offered: the verdict names the contact directly
    when the pass failed, and a "Нет" answer after a fine-looking verdict
    shows the same honest line (``_MSG_NO_CHAT_AFTER_FEEDBACK``) instead
    of a chat box that would 200 with nothing but a refusal on the first
    message.

    **Chat is a real conversation surface (owner feedback: "чат — это не
    поле ввода"):** a scrolling transcript (``#chatLog``, its own
    max-height in ``_CSS``) with each line labelled who said it, a
    "бот думает" line reusing the exact same ``build_heartbeat_text``
    table the outer wait indicator already uses (see
    ``showChatThinking()`` below) while a reply is in flight, Enter-to-send
    alongside the button, and both input and button disabled for the
    duration of one round-trip so a client can't queue up three messages
    ahead of the first reply.

    The rail renders with no host (``_rail_address(None)``/
    ``_header_intro(None)``, the same generic "эта машина" wording every
    non-HTTP wizard caller already gets), its own "Что-то не работает?"
    entry turned off (a client already on this page has no use for a
    second link to the page they're standing on), and its "Вернуться к
    настройке" entry turned ON (``wizard_entry_visible=True`` — owner
    feedback: "со страницы поддержки нет выхода"; see
    ``page._rail_wizard_entry_html``'s own docstring).
    """
    heartbeats_json = _heartbeats_json_for_script()
    failure_message_json = json.dumps(_MSG_SUPPORT_RUN_FAILED, ensure_ascii=False)
    chat_failure_message_json = json.dumps(_MSG_CHAT_SEND_FAILED, ensure_ascii=False)
    no_chat_after_feedback_json = json.dumps(_MSG_NO_CHAT_AFTER_FEEDBACK, ensure_ascii=False)
    escalation_line_json = json.dumps(
        f"Если и это не помогло — напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}.",
        ensure_ascii=False,
    )

    # Reuses the wizard's own classes (h2.screen-title/p.screen-sub/
    # button.accent/p.hint — all defined once in page.py's _CSS) instead of
    # inventing new ones, so an edit to the wizard's look and feel reaches
    # this page automatically. The permanent escalation line lives INSIDE
    # #chat (last child) rather than behind its own hidden flag — as soon
    # as #chat.hidden flips false it is on screen, structurally, with no
    # separate visibility state to keep in sync.
    content_html = """<h2 class="screen-title">Поддержка</h2>
<p class="screen-sub">Если бот не отвечает или что-то работает не так — нажмите кнопку.
Мы проверим машину и попробуем починить то, что получится.</p>
<button id="runBtn" type="button" class="accent">Проверить и починить</button>
<p id="wait" class="hint" hidden></p>
<p id="report" class="hint" hidden></p>
<div id="feedback" hidden>
  <p class="screen-sub" style="margin-bottom:0.6rem">Всё наладилось?</p>
  <button id="yesBtn" type="button">Да</button>
  <button id="noBtn" type="button">Нет</button>
</div>
<p id="noChatNotice" class="hint" hidden></p>
<p id="thanks" class="hint" hidden>Спасибо за ответ.</p>
<div id="chat" hidden>
  <div id="chatLog" role="log" aria-live="polite"></div>
  <p id="chatThinking" class="hint" hidden></p>
  <div class="chat-row">
    <input id="chatInput" type="text" placeholder="Опишите, что случилось">
    <button id="chatSendBtn" type="button" class="accent">Отправить</button>
  </div>
  <p id="chatEscalation" class="hint"></p>
</div>"""

    script = f"""(function () {{
  var HEARTBEATS = {heartbeats_json};
  var runBtn = document.getElementById('runBtn');
  var waitEl = document.getElementById('wait');
  var reportEl = document.getElementById('report');
  var feedbackEl = document.getElementById('feedback');
  var noChatNoticeEl = document.getElementById('noChatNotice');
  var thanksEl = document.getElementById('thanks');
  var yesBtn = document.getElementById('yesBtn');
  var noBtn = document.getElementById('noBtn');
  var chatEl = document.getElementById('chat');
  var chatLogEl = document.getElementById('chatLog');
  var chatThinkingEl = document.getElementById('chatThinking');
  var chatEscalationEl = document.getElementById('chatEscalation');
  var chatInput = document.getElementById('chatInput');
  var chatSendBtn = document.getElementById('chatSendBtn');
  var runId = null;
  var chatAvailable = false;
  var heartbeatTimer = null;
  var minuteIndex = 0;
  var chatHeartbeatTimer = null;
  var chatMinuteIndex = 0;

  chatEscalationEl.textContent = {escalation_line_json};

  function appendChatMessage(role, text) {{
    var p = document.createElement('p');
    p.className = role;
    var who = document.createElement('span');
    who.className = 'who';
    who.textContent = (role === 'user' ? 'Вы' : 'Поддержка') + ': ';
    p.appendChild(who);
    p.appendChild(document.createTextNode(text));
    chatLogEl.appendChild(p);
    chatLogEl.scrollTop = chatLogEl.scrollHeight;
  }}

  function openChat() {{
    chatEl.hidden = false;
    if (!chatLogEl.childElementCount) {{
      appendChatMessage('bot', 'Опишите, что случилось.');
    }}
  }}

  function showChatThinking() {{
    var idx = Math.min(chatMinuteIndex, HEARTBEATS.length - 1);
    chatThinkingEl.textContent = HEARTBEATS[idx];
    chatThinkingEl.hidden = false;
    chatMinuteIndex += 1;
  }}

  function sendChatMessage() {{
    var text = chatInput.value.trim();
    if (!text || !runId || chatInput.disabled) {{ return; }}
    chatInput.value = '';
    chatInput.disabled = true;
    chatSendBtn.disabled = true;
    appendChatMessage('user', text);
    chatMinuteIndex = 0;
    showChatThinking();
    chatHeartbeatTimer = window.setInterval(showChatThinking, 60000);

    fetch('/api/support/chat', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{run_id: runId, message: text}})
    }}).then(function (resp) {{
      return resp.json().then(function (data) {{ return {{resp: resp, data: data}}; }});
    }}).then(function (result) {{
      window.clearInterval(chatHeartbeatTimer);
      chatThinkingEl.hidden = true;
      appendChatMessage('bot', (result.resp.ok && result.data.reply) || {chat_failure_message_json});
      chatInput.disabled = false;
      chatSendBtn.disabled = false;
      chatInput.focus();
    }}).catch(function () {{
      window.clearInterval(chatHeartbeatTimer);
      chatThinkingEl.hidden = true;
      appendChatMessage('bot', {chat_failure_message_json});
      chatInput.disabled = false;
      chatSendBtn.disabled = false;
      chatInput.focus();
    }});
  }}

  chatSendBtn.addEventListener('click', sendChatMessage);
  chatInput.addEventListener('keydown', function (e) {{
    if (e.key === 'Enter') {{ sendChatMessage(); }}
  }});

  function showHeartbeat() {{
    var idx = Math.min(minuteIndex, HEARTBEATS.length - 1);
    waitEl.textContent = HEARTBEATS[idx];
    minuteIndex += 1;
  }}

  function showFailure() {{
    reportEl.textContent = {failure_message_json};
    reportEl.hidden = false;
    runBtn.hidden = false;
    runBtn.disabled = false;
  }}

  // Владелец, 2026-09-03: результат проверки -> если не всё исправлено и
  // чат доступен, "давайте разберёмся" и чат открывается тут же, БЕЗ
  // вопроса "всё наладилось?" (тот вопрос имеет смысл только для вердикта,
  // который уже утверждает успех). Вопрос да/нет остаётся только для
  // "всё хорошо"/"починили" — см. sendFeedback ниже.
  runBtn.addEventListener('click', function () {{
    runBtn.disabled = true;
    runBtn.hidden = true;
    reportEl.hidden = true;
    feedbackEl.hidden = true;
    noChatNoticeEl.hidden = true;
    thanksEl.hidden = true;
    chatEl.hidden = true;
    minuteIndex = 0;
    waitEl.hidden = false;
    showHeartbeat();
    heartbeatTimer = window.setInterval(showHeartbeat, 60000);

    fetch('/api/support/run', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: '{{}}'
    }}).then(function (resp) {{
      window.clearInterval(heartbeatTimer);
      waitEl.hidden = true;
      return resp.json().then(function (data) {{ return {{resp: resp, data: data}}; }});
    }}).then(function (result) {{
      if (result.resp.ok) {{
        runId = result.data.run_id;
        chatAvailable = !!result.data.chat_available;
        reportEl.textContent = result.data.message;
        reportEl.hidden = false;
        if (result.data.resolved) {{
          feedbackEl.hidden = false;
        }} else if (chatAvailable) {{
          openChat();
        }}
      }} else {{
        showFailure();
      }}
    }}).catch(function () {{
      window.clearInterval(heartbeatTimer);
      waitEl.hidden = true;
      showFailure();
    }});
  }});

  function sendFeedback(helped) {{
    feedbackEl.hidden = true;
    if (helped) {{
      thanksEl.hidden = false;
    }} else if (chatAvailable) {{
      openChat();
    }} else {{
      noChatNoticeEl.textContent = {no_chat_after_feedback_json};
      noChatNoticeEl.hidden = false;
    }}
    if (runId) {{
      fetch('/api/support/feedback', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{run_id: runId, helped: helped}})
      }}).catch(function () {{}});
    }}
  }}

  yesBtn.addEventListener('click', function () {{ sendFeedback(true); }});
  noBtn.addEventListener('click', function () {{ sendFeedback(false); }});
}})();"""

    # Адрес машины и подпись передаются так же, как в мастере: без host
    # фраза складывалась в «Ваша машина эта машина», а подпись
    # «Настройка» на странице починки просто неверна. wizard_entry_visible
    # (owner, 2026-09-03: «со страницы поддержки нет выхода») — обратный
    # путь в мастер, симметричный входу в поддержку со стороны мастера.
    rail_html = _rail_html(
        _rail_address(host), _header_intro(host), subtitle="Поддержка", wizard_entry_visible=True
    )
    return render_shell(title="Поддержка", rail_html=rail_html, content_html=content_html, script=script)


def register_support_routes(app: FastAPI) -> None:
    """Wire the support page + its two mutating routes onto ``app``.

    Called once from ``app.py``'s ``create_app()``, after that function has
    already installed the shared middleware stack — see this module's own
    docstring for why nothing here needs to (or should) re-check auth or
    Origin. Initializes the single-flight lock state on ``app.state`` the
    same way ``create_app()`` initializes ``submit_lock``/
    ``submit_in_flight`` for ``/api/submit``.
    """
    app.state.support_lock = threading.Lock()
    app.state.support_in_flight = False
    # run_id -> [{"role": ..., "content"/"tool_calls": ...}, ...] -- the
    # chat's own transient transcript, scoped per check-pass run_id, never
    # the client's conversation with the main bot (support_chat.py's own
    # module docstring). In-memory only: a wizard-process restart loses an
    # in-flight chat, which is acceptable for a short-lived repair
    # conversation and avoids adding a persistence layer for this task.
    app.state.support_chat_history = {}
    # run_id -> the exact chat_available bool /api/support/run reported for
    # that run -- the server-side half of "no chat without a working key"
    # (module docstring). Same in-memory-only lifetime as
    # support_chat_history; a run_id absent from this map is refused, never
    # assumed safe.
    app.state.support_chat_allowed = {}

    @app.get("/support", response_class=HTMLResponse)
    async def support_page(request: Request) -> HTMLResponse:
        # Ленивый импорт: app.py импортирует этот модуль, чтобы
        # зарегистрировать маршруты, поэтому импорт на уровне модуля
        # замкнул бы круг.
        from hermes_cli.setup_wizard.app import _host_from_request

        return HTMLResponse(content=render_support_page(_host_from_request(request)))

    @app.post("/api/support/run")
    async def support_run(request: Request) -> JSONResponse:
        state = request.app.state
        with state.support_lock:
            if state.support_in_flight:
                logger.info("support run rejected: outcome=already_in_flight")
                return JSONResponse(status_code=409, content={"error": _MSG_SUPPORT_IN_PROGRESS})
            state.support_in_flight = True

        try:
            result = await asyncio.to_thread(trix_support.run_support_pass)
            run_id = await asyncio.to_thread(trix_support.write_internal_report, result)
            # Availability is resolved BEFORE the verdict text is picked —
            # owner, 2026-09-03: "проверяй доступность до показа вердикта,
            # не после" — so the wording and the client's actual ability to
            # open a chat can never disagree. See this module's own
            # docstring, "Verdict wording depends on...". The pass's own
            # provider_key check is the primary source (module docstring,
            # "Chat availability comes from...") — support_chat.
            # is_chat_available() only runs as a fallback when that check
            # isn't on the result at all.
            verdict = _provider_key_verdict(result)
            if verdict is not None:
                chat_available = verdict.key_ok
            else:
                chat_available = await asyncio.to_thread(support_chat.is_chat_available)

            if result.ok:
                message = trix_support.build_client_report(result)
            elif verdict is not None and not verdict.key_ok:
                # The key itself is why the chat can't help right now — name
                # the actual cause and what the client can do about it
                # (spec follow-up, "причина, а не факт"), instead of the
                # generic "не всё исправлено" wording below.
                message = verdict.message
            else:
                message = _MSG_SUPPORT_NOT_FIXED_WITH_CHAT if chat_available else _MSG_SUPPORT_NOT_FIXED_NO_CHAT

            # The server-side half of "no chat without a working key"
            # (module docstring) — /api/support/chat reads this by run_id
            # before it will run a single chat turn.
            state.support_chat_allowed[run_id] = chat_available
            logger.info(
                "support run finished: run_id=%s ok=%s chat_available=%s",
                run_id, result.ok, chat_available,
            )
            return JSONResponse(
                status_code=200,
                content={
                    "message": message,
                    "run_id": run_id,
                    "resolved": result.ok,
                    "chat_available": chat_available,
                },
            )
        finally:
            with state.support_lock:
                state.support_in_flight = False

    @app.post("/api/support/feedback")
    async def support_feedback(body: _SupportFeedbackBody) -> JSONResponse:
        run_id = body.run_id.strip()
        if not run_id:
            logger.info("support feedback rejected: outcome=missing_run_id")
            raise HTTPException(status_code=400, detail=_MSG_FEEDBACK_RUN_ID_REQUIRED)
        await asyncio.to_thread(trix_support.record_feedback, run_id, body.helped)
        logger.info("support feedback recorded: run_id=%s helped=%s", run_id, body.helped)
        return JSONResponse(status_code=200, content={"ok": True})

    @app.post("/api/support/chat")
    async def support_chat_endpoint(body: _SupportChatBody, request: Request) -> JSONResponse:
        run_id = body.run_id.strip()
        message = body.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail=_MSG_CHAT_EMPTY_MESSAGE)

        state = request.app.state
        # No chat without a working key (owner ruling; module docstring's
        # "No chat without a working key" note). A run_id this process
        # never recorded as chat_available=True — unknown run_id, or a run
        # whose own provider_key check failed — is refused HERE, before
        # ever resolving an auxiliary client or attempting a completion
        # call. Same graceful sentence run_chat_turn itself would have used
        # on an unreachable client — never a new one.
        if not state.support_chat_allowed.get(run_id):
            logger.info("support chat rejected: run_id=%s outcome=key_not_working", run_id)
            return JSONResponse(status_code=200, content={"reply": support_chat.chat_unavailable_reply()})

        history = list(state.support_chat_history.get(run_id, ()))
        result = await asyncio.to_thread(support_chat.run_chat_turn, run_id, message, history)
        state.support_chat_history[run_id] = list(result.history)[-_MAX_CHAT_HISTORY_MESSAGES:]
        logger.info(
            "support chat turn: run_id=%s actions=%s",
            run_id,
            ",".join(result.actions_executed) or "-",
        )
        # Response shape mirrors /api/support/run's own discipline (module
        # docstring, "Report shape"): exactly the client-safe reply, never
        # the internal message list, tool-call payloads, or action ids.
        return JSONResponse(status_code=200, content={"reply": result.reply})
