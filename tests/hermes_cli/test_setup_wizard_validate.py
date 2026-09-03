"""Input validation for the setup wizard — getMe via proxy, key probe, allowed users (spec §10.1)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock


def _client_returning(resp):
    c = MagicMock()
    c.__enter__ = lambda s: c
    c.__exit__ = lambda s, *a: False
    c.get.return_value = resp
    return c


def test_get_me_ok_extracts_username():
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {"username": "trixbot"}}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)) as mk:
        out = v.check_telegram_token("123:abc", None)
    assert out == {"ok": True, "username": "trixbot"}


def test_get_me_uses_proxy_when_given():
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {"username": "b"}}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)) as mk:
        v.check_telegram_token("123:abc", "socks5://u:p@h:1080")
    assert mk.call_args.kwargs.get("proxy") == "socks5://u:p@h:1080"
    assert mk.call_args.kwargs.get("timeout") == 10.0


def test_get_me_ok_without_username_is_invalid():
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {}}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)):
        out = v.check_telegram_token("123:abc", None)
    assert out["ok"] is False
    assert "BotFather" in out["error"]


def test_get_me_401_message_has_no_token():
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=401)
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)):
        out = v.check_telegram_token("123:SECRET", None)
    assert out["ok"] is False
    assert "SECRET" not in out["error"] and "BotFather" in out["error"]


def test_network_error_suggests_proxy():
    from hermes_cli.setup_wizard import validate as v

    c = MagicMock()
    c.__enter__ = lambda s: c
    c.__exit__ = lambda s, *a: False
    c.get.side_effect = OSError("unreachable")
    with patch.object(v.httpx, "Client", return_value=c):
        out = v.check_telegram_token("123:abc", None)
    assert out["ok"] is False and "Прокси" in out["error"]
    # Structural signal for page.py's step-2 "Проверить" button (owner
    # review, 2026-08-20): a network-shaped failure is tagged so the
    # client can show a distinct, non-alarming hint instead of implying
    # the token itself is wrong.
    assert out["network"] is True


def test_invalid_token_is_not_tagged_as_a_network_failure():
    """The 401/404 and "ok": false JSON branches are token problems, not
    network problems — ``network`` must be absent (falsy) there, unlike
    the connection/timeout/non-200/bad-JSON branches above."""
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=401)
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)):
        out = v.check_telegram_token("123:abc", None)
    assert out["ok"] is False
    assert not out.get("network")

    resp2 = MagicMock(status_code=200)
    resp2.json.return_value = {"ok": False}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp2)):
        out2 = v.check_telegram_token("123:abc", None)
    assert out2["ok"] is False
    assert not out2.get("network")


def test_telegram_user_ok_extracts_name_and_username():
    """Owner feedback п.4: getChat's positive answer surfaces both the
    display name and the @username so page.py can render "Это Имя
    (@username)"."""
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {"first_name": "Иван", "last_name": "Петров", "username": "ivanpetrov"}}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)):
        out = v.check_telegram_user("123:abc", "555", None)
    assert out == {"ok": True, "name": "Иван Петров", "username": "ivanpetrov"}


def test_telegram_user_uses_chat_id_query_param_and_proxy():
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {"first_name": "A", "username": None}}
    client = _client_returning(resp)
    with patch.object(v.httpx, "Client", return_value=client) as mk:
        v.check_telegram_user("123:abc", "555", "socks5://u:p@h:1080")
    assert mk.call_args.kwargs.get("proxy") == "socks5://u:p@h:1080"
    call = client.get.call_args
    assert call.args[0].endswith("/getChat")
    assert call.kwargs.get("params") == {"chat_id": "555"}


def test_telegram_user_chat_not_found_is_a_flat_false_never_an_error():
    """Owner feedback п.4: a user who has not pressed "Старт" yet gets a
    perfectly normal "chat not found" from Telegram — this must collapse
    to a bare ``{"ok": False}``, never a distinguishable error the client
    could mistake for "your id is wrong"."""
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=400)
    resp.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)):
        out = v.check_telegram_user("123:abc", "555", None)
    assert out == {"ok": False}


