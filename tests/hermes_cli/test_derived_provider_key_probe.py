"""Проверка ключа для провайдеров, которых нет в списке курированных.

`CREDENTIAL_PROBES` знает четыре провайдера — OpenRouter, OpenAI, xAI,
Gemini. Российский клиент берёт DeepSeek, Z.AI/GLM, Kimi, Qwen, и мастер
отвечал ему «Ключ провайдера не проверялся автоматически». Честно, но
ошибку в ключе человек узнавал не на шаге ключа, а когда бот молчал.

Адрес проверки выводится из каталога: все эти провайдеры говорят по
протоколу OpenAI и держат `GET {base_url}/models`.

Главное здесь — не полнота, а осторожность. Выведенный адрес это
догадка, поэтому проверке позволено НЕ пустить клиента только при
недвусмысленном отказе в самом ключе. Ошибка в догадке не должна
оборачиваться тем, что человек с исправным ключом не может закончить
настройку.
"""

import httpx
import pytest

from hermes_cli.credential_probes import CREDENTIAL_PROBES, probe_provider_key
from hermes_cli.trix_derived_probes import derived_probe_url, models_endpoint

_UNCURATED = "ZAI_CODING_PLAN_API_KEY"


def test_the_defect_provider_is_genuinely_uncurated():
    """Страховка: если запись появится, тесты ниже перестанут проверять ветку."""
    assert _UNCURATED not in CREDENTIAL_PROBES


def test_models_endpoint_is_built_from_the_base_url():
    assert models_endpoint("https://api.z.ai/api/coding/paas/v4") == (
        "https://api.z.ai/api/coding/paas/v4/models"
    )
    assert models_endpoint("https://api.z.ai/v1/") == "https://api.z.ai/v1/models"


@pytest.mark.parametrize("junk", ["", None, "   ", "api.z.ai/v1", "ftp://x/y"])
def test_nonsense_base_urls_produce_no_probe(junk):
    """Без пригодного адреса проверки нет — и это не ошибка."""
    assert models_endpoint(junk) is None


def test_the_clients_own_base_url_wins_over_the_catalog():
    """Клиент мог указать своё зеркало — проверять надо то, чем он пользуется."""
    url = derived_probe_url(_UNCURATED, base_url="https://mirror.example/v1")
    assert url == "https://mirror.example/v1/models"


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture
def fake_http(monkeypatch):
    """Подменяет httpx.Client так, чтобы запрос не уходил наружу."""

    calls: list[httpx.Request] = []

    def install(handler):
        real_client = httpx.Client

        def factory(**kwargs):
            kwargs.pop("proxy", None)

            def recording(request):
                calls.append(request)
                return handler(request)

            return real_client(transport=_transport(recording), **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)
        return calls

    return install


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_still_blocks_the_client(fake_http, status):
    """Недвусмысленный отказ в ключе — единственное, ради чего всё затевалось."""
    fake_http(lambda req: httpx.Response(status))
    result = probe_provider_key(
        _UNCURATED, "sk-bad-key", base_url="https://api.z.ai/api/coding/paas/v4"
    )
    assert result["ok"] is False
    assert result["reason"] == "auth"


def test_an_empty_account_blocks_too(fake_http):
    fake_http(lambda req: httpx.Response(402))
    result = probe_provider_key(
        _UNCURATED, "sk-key", base_url="https://api.z.ai/api/coding/paas/v4"
    )
    assert result["ok"] is False
    assert result["reason"] == "billing"


def test_a_good_key_is_reported_as_actually_checked(fake_http):
    """`reachable` — то, на чём мастер строит «ключ проверен»."""
    fake_http(lambda req: httpx.Response(200, json={"data": []}))
    result = probe_provider_key(
        _UNCURATED, "sk-good-key", base_url="https://api.z.ai/api/coding/paas/v4"
    )
    assert result["ok"] is True
    assert result["reachable"] is True


def test_the_probe_sends_the_key_as_a_bearer_token(fake_http):
    calls = fake_http(lambda req: httpx.Response(200, json={}))
    probe_provider_key(_UNCURATED, "sk-secret", base_url="https://api.z.ai/v1")
    assert calls[0].headers["Authorization"] == "Bearer sk-secret"
    assert str(calls[0].url) == "https://api.z.ai/v1/models"


@pytest.mark.parametrize("status", [404, 405, 500, 503])
def test_a_provider_without_models_never_blocks_the_client(fake_http, status):
    """Догадка об адресе не удалась — это ничего не говорит о ключе.

    Самая важная проверка файла: цена ошибки здесь — человек с исправным
    ключом не может закончить настройку. Поэтому такой ответ обязан
    читаться как «проверить не удалось», а не как «ключ плохой».
    """
    fake_http(lambda req: httpx.Response(status))
    result = probe_provider_key(
        _UNCURATED, "sk-working-key", base_url="https://api.example/v1"
    )
    assert result["ok"] is True
    assert result["reachable"] is False


def test_a_dead_network_never_blocks_the_client(fake_http):
    def boom(request):
        raise httpx.ConnectError("сеть недоступна")

    fake_http(boom)
    result = probe_provider_key(
        _UNCURATED, "sk-working-key", base_url="https://api.example/v1"
    )
    assert result["ok"] is True
    assert result["reachable"] is False


def test_no_base_url_and_no_catalog_entry_means_no_probe(fake_http):
    calls = fake_http(lambda req: httpx.Response(500))
    result = probe_provider_key("СОВСЕМ_НЕИЗВЕСТНАЯ_ПЕРЕМЕННАЯ", "sk-key")
    assert result == {
        "ok": True,
        "reachable": False,
        "message": "",
        "reason": None,
        "status_code": None,
    }
    assert calls == []


def test_curated_providers_keep_their_stricter_behaviour(fake_http):
    """Курированную запись правка не смягчает: там адрес известен точно."""
    fake_http(lambda req: httpx.Response(500))
    result = probe_provider_key("OPENAI_API_KEY", "sk-key")
    assert result["ok"] is False
    assert result["reason"] == "other"
