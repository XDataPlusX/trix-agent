"""Мастер настройки не переворачивает значение, которое клиенту объяснено.

Финальное ревью сняло исполнением такую картину: кладём клиентский шаблон
``assets/config/trix-config.yaml`` в свежий ``HERMES_HOME``, зовём
``_apply_default_agent_settings(load_config())`` — и ``session_reset.mode``
из ``idle`` становится ``none``, а все 142 комментария остаются на месте.
То есть восемь строк русского объяснения продолжают описывать сброс
разговора через трое суток, а под ними стоит «никогда». Задача 9 выключена,
и увидеть это можно только сравнив поведение машины с текстом комментария.

Достижимо это на обычной установке: ``scripts/install.sh`` зовёт
``hermes setup``, а его ``is_existing`` ложно, пока ключа провайдера нет в
``.env`` — то есть всегда, когда оператор вводит ключ внутри мастера.

Ревью назвало и асимметрию покрытия, ради которой написан этот файл:
мутация ``mode: idle`` → ``none`` В ШАБЛОНЕ убивалась тестом, а такая же по
смыслу мутация в коде мастера проходила молча — пин был защищён с одной
стороны и не защищён с другой.

Тесты ниже утверждают ОТНОШЕНИЕ («что мастер оставил == что документирует
шаблон»), а не конкретное значение: сменится решение о сроке — тесты
останутся верными.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import hermes_cli.setup as setup_mod
from hermes_cli.config import get_config_path, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _template_text() -> str:
    return TRIX_TEMPLATE_PATH.read_text(encoding="utf-8")


def _comment_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))


@pytest.fixture
def client_machine():
    """Машина клиента: в ``HERMES_HOME`` лежит наш шаблон целиком.

    ``HERMES_HOME`` уже уведён в временный каталог автоюзной фикстурой
    ``_isolate_hermes_home`` (tests/conftest.py) — здесь только раскладка
    файла, который клиент реально получает при установке.
    """
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_template_text(), encoding="utf-8")
    return config_path


def _documented_mode() -> str:
    return yaml.safe_load(_template_text())["session_reset"]["mode"]


def test_the_template_documents_a_reset_mode_at_all():
    """Опора остальных тестов: если шаблон перестанет закреплять режим,
    они превратятся в проверку None == None и молча перестанут ловить."""
    mode = _documented_mode()
    assert mode, "шаблон обязан закреплять session_reset.mode"


def test_recommended_defaults_keep_the_mode_the_client_config_documents(
    client_machine,
):
    config = load_config()
    assert config["session_reset"]["mode"] == _documented_mode()

    setup_mod._apply_default_agent_settings(config)

    assert config["session_reset"]["mode"] == _documented_mode(), (
        "мастер переписал режим, который объяснён комментарием в конфиге"
    )
    saved = yaml.safe_load(client_machine.read_text(encoding="utf-8"))
    assert saved["session_reset"]["mode"] == _documented_mode(), (
        "в записанном файле режим разошёлся с шаблоном"
    )


def test_comments_and_the_value_under_them_survive_together(client_machine):
    """Ревью мерило только комментарии — они были целы, а значение под ними
    перевёрнуто. Проверять надо пару: и текст объяснения, и то, что он
    объясняет."""
    before = client_machine.read_text(encoding="utf-8")
    config = load_config()

    setup_mod._apply_default_agent_settings(config)

    after = client_machine.read_text(encoding="utf-8")
    # Не «столько же»: сохранение конфига дописывает свои закомментированные
    # блоки. Проверяется, что ни одного объяснения не потеряно.
    assert _comment_lines(after) >= _comment_lines(before), (
        "мастер потерял комментарии клиентского конфига"
    )
    assert (
        yaml.safe_load(after)["session_reset"]["mode"] == _documented_mode()
    ), "комментарии целы, а значение под ними перевёрнуто"


def test_blank_slate_also_keeps_the_documented_mode(client_machine):
    """Второй такой же сайт. Blank Slate выключает возможности, но не
    вправе переворачивать документированное значение под комментарием."""
    config = load_config()

    setup_mod._blank_slate_minimize_config(config)

    assert config["session_reset"]["mode"] == _documented_mode()


def test_without_a_client_answer_the_wizard_still_writes_its_own_default():
    """Обратная сторона: правка не должна была отменить умолчание мастера
    там, где клиент ни на что не отвечал (обычная установка Hermes без
    нашего шаблона)."""
    config = load_config()
    config.pop("session_reset", None)

    setup_mod._apply_default_agent_settings(config)

    assert config["session_reset"]["mode"] == "none"


def test_blank_slate_without_a_client_answer_writes_its_own_default():
    config = load_config()
    config.pop("session_reset", None)

    setup_mod._blank_slate_minimize_config(config)

    assert config["session_reset"]["mode"] == "none"


def test_an_empty_value_is_not_treated_as_a_client_answer():
    """Ключ, оставленный незаполненным, — не выбор клиента. Иначе пустая
    строка в конфиге навсегда оставила бы шлюз без режима сброса."""
    config = load_config()
    config["session_reset"] = {"mode": ""}

    setup_mod._apply_default_agent_settings(config)

    assert config["session_reset"]["mode"] == "none"


# --- Часовой пояс (спека 11) -------------------------------------------


def test_setup_does_not_clobber_a_timezone_the_client_answered(client_machine):
    """Ответ клиента переживает раскладку «рекомендованных значений».

    Сегодня `hermes setup` ключа `timezone` не касается вовсе, и поимённой
    защиты в `trix_config_pins` ему поэтому не заводили. Держать это на
    сегодняшнем чтении кода нельзя: следующая строка, дописанная в
    `_apply_default_agent_settings`, снимет пояс молча — ровно так уже
    случилось с `display.tool_progress`. Тест утверждает не отсутствие
    строки в исходнике, а исполнением проверенное свойство.
    """
    config = load_config()
    config["timezone"] = "Asia/Yekaterinburg"

    setup_mod._apply_default_agent_settings(config)

    assert config["timezone"] == "Asia/Yekaterinburg", (
        "мастер снял часовой пояс, выбранный клиентом"
    )


def test_blank_slate_also_keeps_the_clients_timezone(client_machine):
    """Второй такой же сайт: у мастера их два, и защищать надо оба."""
    config = load_config()
    config["timezone"] = "Asia/Vladivostok"

    setup_mod._blank_slate_minimize_config(config)

    assert config["timezone"] == "Asia/Vladivostok"
