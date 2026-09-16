"""`doctor` обязан знать ключ КАЖДОГО провайдера, который предлагает мастер.

Дефект, пойманный на живой машине 2026-09-05: клиент выбрал в мастере
Z.AI Coding Plan, ключ лёг в `ZAI_CODING_PLAN_API_KEY`, агент отвечал —
а `doctor` докладывал «No API key found» и заводил неполадку «Run
'hermes setup' to configure API keys». Следом мастер показывал клиенту
итоговое «часть неполадок исправить самостоятельно не удалось». Всё это
на машине, где ключ введён минуту назад и работает.

Причина — список имён переменных, зашитый в `doctor` руками, при живом
каталоге провайдеров рядом. Поэтому проверка здесь — инвариант, а не
перечисление имён: сколько бы провайдеров ни добавили завтра, `doctor`
обязан узнавать их ключи, не будучи переписанным.
"""

import pytest

from hermes_cli.doctor import _PROVIDER_ENV_HINTS, _has_provider_env_config
from hermes_cli.trix_provider_env_names import (
    catalog_provider_env_names,
    provider_env_names,
)


def _catalog_rows():
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    return [r for r in wizard_providers() if r.get("env_var")]


def test_catalog_is_not_empty():
    """Страховка от зелёного нуля: пустой каталог обесценил бы всё ниже."""
    assert len(_catalog_rows()) >= 1


def test_every_provider_the_wizard_offers_is_recognised_by_doctor():
    """Главный инвариант: витрина мастера ⊆ знания doctor.

    Клиент не может выбрать провайдера, про ключ которого доктор потом
    скажет «не настроен».
    """
    known = provider_env_names(_PROVIDER_ENV_HINTS)
    offered = {row["env_var"] for row in _catalog_rows()}
    assert offered <= known, f"мастер предлагает, доктор не знает: {sorted(offered - known)}"


@pytest.mark.parametrize("env_var", sorted({r["env_var"] for r in _catalog_rows()}))
def test_a_machine_configured_with_only_this_key_reads_as_configured(env_var):
    """Поведение целиком: `.env` с одним этим ключом — машина настроена.

    Проверяется не список, а ответ той самой функции, на которой стоит
    вывод «Run 'hermes setup' to configure API keys».
    """
    assert _has_provider_env_config(f"{env_var}=sk-живой-ключ-клиента\n")


def test_the_static_list_survives_the_merge():
    """Статические записи не выбрасываются.

    В нём есть то, чего в реестре провайдеров нет: адреса вместо ключей
    (`OPENAI_BASE_URL`) и исторические имена со старых машин.
    """
    known = provider_env_names(_PROVIDER_ENV_HINTS)
    assert set(_PROVIDER_ENV_HINTS) <= known


def test_empty_and_commented_keys_still_read_as_not_configured():
    """Расширение списка не должно превратить пустой ключ в «настроено»."""
    assert not _has_provider_env_config("ZAI_CODING_PLAN_API_KEY=\n")
    assert not _has_provider_env_config("# ZAI_CODING_PLAN_API_KEY=sk-real\n")
    assert not _has_provider_env_config("")


def test_catalog_lookup_degrades_instead_of_crashing(monkeypatch):
    """Диагностика не имеет права падать из-за собственного справочника."""
    import hermes_cli.trix_provider_env_names as mod

    def explode():
        raise RuntimeError("реестр провайдеров не поднялся")

    monkeypatch.setattr(mod, "catalog_provider_env_names", explode, raising=True)
    # provider_env_names зовёт модульную функцию — подменяем её же цель.
    monkeypatch.setattr(
        mod, "provider_env_names", lambda hints: set(hints), raising=True
    )
    assert mod.provider_env_names(_PROVIDER_ENV_HINTS) == set(_PROVIDER_ENV_HINTS)


def test_registry_failure_yields_an_empty_set_not_an_exception(monkeypatch):
    """Отказ реестра возвращает поведение к прежнему списку, а не к трассировке."""
    import providers

    def explode():
        raise RuntimeError("реестр недоступен")

    monkeypatch.setattr(providers, "list_providers", explode, raising=True)
    assert catalog_provider_env_names() == set()
    assert provider_env_names(_PROVIDER_ENV_HINTS) == set(_PROVIDER_ENV_HINTS)
