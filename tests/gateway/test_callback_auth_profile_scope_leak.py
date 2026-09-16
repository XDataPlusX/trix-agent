"""RAF-176: колбэк кнопки судился в секретном скоупе ЧУЖОГО профиля.

Уточнение владельца (2026-09-16): его основной чат обслуживает профиль
``default``, и кнопка ``/update`` ломалась именно там. Пустой
``TELEGRAM_ALLOWED_USERS=`` при этом лежал ФИЗИЧЕСКИ только в
``profiles/system_admin/.env`` — у ``default`` список заполнен. Значит отказ
пришёл из чужого скоупа, а не из своего: на пути колбэка
``current_secret_scope()`` отдавал ``system_admin``.

Механика утечки (``test_polling_task_freezes_the_profile_scope`` ниже
воспроизводит её на голом asyncio):

``_start_one_profile_adapters`` ждёт ``connect()`` вторичного профиля ВНУТРИ
``_profile_runtime_scope``. Внутри ``connect()`` PTB поднимает
``Application.start()`` / ``updater.start_polling()`` — долгоживущие задачи,
созданные через ``asyncio.create_task``, а он снимает СНИМОК контекста. Скоуп
профиля остаётся вмороженным в эти задачи на весь процесс, хотя блок ``with``
давно закрылся. Всякий колбэк, который адаптер потом раздаёт — из любого чата,
любого профиля — судился по одному этому списку.

Почему сообщения при этом ходили: список пользователей в
``_is_user_authorized`` читается через ``_auth_env``, а он при ПУСТОМ значении
проваливается в ``os.environ`` и находит там заполненный главный ``.env``.
Кнопка же падала в ``_platform_gate_env``, который под мультиплексом
авторитетен и никуда не проваливается. Отсюда ровно та асимметрия, которую
владелец описал как «пишу — доходит, жму — мёртвая».
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.profile_routing import ProfileRoute
from gateway.run import GatewayRunner
from gateway.session import SessionSource


OWNER_ID = "351788701"
OWNER_CHAT_ID = "351788701"
ADMIN_THREAD_ID = "570710"


# ── прод-форма установки владельца ──────────────────────────────────────

@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Дерево профилей в форме прода: default заполнен, system_admin пуст."""
    home = tmp_path / "hermes"
    (home / "profiles" / "system_admin").mkdir(parents=True)

    # Главный .env — заполнен (как у владельца).
    (home / ".env").write_text(
        f"TELEGRAM_ALLOWED_USERS={OWNER_ID}\n"
        f"TELEGRAM_GROUP_ALLOWED_CHATS={OWNER_CHAT_ID}\n",
        encoding="utf-8",
    )
    # Профильный .env — ПУСТАЯ строка. Ровно то, что нашёл оператор, и то,
    # что временный фикс на проде замаскировал.
    (home / "profiles" / "system_admin" / ".env").write_text(
        "TELEGRAM_ALLOWED_USERS=\nTELEGRAM_GROUP_ALLOWED_CHATS=\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def multiplex_active(monkeypatch):
    """Включает мультиплекс и гарантированно снимает флаг после теста.

    ``_MULTIPLEX_ACTIVE`` — модульная глобаль, а не contextvar: она описывает
    режим развёртывания. Оставленный включённым флаг заставляет ``get_secret``
    падать на любом бесскоупном чтении в последующих тестах файла.
    """
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    return secret_scope


def _make_runner(monkeypatch, *, routes=(), multiplex=True):
    """Настоящий ``GatewayRunner`` без ``__init__`` — как в соседних тестах.

    Собирается ``object.__new__``, потому что полный старт runner-а поднимает
    сессии, адаптеры и статус-файлы. Проверяемая цепочка
    (``_make_adapter_auth_check`` → ``_authorize_in_source_profile_scope`` →
    ``_resolve_profile_home_for_source`` → ``_is_user_authorized``) — настоящая.
    """
    from types import SimpleNamespace

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        multiplex_profiles=multiplex,
        profile_routes=list(routes),
        multiplex_profile_allowlist=None,
        platforms={},
    )
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_stores = {}
    runner.pairing_store = None
    runner.session_store = None
    return runner


