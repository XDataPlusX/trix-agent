"""Регрессия для «было → стало → обратно» в мастере настройки (apply.py).

Владелец спросил: клиент настроил что-то, потом ПЕРЕДУМАЛ и выбрал другое —
применится ли это и продолжит ли работать? Разбор кода показал, что
поведение само по себе верное, но регрессионной защиты у сценария
«переключился обратно» почти не было: в test_setup_wizard_apply.py полный
цикл A -> B -> A покрыт только для `extract_backend`. Для поиска, TTS, STT,
браузера и Camofox такого цикла не было вообще — эти тесты его закрывают.

Как и test_setup_wizard_apply.py (см. его докстринг): реальные импорты,
временный HERMES_HOME, никаких моков на пути записи — результат каждого
шага перечитывается с диска (config.yaml через yaml.safe_load, .env как
текст), а не подглядывается из внутренностей apply_settings().
"""
from pathlib import Path

import yaml

from hermes_constants import get_hermes_home

FORM = {
    "telegram_token": "123:abc",
    "allowed_users": "111,222",
    "proxy": "",
    "provider": {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "api_key": "sk-or-test",
        "base_url": "",
        "model": "z-ai/glm-5.2",
    },
}


def _config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text())


def _effective_config() -> dict:
    """Разрешённый конфиг — тем же загрузчиком (``load_config()``), которым
    пользуется сам ``apply_settings()`` и большинство CLI-подкоманд
    (CLAUDE.md, раздел «Config loaders»). Нужен там, где сырой YAML не
    годится: ``save_config(strip_defaults=True)`` (описано в докстринге
    apply.py — «never disabled, so a client's config.yaml doesn't balloon
    with schema defaults») вычищает из файла любое значение, буквально
    совпадающее с DEFAULT_CONFIG — а "edge" (tts.provider) и "local"
    (stt.provider) как раз являются такими значениями по умолчанию. Раунд
    A -> B -> A, возвращающийся ровно на дефолт, поэтому на диске может не
    оставить самого ключа — но РЕЗУЛЬТАТ (то, что реально прочитает
    работающий агент) от этого не меняется: слитый конфиг всё равно
    разрешается в "edge"/"local". Проверка через сырой ``yaml.safe_load``
    в этом случае дала бы ложный ``KeyError``, приняв оптимизацию записи
    за потерю настройки."""
    from hermes_cli.config import load_config

    return load_config()


def _env_text(home: Path) -> str:
    env_path = home / ".env"
    return env_path.read_text() if env_path.exists() else ""


# ---- 1. Поиск: ddgs -> tavily -> ddgs ------------------------------------


