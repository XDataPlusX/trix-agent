"""The support bot's chat turn (spec 15 follow-up: "Экраны и поток", п.7-8) —
the model half that sits on top of the deterministic
``hermes_cli/trix_support.py`` (checks/fixes) and
``hermes_cli/setup_wizard/support_view.py`` (the page + ``/api/support/run``
+ ``/api/support/feedback`` routes, unchanged by this module).

**No ``AIAgent``, no tool framework, no second core.** This is one bounded
call through ``agent.auxiliary_client.get_text_auxiliary_client("support")``
— the client's own key and provider, with an optional
``auxiliary.support.*`` override in ``config.yaml`` — exactly the pattern
``agent/conversation_compression.py``'s ``check_compression_model_feasibility``
already uses to resolve an auxiliary client. The single OpenAI-style
"function" this module offers the model (``run_support_action``) is not a
tool framework; it is the mechanism that makes ``trix_support.py``'s "the
model never writes a command" constraint hold structurally: the model can
only ever emit a string picked from ``trix_support.SUPPORT_ACTIONS``' own
key set (declared as the function's JSON-schema ``enum``), and this module
independently re-validates that string against the same closed registry
before anything runs — the enum is a hint the wire protocol offers the
model, not the security boundary itself. There is no argument slot in the
function schema for a path, a flag, or a command fragment.

**Why every server-side check re-validates against ``SUPPORT_ACTIONS`` by
exact dict key, never by parsing/interpreting the string.**
:func:`execute_support_action` is the one function that turns a
model-supplied string into a real, running check or fix. It does exactly
one lookup (``trix_support.SUPPORT_ACTIONS.get(action_id)``) and refuses
anything that isn't a literal hit — including an unimplemented action
(``handler is None``). A future edit that "helpfully" starts building a
command string from ``action_id`` here would reopen exactly the hole
``trix_support.py``'s own module docstring calls irreversible; this is the
one function in the whole feature a reviewer must read line-by-line before
touching, and ``tests/hermes_cli/test_setup_wizard_support_chat.py`` pins
it with a mutation-style test (swap the registry lookup for a call that
executes ``action_id`` as a command, watch the test fail).

**Why a hard cap on actions per client message
(``MAX_ACTIONS_PER_MESSAGE``).** Each tool round-trips through a real model
call before the next one is even chosen, so an unbounded loop is not just a
cost risk -- it is silence from the client's point of view (spec: "клиент
видит только молчание"). Three is enough for the realistic chain this
feature actually needs -- run one narrower check the client described,
apply its one paired fix (``trix_support.FIX_FOR_CHECK`` has exactly one
implemented fix per check), and use the third slot either to recheck
something adjacent or to try a second independent check if the first one
came back clean -- while still bounding worst-case latency to a small
multiple of one action's own timeout (the longest, ``doctor_fix``, is
already bounded at ~310s by ``trix_support.py`` itself) instead of letting
a confused model chain checks indefinitely. Once the cap is hit mid-turn,
the loop stops offering the tool at all and forces one final, tool-free
reply so the client always gets an answer instead of a timeout.

**Brand guard: block, never rewrite past the point of confidence.** Mirrors
spec 15's owner ruling verbatim (see ``trix_support.py``'s module docstring
for the identical reasoning): a forbidden token triggers a known-safe
token substitution, then a re-check; if that isn't clean, one regeneration
attempt through the model, then a re-check; if *that* still isn't clean,
the reply is replaced wholesale with a fixed Russian fallback sentence that
names the one allowed escalation contact. No global "Hermes" -> "Trix"
sweep is performed anywhere else in the product by this module -- the
substitution here only ever touches this one outgoing chat sentence, never
a path, a service name, or a binary name (this chat process never emits
those in the first place; it has no terminal).

**Telegram-redirect guard: the same block-never-rewrite shape, for spec
10.** The persona (``support_skill.py``) is asked -- with a hard bias
toward NOT doing it -- to send the client to the main Trix bot in Telegram
for anything that's clearly that bot's own job (reminders, scheduled
tasks, files, texts, general "how do I") rather than a malfunction, and to
open any such reply with an exact marker sentence
(``support_skill.TELEGRAM_REDIRECT_MARKER``) when it does. A prompt is
still just a request, so :func:`apply_telegram_redirect_guard` is the
structural backstop: it re-derives whether the last run's own
``telegram_token`` check came back green from the already-written run
record, and refuses -- never edits -- any reply carrying that marker while
it didn't. Sending someone to a bot the very same check just proved is
silent is exactly the "advice the client cannot act on" class spec 10
closes; the model's own opinion of the question is irrelevant once that
check is red.

**Context assembled here, not delegated to the skill/prompt-loading
pipeline.** ``hermes_cli/setup_wizard/support_skill.py`` explains why its
own content is a plain string, not a discoverable ``SKILL.md``. This module
adds exactly two more things per the brief: the persona
(``hermes_cli.default_soul.DEFAULT_SOUL_MD`` -- the same text a real Trix
Agent profile's ``SOUL.md`` is seeded with, so persona and skill agree with
what a client would read on ``/support`` and what the underlying agent
would say elsewhere) and the just-completed check pass's own report, read
back from ``trix_support.write_internal_report``'s own JSONL file by
``run_id`` -- never the client's own conversation with the main bot (this
module has no code path that reads gateway session state at all), and never
a raw log line (only each check's short, already-Russian ``error`` string,
not stdout/stderr/tracebacks).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from agent.auxiliary_client import aux_probe_mode, get_text_auxiliary_client
from hermes_cli import trix_support
from hermes_cli.default_soul import DEFAULT_SOUL_MD
from hermes_cli.setup_wizard import support_skill
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# See the module docstring's "hard cap on actions per client message" note.
MAX_ACTIONS_PER_MESSAGE = 3

_TOOL_NAME = "run_support_action"

_MSG_CHAT_UNAVAILABLE = (
    "Чат поддержки сейчас не открывается. "
    f"Напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}."
)
_MSG_CHAT_FALLBACK = (
    "Не получилось сформировать ответ. "
    f"Напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}."
)
_MSG_CHAT_EMPTY_REPLY = (
    "Не получилось понять, что ответить. "
    f"Опишите проблему подробнее, либо напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}."
)

# Shown instead of a redirect the model tried to make while the last run's
# own telegram_token check did NOT come back green (see
# apply_telegram_redirect_guard below). Honest about why the usual "напишите
# в Телеграм" answer isn't being given, and not a dead end: the client is
# still told to describe the problem so this bot keeps trying, with the
# escalation contact as the fallback -- never just a bare refusal.
_MSG_TELEGRAM_REDIRECT_BLOCKED = (
    "Обычно с этим помогает Trix в Телеграме, но сейчас, по последней проверке, "
    "он сам не отвечает — отправлять вас туда не буду, это не поможет. "
    "Опишите, что случилось, я посмотрю, что можно сделать здесь. Если не "
    f"получится — напишите в поддержку: {trix_support.SUPPORT_ESCALATION_CONTACT}."
)

_MAX_REPLY_TOKENS = 600

_OUTCOME_RU = {
    "good": "в порядке",
    "fixed": "было плохо, исправлено",
    "not_fixed": "не удалось исправить",
}


# ---------------------------------------------------------------------------
# Brand guard -- blocks, never silently rewrites past the point of confidence
# (see module docstring).
# ---------------------------------------------------------------------------

# (detection substring, safe replacement) -- replacement is applied only
# after a forbidden substring was already detected once; this list never
# runs proactively over clean text.
_KNOWN_SAFE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Hermes\s+Agent", re.IGNORECASE), "Trix Agent"),
    (re.compile(r"\bHermes\b", re.IGNORECASE), "Trix"),
    (re.compile(r"Nous\s+Research", re.IGNORECASE), ""),
    (re.compile(r"\bNous\b", re.IGNORECASE), ""),
    (re.compile(r"\bфорк\w*\b", re.IGNORECASE), ""),
    (re.compile(r"основан[а-я]*\s+на\b", re.IGNORECASE), "создан"),
)

# Detection is intentionally a plain substring/word scan over the SAME
# tokens the replacement table targets -- kept separate from the regexes
# above so "is this reply clean" never depends on a replacement regex
# quirk (e.g. a word-boundary miss) reporting false-clean.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = ("Hermes", "Nous", "форк", "основан на")


def _contains_forbidden(text: str) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in _FORBIDDEN_SUBSTRINGS)


def _apply_known_safe_replacements(text: str) -> str:
    for pattern, replacement in _KNOWN_SAFE_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _regenerate_once(client: Any, model: str, conversation: list[dict]) -> Optional[str]:
    nudge = list(conversation) + [
        {
            "role": "system",
            "content": (
                "Твой предыдущий ответ упомянул запрещённые слова. Ответь ещё раз, "
                "не называя других ассистентов, компаний или проектов, и не используя "
                "слова «форк» или «основан на». Просто опиши результат по-русски."
            ),
        }
    ]
    raw = _safe_completion(client, model, nudge, tools=None)
    if raw is None:
        return None
    content = (_message_to_dict(raw).get("content") or "").strip()
    return content or None


def apply_brand_guard(
    reply_text: str,
    *,
    client: Any = None,
    model: Optional[str] = None,
    conversation: Optional[list[dict]] = None,
) -> str:
    """Return a client-safe version of ``reply_text``, or the fixed fallback
    sentence. Never returns text that still contains a forbidden token --
    see the module docstring's "block, never rewrite past the point of
    confidence" note.
    """
    if not _contains_forbidden(reply_text):
        return reply_text

    cleaned = _apply_known_safe_replacements(reply_text)
    if not _contains_forbidden(cleaned):
        return cleaned

    if client is not None and model and conversation is not None:
        regenerated = _regenerate_once(client, model, conversation)
        if regenerated is not None:
            regenerated_clean = _apply_known_safe_replacements(regenerated)
            if not _contains_forbidden(regenerated_clean):
                return regenerated_clean

    return _MSG_CHAT_FALLBACK


# ---------------------------------------------------------------------------
# Telegram-redirect guard -- Layer 2 of the "send them to Telegram" feature
# (Layer 1 is the persona rule in support_skill.py). Blocks, never rewrites
# -- same shape as the brand guard above, for the same reason: a prompt is a
# request the model can get wrong, so the actual promise ("the client can
# act on this") is enforced here in code, not just asked for in text.
#
# Why this is checked against the LAST RUN's own telegram_token check and
# nothing else: a client opens this chat specifically because something is
# broken, often because the Telegram bot itself is silent. Telling them
# "write to it in Telegram" while that same run just proved the bot is
# unreachable is exactly spec 10's forbidden class -- advice the client is
# structurally unable to follow, about the very thing that's broken. The
# check is re-derived from the already-written run record
# (``_load_run_record``, shared with ``format_last_run_report``) rather than
# a fresh probe: this guard runs on every reply, so a second live network
# call per message would be wasteful, and re-deriving from the same run the
# model's own context already carries keeps the two from ever disagreeing.
# ---------------------------------------------------------------------------


def _telegram_check_passed(run_id: Optional[str]) -> bool:
    """``True`` only when the last run's own ``telegram_token`` check outcome
    was ``"good"`` (a live ``getMe`` against the configured bot succeeded).

    Every other case -- no ``run_id``, no matching run record, the run
    carrying no ``telegram_token`` entry at all, or that check's outcome
    being ``"not_fixed"`` (``telegram_token`` has no entry in
    ``trix_support.FIX_FOR_CHECK``, so it can never legitimately be
    ``"fixed"`` -- see that module's own ``CHECK_ORDER``/``FIX_FOR_CHECK``)
    -- returns ``False``. There is no positive evidence the main bot is
    reachable in any of those cases, and the safe default when a redirect
    would send someone somewhere unreachable is to refuse it, not to assume
    it's fine.
    """
    record = _load_run_record(run_id) if run_id else None
    if record is None:
        return False
    for check in record.get("checks", []):
        if check.get("check_id") == "telegram_token":
            return check.get("outcome") == "good"
    return False


def apply_telegram_redirect_guard(reply_text: str, *, run_id: Optional[str]) -> str:
    """Return ``reply_text`` unchanged, or the honest fallback sentence if it
    tries to redirect the client to the main Telegram bot while the last
    run's own ``telegram_token`` check did not pass.

    Detection is a plain substring check against
    ``support_skill.TELEGRAM_REDIRECT_MARKER`` -- the exact opening words
    the persona is asked to use, verbatim, whenever it redirects (see that
    module's own "Когда отправлять..." section). This is a structural
    backstop, not a rewrite: whatever the model itself believed about the
    client's question, a reply that carries the marker while Telegram is
    known-broken never reaches the client as written -- mirrors
    :func:`apply_brand_guard`'s own "block, never rewrite past the point of
    confidence" shape.
    """
    if support_skill.TELEGRAM_REDIRECT_MARKER not in reply_text:
        return reply_text
    if _telegram_check_passed(run_id):
        return reply_text
    return _MSG_TELEGRAM_REDIRECT_BLOCKED


# ---------------------------------------------------------------------------
# Action dispatch -- the security boundary (see module docstring).
# ---------------------------------------------------------------------------


def resolve_action(action_id: object) -> Optional[trix_support.SupportAction]:
    """Look ``action_id`` up in the closed registry by exact key.

    Returns ``None`` for anything that is not literally a key in
    ``trix_support.SUPPORT_ACTIONS`` -- including a non-string value, a
    string that merely resembles an id, or a string carrying shell/path
    content. There is no fallback parsing, normalization, or fuzzy match.
    """
    if not isinstance(action_id, str):
        return None
    return trix_support.SUPPORT_ACTIONS.get(action_id)


def execute_support_action(action_id: object) -> dict:
    """Run exactly one action chosen by the model, or refuse it.

    This is the one function in this module that is allowed to call a
    ``SupportAction.handler``. It is deliberately the *only* function that
    touches ``trix_support._execute`` (the same bounded, isolated execution
    primitive ``trix_support.run_support_pass`` uses for every check/fix in
    the deterministic pass), so a single action chosen mid-chat is bounded
    and isolated the exact same way.
    """
    action = resolve_action(action_id)
    if action is None:
        return {"executed": False, "reason": "unknown_action_id"}
    if not action.implemented or action.handler is None:
        return {"executed": False, "reason": "not_implemented"}

    result = trix_support._execute(action.action_id, action.handler, action.timeout_s)
    return {"executed": True, "ok": result.ok, "error": result.error}


# ---------------------------------------------------------------------------
# Context assembly -- persona + skill + last run's report + allowed actions.
# ---------------------------------------------------------------------------


def _support_log_path():
    return get_hermes_home() / "support" / "runs.jsonl"


def _load_run_record(run_id: str) -> Optional[dict]:
    path = _support_log_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "run" and record.get("run_id") == run_id:
            return record
    return None


def _label_for_check(check_id: str) -> str:
    action = trix_support.SUPPORT_ACTIONS.get(check_id)
    return action.label_ru if action is not None else check_id


def format_last_run_report(run_id: Optional[str]) -> str:
    """Compact, Russian, already-short summary of the check pass the client
    just ran -- never the raw internal detail dict, never a log line, only
    each check's own short ``error`` string (module docstring)."""
    record = _load_run_record(run_id) if run_id else None
    if record is None:
        return "Отчёт последнего прогона проверок недоступен."

    lines = ["Отчёт последнего прогона проверок (уже есть, повторно не выясняй):"]
    for check in record.get("checks", []):
        check_id = check.get("check_id", "?")
        label = _label_for_check(check_id)
        outcome = check.get("outcome", "?")
        outcome_ru = _OUTCOME_RU.get(outcome, outcome)
        error = (check.get("initial") or {}).get("error")
        line = f"- {label}: {outcome_ru}"
        if error:
            line += f" ({error})"
        lines.append(line)
    return "\n".join(lines)


def format_allowed_actions() -> str:
    lines = [
        "Список разрешённых действий "
        f"(используй только эти идентификаторы через {_TOOL_NAME}):"
    ]
    for action_id in sorted(trix_support.SUPPORT_ACTIONS):
        action = trix_support.SUPPORT_ACTIONS[action_id]
        state = "доступно" if action.implemented else "названо, но пока не реализовано"
        lines.append(f"- {action_id}: {action.label_ru} ({state})")
    return "\n".join(lines)


def build_system_prompt(run_id: Optional[str]) -> str:
    # DEFAULT_SOUL_MD здесь НЕ подставляется намеренно. Это общая персона
    # продукта, и она обещает то, чего у этого бота нет: «assist users with
    # a wide range of tasks including writing and editing code, creative
    # work». Для бота, умеющего пятнадцать починок из закрытого списка, это
    # ложь про самого себя, и она работает против переадресации посторонних
    # просьб к основному боту: одна часть промпта говорит «я умею писать
    # код», другая — «это не ко мне». SUPPORT_SKILL_MD открывается своей,
    # верной персоной («Ты — бот технической поддержки»), её и достаточно.
    parts = [
        support_skill.SUPPORT_SKILL_MD,
        format_last_run_report(run_id),
        format_allowed_actions(),
    ]
    return "\n\n".join(p for p in parts if p)


def _action_tool_schema() -> dict:
    action_ids = sorted(trix_support.SUPPORT_ACTIONS.keys())
    return {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": (
                "Выполнить одно действие из закрытого списка проверок и починок. "
                "Указывай только action_id из списка выше — свободный текст, "
                "команды и параметры передавать нельзя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "enum": action_ids,
                        "description": "Идентификатор действия из закрытого списка.",
                    }
                },
                "required": ["action_id"],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Wire-format helpers -- normalize whatever the OpenAI-compatible SDK object
# looks like into plain dicts, so the loop below never depends on which SDK
# class an auxiliary provider happens to return.
# ---------------------------------------------------------------------------


def _tool_call_to_dict(tc: Any) -> dict:
    if isinstance(tc, dict):
        return tc
    if hasattr(tc, "model_dump"):
        return tc.model_dump(exclude_none=True)
    fn = getattr(tc, "function", None)
    return {
        "id": getattr(tc, "id", "") or "",
        "type": "function",
        "function": {
            "name": getattr(fn, "name", "") or "",
            "arguments": getattr(fn, "arguments", "{}") or "{}",
        },
    }


def _message_to_dict(msg: Any) -> dict:
    if isinstance(msg, dict):
        base = dict(msg)
    elif hasattr(msg, "model_dump"):
        base = msg.model_dump(exclude_none=True)
    else:
        base = {"role": getattr(msg, "role", "assistant") or "assistant", "content": getattr(msg, "content", None)}
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            base["tool_calls"] = tool_calls
    base.setdefault("role", "assistant")
    tool_calls = base.get("tool_calls")
    if tool_calls:
        base["tool_calls"] = [_tool_call_to_dict(tc) for tc in tool_calls]
    return base


def _parse_action_id(tool_call: dict) -> Optional[str]:
    fn = tool_call.get("function") or {}
    raw_args = fn.get("arguments")
    if not raw_args:
        return None
    try:
        parsed = json.loads(raw_args)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    action_id = parsed.get("action_id")
    return action_id if isinstance(action_id, str) else None


def _safe_completion(client: Any, model: str, messages: list[dict], tools: Optional[list[dict]]):
    kwargs: dict = {"model": model, "messages": messages, "max_tokens": _MAX_REPLY_TOKENS}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    try:
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message
    except Exception:  # noqa: BLE001 -- any failure means "unavailable", not a crash
        logger.warning("support chat: auxiliary model call failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The bounded chat turn.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatTurnResult:
    reply: str
    history: tuple = field(default_factory=tuple)
    actions_executed: tuple = field(default_factory=tuple)


GetClientFn = Callable[[str], tuple]


def _resolve_client(
    get_client: Optional[GetClientFn], *, probe: bool = False
) -> tuple[Any, Optional[str]]:
    """The one place that turns ``get_client`` (or the real
    ``get_text_auxiliary_client``) into a ``(client, model)`` pair,
    swallowing any resolution failure into ``(None, None)`` -- shared by
    :func:`run_chat_turn` and :func:`is_chat_available` so the two never
    drift on what "the chat is reachable" means.

    ``probe=True`` (used only by :func:`is_chat_available`) resolves
    inside ``agent.auxiliary_client.aux_probe_mode()`` -- the same
    "is a client resolvable" mode Hermes's own tool-gating ``check_fn``s
    already use (see ``tools/vision_tools.py``'s own comment). Credential
    lookup and provider/fallback ORDER are identical to a real call; what
    probe mode skips is constructing a real SDK client and, for the Nous
    Portal branch specifically, its "recommended model" network fetch
    (``agent/auxiliary_client.py``'s own module comment: "the exact model
    is irrelevant to 'is Nous resolvable?'"). Never pass ``probe=True``
    for a resolution whose client will actually be used to make a
    completion call -- ``aux_probe_mode()`` returns a non-functional stub
    that raises on first real use, by design (its own docstring).
    """
    resolver = get_client or get_text_auxiliary_client
    try:
        if probe:
            with aux_probe_mode():
                client, model = resolver("support")
        else:
            client, model = resolver("support")
    except Exception:  # noqa: BLE001 -- resolution failure == unavailable
        logger.warning("support chat: auxiliary client resolution failed", exc_info=True)
        return None, None
    return client, model


def chat_unavailable_reply() -> str:
    """The exact sentence :func:`run_chat_turn` itself returns when the
    auxiliary client can't be resolved (``_MSG_CHAT_UNAVAILABLE``) — exposed
    so a caller that refuses a chat turn BEFORE ever calling
    :func:`run_chat_turn` (``support_view.py``'s server-side "no working
    key, no chat" gate) reuses the identical, already-reviewed Russian
    sentence instead of duplicating the literal string in a second place.
    """
    return _MSG_CHAT_UNAVAILABLE


def is_chat_available(get_client: Optional[GetClientFn] = None) -> bool:
    """Can the support chat open at all right now -- checked BEFORE a
    verdict is ever shown, never after (spec 15 follow-up, owner
    2026-09-03: "проверяй доступность до показа вердикта, не после").

    Resolves through ``_resolve_client(..., probe=True)`` -- see that
    function's own docstring for why this deliberately does NOT build a
    real SDK client or make a live network call: it costs the same
    config/credential read ``run_chat_turn`` pays for on its own first
    turn, nothing more, so calling this ahead of the verdict is cheap
    even though it runs synchronously inside the request handler. The one
    place a client's key most commonly breaks is exactly what this
    catches: a missing/invalid provider key means
    ``resolve_provider_client`` (`agent/auxiliary_client.py`) resolves to
    ``(None, None)`` rather than raising, so the ``client is None or not
    model`` check below is the same success condition
    :func:`run_chat_turn`'s own graceful-degradation branch already uses
    -- one definition of "available", shared by both.

    ``support_view.py`` calls this once per ``POST /api/support/run`` and
    uses the result to pick the verdict's own wording: when the model
    genuinely cannot be reached, saying "давайте разберёмся" and then
    silently failing to open a chat would be a false promise the client
    has no way to act on -- see that module's own module docstring.
    """
    client, model = _resolve_client(get_client, probe=True)
    return client is not None and bool(model)


def run_chat_turn(
    run_id: Optional[str],
    user_message: str,
    history: Sequence[dict],
    *,
    get_client: Optional[GetClientFn] = None,
) -> ChatTurnResult:
    """Run one client message through the support bot.

    ``get_client`` is injectable purely for tests -- production callers
    leave it as the default, ``get_text_auxiliary_client`` (module docstring:
    "the client's own key and provider"). Tests replace this one boundary
    with a fake client instead of mocking anything inside this module's own
    dispatch/brand-guard logic.
    """
    client, model = _resolve_client(get_client)

    if client is None or not model:
        return ChatTurnResult(reply=_MSG_CHAT_UNAVAILABLE, history=tuple(history), actions_executed=())

    system_prompt = build_system_prompt(run_id)
    conversation: list[dict] = [{"role": "system", "content": system_prompt}]
    conversation.extend(history)
    conversation.append({"role": "user", "content": user_message})

    tool_schema = _action_tool_schema()
    actions_executed: list[str] = []
    attempts_used = 0

    # Bounded regardless of model behavior: at most MAX_ACTIONS_PER_MESSAGE
    # tool-using rounds, plus a couple of spare rounds for a tool-free final
    # reply. See the module docstring for why the cap exists at all.
    for _round in range(MAX_ACTIONS_PER_MESSAGE + 2):
        tools_param = [tool_schema] if attempts_used < MAX_ACTIONS_PER_MESSAGE else None
        raw = _safe_completion(client, model, conversation, tools_param)
        if raw is None:
            return ChatTurnResult(
                reply=_MSG_CHAT_UNAVAILABLE, history=tuple(history), actions_executed=tuple(actions_executed)
            )

        message_dict = _message_to_dict(raw)
        tool_calls = message_dict.get("tool_calls") or []

        if not tool_calls:
            reply_text = (message_dict.get("content") or "").strip() or _MSG_CHAT_EMPTY_REPLY
            safe_reply = apply_brand_guard(reply_text, client=client, model=model, conversation=conversation)
            safe_reply = apply_telegram_redirect_guard(safe_reply, run_id=run_id)
            new_history = list(history) + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": safe_reply},
            ]
            return ChatTurnResult(
                reply=safe_reply, history=tuple(new_history), actions_executed=tuple(actions_executed)
            )

        conversation.append(message_dict)
        for tool_call in tool_calls:
            if attempts_used >= MAX_ACTIONS_PER_MESSAGE:
                outcome = {"executed": False, "reason": "action_limit_reached"}
            else:
                attempts_used += 1
                action_id = _parse_action_id(tool_call)
                if action_id is None:
                    outcome = {"executed": False, "reason": "malformed_call"}
                else:
                    outcome = execute_support_action(action_id)
                    if outcome.get("executed"):
                        actions_executed.append(action_id)
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", ""),
                    "content": json.dumps(outcome, ensure_ascii=False),
                }
            )

    # Should not normally be reached (the loop above always returns once
    # tool_calls is empty, and disabling tools at the cap forces that on the
    # very next round) -- kept as a safety net, never a silent hang.
    return ChatTurnResult(reply=_MSG_CHAT_FALLBACK, history=tuple(history), actions_executed=tuple(actions_executed))