def _observe_scope_during_auth(runner, monkeypatch):
    """Подменяет ``_is_user_authorized`` наблюдателем скоупа.

    Возвращает список, в который пишется значение
    ``TELEGRAM_ALLOWED_USERS``, видимое АВТОРИТЕТНЫМ чтением
    (``_platform_gate_env``) в момент решения — то есть ровно то, что владелец
    просил выяснить: какой скоуп видит путь колбэка.
    """
    from gateway.authz_mixin import _platform_gate_env

    seen = []

    def _spy(source):
        seen.append(_platform_gate_env("TELEGRAM_ALLOWED_USERS"))
        return True

    monkeypatch.setattr(runner, "_is_user_authorized", _spy, raising=False)
    return seen


# ── 1. Сама утечка: скоуп на пути колбэка ───────────────────────────────

def test_callback_auth_reads_the_event_profile_not_the_frozen_one(
    hermes_home, multiplex_active, monkeypatch
):
    """Колбэк из чата default судится списком default, а не system_admin.

    Вмороженный скоуп ставится ровно так, как его оставляет
    ``connect()`` вторичного профиля: установлен и не снят.
    """
    from agent import secret_scope

    runner = _make_runner(monkeypatch)
    seen = _observe_scope_during_auth(runner, monkeypatch)
    check = runner._make_adapter_auth_check(Platform.TELEGRAM)

    frozen = secret_scope.set_secret_scope({"TELEGRAM_ALLOWED_USERS": ""})
    try:
        check(OWNER_ID, "dm", OWNER_CHAT_ID)
    finally:
        secret_scope.reset_secret_scope(frozen)

    assert seen == [OWNER_ID], (
        "решение принято в чужом скоупе: путь колбэка прочитал "
        f"TELEGRAM_ALLOWED_USERS={seen!r} вместо списка профиля default"
    )


def test_callback_auth_installs_a_scope_when_none_is_frozen(
    hermes_home, multiplex_active, monkeypatch
):
    """Первичный адаптер коннектится БЕЗ скоупа — его тоже надо поставить.

    Бесскоупный ``_platform_gate_env`` проваливается в ``os.environ``, а под
    мультиплексом там может лежать список другого профиля, пробрriдженный из
    его ``config.yaml`` по принципу «кто первый» (#72348). «Скоупа нет» —
    это не «скоуп правильный».
    """
    from agent import secret_scope

    runner = _make_runner(monkeypatch)
    check = runner._make_adapter_auth_check(Platform.TELEGRAM)

    installed = []

    def _spy(source):
        installed.append(secret_scope.current_secret_scope())
        return True

    monkeypatch.setattr(runner, "_is_user_authorized", _spy, raising=False)

    assert secret_scope.current_secret_scope() is None
    check(OWNER_ID, "dm", OWNER_CHAT_ID)

    assert installed and installed[0] is not None, (
        "решение принято вообще без скоупа — чтение провалилось в os.environ"
    )
    assert installed[0].get("TELEGRAM_ALLOWED_USERS") == OWNER_ID


def test_group_button_flips_allow_deny_on_the_leaked_scope(
    hermes_home, multiplex_active, monkeypatch
):
    """Наблюдаемый переворот решения, а не только «не тот скоуп».

    Чат группы/темы авторизуется ``TELEGRAM_GROUP_ALLOWED_CHATS``, и этот
    список читается через ``_platform_gate_env`` — авторитетно, без провала в
    ``os.environ``. Поэтому здесь чужой скоуп даёт именно ОТКАЗ, а не
    случайное спасение через fallback ``_auth_env``.
    """
    from agent import secret_scope

    runner = _make_runner(monkeypatch)
    check = runner._make_adapter_auth_check(Platform.TELEGRAM)

    frozen = secret_scope.set_secret_scope({"TELEGRAM_GROUP_ALLOWED_CHATS": ""})
    try:
        allowed = check(OWNER_ID, "group", OWNER_CHAT_ID)
    finally:
        secret_scope.reset_secret_scope(frozen)

    assert allowed is True, (
        "кнопка в разрешённом чате отклонена: групповой список прочитан из "
        "чужого (пустого) скоупа"
    )