def test_search_backend_round_trip_ddgs_tavily_ddgs(tmp_path, monkeypatch):
    """Клиент выбрал бесплатный ddgs, затем передумал на Tavily (со своим
    ключом), затем вернулся на ddgs. На каждом шаге `web.search_backend`
    должен буквально совпадать с последним выбором — а НЕ залипать на
    промежуточном значении (это и есть регрессия, которую тест ловит: код
    читает `search_backend` из формы каждый раз заново и просто
    перезаписывает `web.search_backend`, без какого-либо «залипания»).

    Ключевая деталь по возврату: TAVILY_API_KEY НЕ удаляется из .env, когда
    клиент уходит с Tavily обратно на ddgs. Это осознанное решение продукта
    (apply.py, docstring `tool_provider.<category>: null`) — механизм
    `tool_env_clear`, который раньше стирал ключи при смене провайдера, был
    снят намеренно: сервер не может безопасно знать, не нужен ли этот же
    ключ ещё где-то (vision, auxiliary, credential pool). Оставшийся в .env
    ключ ничему не мешает — какой бэкенд реально используется, решает
    ТОЛЬКО значение `web.search_backend` в config.yaml, а не факт наличия
    ключа в .env.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()

    form_a = dict(FORM)
    form_a["search_backend"] = "ddgs"
    out = apply_settings(form_a)
    assert out["ok"], out
    assert _config(home)["web"]["search_backend"] == "ddgs"

    form_b = dict(FORM)
    form_b["search_backend"] = "tavily"
    form_b["search_env"] = {"key": "TAVILY_API_KEY", "value": "tvly-test"}
    out = apply_settings(form_b)
    assert out["ok"], out
    assert _config(home)["web"]["search_backend"] == "tavily"
    env_text = _env_text(home)
    assert "TAVILY_API_KEY=" in env_text and "tvly-test" in env_text

    form_c = dict(FORM)
    form_c["search_backend"] = "ddgs"
    out = apply_settings(form_c)
    assert out["ok"], out
    assert _config(home)["web"]["search_backend"] == "ddgs"

    # Ключ пережил возврат на ddgs — не был стёрт «на всякий случай».
    env_text = _env_text(home)
    assert "TAVILY_API_KEY=" in env_text and "tvly-test" in env_text


# ---- 2. TTS: edge -> elevenlabs -> edge ----------------------------------


def test_tts_provider_round_trip_edge_elevenlabs_edge(tmp_path, monkeypatch):
    """`tts.provider` — «поле-разграничитель» между несколькими
    сконфигурированными провайдерами (apply.py docstring,
    `_TOOL_PROVIDER_CONFIG_SECTIONS`). Клиент включил ElevenLabs, а затем
    реально вернулся на бесплатный Edge — итоговое разрешённое значение
    должно быть буквально "edge", а не оставшимся "elevenlabs" от
    предыдущего шага.

    Читаем через `_effective_config()` (мерженый `load_config()`), а не
    сырой YAML: "edge" — это же значение по умолчанию из DEFAULT_CONFIG, и
    `save_config(strip_defaults=True)` (см. её же докстринг) на шагах 1 и 3
    честно убирает буквальный ключ "tts.provider" из файла, раз он снова
    совпал с дефолтом — это оптимизация записи, а не потеря настройки, и
    `_effective_config()` видит итоговое значение так же, как его увидел бы
    работающий агент.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form_a = dict(FORM)
    form_a["tool_provider"] = {"tts": "edge"}
    out = apply_settings(form_a)
    assert out["ok"], out
    assert "tts.provider" in out["written"]
    assert _effective_config()["tts"]["provider"] == "edge"

    form_b = dict(FORM)
    form_b["tool_provider"] = {"tts": "elevenlabs"}
    form_b["tool_env"] = [{"key": "ELEVENLABS_API_KEY", "value": "el-test-key"}]
    out = apply_settings(form_b)
    assert out["ok"], out
    assert _effective_config()["tts"]["provider"] == "elevenlabs"

    form_c = dict(FORM)
    form_c["tool_provider"] = {"tts": "edge"}
    out = apply_settings(form_c)
    assert out["ok"], out
    assert "tts.provider" in out["written"]
    assert _effective_config()["tts"]["provider"] == "edge"


# ---- 3. STT: local -> openai -> local ------------------------------------


def test_stt_provider_round_trip_local_openai_local(tmp_path, monkeypatch):
    """Тот же «поле-разграничитель», что и для tts, но для `stt`
    (`_TOOL_PROVIDER_CONFIG_SECTIONS` включает "stt" — комментарий apply.py
    объясняет, что поле присоединилось 2026-08-20 вместе с категорией
    "Распознавание речи"). Клиент попробовал платный OpenAI, затем вернулся
    на локальный Whisper — `stt.provider` должен реально стать "local"
    обратно, а не остаться "openai".

    Как и для tts выше: "local" — дефолт DEFAULT_CONFIG, поэтому шаги 1 и 3
    сверяются через `_effective_config()` (мерженый `load_config()`), а не
    сырой YAML — см. её докстринг про `strip_defaults`.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form_a = dict(FORM)
    form_a["tool_provider"] = {"stt": "local"}
    out = apply_settings(form_a)
    assert out["ok"], out
    assert "stt.provider" in out["written"]
    assert _effective_config()["stt"]["provider"] == "local"

    form_b = dict(FORM)
    form_b["tool_provider"] = {"stt": "openai"}
    form_b["tool_env"] = [{"key": "VOICE_TOOLS_OPENAI_KEY", "value": "openai-test-key"}]
    out = apply_settings(form_b)
    assert out["ok"], out
    assert _effective_config()["stt"]["provider"] == "openai"

    form_c = dict(FORM)
    form_c["tool_provider"] = {"stt": "local"}
    out = apply_settings(form_c)
    assert out["ok"], out
    assert "stt.provider" in out["written"]
    assert _effective_config()["stt"]["provider"] == "local"


# ---- 4. Браузер: off -> browser-use -> off -------------------------------


def test_browser_backend_round_trip_off_browser_use_off(tmp_path, monkeypatch):
    """`browser.backend` пишется напрямую из `form.browser_backend` (apply.py,
    единственный писатель для этого поля). Клиент включил Browser Use CLI,
    затем вернулся на встроенный локальный Chromium ("off" — см. tools_view.py:
    "built-in tools over the local Chromium the installer set up") —
    итоговое значение должно снова стать "off".
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()

    form_a = dict(FORM)
    form_a["browser_backend"] = "off"
    out = apply_settings(form_a)
    assert out["ok"], out
    assert _config(home)["browser"]["backend"] == "off"

    form_b = dict(FORM)
    form_b["browser_backend"] = "browser-use"
    out = apply_settings(form_b)
    assert out["ok"], out
    assert _config(home)["browser"]["backend"] == "browser-use"

    form_c = dict(FORM)
    form_c["browser_backend"] = "off"
    out = apply_settings(form_c)
    assert out["ok"], out
    assert _config(home)["browser"]["backend"] == "off"