def test_telegram_user_network_failure_is_also_a_flat_false():
    from hermes_cli.setup_wizard import validate as v

    c = MagicMock()
    c.__enter__ = lambda s: c
    c.__exit__ = lambda s, *a: False
    c.get.side_effect = OSError("unreachable")
    with patch.object(v.httpx, "Client", return_value=c):
        out = v.check_telegram_user("123:abc", "555", None)
    assert out == {"ok": False}


def test_telegram_user_malformed_proxy_is_a_flat_false_no_network_call():
    """Finding 13's proxy-syntax guard applies here too — a bad proxy must
    not reach httpx.Client() at all, and the outcome is still the same
    flat ``{"ok": False}`` (never a distinguishable error)."""
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client") as mk:
        out = v.check_telegram_user("123:abc", "555", "1.2.3.4:1080")
    assert out == {"ok": False}
    mk.assert_not_called()


def test_telegram_user_answer_with_no_name_or_username_is_a_flat_false():
    """checked==True but nothing human-recognizable in the result — honest
    silence beats a blank "Это (@)"."""
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {}}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)):
        out = v.check_telegram_user("123:abc", "555", None)
    assert out == {"ok": False}


def test_allowed_users_normalizes():
    from hermes_cli.setup_wizard.validate import check_allowed_users

    assert check_allowed_users(" 123, 456 ")["normalized"] == "123,456"
    assert check_allowed_users("abc")["ok"] is False
    assert check_allowed_users("")["ok"] is False


def test_allowed_users_rejects_non_ascii_and_malformed_ints():
    from hermes_cli.setup_wizard.validate import check_allowed_users

    assert check_allowed_users("--5")["ok"] is False
    assert check_allowed_users("١٢٣")["ok"] is False
    assert check_allowed_users("²")["ok"] is False


def test_allowed_users_normalizes_leading_zeros_and_negative_ids():
    from hermes_cli.setup_wizard.validate import check_allowed_users

    out = check_allowed_users("007")
    assert out["ok"] is True and out["normalized"] == "7"

    out = check_allowed_users("-100123")
    assert out["ok"] is True and out["normalized"] == "-100123"


def test_provider_key_none_env_var_skips_network():
    from hermes_cli.setup_wizard import validate as v

    with patch("hermes_cli.setup_wizard.validate.probe_provider_key") as mk:
        out = v.check_provider_key(None, "sk-whatever")
    assert out == {"ok": True, "checked": False}
    mk.assert_not_called()


def test_provider_key_delegates_and_tags_checked():
    from hermes_cli.setup_wizard import validate as v

    with patch(
        "hermes_cli.setup_wizard.validate.probe_provider_key",
        return_value={"ok": True, "reachable": True, "message": ""},
    ) as mk:
        out = v.check_provider_key("OPENAI_API_KEY", "sk-live")
    mk.assert_called_once_with("OPENAI_API_KEY", "sk-live", timeout=10.0, proxy=None)
    assert out == {"ok": True, "reachable": True, "message": "", "checked": True}


def test_provider_key_forwards_the_form_proxy():
    """Owner requirement: a RU-hosted server can't reach OpenAI/OpenRouter/
    Anthropic directly — the live key probe must go through whatever
    proxy the client typed into the form's own proxy field."""
    from hermes_cli.setup_wizard import validate as v

    with patch(
        "hermes_cli.setup_wizard.validate.probe_provider_key",
        return_value={"ok": True, "reachable": True, "message": ""},
    ) as mk:
        v.check_provider_key("OPENAI_API_KEY", "sk-live", "socks5://u:p@h:1080")
    mk.assert_called_once_with("OPENAI_API_KEY", "sk-live", timeout=10.0, proxy="socks5://u:p@h:1080")


def _resp(status_code):
    return MagicMock(status_code=status_code)