def test_single_profile_gateway_keeps_its_exact_path(monkeypatch, tmp_path):
    """Без мультиплекса скоуп не ставится вовсе — поведение байт-в-байт прежнее."""
    runner = _make_runner(monkeypatch, multiplex=False)

    called = {}

    def _resolve(source):  # pragma: no cover - должен остаться невызванным
        called["resolved"] = True
        return tmp_path

    monkeypatch.setattr(
        runner, "_resolve_profile_home_for_source", _resolve, raising=False
    )
    monkeypatch.setattr(runner, "_is_user_authorized", lambda s: True, raising=False)

    check = runner._make_adapter_auth_check(Platform.TELEGRAM)
    assert check(OWNER_ID, "dm", OWNER_CHAT_ID) is True
    assert "resolved" not in called


def test_unresolvable_profile_denies_instead_of_guessing(
    hermes_home, multiplex_active, monkeypatch
):
    """Не смогли определить профиль — отказ, а не суд в случайном скоупе."""
    runner = _make_runner(monkeypatch)

    def _boom(source):
        raise RuntimeError("route names a profile this gateway does not serve")

    monkeypatch.setattr(
        runner, "_resolve_profile_home_for_source", _boom, raising=False
    )
    monkeypatch.setattr(runner, "_is_user_authorized", lambda s: True, raising=False)

    check = runner._make_adapter_auth_check(Platform.TELEGRAM)
    assert check(OWNER_ID, "dm", OWNER_CHAT_ID) is False


# ── 2. thread_id: чат и его тема — РАЗНЫЕ профили ───────────────────────

_ADMIN_ROUTE = ProfileRoute(
    name="system-admin-topic",
    platform="telegram",
    profile="system_admin",
    chat_id=OWNER_CHAT_ID,
    thread_id=ADMIN_THREAD_ID,
)


def test_thread_id_selects_the_topic_profile(
    hermes_home, multiplex_active, monkeypatch
):
    """Кнопка в теме 570710 судится списком system_admin, в чате — default.

    Это и есть причина, по которой ``thread_id`` обязан доехать до проверки:
    один и тот же ``chat_id`` принадлежит двум профилям, и различает их
    только тема.
    """
    runner = _make_runner(monkeypatch, routes=[_ADMIN_ROUTE])
    seen = _observe_scope_during_auth(runner, monkeypatch)
    check = runner._make_adapter_auth_check(Platform.TELEGRAM)

    check(OWNER_ID, "forum", OWNER_CHAT_ID, thread_id=ADMIN_THREAD_ID)
    check(OWNER_ID, "dm", OWNER_CHAT_ID)

    assert seen == ["", OWNER_ID], (
        "тема и основной чат разошлись не по своим профилям: "
        f"прочитано {seen!r}"
    )


def _bare_adapter():
    """Адаптер без ``__init__`` — форма, в которой его строят соседние тесты.

    ``BasePlatformAdapter`` абстрактен, поэтому минимальный конкретный
    наследник; ``object.__new__`` мимо ``__init__`` — намеренно: именно так
    выясняется, что «атрибута нет» и «проверка не зарегистрирована» должны
    значить одно и то же.
    """
    from gateway.platforms.base import BasePlatformAdapter

    class _Bare(BasePlatformAdapter):
        async def connect(self):  # pragma: no cover - не вызывается
            return True

        async def disconnect(self):  # pragma: no cover - не вызывается
            return None

        async def get_chat_info(self, chat_id):  # pragma: no cover
            return {}

        async def send(self, *a, **kw):  # pragma: no cover
            return None

        @property
        def name(self):
            return "telegram"

    return object.__new__(_Bare)


def test_adapter_forwards_thread_id_to_the_check(monkeypatch):
    """Телеграм-адаптер доносит ``thread_id`` до зарегистрированной проверки."""
    adapter = _bare_adapter()
    seen = {}

    def check(user_id, chat_type=None, chat_id=None, thread_id=None):
        seen.update(
            user_id=user_id, chat_type=chat_type, chat_id=chat_id, thread_id=thread_id
        )
        return True

    adapter.set_authorization_check(check)
    assert adapter._is_sender_authorized(
        OWNER_ID, "forum", OWNER_CHAT_ID, thread_id=ADMIN_THREAD_ID
    ) is True
    assert seen["thread_id"] == ADMIN_THREAD_ID


