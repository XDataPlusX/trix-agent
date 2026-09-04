"""Сообщения клиенту об отказе провайдера — наше дополнение к Hermes.

Апстримный `conversation_loop` печатает клиенту английскую строку с кодом
HTTP. Для клиента Trix это худший из финалов: агент замолчал, объяснение —
чужой язык и число. Модуль держит соответствие «причина отказа → фраза,
которую человек может понять и по которой может действовать».

Вся формулировка живёт в `locales/*.yaml`; здесь только выбор ветки, чтобы
правка текста не требовала правки кода.

Every ``t()`` call below carries an explicit ``default=``. ``agent.i18n``'s
``_load_catalog`` swallows any read/parse error and caches an EMPTY catalog
for that process -- on a client machine where the locale file failed to
read, ``t()`` would otherwise fall through to returning the bare dotted key
path (e.g. ``"trix.errors.provider.billing"``) straight to the customer.
The ``default=`` is the last line of defense against that: real English
text instead of a key path, even with a completely broken catalog.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.i18n import t

# Kept in sync with locales/en.yaml by hand -- these only render if BOTH the
# ru and en catalogs fail to load or are missing the key (see module
# docstring). They are not meant to be the primary copy; the catalog is.
_DEFAULT_BILLING = (
    "💳 The provider rejected the request: the key is out of funds. Top up "
    "the balance in the provider's dashboard — I'll continue right where I "
    "left off."
)
# Вторая — и последняя — фраза, которую клиент читает на отказе по деньгам.
# Она НЕ обязана повторять первую (round-final review): до этой правки
# терминальный статус и final_response звали одну и ту же
# client_error_message(billing), и клиент получал байт-в-байт одинаковое
# сообщение дважды подряд, причём ко второй копии был приклеен английский
# хвост _billing_or_entitlement_message с советом
# "/model <model> --provider <provider>" — синтаксисом с флагами, который в
# Telegram не набирается. Ровно та регрессия, которую задача 3 уже разводила
# обратно для семейства fallback.switched / switched_after_empty.
#
# Разделение обязанностей: первая строка говорит, ЧТО произошло (провайдер
# отказал, пополните баланс), эта — ЧТО ДЕЛАТЬ ДАЛЬШЕ (ход не выполнен,
# разговор цел, повторите сообщение после пополнения). Ни одна не является
# перефразировкой другой.
_DEFAULT_BILLING_NEXT_STEP = (
    "This turn did not run — nothing you asked for was done. The "
    "conversation itself is intact: once the balance is topped up, just "
    "send that last message again."
)
_DEFAULT_BILLING_TOP_UP_LINK = "Top-up page: {url}"
_DEFAULT_AUTH_FAILED = (
    "🔑 The provider rejected the key. Check it in the setup wizard — the "
    "address and password arrived by email when the machine was created."
)
_DEFAULT_POLICY_REJECTED = (
    "🚫 The model provider rejected the request — its safety filter "
    "triggered. Try rephrasing your question."
)
_DEFAULT_TLS = (
    "🔒 Couldn't verify the certificate while connecting to the provider. "
    "This is usually a proxy or a network issue on the machine."
)
_DEFAULT_UNAVAILABLE = (
    "⚠️ The provider isn't responding right now. This is on their end — "
    "please try again shortly. (code {status})"
)
_DEFAULT_UNAVAILABLE_NO_CODE = (
    "⚠️ The provider isn't responding right now. This is on their end — "
    "please try again shortly."
)
# These three reasons are deterministic per-request/per-config failures --
# unlike ``unavailable``/``unavailable_no_code`` above, telling the client
# "try again shortly" here is actively wrong: retrying the identical request
# reproduces the identical rejection every time (audited against
# agent/error_classifier.py's ``retryable=False`` FailoverReason members that
# can still reach the terminal non-retryable branch in
# agent/conversation_loop.py -- see that module's ``_emit_client_terminal_error``
# call site).
#
# Round-1 review fixes (task-2-report.md, "Раунд 1"):
#   * model_not_found pointed at `hermes model`, a shell command a
#     Telegram-only client cannot run. `/model` is the same switch, wired
#     into the gateway as a normal slash command (hermes_cli/commands.py) --
#     no CLI/shell access needed, and it drops the upstream binary name
#     ("hermes") from the client-facing phrase as a side effect.
#   * account_policy_blocked: "aggregator provider" was unexplained jargon
#     to a non-technical client. Plain "provider" says the same thing.
#   * malformed_request (bound to FailoverReason.format_error) used to
#     diagnose "the conversation is in an odd state" and suggest /reset --
#     but format_error is the classifier's catch-all for ANY unrecognized
#     4xx (405/409/415/422/451, ...) and any 400 that matches no specific
#     heuristic. That diagnosis is usually wrong for this population, and
#     /reset is irreversible (destroys conversation history) -- a confident,
#     often-wrong, destructive suggestion is worse than the vague-but-
#     harmless text it replaced. Say only what's certain (provider rejected
#     it, retrying as-is may not help) and point at a human, not a nuke.
_DEFAULT_MODEL_NOT_FOUND = (
    "🧭 The provider couldn't find that model. Check the model name with "
    "/model — it may have been renamed or isn't available on this key. "
    "Retrying won't fix this on its own."
)
_DEFAULT_ACCOUNT_POLICY_BLOCKED = (
    "🚫 The provider blocked the request because of the account's "
    "data-privacy policy settings, not because of anything in your "
    "message. Check the data policy in the provider's account settings "
    "(e.g. OpenRouter's Data Policy)."
)
_DEFAULT_MALFORMED_REQUEST = (
    "⚠️ The provider rejected this request. Retrying it unchanged may not "
    "help — if this keeps happening, let whoever manages this machine know."
)
# Round-5 review: this used to open with "The main provider didn't respond",
# the same false-for-402/401 diagnosis round 4 stripped out of the
# switch-attempt line below. It is worse here than there: this is the ONE
# line a client reads when the switch WORKS, so on a billing failover it was
# the only thing they saw and it told them the wrong story about why. It now
# reports the transition and who is answering, and leaves the cause to the
# terminal/attempt messages that actually know it.
_DEFAULT_FALLBACK_SWITCHED = (
    "🔄 Switched to the fallback provider — answering through {new_model} "
    "({new_provider}). The main one was {old_model} ({old_provider})."
)
# Distinct from _DEFAULT_FALLBACK_SWITCHED above on purpose (round-3 review
# fix). client_fallback_switch_attempt_message() below is for the BUFFERED
# line that fires the instant a switch is attempted -- it only reaches the
# client when the fallback attempt ALSO ends up failing (see
# _flush_status_buffer). Rendering it with "answering through the fallback"
# claims a completed, successful transition one line before the very next
# message tells the client the whole turn failed -- two contradictory
# statements in a row. This one says "trying", not "answering through".
#
# Round-4 review: it also says NOTHING about what happened to the primary.
# It used to open with "The main provider didn't respond", which is simply
# false for the two reasons that reach this line most often -- a 402 (the
# provider answered, refusing on money) and a 401 (it answered, refusing the
# key). Worse, it is redundant: this line is always preceded in the same
# buffer by client_fallback_attempt_message(), which already named the real
# cause ("the main key ran out of funds", "authentication failed", ...).
# Diagnosing the primary a second time could only ever contradict that line,
# never add to it, so the switch line now reports the transition and nothing
# else.
_DEFAULT_FALLBACK_SWITCH_ATTEMPT = (
    "🔄 Trying the fallback provider: {new_model} ({new_provider}). "
    "The main one was {old_model} ({old_provider})."
)
# The third member of the switch family, and the reason the other two are not
# enough (round-5 review). Upstream had two DIFFERENT sentences here: a
# generic "🔄 Primary model failed — switching to fallback: X via Y" inside
# try_activate_fallback(), and a specific "↻ Switched to fallback: X (Y)" at
# conversation_loop.py's empty-response-exhausted branch, which fires right
# after that helper returns. Task 3 localized both through one function and
# collapsed them into two byte-identical sentences, so a client whose
# fallback also failed read the same line twice in a row -- a regression we
# introduced, not an upstream defect.
#
# This key restores the distinction. Unlike the generic attempt line it is
# emitted AFTER the swap succeeded, and it exists on exactly one code path,
# so it can name that path's cause truthfully -- the model really did come
# back empty several times in a row; there is no reason ambiguity to get
# wrong here. It still must not claim the turn is being answered ("отвечаю
# через"): it is buffered, so it only ever reaches the client when the turn
# failed anyway (round-3 review).
_DEFAULT_FALLBACK_SWITCHED_AFTER_EMPTY = (
    "↻ Switched to the fallback model: {new_model} ({new_provider}). The "
    "previous one, {old_model} ({old_provider}), came back empty several "
    "times in a row."
)
_DEFAULT_ATTEMPT_BILLING = (
    "⚠️ The main key ran out of funds — switching to the fallback provider…"
)
_DEFAULT_ATTEMPT_LIMIT = (
    "⚠️ The main provider rate-limited requests — switching to the fallback…"
)
_DEFAULT_ATTEMPT_UNREACHABLE = (
    "⚠️ The main provider is unreachable — switching to the fallback…"
)
_DEFAULT_ATTEMPT_UPSTREAM = (
    "⚠️ The {upstream} model rate-limited the request — switching to the "
    "fallback model…"
)
_DEFAULT_ATTEMPT_UPSTREAM_UNKNOWN = (
    "⚠️ The upstream model rate-limited the request — switching to the "
    "fallback model…"
)
_DEFAULT_ATTEMPT_AUTH_FAILED = (
    "🔐 Authentication failed and couldn't be refreshed — switching to the "
    "fallback provider…"
)
_DEFAULT_ATTEMPT_CONTENT_FILTER = (
    "⚠️ The provider's safety filter blocked this request — switching to "
    "the fallback provider…"
)
_DEFAULT_ATTEMPT_EMPTY_RESPONSE = (
    "⚠️ The model returned an empty or malformed response — switching to "
    "the fallback provider…"
)


def _reason_name(reason: Any) -> str:
    """Имя причины, устойчивое к тому, что придёт enum, строка или None."""
    if reason is None:
        return ""
    return str(getattr(reason, "value", reason) or "")


def client_error_message(
    reason: Any,
    status_code: Optional[int] = None,
    *,
    is_auth: bool = False,
) -> str:
    """Фраза клиенту о том, почему ход не состоялся."""
    name = _reason_name(reason)
    status = "" if status_code is None else str(status_code)

    if name == "billing":
        # No status code here on purpose: the classifier can reach billing
        # from text patterns alone with no HTTP status (agent/error_classifier.py
        # ~1574-1608), and a customer staring at an empty balance doesn't need
        # a number -- "(code )" is worse than no code at all.
        return t("trix.errors.provider.billing", default=_DEFAULT_BILLING)
    if is_auth or name in ("auth", "auth_permanent"):
        # Same key the gateway's own provider-error path uses
        # (`_gateway_provider_error_reply` in gateway/run.py) -- one string,
        # not a parallel dup. It carries no {status} placeholder; the extra
        # kwarg is harmless (str.format ignores unused kwargs).
        return t(
            "trix.errors.provider.auth_failed",
            status=status,
            default=_DEFAULT_AUTH_FAILED,
        )
    if name == "content_policy_blocked":
        return t(
            "trix.errors.provider.policy_rejected",
            default=_DEFAULT_POLICY_REJECTED,
        )
    if name == "ssl_cert_verification":
        return t("trix.errors.provider.tls", default=_DEFAULT_TLS)
    if name == "model_not_found":
        return t(
            "trix.errors.provider.model_not_found",
            default=_DEFAULT_MODEL_NOT_FOUND,
        )
    if name == "provider_policy_blocked":
        return t(
            "trix.errors.provider.account_policy_blocked",
            default=_DEFAULT_ACCOUNT_POLICY_BLOCKED,
        )
    if name == "format_error":
        return t(
            "trix.errors.provider.malformed_request",
            default=_DEFAULT_MALFORMED_REQUEST,
        )
    if status:
        return t(
            "trix.errors.provider.unavailable",
            status=status,
            default=_DEFAULT_UNAVAILABLE,
        )
    return t(
        "trix.errors.provider.unavailable_no_code",
        default=_DEFAULT_UNAVAILABLE_NO_CODE,
    )


def client_billing_next_step_message(billing_url: Optional[str] = None) -> str:
    """Что клиенту делать дальше после отказа по деньгам.

    Отдельная от ``client_error_message(billing)`` намеренно: те две строки
    клиент читает подряд, и вторая не имеет права быть копией первой. Ссылка
    на страницу пополнения — единственное, что уцелело от прежнего
    английского хвоста: адрес сам по себе понятен и выполним, в отличие от
    совета набрать команду с флагами.
    """
    text = t(
        "trix.errors.provider.billing_next_step",
        default=_DEFAULT_BILLING_NEXT_STEP,
    )
    url = (billing_url or "").strip()
    if url:
        text += "\n" + t(
            "trix.errors.provider.billing_top_up_link",
            url=url,
            default=_DEFAULT_BILLING_TOP_UP_LINK,
        )
    return text


def client_fallback_message(
    old_model: str, old_provider: str, new_model: str, new_provider: str
) -> str:
    """Одноразовое уведомление об успешном переходе на запасного."""
    return t(
        "trix.errors.fallback.switched",
        old_model=old_model,
        old_provider=old_provider,
        new_model=new_model,
        new_provider=new_provider,
        default=_DEFAULT_FALLBACK_SWITCHED,
    )


def client_fallback_switch_attempt_message(
    old_model: str, old_provider: str, new_model: str, new_provider: str
) -> str:
    """Буферизуемая строка о ПОПЫТКЕ переключения — видна только при
    окончательном отказе хода (см. ``_buffer_status``/``_flush_status_buffer``
    в run_agent.py). Отдельная от ``client_fallback_message`` (round-3
    review): та говорит о свершившемся переходе и всплывает ровно один раз
    при успехе (``_pending_fallback_notice``). Если эту буферизуемую строку
    отдать через ту же функцию, клиент на пути отказа читает подряд два
    противоречащих друг другу утверждения: «отвечаю через запасного: X» и,
    следом, «провайдер не ответил после нескольких попыток» — переход,
    который тут же был назван свершившимся, на самом деле провалился.
    """
    return t(
        "trix.errors.fallback.switch_attempt",
        old_model=old_model,
        old_provider=old_provider,
        new_model=new_model,
        new_provider=new_provider,
        default=_DEFAULT_FALLBACK_SWITCH_ATTEMPT,
    )


def client_fallback_empty_response_switch_message(
    old_model: str, old_provider: str, new_model: str, new_provider: str
) -> str:
    """Буферизуемое подтверждение перехода ПОСЛЕ серии пустых ответов —
    ``agent/conversation_loop.py``'s empty-response-exhausted branch.

    Отдельная от ``client_fallback_switch_attempt_message`` намеренно
    (round-5 review). Та строка общая: её кладёт сам
    ``try_activate_fallback`` на любом переходе, ещё до того, как обмен
    состоялся. Эта — про один конкретный случай и звучит уже после успешного
    обмена, поэтому может назвать причину, не рискуя соврать. Если пустить
    оба места через одну функцию, клиент на пути отказа читает два
    тождественных предложения подряд: буфер выливается как есть,
    дедупликации в ``_flush_status_buffer`` нет.
    """
    return t(
        "trix.errors.fallback.switched_after_empty",
        old_model=old_model,
        old_provider=old_provider,
        new_model=new_model,
        new_provider=new_provider,
        default=_DEFAULT_FALLBACK_SWITCHED_AFTER_EMPTY,
    )


def client_fallback_attempt_message(
    reason: Any, upstream: Optional[str] = None
) -> str:
    """Строка о попытке перехода (буферизуется, видна при отказе хода)."""
    name = _reason_name(reason)
    if name == "upstream_rate_limit":
        if upstream:
            return t(
                "trix.errors.fallback.attempt_upstream",
                upstream=upstream,
                default=_DEFAULT_ATTEMPT_UPSTREAM,
            )
        # No upstream name given: don't splice a Russian placeholder word
        # ("провайдера") into what may render as an English sentence --
        # this key has no {upstream} slot to fill at all.
        return t(
            "trix.errors.fallback.attempt_upstream_unknown",
            default=_DEFAULT_ATTEMPT_UPSTREAM_UNKNOWN,
        )
    if name == "billing":
        return t(
            "trix.errors.fallback.attempt_billing",
            default=_DEFAULT_ATTEMPT_BILLING,
        )
    if name in ("timeout", "server_error", "overloaded"):
        return t(
            "trix.errors.fallback.attempt_unreachable",
            default=_DEFAULT_ATTEMPT_UNREACHABLE,
        )
    if name in ("auth", "auth_permanent"):
        # Distinct from the generic rate-limited default below -- an auth
        # failure told the client "rate limited" before this branch existed,
        # which is simply false and points them at the wrong fix.
        return t(
            "trix.errors.fallback.attempt_auth_failed",
            default=_DEFAULT_ATTEMPT_AUTH_FAILED,
        )
    if name == "content_policy_blocked":
        return t(
            "trix.errors.fallback.attempt_content_filter",
            default=_DEFAULT_ATTEMPT_CONTENT_FILTER,
        )
    if name == "invalid_response":
        # Sentinel used by conversation_loop.py's empty/malformed-response
        # eager-fallback branches, which never go through the FailoverReason
        # classifier (there is no HTTP error to classify -- the response
        # just came back empty).
        return t(
            "trix.errors.fallback.attempt_empty_response",
            default=_DEFAULT_ATTEMPT_EMPTY_RESPONSE,
        )
    return t("trix.errors.fallback.attempt_limit", default=_DEFAULT_ATTEMPT_LIMIT)