# ---------------------------------------------------------------------------
# check_reachability (spec A4) — the redesigned "Прокси" step's
# auto-check-on-entry probe: 6 provider hosts + Telegram, parallel.
# ---------------------------------------------------------------------------


def _reachability_client_factory(status_by_url=None, default_status=200, raise_for_urls=()):
    """Build an ``httpx.Client`` factory whose ``.get(url)`` response (or
    exception) is keyed by the URL actually requested, not by call order —
    the seven probes run concurrently (ThreadPoolExecutor), so anything
    keyed on call sequence would be flaky."""
    status_by_url = status_by_url or {}

    def factory(*args, **kwargs):
        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = lambda s, *a: False
        proxy_kwarg = kwargs.get("proxy")
        timeout_kwarg = kwargs.get("timeout")

        def _get(url, *a, **k):
            client.last_proxy = proxy_kwarg
            client.last_timeout = timeout_kwarg
            if url in raise_for_urls:
                raise OSError(f"unreachable: {url}")
            return MagicMock(status_code=status_by_url.get(url, default_status))

        client.get = _get
        return client

    return factory


def test_check_reachability_all_reachable_with_proxy():
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=_reachability_client_factory()):
        out = v.check_reachability("socks5://u:p@h:1080")

    assert out == {
        "telegram": True,
        "via_proxy": {"openai-api": True, "anthropic": True, "openrouter": True},
        "direct": {"deepseek": True, "zai": True, "gemini": True},
        "proxy_invalid": False,
    }


def test_check_reachability_empty_proxy_is_a_legal_input():
    """Spec A4: an empty proxy is the "do I need one at all" check — not
    an error and not a no-op. Every target still gets a real
    (proxy-less) probe."""
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=_reachability_client_factory()):
        out_empty = v.check_reachability("")
        out_none = v.check_reachability(None)

    for out in (out_empty, out_none):
        assert out["telegram"] is True
        assert all(out["via_proxy"].values())
        assert all(out["direct"].values())


def test_check_reachability_direct_targets_never_use_the_proxy():
    """DeepSeek/GLM (Z.ai)/Gemini are ALWAYS probed with no proxy, even
    when one is configured — spec's "Напрямую, мимо прокси" column."""
    from hermes_cli.setup_wizard import validate as v

    seen_proxy_by_url: dict[str, str | None] = {}

    def factory(*args, **kwargs):
        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = lambda s, *a: False
        proxy_kwarg = kwargs.get("proxy")

        def _get(url, *a, **k):
            seen_proxy_by_url[url] = proxy_kwarg
            return MagicMock(status_code=200)

        client.get = _get
        return client

    with patch.object(v.httpx, "Client", side_effect=factory):
        v.check_reachability("socks5://u:p@h:1080")

    for name, url in v._REACHABLE_DIRECT_TARGETS.items():
        assert seen_proxy_by_url[url] is None, name
    for name, url in v._REACHABLE_VIA_PROXY_TARGETS.items():
        assert seen_proxy_by_url[url] == "socks5://u:p@h:1080", name
    assert seen_proxy_by_url[v._REACHABILITY_TELEGRAM_URL] == "socks5://u:p@h:1080"


def test_check_reachability_malformed_proxy_short_circuits_telegram_and_via_proxy():
    """A syntactically broken proxy can't be used for anything routed
    through it — telegram/via_proxy resolve to False WITHOUT a network
    call, while direct targets (proxy-independent) still get probed."""
    from hermes_cli.setup_wizard import validate as v

    called_urls: list[str] = []

    def factory(*args, **kwargs):
        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = lambda s, *a: False

        def _get(url, *a, **k):
            called_urls.append(url)
            return MagicMock(status_code=200)

        client.get = _get
        return client

    with patch.object(v.httpx, "Client", side_effect=factory):
        out = v.check_reachability("1.2.3.4:1080")  # no scheme

    assert out["telegram"] is False
    assert out["via_proxy"] == {"openai-api": False, "anthropic": False, "openrouter": False}
    assert all(out["direct"].values())
    assert out["proxy_invalid"] is True

    assert v._REACHABILITY_TELEGRAM_URL not in called_urls
    for url in v._REACHABLE_VIA_PROXY_TARGETS.values():
        assert url not in called_urls
    for url in v._REACHABLE_DIRECT_TARGETS.values():
        assert url in called_urls