def test_slack_interactive_forwards_thread_ts(monkeypatch):
    """Slack-кнопка в треде доносит ``thread_ts`` до проверки.

    В Slack ``thread_id`` источника — это ``thread_ts`` (так во всём адаптере).
    Без него тред-маршрут никогда не совпадает, и кнопка судится списком
    КАНАЛА, то есть более широким. Тот же дефект, что у телеграмной темы.
    """
    import sys
    import types

    # Slack-адаптер тянет slack_sdk на импорте; тест смотрит только на
    # проброс аргумента, поэтому SDK заменяется заглушкой, как в соседних
    # slack-тестах.
    if "slack_sdk" not in sys.modules:
        fake = types.ModuleType("slack_sdk")
        fake.WebClient = type("WebClient", (), {"__init__": lambda *a, **kw: None})
        errors = types.ModuleType("slack_sdk.errors")
        errors.SlackApiError = type("SlackApiError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "slack_sdk", fake)
        monkeypatch.setitem(sys.modules, "slack_sdk.errors", errors)

    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = object.__new__(SlackAdapter)
    adapter._message_handler = None  # замыкание/отсутствие — runner не ловится
    seen = {}

    def check(user_id, chat_type=None, chat_id=None, thread_id=None):
        seen["thread_id"] = thread_id
        return True

    adapter._authorization_check = check

    assert adapter._is_interactive_user_authorized(
        "U123", channel_id="C999", thread_ts="1726000000.000100"
    ) is True
    assert seen["thread_id"] == "1726000000.000100"


def test_legacy_three_arg_check_is_not_broken(monkeypatch):
    """Проверки без ``thread_id`` (тесты, сторонние адаптеры) не ломаются.

    Арность решается один раз при регистрации: ловить ``TypeError`` вокруг
    вызова нельзя — он неотличим от ``TypeError`` из тела проверки, и
    повторный вызов переигрывал бы решение об авторизации.
    """
    adapter = _bare_adapter()
    adapter.set_authorization_check(
        lambda user_id, chat_type=None, chat_id=None: user_id == OWNER_ID
    )

    assert adapter._is_sender_authorized(
        OWNER_ID, "forum", OWNER_CHAT_ID, thread_id=ADMIN_THREAD_ID
    ) is True
    assert adapter._is_sender_authorized("999", "dm", OWNER_CHAT_ID) is False


def test_check_assigned_without_the_setter_still_gets_thread_id():
    """Проверка, присвоенная в обход сеттера, всё равно получает ``thread_id``.

    Соседние тесты присваивают ``_authorization_check`` напрямую, и адаптеры,
    собранные через ``object.__new__``, не проходят ``__init__`` — кэш арности
    в обоих случаях отсутствует, а не равен ``False``.
    """
    adapter = _bare_adapter()
    seen = {}

    def check(user_id, chat_type=None, chat_id=None, thread_id=None):
        seen["thread_id"] = thread_id
        return True

    adapter._authorization_check = check
    assert adapter._is_sender_authorized(
        OWNER_ID, "forum", OWNER_CHAT_ID, thread_id=ADMIN_THREAD_ID
    ) is True
    assert seen["thread_id"] == ADMIN_THREAD_ID


# ── 3. Механика: почему скоуп вообще оказался чужим ─────────────────────

def test_polling_task_freezes_the_profile_scope(multiplex_active):
    """Задача, созданная внутри ``_profile_runtime_scope``, уносит скоуп с собой.

    Это причина утечки, а не следствие: ``asyncio.create_task`` снимает снимок
    контекста, поэтому долгоживущие задачи PTB, поднятые в ``connect()``
    вторичного профиля, продолжают видеть его секреты и после выхода из блока
    ``with``. Тест держит инвариант на голом asyncio — он остаётся верным и
    если однажды исчезнет сам PTB.
    """
    from agent import secret_scope
    from gateway.authz_mixin import _platform_gate_env

    observed = {}

    async def ptb_update_processor(label):
        await asyncio.sleep(0)  # переживает закрытие блока `with` ниже
        observed[label] = _platform_gate_env("TELEGRAM_ALLOWED_USERS")

    async def main():
        token = secret_scope.set_secret_scope({"TELEGRAM_ALLOWED_USERS": ""})
        try:
            task = asyncio.create_task(ptb_update_processor("secondary"))
        finally:
            secret_scope.reset_secret_scope(token)
        await task

    asyncio.run(main())

    assert observed["secondary"] == "", (
        "снимок контекста больше не уносит скоуп в задачу — утечка, которую "
        "чинит _authorize_in_source_profile_scope, изменила форму; перепроверь "
        "обоснование правки"
    )
