"""Живое зеркало провайдера/модели/прокси из ``default`` в ``system_admin``.

**Почему это не разовое клонирование.** ``hermes business setup`` создаёт
профиль администратора один раз, но провайдер, модель, ключи и прокси
клиента продолжают меняться — обычно через мастер настройки, который правит
ТОЛЬКО ``default``. Разовый ``--clone`` даёт снимок на момент установки;
дальше темы администрирования тихо расходится с тем, чем на самом деле
думает основной агент, и это всплывает через дни как «админ-бот перестал
отвечать» без видимой причины на момент отказа.

**То же правило, что уже есть в** :mod:`hermes_cli.trix_config_defaults`
**— «изменение клиента превыше всего, но если он ничего не менял, то мы
меняем за него»** (формулировка владельца, 2026-09-10). Там сравнение идёт
между ШАБЛОНОМ и клиентским ``config.yaml``; здесь — между ЖИВЫМ значением
``default`` и профилем ``system_admin``. Устроено буквально так же:

- отдельный sidecar (:data:`MIRROR_STATE_RELATIVE`, внутри HERMES_HOME
  профиля-администратора) хранит по каждому ключу либо
  ``{"mirrored": <значение>}`` — последнее, что мы САМИ туда положили, либо
  ``{"owned_by_client": true}`` — администратор правил это поле профиля
  ``system_admin`` руками, и с этого момента мы его не трогаем НИКОГДА;
- поле сравнивается не с шаблоном, а с ТЕКУЩИМ значением ``default``:
  разошлось с тем, что мы отгружали, — значит администратор поправил его
  сам, и это НАВСЕГДА его значение, даже если ``default`` потом снова
  сменится;
- если совпадает с тем, что мы отгружали, а ``default`` уже другой —
  переносим новое значение и запоминаем его как отгруженное.

**Отличие от trix_config_defaults, и почему оно необходимо.** Тот модуль
никогда не трогает путь, которого нет у клиента вовсе — «отсутствующее
дописывает trix_config_sync, не наша работа»: у ЕГО клиента отсутствие
ключа — осознанный выбор владельца документа. Здесь же профиль
``system_admin`` — не независимый документ, а НАШЕ создание, и «в нём нет
model.provider» на первом прогоне означает ровно то, из-за чего написан
этот модуль (боевая поломка, спека 21 review): ``create_profile`` без
клонирования оставила профиль без единого ключа, и агент отвечал ошибкой
провайдера. Поэтому первая встреча с отсутствующим на стороне admin ключом
здесь means «почини» (запиши значение default), а не «запомни и не
трогай» — единственная содержательная правка поверх трёхстороннего правила
trix_config_defaults.

**Что зеркалится:** выбор провайдера и модель (``model.*`` в
``config.yaml`` — каждый скалярный лист блока, а не фиксированный список
имён, чтобы не отставать от новых полей), учётные данные провайдера в
``.env`` (объединение ``env_vars`` каждого зарегистрированного
:class:`providers.ProviderProfile` — тот же приём, каким
``gateway.config.PLATFORM_TOKEN_ENV_NAMES`` задаёт список токенов ботов, а
не жёстко прописанное имя одного провайдера) и восемь канонических имён
прокси (``HTTP(S)_PROXY``/``ALL_PROXY``/``NO_PROXY`` и их lower-case пары —
тот же набор, что досевает :mod:`hermes_cli.trix_proxy_backfill`).

**Что НИКОГДА не зеркалится:**

- учётные данные ботов мессенджеров (иначе профиль администратора завёл бы
  СВОЙ адаптер поверх токена, которым уже владеет ``default`` —
  ``Conflict: terminated by other getUpdates request``). Исключение
  строится из ``gateway.config.PLATFORM_TOKEN_ENV_NAMES`` плюс категории
  ``"messaging"`` в ``hermes_cli.config_defaults.OPTIONAL_ENV_VARS`` —
  belt-and-braces, а не одно захардкоженное имя;
- ``TELEGRAM_PROXY`` — это прокси для САМОГО адаптера Telegram, а у
  ``system_admin`` нет собственного адаптера, который бы его использовал;
- всё, что ``hermes business setup`` намеренно делает РАЗНЫМ между
  профилями: закрытый список тулсетов (``platform_toolsets``),
  ``approvals.deny``, ``SOUL.md``, ``skills.external_dirs``,
  ``terminal.*`` (в т.ч. ``terminal.backend``/``terminal.docker_volumes``),
  весь ``gateway.*``. Ни один из этих путей не попадает в перечисление
  ``model.*``/провайдерских переменных, но :data:`_NEVER_MIRROR_CONFIG_TOP_KEYS`
  всё равно называет их явно как страховку от будущей правки, расширяющей
  перечисление по ошибке.

**Когда это запускается.** :func:`sync_admin_profile_from_default` вызывает
и ``hermes_cli.trix_business.run_setup`` (на каждом прогоне — это и есть
путь починки уже сломанной боевой машины), и
``gateway/builtin_hooks/business_admin_mirror.py`` при каждом старте шлюза
(шлюз уже перезапускается после любой правки мастера настройки — второго
триггера заводить не нужно). Идемпотентна в обе стороны: прогон без
расхождений ничего не пишет и возвращает пустой отчёт.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from utils import atomic_write_text

from hermes_cli.trix_business import _ensure_value, _profile_home_scope
from hermes_cli.trix_config_defaults import _is_scalar, _lookup, _scalar_paths
from hermes_cli.trix_config_sync import (
    _dominant_newline,
    _find_key_line,
    _is_safe_block_parent_line,
    _read_raw_text,
)

logger = logging.getLogger(__name__)

#: Относительно HERMES_HOME профиля-администратора — рядом с маркером
#: незавершённой DM-темы (``hermes_cli.trix_business.DM_TOPIC_MARKER_RELATIVE``),
#: в том же подкаталоге ``business_setup``.
MIRROR_STATE_RELATIVE = Path("business_setup") / "mirror_baseline.json"

#: Тоже в ``business_setup`` — factus «этот профиль зеркалит настройки из
#: source_profile». Пишется/подтверждается на каждом ``run_setup`` (не
#: только при создании), чтобы уже существующая, поставленная ДО этой
#: функции машина получила маркер на следующем прогоне ``hermes business
#: setup`` — том самом, что чинит боевую поломку.
MIRROR_SOURCE_MARKER_RELATIVE = Path("business_setup") / "mirror_source.json"

#: Восемь канонических имён прокси — тот же набор, что
#: ``hermes_cli.trix_proxy_backfill`` досевает в уже существующий ``.env``.
#: ``TELEGRAM_PROXY`` сюда намеренно не входит (см. модульный докстринг).
_PROXY_ENV_NAMES: tuple = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "NO_PROXY", "no_proxy",
)

#: Верхнеуровневые ключи ``config.yaml``, которые ``hermes business setup``
#: намеренно делает РАЗНЫМИ между профилями. Страховка (см. докстринг) —
#: единственный путь, который зеркало реально читает, это ``model``.
_NEVER_MIRROR_CONFIG_TOP_KEYS: frozenset = frozenset({
    "platform_toolsets", "approvals", "terminal", "gateway",
})

#: Сентинел «писать нечего», отличимый от легитимного значения ``None``
#: (например ``model.max_tokens: null``).
_NO_WRITE = object()


# ---------------------------------------------------------------------------
# Источники «что можно зеркалить» — выводятся из тех же реестров, которыми
# пользуется сам шлюз, а не захардкожены здесь по одному имени за раз.
# ---------------------------------------------------------------------------

def _provider_credential_env_names() -> set:
    try:
        import providers

        names: set = set()
        for profile in providers.list_providers():
            for name in getattr(profile, "env_vars", None) or ():
                if isinstance(name, str) and name:
                    names.add(name)
        return names
    except Exception:
        logger.warning("mirror: не удалось перечислить env_vars провайдеров", exc_info=True)
        return set()


def _messaging_env_names() -> set:
    try:
        from hermes_cli.config_defaults import OPTIONAL_ENV_VARS

        return {
            name
            for name, meta in OPTIONAL_ENV_VARS.items()
            if isinstance(meta, dict) and meta.get("category") == "messaging"
        }
    except Exception:
        return set()


def _bot_token_env_names() -> set:
    try:
        from gateway.config import PLATFORM_TOKEN_ENV_NAMES

        return set(PLATFORM_TOKEN_ENV_NAMES.values())
    except Exception:
        return set()


def _mirror_eligible_env_names() -> set:
    """Имена ``.env``, которые зеркалу вообще разрешено трогать.

    Belt-and-braces: даже если провайдерский плагин когда-нибудь ошибочно
    перечислит токен бота в своих ``env_vars``, исключение мессенджинговых
    имён снимет его отсюда всё равно.
    """
    eligible = _provider_credential_env_names() | set(_PROXY_ENV_NAMES)
    exclude = _messaging_env_names() | _bot_token_env_names()
    return eligible - exclude


def _parse_env_text(text: str) -> dict:
    """Разобрать произвольный текст ``.env`` без привязки к файлу профиля —
    та же нормализация, что :func:`hermes_cli.config.load_env`, но не
    завязанная на ``get_env_path()``/кэш по mtime, потому что здесь
    разбирается ШАБЛОН, а не файл текущего профиля."""
    from hermes_cli.config import _parse_env_value, _sanitize_env_lines

    out: dict = {}
    for line in _sanitize_env_lines(text.splitlines(keepends=True)):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            if line.startswith("export "):
                line = line[7:]
            k, _, v = line.partition("=")
            out[k.strip()] = _parse_env_value(v)
    return out


def _fresh_profile_env_defaults() -> dict:
    """Значения, с которыми ``.env`` СВЕЖЕСОЗДАННОГО профиля выходит из
    ``create_profile`` (``hermes_cli.profiles``, seed из
    ``resolve_env_template``). Почти всё в шаблоне — пустая заготовка
    (``OPENROUTER_API_KEY=``), но не всё: ``NO_PROXY``/``no_proxy`` несут
    непустой список хостов по умолчанию. Без этого сравнения первая же
    синхронизация приняла бы этот список за ручную правку администратора
    и никогда не подхватила бы настоящий ``NO_PROXY`` дефолтного профиля.
    """
    try:
        from hermes_cli.config import get_project_root
        from hermes_cli.config_template import resolve_env_template

        template_path = resolve_env_template(get_project_root())
        if template_path is None:
            return {}
        return _parse_env_text(Path(template_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _model_scalar_paths(config_data: dict) -> list:
    node = config_data.get("model") if isinstance(config_data, dict) else None
    if not isinstance(node, dict):
        return []
    return _scalar_paths(node, ("model",))


# ---------------------------------------------------------------------------
# Sidecar-состояние — тот же формат, что trix_config_defaults, но со своим
# файлом (свой профиль, свой смысл записи).
# ---------------------------------------------------------------------------

def _state_path(admin_home: Path) -> Path:
    return Path(admin_home) / MIRROR_STATE_RELATIVE


def _load_state(admin_home: Path) -> dict:
    try:
        raw = _state_path(admin_home).read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_state(admin_home: Path, data: dict) -> None:
    try:
        path = _state_path(admin_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        try:
            # Хранит значения провайдерских ключей — тот же уровень
            # защиты, что у .env самого профиля.
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    except Exception:
        logger.warning("mirror: не удалось сохранить baseline-состояние", exc_info=True)


def ensure_mirror_source_marker(admin_home: Path, *, source_profile: str = "default") -> bool:
    """Записать/подтвердить маркер «этот профиль зеркалит source_profile».

    Идемпотентна: не переписывает файл, если содержимое уже совпадает.
    Возвращает ``True``, если файл был записан или изменён.
    """
    path = Path(admin_home) / MIRROR_SOURCE_MARKER_RELATIVE
    payload = {"source_profile": source_profile}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current == payload:
            return False
    except Exception:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return True
    except Exception:
        logger.warning("mirror: не удалось записать маркер источника", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Трёхстороннее правило — общее для config.yaml и .env.
# ---------------------------------------------------------------------------

def _decide(
    state: dict,
    new_state: dict,
    key: str,
    *,
    admin_has: bool,
    admin_value: Any,
    default_value: Any,
) -> Tuple[Any, bool]:
    """Вернуть ``(значение_для_записи_или__NO_WRITE, впервые_разошлось)``.

    ``впервые_разошлось`` — True ровно в момент перехода записи в
    ``owned_by_client`` (не на каждом последующем прогоне) — операторский
    отчёт обязан сказать об этом один раз, а не шуметь на каждом старте
    шлюза.
    """
    record = state.get(key)

    if record is None:
        # Каждый свежесозданный профиль сеется из общего шаблона
        # (``assets/config/trix.env.example`` / ``trix-config.yaml``), а
        # тот перечисляет ВСЕ известные ключи с пустым значением
        # (``OPENROUTER_API_KEY=``, ``model: ''``) — это заготовка, не
        # правка администратора. Пустая строка на стороне admin здесь
        # приравнена к «ключа нет вовсе», иначе первая же синхронизация
        # объявила бы каждый провайдерский ключ «правкой администратора» и
        # никогда бы его не заполнила — ровно боевая поломка (defect 1).
        admin_is_blank = not admin_has or admin_value is None or (
            isinstance(admin_value, str) and not admin_value.strip()
        )
        if admin_is_blank:
            # Ничего не помним и на стороне администратора пусто — это и
            # есть путь починки: значение default записывается впервые.
            new_state[key] = {"mirrored": default_value}
            return default_value, False
        if admin_value == default_value:
            new_state[key] = {"mirrored": default_value}
            return _NO_WRITE, False
        # У администратора уже есть значение, которое мы никогда не
        # отгружали, — правка предшествует существованию зеркала. Его.
        new_state[key] = {"owned_by_client": True}
        return _NO_WRITE, True

    if record.get("owned_by_client"):
        new_state[key] = record
        return _NO_WRITE, False

    mirrored = record.get("mirrored")
    current_admin = admin_value if admin_has else None
    if current_admin != mirrored:
        # Администратор поправил профиль после того, как мы его туда
        # положили. Навсегда его — как и у trix_config_defaults.
        new_state[key] = {"owned_by_client": True}
        return _NO_WRITE, True

    if default_value == mirrored:
        new_state[key] = record
        return _NO_WRITE, False

    new_state[key] = {"mirrored": default_value}
    return default_value, False


# ---------------------------------------------------------------------------
# config.yaml: model.*
# ---------------------------------------------------------------------------

def _prepare_model_block(lines: list, admin_data: dict) -> Tuple[list, bool, Optional[str]]:
    """Довести ``model:`` до «голого» блочного ключа, готового принять
    потомков. Вызывается ТОЛЬКО когда вызывающий код уже убедился, что
    ``model`` — не непустой скаляр, заданный человеком руками (см.
    :func:`_sync_config_model_block`)."""
    current = admin_data.get("model")
    if isinstance(current, dict):
        return lines, True, None
    idx = _find_key_line(lines, "model", 0, 0, len(lines))
    if idx is None:
        return lines, False, "'model:' не найден как ключ верхнего уровня"
    if _is_safe_block_parent_line(lines[idx], "model", 0):
        return lines, True, None
    head, _, tail = lines[idx].partition(":")
    stripped = tail.strip()
    comment = "  " + stripped if stripped.startswith("#") else ""
    out = list(lines)
    out[idx] = f"{head}:{comment}"
    return out, True, None


def _verify_model_sync(new_text: str, before: dict, expected: dict) -> bool:
    """Как ``trix_config_defaults._verify``, но исключает ``model.*`` целиком
    из проверки «больше ничего не изменилось» — единственный путь, где
    ``model`` меняет ФОРМУ (скаляр/``null`` -> словарь), а не только
    значение внутри уже существующего блока (см. :func:`_prepare_model_block`)."""
    try:
        after = yaml.safe_load(new_text)
    except Exception:
        return False
    if not isinstance(after, dict):
        return False
    for path, value in expected.items():
        found, got = _lookup(after, path)
        if not found or got != value:
            return False
    for path, value in _scalar_paths(before):
        if path[0] == "model":
            continue
        if path in expected:
            continue
        found, got = _lookup(after, path)
        if not found or got != value:
            return False
    return True


def _sync_config_model_block(default_home: Path, admin_home: Path, report: dict) -> None:
    default_config = Path(default_home) / "config.yaml"
    admin_config = Path(admin_home) / "config.yaml"
    if not default_config.exists() or not admin_config.exists():
        return

    try:
        default_data = yaml.safe_load(_read_raw_text(default_config))
    except Exception:
        return
    if not isinstance(default_data, dict):
        return
    paths = [
        (path, value) for path, value in _model_scalar_paths(default_data)
        if path[0] not in _NEVER_MIRROR_CONFIG_TOP_KEYS and _is_scalar(value)
    ]
    if not paths:
        return

    before_text = _read_raw_text(admin_config)
    try:
        before_data = yaml.safe_load(before_text)
    except Exception:
        return
    if not isinstance(before_data, dict):
        return

    current_admin_model = before_data.get("model")
    if isinstance(current_admin_model, str) and current_admin_model.strip():
        # Старая скалярная форма выбора модели, заданная администратором
        # руками, — не трогаем и не запоминаем ничего в baseline, чтобы
        # расхождение сообщалось на каждом прогоне, пока человек не решит
        # его сам (см. модульный докстринг — «правка клиента навсегда
        # его»).
        report["diverged_now"].append("config.model (задан скаляром вручную — не трогаю)")
        return

    state = _load_state(admin_home)
    new_state = dict(state)
    plan: Dict[Tuple[str, ...], Any] = {}
    for path, default_value in paths:
        key = "config." + ".".join(path)
        found, admin_value = _lookup(before_data, path)
        write_value, newly_diverged = _decide(
            state, new_state, key,
            admin_has=found, admin_value=admin_value, default_value=default_value,
        )
        if newly_diverged:
            report["diverged_now"].append(key)
        if write_value is not _NO_WRITE:
            plan[path] = write_value

    if not plan:
        _save_state(admin_home, new_state)
        return

    lines = before_text.splitlines()
    lines, ok, reason = _prepare_model_block(lines, before_data)
    if not ok:
        report["diverged_now"].append(f"config.model ({reason})")
        return

    expected: Dict[Tuple[str, ...], Any] = {}
    changes: List[str] = []
    for path, value in plan.items():
        try:
            lines, changed, final_value = _ensure_value(
                lines, before_data, path, lambda _cur, v=value: v
            )
        except Exception:
            logger.warning("mirror: не удалось применить %s", ".".join(path), exc_info=True)
            continue
        expected[path] = final_value
        if changed:
            changes.append(".".join(path))

    if not changes:
        _save_state(admin_home, new_state)
        return

    sep = _dominant_newline(before_text)
    had_trailing = before_text.endswith(("\n", "\r"))
    new_text = sep.join(lines) + (sep if had_trailing else "")

    if not _verify_model_sync(new_text, before_data, expected):
        logger.warning(
            "mirror: проверка результата не прошла для %s — файл не тронут", admin_config
        )
        return

    try:
        atomic_write_text(admin_config, new_text, newline="", preserve_mode=True)
    except Exception:
        logger.warning("mirror: не удалось записать %s", admin_config, exc_info=True)
        return

    report["config_mirrored"].extend(changes)
    report["changed"] = True
    _save_state(admin_home, new_state)


# ---------------------------------------------------------------------------
# .env: провайдерские ключи + прокси
# ---------------------------------------------------------------------------

def _sync_env(default_home: Path, admin_home: Path, report: dict) -> None:
    from hermes_cli.config import load_env, save_env_value

    with _profile_home_scope(default_home):
        default_env = load_env()
    with _profile_home_scope(admin_home):
        admin_env = load_env()

    eligible = _mirror_eligible_env_names()
    state = _load_state(admin_home)
    new_state = dict(state)
    to_write: Dict[str, Any] = {}
    fresh_defaults = _fresh_profile_env_defaults()

    for name in sorted(eligible):
        default_value = default_env.get(name)
        if not default_value or not str(default_value).strip():
            continue
        key = "env." + name
        admin_has = name in admin_env
        admin_value = admin_env.get(name)
        # Only on FIRST sight (no baseline record yet): a value that's
        # still exactly the template's own seed (e.g. NO_PROXY's built-in
        # host list) is a blank slate, not an administrator's edit — see
        # _fresh_profile_env_defaults' docstring. Once a record exists this
        # no longer applies; a later edit back to the template's literal
        # value is a real (if unusual) administrator choice.
        if admin_has and key not in state:
            if admin_value == fresh_defaults.get(name):
                admin_has = False
        write_value, newly_diverged = _decide(
            state, new_state, key,
            admin_has=admin_has, admin_value=admin_value, default_value=default_value,
        )
        if newly_diverged:
            report["diverged_now"].append(key)
        if write_value is not _NO_WRITE:
            to_write[name] = write_value

    if not to_write:
        _save_state(admin_home, new_state)
        return

    with _profile_home_scope(admin_home):
        for name, value in to_write.items():
            try:
                save_env_value(name, str(value))
                report["env_mirrored"].append(name)
                report["changed"] = True
            except Exception:
                logger.warning("mirror: не удалось записать %s в .env администратора", name, exc_info=True)

    _save_state(admin_home, new_state)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def sync_admin_profile_from_default(*, default_home: Path, admin_home: Path) -> dict:
    """Свести провайдера/модель/прокси профиля-администратора с ``default``.

    Best-effort и идемпотентна: никогда не бросает исключение (вызывается и
    из тихого хука старта шлюза), прогон без расхождений ничего не пишет.
    Возвращает отчёт::

        {"config_mirrored": [...], "env_mirrored": [...],
         "diverged_now": [...], "changed": bool}

    ``diverged_now`` — ключи, которые ИМЕННО в этом прогоне впервые
    признаны собственностью администратора (правка вручную) — не
    повторяется на последующих no-op прогонах.
    """
    default_home = Path(default_home)
    admin_home = Path(admin_home)
    report: dict = {
        "config_mirrored": [],
        "env_mirrored": [],
        "diverged_now": [],
        "changed": False,
    }

    try:
        _sync_config_model_block(default_home, admin_home, report)
    except Exception:
        logger.warning("mirror: синхронизация config.yaml упала", exc_info=True)

    try:
        _sync_env(default_home, admin_home, report)
    except Exception:
        logger.warning("mirror: синхронизация .env упала", exc_info=True)

    return report


def format_sync_report(report: dict) -> str:
    """Человеческая строка отчёта — для SSH-оператора (`hermes business
    setup`) и для строки в agent.log при старте шлюза."""
    parts = []
    if report.get("config_mirrored"):
        parts.append("config.yaml: " + ", ".join(report["config_mirrored"]))
    if report.get("env_mirrored"):
        parts.append(".env: " + ", ".join(report["env_mirrored"]))
    if report.get("diverged_now"):
        parts.append(
            "администратор правил вручную, больше не трогаю: "
            + ", ".join(report["diverged_now"])
        )
    if not parts:
        return "нечего синхронизировать — всё уже совпадает"
    return "; ".join(parts)