# ---- 5. Camofox: самый важный цикл ---------------------------------------


def test_camofox_round_trip_chromium_camofox_chromium(tmp_path, monkeypatch):
    """Самый важный из циклов: и Chromium, и Camofox шлют одинаковый
    `browser_backend: "off"` (apply.py's own docstring; tools_view.py:
    "off" means "built-in tools over the local Chromium the installer set
    up" — то есть буквально «встроенный локальный браузер»). Единственный
    признак, отличающий Camofox от обычного Chromium — наличие/отсутствие
    CAMOFOX_URL в .env; `is_camofox_mode()` (tools/browser_camofox.py)
    читает именно его, а не `browser.backend`/`browser.cloud_provider`.

    Цикл: Chromium (нет camofox_url) -> клиент включает Camofox (шлёт
    camofox_url) -> клиент возвращается на Chromium (клиентский JS явно
    шлёт `camofox_url: null` — это и есть сигнал очистки, см. apply.py's
    docstring про finding 5/7).

    После шага 2 CAMOFOX_URL должен появиться в .env.
    После шага 3 CAMOFOX_URL должен реально ИСЧЕЗНУТЬ из .env — иначе
    is_camofox_mode() продолжит считать, что клиент на Camofox, хотя он
    явно выбрал обратно Chromium.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()

    form_a = dict(FORM)
    form_a["browser_backend"] = "off"
    out = apply_settings(form_a)
    assert out["ok"], out
    assert _config(home)["browser"]["backend"] == "off"
    assert "CAMOFOX_URL=" not in _env_text(home)

    form_b = dict(FORM)
    form_b["browser_backend"] = "off"
    form_b["camofox_url"] = "http://localhost:9377"
    out = apply_settings(form_b)
    assert out["ok"], out
    assert _config(home)["browser"]["backend"] == "off"
    env_text = _env_text(home)
    assert "CAMOFOX_URL=" in env_text and "http://localhost:9377" in env_text

    form_c = dict(FORM)
    form_c["browser_backend"] = "off"
    form_c["camofox_url"] = None
    out = apply_settings(form_c)
    assert out["ok"], out
    assert _config(home)["browser"]["backend"] == "off"
    assert "CAMOFOX_URL" in out["removed"]
    assert "CAMOFOX_URL=" not in _env_text(home)


def test_camofox_url_key_absent_leaves_camofox_url_in_place(tmp_path, monkeypatch):
    """Известная асимметрия, зафиксированная намеренно (не баг, который надо
    чинить этим тестом — а поведение, которое нельзя случайно сломать).

    Разница между "camofox_url отсутствует в форме вовсе" и "camofox_url
    явно None" — это ровно граница между «не трогай» и «сотри» (apply.py:
    `camofox_url = form.get("camofox_url") if "camofox_url" in form else ""`
    — проверка через membership, а не через `.get()` с default). Если
    ключа "camofox_url" в словаре формы нет совсем (не то же самое, что
    JSON `null` — см. `_SubmitBody`'s docstring в app.py про то, как
    клиентский JS отличает эти два случая), apply_settings() обязан вести
    себя как «это поле не трогали» и оставить CAMOFOX_URL как есть.

    Почему это не может быть исправлено на сервере: и Chromium, и Camofox
    шлют один и тот же `browser_backend == "off"` — сервер физически не
    может по одному этому полю понять, что клиент имел в виду «выключи
    Camofox», а не «я вообще не касался этой настройки в этом запросе».
    Единственный однозначный сигнал — явный `null` от браузера, который
    видел актуальное состояние переключателя. Поэтому «ключа нет вовсе»
    остаётся штатным «не трогать», а не превращается в скрытую очистку.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()
    save_env_value_secure("CAMOFOX_URL", "http://localhost:9377")

    form = dict(FORM)
    form["browser_backend"] = "off"
    assert "camofox_url" not in form
    out = apply_settings(form)
    assert out["ok"], out
    assert "CAMOFOX_URL" not in out.get("removed", [])

    env_text = _env_text(home)
    assert "CAMOFOX_URL=" in env_text and "http://localhost:9377" in env_text