def test_check_reachability_5xx_counts_as_unreachable():
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=_reachability_client_factory(default_status=502)):
        out = v.check_reachability(None)

    assert out["telegram"] is False
    assert not any(out["via_proxy"].values())
    assert not any(out["direct"].values())


def test_check_reachability_one_host_down_does_not_affect_the_others():
    """Mutation: only Telegram fails (OSError) — every other target must
    still resolve normally, proving the seven probes are isolated from
    each other, not one shared try/except around the whole batch."""
    from hermes_cli.setup_wizard import validate as v

    with patch.object(
        v.httpx,
        "Client",
        side_effect=_reachability_client_factory(raise_for_urls={v._REACHABILITY_TELEGRAM_URL}),
    ):
        out = v.check_reachability("socks5://u:p@h:1080")

    assert out["telegram"] is False
    assert all(out["via_proxy"].values())
    assert all(out["direct"].values())


def test_check_reachability_uses_the_short_timeout():
    from hermes_cli.setup_wizard import validate as v

    seen_timeouts: list[float | None] = []

    def factory(*args, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = lambda s, *a: False
        client.get = lambda url, *a, **k: MagicMock(status_code=200)
        return client

    with patch.object(v.httpx, "Client", side_effect=factory):
        v.check_reachability("socks5://u:p@h:1080")

    assert seen_timeouts and all(t == v._REACHABILITY_TIMEOUT for t in seen_timeouts)


def test_check_reachability_shape_never_leaks_per_host_detail():
    """Structural verdict only (spec A4): exactly these four top-level
    keys, and via_proxy/direct expose only their own three provider slugs
    — no extra host key ever leaks through."""
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=_reachability_client_factory()):
        out = v.check_reachability("socks5://u:p@h:1080")

    assert set(out) == {"telegram", "via_proxy", "direct", "proxy_invalid"}
    assert set(out["via_proxy"]) == {"openai-api", "anthropic", "openrouter"}
    assert set(out["direct"]) == {"deepseek", "zai", "gemini"}


# ---------------------------------------------------------------------------
# Finding 1 (this review pass): check_reachability() computed proxy_invalid
# internally but dropped it from the returned dict — the "Прокси" step's
# own auto-check couldn't tell a malformed proxy apart from "nothing
# answered" and told the client (who had already typed one) that they
# needed a proxy. See page.py's renderProxyVerdict() for the client side.
# ---------------------------------------------------------------------------


def test_check_reachability_reports_proxy_invalid_on_malformed_proxy():
    """Direct targets still get probed (proxy-independent — see
    test_check_reachability_malformed_proxy_short_circuits_telegram_and_via_proxy
    above), so this only asserts the new proxy_invalid flag, not that
    httpx.Client goes uncalled."""
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=_reachability_client_factory()):
        out = v.check_reachability("1.2.3.4:1080")  # no scheme
    assert out["proxy_invalid"] is True


def test_check_reachability_proxy_invalid_is_false_for_a_well_formed_proxy():
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=_reachability_client_factory()):
        out = v.check_reachability("socks5://u:p@h:1080")
    assert out["proxy_invalid"] is False


def test_check_reachability_proxy_invalid_is_false_for_an_empty_proxy():
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=_reachability_client_factory()):
        out = v.check_reachability("")
    assert out["proxy_invalid"] is False


def test_reachable_unit_tolerant_of_any_failure():
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client", side_effect=OSError("boom")):
        assert v._reachable("https://example.com/", None) is False


# ---------------------------------------------------------------------------
# check_proxy_syntax (finding 13's fix) — cheap, network-free format check.
# ---------------------------------------------------------------------------


def test_proxy_syntax_accepts_every_documented_scheme():
    from hermes_cli.setup_wizard import validate as v

    for proxy in (
        "socks5://user:pass@host:1080",
        "socks5h://host:1080",
        "http://host:8080",
        "https://host:8443",
        "socks5://u:p@h:1080",
    ):
        assert v.check_proxy_syntax(proxy) == {"ok": True}, proxy


def test_proxy_syntax_empty_or_missing_is_a_safe_no_op():
    from hermes_cli.setup_wizard import validate as v

    assert v.check_proxy_syntax("") == {"ok": True}
    assert v.check_proxy_syntax(None) == {"ok": True}


def test_proxy_syntax_rejects_bare_host_port_with_no_scheme():
    from hermes_cli.setup_wizard import validate as v

    out = v.check_proxy_syntax("1.2.3.4:1080")
    assert out["ok"] is False
    assert "прокси" in out["error"].lower() or "формат" in out["error"].lower()


def test_proxy_syntax_rejects_an_unsupported_scheme():
    from hermes_cli.setup_wizard import validate as v

    out = v.check_proxy_syntax("socks://user:pass@host:1080")
    assert out["ok"] is False


def test_proxy_syntax_rejects_a_scheme_with_no_host():
    from hermes_cli.setup_wizard import validate as v

    for proxy in ("socks5://", "http://:8080", "socks5://user:pass@"):
        out = v.check_proxy_syntax(proxy)
        assert out["ok"] is False, proxy


def test_get_me_bad_proxy_syntax_short_circuits_before_any_network_call():
    """Finding 13: check_telegram_token must reject a malformed proxy
    itself, before ever constructing an httpx.Client — and the result must
    NOT carry `network: True` (that flag means "reached out and failed",
    not "never even tried because the input was malformed")."""
    from hermes_cli.setup_wizard import validate as v

    with patch.object(v.httpx, "Client") as mk:
        out = v.check_telegram_token("123:abc", "1.2.3.4:1080")
    mk.assert_not_called()
    assert out["ok"] is False
    assert not out.get("network")
    assert out.get("proxy_invalid") is True


def test_get_me_valid_proxy_syntax_still_reaches_the_network_call():
    from hermes_cli.setup_wizard import validate as v

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"ok": True, "result": {"username": "b"}}
    with patch.object(v.httpx, "Client", return_value=_client_returning(resp)) as mk:
        out = v.check_telegram_token("123:abc", "socks5://u:p@h:1080")
    mk.assert_called_once()
    assert out == {"ok": True, "username": "b"}


# --- Часовой пояс (спека 11) -------------------------------------------


def test_check_timezone_accepts_a_real_zone_and_normalizes_whitespace():
    from hermes_cli.setup_wizard import validate as v

    assert v.check_timezone("  Europe/Moscow  ") == {
        "ok": True,
        "normalized": "Europe/Moscow",
    }


def test_check_timezone_rejects_an_empty_value_as_a_missing_answer():
    """Пусто — не выбор, а незаполненное поле: `hermes_time` трактует
    пустой ключ как «взять время сервера», то есть ровно та тихая
    подстановка чужого пояса, ради которой спека и появилась."""
    from hermes_cli.setup_wizard import validate as v

    out = v.check_timezone("")
    assert out["ok"] is False
    assert out["error"]


def test_check_timezone_rejects_a_zone_the_runtime_does_not_know():
    from hermes_cli.setup_wizard import validate as v

    out = v.check_timezone("Europe/Nowhere")
    assert out["ok"] is False
    assert out["error"]


def test_check_timezone_does_not_trust_the_browsers_list():
    """Ворота — на сервере. Строка, которой нет в базе зон, не проходит,
    даже если браузер уверяет, что выбрал её из нашего же списка."""
    from hermes_cli.setup_wizard import validate as v

    assert v.check_timezone("../../etc/passwd")["ok"] is False
    assert v.check_timezone("Москва")["ok"] is False
