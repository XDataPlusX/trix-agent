"""Файловый контур компании — закрытая грамматика + машина состояния хоста.

Заменяет прототип ``optional-skills/.../trix-file-contour/scripts/contour.py``.
Прототип принимал свободный YAML, где ``root`` — любой абсолютный путь, пути
никогда не резолвились через ``realpath`` (символическая ссылка внутри
``root`` давала выход за его пределы), папки не были привязаны к профилям
(любой профиль мог получить ``rw`` на любую чужую папку), а общая папка
навыков могла быть смонтирована на запись. На машине, где ``docker_extra_args``
из конфига ЛЮБОГО профиля уходят в ``docker run`` без проверки (см.
``tools/environments/docker.py``), а ``user`` состоит в группе ``docker``
(root-эквивалент — ``docker run -v /:/host``), любая из этих дыр — это полный
захват машины. Этот модуль пишет конфиги профилей. Он написан и обязан
читаться как привилегированный код.

**Кто владелец угрозы.** Запросы этому модулю пишет LLM-агент профиля
``system_admin`` — тот же самый агент, которому в чат прилетают документы,
письма и произвольный текст от посторонних. Считаем его скомпрометированным
по умолчанию (prompt injection). Всё, что он мог бы написать в файл запроса,
проверяется здесь так, будто это написал злоумышленник напрямую — потому что
именно так и есть. Единственная защита, которую даёт этот модуль, —
ЗАКРЫТАЯ грамматика: ровно пять операций (см. :data:`VALID_OPS`), и любая
попытка добавить операцию, ключ или значение вне грамматики отклоняет ВЕСЬ
запрос, а не игнорирует лишнее.

**Модель угрозы по пунктам, и где в коде каждый пункт закрыт:**

1. *«root» в запросе.* Не существует — :data:`DEFAULT_ROOT` это константа
   модуля; функции принимают ``root`` только как именованный аргумент со
   значением по умолчанию, и он нужен ИСКЛЮЧИТЕЛЬНО тестам. Ни один код пути
   не читает ``root`` из JSON запроса, из переменной окружения или из чего бы
   то ни было ещё, до чего может дотянуться агент.
2. *Побег из root через ``..`` или символическую ссылку.* Каждый путь
   разрешается через :func:`_resolve_under_root`: раскладка по сегментам,
   строгий allowlist символов (без точек — значит, без ``..`` тоже, см.
   :func:`_validate_segment`), затем ``os.path.realpath`` ОБЯЗАН остаться
   внутри ``root``, И ни один компонент пути (по всей цепочке от корня
   файловой системы) не должен быть символической ссылкой —
   :func:`_check_no_symlink_components` идёт по частям и останавливается на
   первом ещё не существующем сегменте (дальше по определению ничего нет,
   резолвить нечего). Это эквивалент ``O_NOFOLLOW`` на каждом сегменте, а не
   только на последнем, — потому что ``realpath`` сам по себе говорит, КУДА
   резолвится путь, но не мешает пройти ВНУТРЬ ссылки по дороге.
3. *Точка монтирования внутри контейнера.* Никогда не берётся из запроса.
   :func:`_derive_container_path` вычисляет её из ВСЕГО пути: ``company``
   (ровно) → ``/company``; ``dept/<X>``, выданная профилю ``<X>`` →
   ``/dept``; ``dept/<X>``, выданная НЕ профилю ``<X>`` → ``/dept/<X>``. Это
   ровно два грантуемых пути (см. п.5 ниже) — третьего «анидшего» бакета
   («всё остальное → ``/shared/<последний сегмент>``») больше нет: он
   позволял двум РАЗНЫМ папкам с одинаковым последним сегментом получить
   ОДНУ И ТУ ЖЕ точку монтирования, из-за чего вторая молча вытесняла первую
   при apply (пункт 8 обзора). Результат дополнительно проверяется
   :func:`_check_derived_mount_safe` — он никогда не может начаться
   с ``/workspace`` или ``/root``, и не может быть ``/``: это чужие маунты
   самой песочницы (домашний каталог контейнера и рабочий стол профиля), и
   перекрыть их — то же самое, что подменить агенту диск.
4. *Профиль, которого нет.* :func:`_check_profile_exists` сверяется с
   реальным списком профилей (``hermes_cli.profiles``). Модуль профили не
   создаёт — только видит существующие. ``default`` — валидное имя (см.
   ``profiles.profile_exists``).
5. *Привязка папки к профилю.* :func:`_validate_grantable_folder` —
   закрытая грамматика ГРАНТУЕМЫХ форм: РОВНО ``company`` (без подпапки) и
   ``dept/<X>`` (``<X>`` — существующий профиль). Голый ``dept``,
   ``company/<подпапка>``, путь глубже двух сегментов и вообще что угодно
   ещё — отказ ВСЕГО запроса, до вызова :func:`_check_binding`. Третьего
   бакета «анидшее — общий ресурс без привязки» больше нет: это была дыра
   уровня захвата машины (см. докстринг :func:`_validate_grantable_folder`
   за полным разбором TOCTOU). :func:`_check_binding` — сердце модели прав
   ДЛЯ ЭТИХ ДВУХ форм: ``dept/<X>`` можно выдать ``rw`` ТОЛЬКО профилю
   ``<X>``, ``ro`` — только профилю из списка администраторов
   (:func:`load_admin_profiles`, файл ВНЕ ``root``, недостижимый из
   запроса). ``company`` можно выдать ``rw`` только администратору, ``ro``
   — кому угодно из существующих профилей. ``revoke`` проходит ТУ ЖЕ
   грамматику форм и биндинг (см. :func:`_check_revoke_allowed`) — раньше
   не проверял вообще ничего (пункт 4 обзора).
6. *Общая папка навыков как маунт.* :func:`_check_not_skills_dir` отдельно
   запрещает ``grant``/``revoke`` на путь, начинающийся с ``skills`` — она
   ходит до профилей через ``skills.external_dirs``, а не через
   ``terminal.docker_volumes``, и это единственный путь, которым модуль вообще
   касается ``skills.external_dirs`` — ТОЛЬКО когда в том же вызове был хотя
   бы один ``grant`` (не при чисто ``revoke``: раньше правилось безусловно
   при ЛЮБОЙ правке ``docker_volumes`` профиля, а значит ``revoke`` мог
   форсировать общую папку навыков профилю, у которого администратор её
   явно снял — пункт 4 обзора).
7. *Подмена конфига в обход этого модуля.* Если в конфиге профиля
   ``terminal.docker_extra_args`` / ``terminal.docker_forward_env`` /
   ``terminal.credential_files`` ОТЛИЧАЮТСЯ от того, что мы сами отгрузили
   в курируемом шаблоне (``assets/config/trix-config.yaml`` — свежая
   машина уже несёт непустой ``docker_extra_args``, поэтому правило
   сравнивает со значением, а не с фактом присутствия ключа), или
   ``terminal.backend`` не ``docker`` — :func:`_check_config_not_tampered`
   отказывает НЕ ЧИНЯ: это сигнал, что кто-то (не обязательно этот модуль)
   уже трогал терминал этого профиля вручную, и молча продолжать значило
   бы затирать чужую правку или писать поверх состояния, которое мы не
   понимаем.
8. *Неизвестная операция.* :func:`validate_request` отказывает ВСЕМУ запросу
   при первой же операции вне :data:`VALID_OPS` — до единой мутации на диске
   (см. модульный инвариант ниже).
9. *Токен git.* Стирается из файла заявки СРАЗУ после того, как JSON
   разобрался — ДО ЛЮБОЙ ветки (`when`/валидация/применение), а не только
   на пути успеха (см. :func:`_extract_raw_git_tokens` +
   :func:`_scrub_tokens_in_place` в :func:`run_executor` — иначе `refused`
   и отложенный до ночи `pending` архивировали/оставляли заявку с токеном
   открытым текстом, реальный найденный дефект). Настоящее значение к
   этому же моменту сохраняется в файл ``0600`` вне ``root``
   (:func:`_token_file_path`) — так отложенное до ночи применение всё
   равно видит настоящий токен, а не плейсхолдер (см. докстринг
   :func:`_plan_set_git_remote`). Никогда в самом репозитории, никогда в
   логах и в возвращаемых строках. При push'е — НЕ в argv (виден в ``ps``
   любому локальному пользователю по умолчанию), а через переменные
   окружения дочернего git-процесса (:func:`push`) и никогда не
   логируется.

**Инвариант «валидация — сначала и целиком».** :func:`validate_request`
проверяет КАЖДУЮ операцию запроса против реального состояния машины
(существующие профили, актуальные конфиги, список администраторов) и
возвращает план (:class:`PlannedOp`) целиком, ИЛИ бросает
:class:`ContourError` — без частичного плана. Ни одна мутация в
:func:`apply_planned` не начинается, пока весь запрос не прошёл валидацию.
Это разводит два разных класса отказа, которые нельзя путать: отказ на
ВАЛИДАЦИИ — это грамматика/права, реакция «весь запрос отклонён, диск не
тронут»; отказ на ПРИМЕНЕНИИ (диск полон, git недоступен, конфиг откуда-то
изменился за секунду между валидацией и записью) — это исполнение уже
одобренного плана, и там частичное применение — ожидаемое поведение с
явным отчётом, что применилось, а что нет, и с резервной копией каждого
тронутого конфига (``<path>.bak-<timestamp>``) на случай отката руками.

**Что этот модуль сознательно не делает** (см. CLAUDE.md, «Contribution
Rubric» и сам бриф этой задачи): не создаёт профили, не трогает
``run_as_host_user``, ``approvals.*``, ``skills.inline_shell``, ``.env``,
systemd — этих слов нет в грамматике, и не будет: любое из них — это
отдельное, куда более рискованное решение, которое обязано быть отдельным
явным действием администратора-человека, а не строкой в JSON, который пишет
модель.

**Пересоздание песочницы — вне этого модуля.** Правка ``docker_volumes``
ничего не меняет в уже запущенном контейнере (маунты фиксируются при
создании) — так же, как в прототипе. Это ответственность вызывающего кода
(cron-скрипт или администратор), а не библиотеки: библиотека отвечает
только за то, чтобы КОНФИГ был безопасен и верен, а не за жизненный цикл
контейнеров.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from utils import atomic_write_text

from hermes_cli.trix_config_sync import _read_raw_text

# ---------------------------------------------------------------------------
# Константы модели прав — НИЧЕГО из этого раздела не приходит из запроса.
# ---------------------------------------------------------------------------

#: Корень контура. Единственное официальное место, где это значение
#: задаётся. Переопределяется ТОЛЬКО именованным аргументом ``root=`` — тесты
#: делают это явно; продовый код (CLI, executor) — никогда.
DEFAULT_ROOT = Path("/srv/trix")

#: Имя общей папки навыков внутри root — зарезервировано, недоступно grant/revoke.
SKILLS_DIR_NAME = "skills"

#: Имя файла заявки внутри ``/workspace`` профиля ``system_admin`` — единственное
#: имя, которое обязаны знать одновременно навык (кладёт заявку по этому имени
#: в свою песочницу) и хостовый лаунчер, который заводит ``hermes business setup``
#: (ищет заявку по хостовому пути, соответствующему тому же ``/workspace``, —
#: см. ``hermes_cli/trix_business.py`` и ``tools/environments/base.py::get_sandbox_dir``).
#: Не читается этим модулем напрямую — ``run_executor`` принимает уже
#: разрешённый ``request_path``; константа существует, чтобы обе стороны не
#: могли разойтись по имени файла.
CONTOUR_REQUEST_FILENAME = "contour-request.json"

#: Пять операций грамматики. Всё остальное — hard-отказ ВСЕГО запроса.
VALID_OPS = ("create_folder", "grant", "revoke", "publish_skill", "set_git_remote")

VALID_MODES = ("ro", "rw")

#: Плейсхолдер, которым :func:`run_executor` затирает значение токена в
#: файле заявки — сразу после того, как JSON разобрался, до какой бы то ни
#: было ветки (пункт 6 обзора). Обязан быть тем же текстом и здесь, и в
#: :func:`_plan_set_git_remote` (см. её докстринг за тем, почему заявка,
#: отложенная до ночи, обязана узнавать этот текст как «уже стёрто, бери
#: сохранённое», а не как буквальное новое значение токена).
_TOKEN_SCRUB_PLACEHOLDER = "***scrubbed***"

#: Собственные маунты песочницы — сюда монтировать нельзя ни в каком виде.
RESERVED_CONTAINER_PREFIXES = ("/workspace", "/root")

_SEGMENT_MAX_LEN = 64
_MAX_FOLDER_DEPTH = 3

#: Ключи `terminal.*`, чьё ЗНАЧЕНИЕ сверяется с курируемым шаблоном (не
#: «отсутствие ли» — «то же самое ли, что мы отгрузили»). Все три уходят в
#: `docker run` без валидации (`tools/environments/docker.py`), и правка
#: любого — root на машине (`user` в группе `docker`). См.
#: :func:`_check_config_not_tampered` за тем, почему форма правила именно
#: такая, а не «ключ не должен существовать».
_TEMPLATE_COMPARED_TERMINAL_KEYS = ("docker_extra_args", "docker_forward_env", "credential_files")

#: Предел на публикуемый навык. Регламенты и шаблоны — единицы мегабайт;
#: 50 МиБ — с запасом на скриншоты и PDF внутри references/, но всё ещё на
#: два порядка меньше, чем нужно, чтобы этим можно было залить диск машины
#: по кругу (публикация — не архивное хранилище). Выбор задокументирован
#: здесь, а не угадывается заново при каждом чтении кода.
MAX_SKILL_SIZE_BYTES = 50 * 1024 * 1024

#: Ночное окно для заявок ``when: night`` — по времени машины (сервер один на
#: компанию, часовой пояс серверный). 02:00–04:00 выбрано как типичное окно
#: наименьшей активности; вынесено в константы, чтобы не искать магическое
#: число внутри функции, если понадобится сузить/сдвинуть.
NIGHT_WINDOW_START_HOUR = 2
NIGHT_WINDOW_END_HOUR = 4

#: Верхний предел числа операций в ОДНОЙ заявке. Обзор: до этого предела не
#: было вовсе — заявка с 100 000 `create_folder` была дешёвым способом
#: занять исполнителя (каждая операция — минимум один `stat`/`mkdir`, плюс
#: `_check_config_not_tampered` на каждый затронутый профиль) на минуты,
#: пока `run_executor` тикает раз в минуту и ничего другого сделать не
#: может. 200 — с большим запасом: реальная административная заявка
#: (завести отдел, дать/забрать пачку доступов, опубликовать пару навыков)
#: — это единицы-десятки операций; отдел из десятков сотрудников с
#: индивидуальными grant'ами всё ещё укладывается на порядок ниже предела.
MAX_OPS_PER_REQUEST = 200


class ContourError(Exception):
    """Отказ на валидации запроса, на бинде прав или на применении операции."""


# ---------------------------------------------------------------------------
# Пути: сегменты, символические ссылки, границы root
# ---------------------------------------------------------------------------


def _segment_char_script(ch: str) -> Optional[str]:
    """Грубая классификация «алфавита» одного символа — только для проверки
    на СМЕШЕНИЕ разных алфавитов внутри одного сегмента
    (см. :func:`_validate_segment`, пункт 9 обзора: гомоглифы вроде
    кириллической «с» внутри латинского «company»).

    Цифры считаются нейтральными (``None``) — они не участвуют в проверке
    на смешение, иначе ``dept1`` (латиница + цифра) уже считался бы
    «смешанным». Для остального — префикс имени символа по Unicode
    (``unicodedata.name``): для латиницы/кириллицы имя всегда начинается с
    ``LATIN``/``CYRILLIC``, этого достаточно, чтобы отличить «просто другой
    алфавит» от «то же самое, но выглядит иначе» — полноценная таблица
    Script= не нужна для двух алфавитов, которые реально встречаются в
    именах профилей/папок этого продукта.
    """
    if ch.isdigit():
        return None
    name = unicodedata.name(ch, "")
    return name.split(" ", 1)[0] if name else "?"


def _validate_segment(seg: Any, what: str) -> None:
    """Один сегмент относительного пути.

    Разрешены unicode-буквы (кириллица обязана работать — компания
    русскоязычная) и цифры (``str.isalnum()`` покрывает оба класса для
    ЛЮБОГО алфавита), плюс ``_`` и ``-``. Точки НЕ входят в allowlist — это
    заодно и запрет ``..``: сегмент из одних точек не проходит ни одной
    буквой/цифрой/дефисом/подчёркиванием.

    Две ДОПОЛНИТЕЛЬНЫЕ проверки закрывают гомоглифы (пункт 9 обзора) — без
    них кириллическая «с» в «сompany» или полноширинные ``ＣＯＭＰＡＮＹ``
    визуально неотличимы от настоящих ``company`` в ``hermes contour
    status``, но резолвятся в РАЗНЫЕ файловые пути:

    1. ``unicodedata.normalize("NFKC", seg) != seg`` — сегмент обязан быть
       НЕПОДВИЖНОЙ точкой NFKC-нормализации. Полноширинные/лигатурные формы
       (``ＣＯＭＰＡＮＹ`` → ``COMPANY``) под NFKC меняются — отказ. Обычная
       кириллица (``регламенты``) НЕ меняется под NFKC — проходит: это
       продуктовое требование (компания русскоязычная), проверено тестом.
    2. Один сегмент не может смешивать алфавиты — NFKC не ловит
       кириллицу-среди-латиницы (это РАЗНЫЕ символы, не компат-варианты
       одного и того же), поэтому смешение алфавитов проверяется отдельно
       через :func:`_segment_char_script`.
    """
    if not isinstance(seg, str) or not (1 <= len(seg) <= _SEGMENT_MAX_LEN):
        raise ContourError(
            f"{what}: сегмент пути должен быть строкой длиной 1-{_SEGMENT_MAX_LEN} "
            f"символов, получено {seg!r}"
        )
    if unicodedata.normalize("NFKC", seg) != seg:
        raise ContourError(
            f"{what}: сегмент {seg!r} не проходит NFKC-нормализацию без изменений — "
            "похоже на визуальную подмену символов (полноширинные/лигатурные формы), отказ"
        )
    scripts: set = set()
    for ch in seg:
        if ch in ("_", "-"):
            continue
        if not ch.isalnum():
            raise ContourError(
                f"{what}: недопустимый символ {ch!r} в сегменте {seg!r} — только "
                "буквы (включая кириллицу), цифры, «_», «-»"
            )
        script = _segment_char_script(ch)
        if script is not None:
            scripts.add(script)
    if len(scripts) > 1:
        raise ContourError(
            f"{what}: сегмент {seg!r} смешивает разные алфавиты "
            f"({', '.join(sorted(scripts))}) в одном имени — похоже на гомоглиф, отказ"
        )


def _validate_folder_rel(folder: Any, what: str) -> list:
    """Проверить относительный путь папки и вернуть список сегментов.

    Отдельно от allowlist символов: явная проверка на абсолютность и на
    пустые сегменты (``//``, ведущий/хвостовой ``/``) — они бы иначе прошли
    как «0 сегментов между слэшами», а не как ошибка charset'а.
    """
    if not isinstance(folder, str) or not folder:
        raise ContourError(f"{what}: должен быть непустой строкой")
    if folder.startswith("/") or folder.startswith("\\") or ":" in folder:
        raise ContourError(f"{what}: должен быть ОТНОСИТЕЛЬНЫМ путём, получено {folder!r}")
    parts = folder.split("/")
    if any(p == "" for p in parts):
        raise ContourError(f"{what}: пустой сегмент в {folder!r} (двойной «/»? ведущий/хвостовой «/»?)")
    if len(parts) > _MAX_FOLDER_DEPTH:
        raise ContourError(f"{what}: {folder!r} глубже {_MAX_FOLDER_DEPTH} уровней")
    for p in parts:
        _validate_segment(p, what)
    return parts


def _check_no_symlink_components(path: Path) -> None:
    """Пройти по ВСЕМ существующим предкам ``path`` и убедиться, что ни один —
    символическая ссылка.

    Эквивалент ``O_NOFOLLOW`` на каждом сегменте, а не только на последнем:
    ``os.path.realpath`` говорит, куда путь резолвится, но сам по себе не
    мешает пройти ВНУТРЬ ссылки по дороге туда (например, ``root/company``,
    где ``company`` — обычная папка, но сам ``root`` — символическая ссылка
    на ``/``). Останавливаемся на первом ещё не существующем сегменте:
    дальше по определению ничего нет, резолвить и проверять нечего — это и
    есть путь, который будет создан ``mkdir(parents=True)``.
    """
    path = Path(path)
    if not path.is_absolute():
        raise ContourError(f"внутренняя ошибка контура: {path} должен быть абсолютным")
    cur = Path(path.anchor)
    for part in path.parts[1:]:
        cur = cur / part
        if cur.is_symlink():
            raise ContourError(f"{cur} — символическая ссылка, отказ")
        if not cur.exists():
            break


def _resolve_under_root(root: Path, rel_parts) -> Path:
    """``root/rel_parts``, проверенный на символические ссылки и на то, что
    ``realpath`` результата не выходит за пределы ``realpath(root)``.

    Обе проверки нужны одновременно и ловят разное: символические ссылки —
    подмену КОМПОНЕНТА пути; ``realpath``-containment — общий случай, когда
    итоговое разрешение (даже без единой ссылки на пути, например через
    ``..``, которого грамматика уже не пропускает, но лишняя защита не
    вредит) оказалось бы вне root.
    """
    root = Path(root)
    candidate = root.joinpath(*rel_parts)
    _check_no_symlink_components(candidate)
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    if not real_candidate.startswith(real_root + os.sep):
        raise ContourError(f"{candidate} выходит за пределы root {root} после разрешения")
    return candidate


def _check_not_skills_dir(rel_parts) -> None:
    if rel_parts and rel_parts[0] == SKILLS_DIR_NAME:
        raise ContourError(
            "общая папка навыков не выдаётся через grant/revoke — она приезжает "
            "профилям через skills.external_dirs, а не через монтирование"
        )


#: Ровно две выдаваемые формы грамматики grant/revoke — НИЧЕГО кроме этого,
#: см. :func:`_validate_grantable_folder`. Держать этот список закрытым —
#: не только модель прав, но и единственный дешёвый способ закрыть TOCTOU
#: на путях монтирования (см. докстринг :func:`_validate_grantable_folder`).
GRANTABLE_COMPANY = ("company",)


def _validate_grantable_folder(rel_parts: list) -> None:
    """Единственный вход для «эта папка вообще может быть выдана/отозвана».

    Ровно ДВЕ формы — и это НАМЕРЕННОЕ сужение грамматики, а не одна из
    равноправных проверок: ``company`` (ровно, без подпапки) и ``dept/<X>``,
    где ``<X>`` — СУЩЕСТВУЮЩИЙ профиль. Любая другая форма (голый ``dept``,
    ``company/<подпапка>``, путь глубже двух сегментов, что угодно ещё) —
    отказ ВСЕГО запроса. Раньше здесь был третий бакет — «всё остальное»,
    доступное любому профилю как низкорискованный общий ресурс. Он убран
    целиком, и не потому, что стал «неудобным», а потому, что был дырой:

    Инвариант, ради которого форма именно такая — TOCTOU на путях
    монтирования. ``terminal.docker_volumes`` — это ЛИТЕРАЛЬНЫЙ хостовый
    путь; ядро резолвит его в момент ``docker run``, часы спустя и заново
    при каждом пересоздании контейнера. Если у любого профиля есть ``rw`` на
    РОДИТЕЛЯ пути, из которого смонтирована папка ДРУГОГО профиля, этот
    профиль может удалить чужой источник монтирования и подменить его
    символической ссылкой на ``/`` — и при следующем пересоздании песочницы
    ядро смонтирует ВЕСЬ хостовый диск внутрь чужого контейнера. С
    источниками монтирования, ограниченными РОВНО двумя формами
    фиксированной глубины (``root/company``, ``root/dept/<X>``), которые
    НИКОГДА не вложены друг в друга (``company`` не может быть предком
    ``dept/<X>`` и наоборот, а два разных ``dept/<X>``/``dept/<Y>`` —
    братья, не предок/потомок), ни одна writable-папка не может быть
    предком папки, из которой смонтировано что-то ещё. Подмена через
    произвольную «анидшую» папку (бывший третий бакет) была ровно такой
    дырой: ``rw`` на, скажем, ``templates`` не давало прямого захвата
    ЧЕРЕЗ НЕЁ, но ``rw`` на любой ПРЕДОК чужого маунта — давало. Не давать
    грамматике вообще создавать предков — единственный дешёвый способ
    закрыть это класс целиком, не разбирая каждую будущую форму по
    отдельности. НЕ ослаблять эту форму, даже если понадобится «ещё одна
    удобная общая папка» — см. §3 спеки 21 и это же предупреждение в
    модульном докстринге.
    """
    if rel_parts == list(GRANTABLE_COMPANY):
        return
    if len(rel_parts) == 2 and rel_parts[0] == "dept":
        _check_profile_exists(rel_parts[1])
        return
    raise ContourError(
        "grant/revoke принимают ровно две формы — `company` (без подпапки) или "
        f"`dept/<профиль>` — {'/'.join(rel_parts)!r} не входит в закрытую грамматику"
    )


def _derive_container_path(rel_parts, profile: str) -> str:
    """Точка монтирования внутри контейнера — производная от ВСЕГО
    относительного пути, никогда не из запроса и никогда только от
    последнего сегмента (см. пункт 8 обзора — вычисление по последнему
    сегменту раньше давало двум РАЗНЫМ папкам одну и ту же точку
    монтирования, если у них совпадал хвост).

    Ровно два случая — других папок эта функция уже не видит,
    :func:`_validate_grantable_folder` вызывается раньше и отклоняет всё
    остальное:

    - ``company`` (ровно) → ``/company``;
    - ``dept/<X>``, если ``<X>`` совпадает с профилем-получателем (свой
      отдел) → ``/dept`` (короткое имя для «моей» папки);
    - ``dept/<X>``, выданная НЕ профилю ``<X>`` (например, ``ro``-обзор
      чужого отдела администратору) → ``/dept/<X>`` — путь из ВСЕХ
      сегментов, поэтому обзор двух разных отделов у одного администратора
      никогда не коллизирует («/dept/sales» != «/dept/accounting»), и
      никогда не совпадает с «моим» коротким ``/dept``.
    """
    if rel_parts == list(GRANTABLE_COMPANY):
        return "/company"
    if len(rel_parts) == 2 and rel_parts[0] == "dept":
        if rel_parts[1] == profile:
            return "/dept"
        return "/" + "/".join(rel_parts)
    raise ContourError(
        "внутренняя ошибка контура: производный путь монтирования запрошен для формы "
        f"{'/'.join(rel_parts)!r}, не прошедшей _validate_grantable_folder"
    )


def _check_derived_mount_safe(at: str) -> None:
    if at == "/" or not at.strip("/"):
        raise ContourError("внутренняя ошибка контура: производный путь монтирования пуст")
    for prefix in RESERVED_CONTAINER_PREFIXES:
        if at == prefix or at.startswith(prefix + "/"):
            raise ContourError(f"производный путь монтирования {at} занят самой песочницей")


def _check_binding(rel_parts, profile: str, mode: str, admin_profiles: set) -> None:
    """Кому какую папку можно выдать в каком режиме — сердце модели прав.

    Вызывается ПОСЛЕ :func:`_validate_grantable_folder` — видит ровно две
    формы, «анидшего» бакета больше нет (см. докстринг той функции за тем,
    почему это не просто чистка, а закрытие TOCTOU).

    ``company`` (ровно): ``rw`` только администратору, ``ro`` кому угодно
    (из уже проверенных :func:`_check_profile_exists` существующих
    профилей). ``dept/<X>`` (ровно, ``<X>`` уже проверен на существование
    выше): ``rw`` только профилю ``<X>``, ``ro`` только администратору.
    """
    if rel_parts == list(GRANTABLE_COMPANY):
        if mode == "rw" and profile not in admin_profiles:
            raise ContourError(
                f"company можно выдать rw только администратору, не «{profile}»"
            )
        return
    if len(rel_parts) != 2 or rel_parts[0] != "dept":
        raise ContourError(
            f"внутренняя ошибка контура: _check_binding вызвана для формы "
            f"{'/'.join(rel_parts)!r}, не прошедшей _validate_grantable_folder"
        )
    owner = rel_parts[1]
    if mode == "rw":
        if profile != owner:
            raise ContourError(
                f"dept/{owner} можно выдать rw только профилю «{owner}», не «{profile}»"
            )
    else:
        if profile not in admin_profiles:
            raise ContourError(
                f"dept/{owner} можно выдать ro только администратору, не «{profile}»"
            )


def _check_profile_exists(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise ContourError("имя профиля должно быть непустой строкой")
    from hermes_cli.profiles import normalize_profile_name, profile_exists

    try:
        canon = normalize_profile_name(name)
    except ValueError as e:
        raise ContourError(f"имя профиля {name!r} некорректно: {e}") from e
    if not profile_exists(canon):
        raise ContourError(f"профиль «{name}» не существует — контур профили не создаёт")
    return canon


# ---------------------------------------------------------------------------
# Администраторы исполнителя — читается ТОЛЬКО с диска исполнителя
# ---------------------------------------------------------------------------


def _executor_state_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "trix_contour"


def _admin_profiles_path() -> Path:
    return _executor_state_dir() / "admin_profiles.json"


def load_admin_profiles() -> set:
    """Профили-администраторы контура.

    Читается ИСКЛЮЧИТЕЛЬНО из файла на диске исполнителя, вне ``root`` — не
    из запроса, не из чего-либо, до чего может дотянуться агент
    ``system_admin`` изнутри своей песочницы (её маунты — это ``root``, не
    ``HERMES_HOME`` исполнителя). Отсутствующий/битый файл — пустой список:
    отказ-по-умолчанию (нельзя выдать rw на company или ro на dept), а не
    разрешение-по-умолчанию.
    """
    try:
        data = json.loads(_admin_profiles_path().read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(data, dict):
        data = data.get("admin_profiles")
    if not isinstance(data, list):
        return set()
    from hermes_cli.profiles import normalize_profile_name

    out = set()
    for name in data:
        if isinstance(name, str):
            try:
                out.add(normalize_profile_name(name))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------------------
# Строчная правка config.yaml — то самое, что стоило унаследовать из прототипа.
# Тот же алгоритм, что в hermes_cli/trix_config_sync.py и в прототипе
# contour.py: разбор до и после, побеждает сравнение по РАЗОБРАННОМУ
# результату, а не по тексту.
# ---------------------------------------------------------------------------

_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):(\s|$)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _top_block(lines: list, key: str) -> Optional[tuple]:
    start = None
    for i, line in enumerate(lines):
        m = _TOP_KEY_RE.match(line)
        if m and m.group(1) == key:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.strip() and _indent(line) == 0 and not line.lstrip().startswith("#"):
            end = j
            break
    while end > start + 1 and (
        not lines[end - 1].strip()
        or (_indent(lines[end - 1]) == 0 and lines[end - 1].lstrip().startswith("#"))
    ):
        end -= 1
    return start, end


def _find_sub_key(lines: list, start: int, end: int, key: str, indent: int) -> Optional[int]:
    pat = re.compile(r"^" + " " * indent + re.escape(key) + r":(\s|$)")
    for i in range(start + 1, end):
        if pat.match(lines[i]):
            return i
    return None


def _sub_extent(lines: list, idx: int, indent: int) -> int:
    j = idx + 1
    while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > indent):
        j += 1
    while j > idx + 1 and not lines[j - 1].strip():
        j -= 1
    return j


def _render_list(key: str, items, indent: int) -> list:
    pad = " " * indent
    items = list(items)
    if not items:
        return [f"{pad}{key}: []"]
    return [f"{pad}{key}:"] + [f"{pad}  - {json.dumps(it, ensure_ascii=False)}" for it in items]


def _render_scalar(line_before, key: str, value: Any, indent: int) -> str:
    rendered = json.dumps(value) if isinstance(value, bool) else str(value)
    comment = ""
    if line_before is not None:
        m = re.search(r"\s+#.*$", line_before)
        if m:
            comment = m.group(0)
    return f"{' ' * indent}{key}: {rendered}{comment}"


def set_nested(text: str, top: str, sub: str, value: Any) -> str:
    """Как в прототипе: правит ``top.sub``, всё остальное — байт в байт."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    indent = 2
    block = _top_block(lines, top)
    new_lines = _render_list(sub, value, indent) if isinstance(value, list) else None

    if block is None:
        tail = new_lines if new_lines is not None else [_render_scalar(None, sub, value, indent)]
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"{top}:"] + tail + [""]
        return nl.join(lines)

    start, end = block
    idx = _find_sub_key(lines, start, end, sub, indent)
    if idx is None:
        tail = new_lines if new_lines is not None else [_render_scalar(None, sub, value, indent)]
        lines[end:end] = tail
        return nl.join(lines)

    stop = _sub_extent(lines, idx, indent)
    repl = new_lines if new_lines is not None else [_render_scalar(lines[idx], sub, value, indent)]
    lines[idx:stop] = repl
    return nl.join(lines)


def _apply_expected(before: dict, expected: dict) -> dict:
    import copy

    d = copy.deepcopy(before or {})
    for (top, sub), value in expected.items():
        d.setdefault(top, {})
        if not isinstance(d[top], dict):
            raise ContourError(f"в конфиге `{top}` не словарь — руками не правлю")
        d[top][sub] = value
    return d


def rewrite_config(text: str, expected: dict) -> str:
    """Применить правки и ДОКАЗАТЬ, что изменилось ровно ожидаемое (см.
    модульный докстринг ``trix_config_sync``, «последний рубеж» — тот же
    приём: разобрать результат и сравнить со словарём, куда те же ключи
    подставлены напрямую)."""
    before = yaml.safe_load(text) or {}
    if not isinstance(before, dict):
        raise ContourError("config.yaml — не словарь на верхнем уровне")
    after_text = text
    for (top, sub), value in expected.items():
        after_text = set_nested(after_text, top, sub, value)
    after = yaml.safe_load(after_text) or {}
    want = _apply_expected(before, expected)
    if after != want:
        raise ContourError(
            "правка изменила бы в конфиге больше, чем ожидалось — файл оставлен как был"
        )
    return after_text


# ---------------------------------------------------------------------------
# Профильные конфиги: путь, чтение, проверка на подмену
# ---------------------------------------------------------------------------


def _profile_config_path(profile: str) -> Path:
    from hermes_cli.profiles import get_profile_dir

    return get_profile_dir(profile) / "config.yaml"


def _load_profile_config(cfg_path: Path) -> dict:
    """Разобранный (но НЕ смёрженный с DEFAULT_CONFIG, НЕ прошедший
    managed-overlay/`${ENV_VAR}`) словарь конфига профиля — ровно то, что
    физически лежит на диске, чтобы проверка на подмену
    (:func:`_check_config_not_tampered`) видела то же самое, что увидел бы
    человек, открыв файл. Через :func:`hermes_cli.config.read_user_config_raw`
    — единственный легальный сырой примитив вне ``hermes_cli/config.py``
    (см. ``tests/hermes_cli/test_config_read_guard.py`` — этот модуль
    читает МНОГО профильных конфигов по явному пути, а не свой собственный,
    так что канонические ``load_config()``/``load_config_readonly()`` тут
    не годятся: они завязаны на активный процесс, а не на произвольный
    путь)."""
    from hermes_cli.config import read_user_config_raw

    if not cfg_path.is_file():
        raise ContourError(f"нет {cfg_path} — профиль должен быть создан заранее (`hermes profile create`)")
    try:
        data = read_user_config_raw(cfg_path)
    except Exception as e:
        raise ContourError(f"{cfg_path} не разбирается как YAML: {e}") from e
    if not isinstance(data, dict):
        raise ContourError(f"{cfg_path}: верхний уровень конфига — не словарь")
    return data


def _shipped_template_terminal_dict() -> dict:
    """``terminal`` из НАШЕГО курируемого шаблона — эталон для сравнения в
    :func:`_check_config_not_tampered`.

    Через ``resolve_trix_config_template_only()``, а не через
    ``resolve_config_template()``: последний при отсутствии нашего файла
    молча подставляет 1700-строчный пример апстрима — сравнение с чужим
    файлом было бы даже опаснее отсутствия сравнения вовсе (см. докстринг
    ``hermes_cli.config_template``). Отсутствие курируемого шаблона на
    машине (урезанная установка) — отказ ЗАКРЫТЫЙ: без эталона сравнивать
    не с чем, а «сравнивать не с чем — значит пропустить проверку» это
    ровно тот тихий проход, ради недопущения которого весь метод и
    существует.
    """
    from hermes_cli.config import get_project_root
    from hermes_cli.config_template import resolve_trix_config_template_only

    template_path = resolve_trix_config_template_only(get_project_root())
    if template_path is None:
        raise ContourError(
            "курируемый шаблон конфига не найден на этой машине — проверка "
            "терминала на подмену не может сравнить со своим эталоном, "
            "отказываю закрыто, а не пропускаю проверку"
        )
    try:
        data = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ContourError(f"курируемый шаблон конфига не читается: {e}") from e
    terminal = data.get("terminal") if isinstance(data, dict) else None
    return terminal if isinstance(terminal, dict) else {}


def _check_config_not_tampered(profile: str, cfg: dict) -> None:
    """Отказ вместо тихой правки, если терминал профиля тронут В ОБХОД контура.

    **Форма правила была неверной и это баг, который поймал реальный
    прогон, а не гипотеза.** Первая версия отказывала на самом
    ПРИСУТСТВИИ ``docker_extra_args``/``docker_forward_env``/
    ``credential_files`` — и ломалась об собственный курируемый шаблон:
    ``assets/config/trix-config.yaml`` несёт непустой `docker_extra_args`
    (диапазон портов для демо) и пустой `docker_forward_env` НА КАЖДОЙ
    свежей машине, включая ту, что становится администратором контура.
    Буквально каждый свежий профиль отказывался бы с первого же `grant`.

    Настоящая инварианта — не «ключа нет», а «ключ равен тому, что МЫ
    отгрузили»: все три ключа уходят в `docker run` без валидации
    (`tools/environments/docker.py:1669-1673`), а `user` состоит в группе
    `docker` — то есть их правка и есть root на машине. Сравниваем со
    своим же курируемым шаблоном (:func:`_shipped_template_terminal_dict`):
    совпадает (или отсутствует и там, и там) — не подмена; отличается хоть
    чем-то (лишний аргумент, другой диапазон портов, что угодно ещё) —
    подмена, и в тексте отказа называются КЛЮЧ и ОБА значения, чтобы
    человек в одну строку увидел, что изменилось, и решил сам. Это верный
    исход для привилегированного кода: администратор, вручную расширивший
    диапазон портов, ОБЯЗАН получить этот отказ — непредвиденный аргумент
    `docker run` останавливает исполнитель и спрашивает человека, а не
    молча проходит.

    **Второй баг того же класса — отказ на отсутствии `terminal:` вовсе.**
    Прежняя версия при ``terminal`` не-словаре (в том числе полностью
    отсутствующем ключе) тихо ВОЗВРАЩАЛАСЬ — то есть конфиг, где блока
    ``terminal`` нет СОВСЕМ (наименее заслуживающий доверия случай: кто-то
    мог вырезать блок целиком, а не поправить в нём одно значение), проходил
    БЕЗ единой проверки, включая ``backend``. Отсутствующий блок
    приравнивается к пустому словарю и идёт через ТЕ ЖЕ правила — реальный
    шаблон несёт непустой ``docker_extra_args``, так что полностью пустой
    ``terminal`` закономерно откажет как подмена, а не тихо пройдёт.

    **Третий баг того же класса — умолчание `backend` не совпадало с
    реальным умолчанием рантайма.** Эта функция считала отсутствующий
    ``terminal.backend`` равным ``"docker"`` — но настоящее умолчание
    (``DEFAULT_CONFIG["terminal"]["backend"]`` в
    ``hermes_cli/config_defaults.py``) — ``"local"``. Конфиг с блоком
    ``terminal:``, где просто нет строки ``backend:`` (человек стёр её или
    никогда не писал), проходил эту проверку как «docker» и получал
    маунты, а исполняться при этом продолжал на хосте: маунт создаётся, но
    его никто не видит внутри — тихая полу-рабочая установка. Умолчание
    здесь обязано совпадать с умолчанием рантайма, а не с тем, что
    ожидает контур.

    Заодно: ``None`` и ``[]`` считаются РАВНОЗНАЧНЫМИ при сравнении — все
    три ключа по типу значения либо список, либо ничего; профиль, у
    которого явно записано ``credential_files: []``, не обязан совпадать
    БУКВАЛЬНО с шаблоном, где этого ключа нет вовсе (``None``) — семантика
    «ничего» одна и та же в обоих случаях.
    """
    terminal = cfg.get("terminal")
    if not isinstance(terminal, dict):
        terminal = {}
    shipped_terminal = _shipped_template_terminal_dict()
    for key in _TEMPLATE_COMPARED_TERMINAL_KEYS:
        shipped = shipped_terminal.get(key)
        found = terminal.get(key)
        shipped_cmp = [] if shipped is None else shipped
        found_cmp = [] if found is None else found
        if found_cmp != shipped_cmp:
            raise ContourError(
                f"профиль «{profile}»: terminal.{key} = {found!r}, а в курируемом "
                f"шаблоне это {shipped!r} — похоже на постороннюю правку, файл "
                "контур не трогает"
            )
    backend = terminal.get("backend", "local")
    if backend != "docker":
        raise ContourError(
            f"профиль «{profile}»: terminal.backend = {backend!r}, а не docker — "
            "файл контур не трогает"
        )


def _volume_target(v: Any) -> Optional[str]:
    """Точка монтирования из строки ``host:at:mode`` (``None``, если не парсится)."""
    if not isinstance(v, str):
        return None
    try:
        _host, at, _mode = v.rsplit(":", 2)
        return at
    except ValueError:
        return None


def _volume_host(v: Any) -> Optional[str]:
    """Хостовый путь из строки ``host:at:mode`` (``None``, если не парсится)."""
    if not isinstance(v, str):
        return None
    try:
        host, _at, _mode = v.rsplit(":", 2)
        return host
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# План операций
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedOp:
    """Одна проверенная операция, готовая к применению.

    Поля, не относящиеся к ``kind`` данной операции, остаются ``None`` —
    проще одного класса на все пять операций, чем пять почти одинаковых.
    """

    index: int
    kind: str
    profile: Optional[str] = None
    rel_parts: Optional[tuple] = None
    mode: Optional[str] = None
    container_path: Optional[str] = None
    from_profile: Optional[str] = None
    skill: Optional[str] = None
    url: Optional[str] = None
    token: Optional[str] = None


def _plan_create_folder(i: int, op: dict, root: Path) -> PlannedOp:
    rel_parts = _validate_folder_rel(op.get("folder"), "`folder`")
    # Обзор: `create_folder` — единственная из трёх пишущих операций, которая
    # не проходила через `_check_not_skills_dir` (у `grant`/`revoke` эта
    # проверка есть с самого начала) — агент мог создавать директории ВНУТРИ
    # общей папки навыков, хотя §9 спеки прямо запрещает запись туда в любом
    # виде. Сама папка навыков не даётся через `create_folder` тоже.
    _check_not_skills_dir(rel_parts)
    _resolve_under_root(root, rel_parts)
    return PlannedOp(index=i, kind="create_folder", rel_parts=tuple(rel_parts))


def _plan_grant(i: int, op: dict, root: Path, admin_profiles: set) -> PlannedOp:
    mode = op.get("mode")
    if mode not in VALID_MODES:
        raise ContourError(f"`mode` должен быть «ro» или «rw», получено {mode!r}")
    profile = _check_profile_exists(op.get("profile"))
    rel_parts = _validate_folder_rel(op.get("folder"), "`folder`")
    _check_not_skills_dir(rel_parts)
    _validate_grantable_folder(rel_parts)
    _resolve_under_root(root, rel_parts)
    _check_binding(rel_parts, profile, mode, admin_profiles)
    at = _derive_container_path(rel_parts, profile)
    _check_derived_mount_safe(at)
    return PlannedOp(
        index=i, kind="grant", profile=profile, rel_parts=tuple(rel_parts),
        mode=mode, container_path=at,
    )


def _check_revoke_allowed(rel_parts, profile: str, admin_profiles: set) -> None:
    """Тот же биндинг, что и у ``grant`` (пункт 4 обзора: revoke раньше не
    проверял вообще ничего, кроме формы пути) — но ``revoke`` не несёт
    ``mode`` (снимается монтирование целиком, независимо от того, каким
    режимом оно было выдано), поэтому пара (папка, профиль) допускается,
    если она была бы легитимна ХОТЯ БЫ в одном из двух режимов. Это не
    ослабление :func:`_check_binding` — оно вызывается как есть, дважды;
    просто «отказано» здесь — это отказ ОБОИХ режимов сразу.
    """
    for mode in VALID_MODES:
        try:
            _check_binding(rel_parts, profile, mode, admin_profiles)
            return
        except ContourError:
            continue
    raise ContourError(
        f"revoke {'/'.join(rel_parts)} для «{profile}» отклонён — эта пара профиль/папка "
        "не могла легитимно иметь доступ ни в режиме rw, ни в режиме ro"
    )


def _plan_revoke(i: int, op: dict, root: Path, admin_profiles: set) -> PlannedOp:
    profile = _check_profile_exists(op.get("profile"))
    rel_parts = _validate_folder_rel(op.get("folder"), "`folder`")
    _check_not_skills_dir(rel_parts)
    _validate_grantable_folder(rel_parts)
    _resolve_under_root(root, rel_parts)
    _check_revoke_allowed(rel_parts, profile, admin_profiles)
    at = _derive_container_path(rel_parts, profile)
    _check_derived_mount_safe(at)
    return PlannedOp(index=i, kind="revoke", profile=profile, rel_parts=tuple(rel_parts), container_path=at)


def _plan_publish_skill(i: int, op: dict, root: Path) -> PlannedOp:
    from hermes_cli.profiles import get_profile_dir

    from_profile = _check_profile_exists(op.get("from_profile"))
    skill = op.get("skill")
    _validate_segment(skill, "`skill`")
    src = get_profile_dir(from_profile) / "skills" / skill
    # Пункт 2 обзора: САМА папка навыка никогда не проверялась на symlink —
    # только её ДЕТИ (цикл ниже). ``Path.is_dir()`` следует за символическими
    # ссылками, так что симлинк-каталог «навыка» на что угодно ещё проходил
    # бы дальше как обычная папка, а ``copytree(symlinks=False)`` при apply
    # скопировал бы содержимое ЦЕЛИ ссылки — включая всё, что угодно вне
    # песочницы профиля. Проверка обязана идти ДО ``is_dir()``.
    if src.is_symlink():
        raise ContourError(f"{src} — символическая ссылка, отказ")
    if not src.is_dir():
        raise ContourError(f"источник {src} не папка (или не существует)")
    if not (src / "SKILL.md").is_file():
        raise ContourError(f"{src} не содержит SKILL.md — не похоже на навык")
    total = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        for name in list(dirnames) + list(filenames):
            p = Path(dirpath) / name
            if p.is_symlink():
                raise ContourError(f"{p} — символическая ссылка внутри навыка, отказ")
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    if total > MAX_SKILL_SIZE_BYTES:
        raise ContourError(
            f"навык «{skill}» весит {total} байт — больше предела {MAX_SKILL_SIZE_BYTES}"
        )
    return PlannedOp(index=i, kind="publish_skill", from_profile=from_profile, skill=skill)


_HTTPS_URL_RE = re.compile(r"^https://[^@\s]+$")


def _plan_set_git_remote(i: int, op: dict) -> PlannedOp:
    url = op.get("url")
    token = op.get("token")
    if not isinstance(url, str) or not _HTTPS_URL_RE.match(url):
        raise ContourError("`url` должен быть https-адресом без встроенных учётных данных (без «@»)")
    if token == _TOKEN_SCRUB_PLACEHOLDER:
        # Пункт 6 обзора: run_executor стирает токен из файла заявки СРАЗУ
        # после разбора JSON — раньше проверки `when: night`, — и сохраняет
        # настоящее значение в защищённый файл в тот же момент. Заявка,
        # отложенная до ночи, перечитывается заново на следующем тике: к
        # тому моменту `token` в файле — уже этот плейсхолдер, а не
        # секрет. Настоящее значение берём из защищённого файла, а не
        # принимаем текст плейсхолдера буквально как новый токен.
        token = _load_token()
        if not token:
            raise ContourError(
                "`token` в заявке уже стёрт (`***scrubbed***`), а сохранённого токена нет — "
                "set_git_remote невозможно применить"
            )
    elif not isinstance(token, str) or not token:
        raise ContourError("`token` должен быть непустой строкой")
    return PlannedOp(index=i, kind="set_git_remote", url=url, token=token)


def validate_request(request: Any, *, root: Path = DEFAULT_ROOT, admin_profiles: Optional[set] = None):
    """Проверить запрос ЦЕЛИКОМ против грамматики и состояния машины.

    Возвращает ``(planned, when)`` — ``planned`` в исходном порядке запроса.
    Бросает :class:`ContourError` при первом же нарушении, включая
    неизвестную операцию: приняли решение бросать сразу, а не собирать все
    ошибки, — потому что «весь запрос отклонён» не нуждается в полном списке
    причин, а отдавать модели длинный список того, что она нарушила, —
    прямой путь научить её будущий обход валидации.

    Ничего не пишет на диск. Единственный ввод-вывод — ЧТЕНИЕ: список
    профилей, их текущие config.yaml (для проверки на подмену) и файл
    администраторов. Это оправданный ввод-вывод для валидации «против
    состояния машины», а не нарушение чистоты — просто нет мутаций.
    """
    if not isinstance(request, dict):
        raise ContourError("запрос должен быть JSON-объектом")
    when = request.get("when")
    if when not in ("now", "night"):
        raise ContourError("`when` должен быть «now» или «night»")
    ops = request.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ContourError("`ops` должен быть непустым списком")
    if len(ops) > MAX_OPS_PER_REQUEST:
        raise ContourError(
            f"`ops` содержит {len(ops)} операций — больше предела {MAX_OPS_PER_REQUEST} "
            "за одну заявку; разбейте на несколько заявок"
        )

    if admin_profiles is None:
        admin_profiles = load_admin_profiles()
    root = Path(root)

    planned = []
    touched_profiles = set()
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ContourError(f"ops[{i}]: операция должна быть объектом")
        kind = op.get("op")
        if kind not in VALID_OPS:
            raise ContourError(f"ops[{i}]: неизвестная операция «{kind}» — весь запрос отклонён")
        try:
            if kind == "create_folder":
                planned.append(_plan_create_folder(i, op, root))
            elif kind == "grant":
                p = _plan_grant(i, op, root, admin_profiles)
                planned.append(p)
                touched_profiles.add(p.profile)
            elif kind == "revoke":
                p = _plan_revoke(i, op, root, admin_profiles)
                planned.append(p)
                touched_profiles.add(p.profile)
            elif kind == "publish_skill":
                planned.append(_plan_publish_skill(i, op, root))
            elif kind == "set_git_remote":
                planned.append(_plan_set_git_remote(i, op))
        except ContourError as e:
            raise ContourError(f"ops[{i}] ({kind}): {e}") from e

    for profile in touched_profiles:
        cfg = _load_profile_config(_profile_config_path(profile))
        _check_config_not_tampered(profile, cfg)

    # Пункт 8 обзора: точки монтирования теперь считаются от ВСЕГО пути (не
    # от последнего сегмента), поэтому с текущей грамматикой это структурно
    # не должно случаться — оставлено как belt-and-braces инвариант, а не
    # как достижимая ветка: тот же СПОСОБ отказа (grant молча перезаписывает
    # grant), какой раньше давала коллизия по последнему сегменту, доступен
    # и без неё — два ``grant`` на РОВНО одну и ту же папку одному профилю
    # разными режимами (`rw` затем `ro`) резолвятся в одну и ту же точку
    # монтирования, и вторая молча «выигрывает» при apply, пока обе
    # операции всё равно репортятся как «ok». Отказываем целиком, а не
    # позволяем одной операции тихо съесть другую.
    seen_mount_points: dict = {}
    for p in planned:
        if p.kind != "grant":
            continue
        key = (p.profile, p.container_path)
        prior = seen_mount_points.get(key)
        if prior is not None:
            raise ContourError(
                f"ops[{prior}] и ops[{p.index}] выдают профилю «{p.profile}» одну и ту же "
                f"точку монтирования {p.container_path!r} — заявка отклонена целиком, а не "
                "перезаписывает одну операцию другой"
            )
        seen_mount_points[key] = p.index

    return planned, when


# ---------------------------------------------------------------------------
# Git-слой: локальный, без GitHub
# ---------------------------------------------------------------------------

_GITIGNORE_CONTENT = """\
# dept/ — рабочие файлы отделов: это данные, а не знания компании.
# История отвечает на «кто и когда поменял регламент», а не «что отгрузили
# сегодня в отдел продаж» — см. references/model.md, «Git — на знаниях».
dept/

# Тяжёлые бинарники — не раздувать репозиторий регламентов.
*.zip
*.tar
*.tar.gz
*.tgz
*.7z
*.rar
*.iso
*.dmg
*.exe
*.mp4
*.mov
*.avi
*.mkv
*.psd
node_modules/
.DS_Store
"""


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run_git(root: Path, args, *, check: bool = True, capture: bool = True):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise ContourError(f"git {' '.join(args)} упал: {(result.stderr or '').strip()}")
    return result


def ensure_repo(root: Path) -> bool:
    """``git init`` + ``.gitignore`` + локальные ``user.name``/``user.email``,
    если ещё не сделано. Возвращает True, если репозиторий только что создан.

    Локальные ``user.name``/``user.email`` обязательны: свежая машина клиента
    не обязана иметь глобальный ``git config`` — без них первый же коммит
    падает с «Please tell me who you are».
    """
    if not _git_available():
        raise ContourError("git не найден в PATH — история контура вестись не может")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    git_dir = root / ".git"
    freshly_initialized = not git_dir.is_dir()
    if freshly_initialized:
        result = subprocess.run(
            ["git", "-C", str(root), "init", "-b", "main"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            # Старый git (< 2.28) не знает `-b` при `init` — обычный init,
            # затем переключение ветки до первого коммита.
            _run_git(root, ["init"])
            subprocess.run(
                ["git", "-C", str(root), "symbolic-ref", "HEAD", "refs/heads/main"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        _run_git(root, ["config", "user.name", "Trix Contour"])
        _run_git(root, ["config", "user.email", "trix-contour@localhost"])
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        atomic_write_text(gitignore, _GITIGNORE_CONTENT)
    return freshly_initialized


def commit_all(root: Path, message: str) -> Optional[str]:
    """Закоммитить всё, что изменилось (кроме ``.gitignore``'нутого). ``None``,
    если менять нечего — второй одинаковый ``apply`` не плодит пустые коммиты."""
    if not _git_available():
        raise ContourError("git не найден в PATH")
    _run_git(root, ["add", "-A"])
    diff = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet"], capture_output=True)
    if diff.returncode == 0:
        return None
    _run_git(root, ["commit", "-m", message])
    rev = _run_git(root, ["rev-parse", "HEAD"])
    return rev.stdout.strip()


def _token_file_path() -> Path:
    return _executor_state_dir() / "git_token"


def _store_token(token: str) -> None:
    path = _token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_token() -> Optional[str]:
    try:
        token = _token_file_path().read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def _pending_remote_path() -> Path:
    return _executor_state_dir() / "pending_remote.json"


def _write_pending_remote(url: str, now: datetime) -> None:
    """Заявить remote, не активируя его — см. :func:`approve_remote`."""
    path = _pending_remote_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps({"url": url, "requested_at": now.isoformat()}, ensure_ascii=False)
    )


def read_pending_remote() -> Optional[dict]:
    """Remote, заявленный агентом и ждущий человеческой активации, или
    ``None``. Только чтение — не привязано к ``root`` (лежит под
    ``HERMES_HOME`` исполнителя, как и токен)."""
    try:
        data = json.loads(_pending_remote_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("url"), str) and data["url"]:
        return data
    return None


def _clear_pending_remote() -> None:
    try:
        _pending_remote_path().unlink()
    except OSError:
        pass


def approve_remote(root: Path) -> dict:
    """Человек активирует remote, заявленный агентом через `set_git_remote`.

    Обзор (blocker 2): `set_git_remote` раньше настраивал НАСТОЯЩИЙ git
    remote немедленно, из одной агентской заявки, без единого человека в
    петле — при этом URL принимал ЛЮБОЙ https-хост (`_HTTPS_URL_RE` не
    сверяется ни с каким allowlist'ом), а ежедневная `trix-contour-push`
    cron-задача затем сама отправляла ВЕСЬ рабочий каталог компании
    (регламенты, прайсы, решения, навыки) туда. Отравленному агенту не
    нужен был даже рабочий токен — сервер под контролем атакующего
    принимает что угодно. Это шире по радиусу поражения, чем
    `publish_skill`, вокруг которого уже стоят два барьера (`.pending/` +
    `hermes contour approve-skill`) — тот же барьер поставлен и здесь:
    заявка только запоминает URL как ОЖИДАЮЩИЙ (:func:`_write_pending_remote`,
    виден в файле результата заявки), реальный `git remote add/set-url`
    происходит ТОЛЬКО отсюда — из команды, которую запускает человек
    (`hermes contour approve-remote`), а не агент (агенту `hermes ...`
    недостижим — `approvals.deny` профиля `system_admin`, спека 21 §5/§9).
    До активации :func:`push` отказывает с понятной причиной, а не
    молчаливым no-op — иначе человек мог бы решить, что резервная копия
    уже настроена, хотя remote ещё даже не существует.
    """
    root = Path(root)
    pending = read_pending_remote()
    if not pending:
        raise ContourError(
            "нет заявленного remote, ожидающего активации — сначала подайте "
            "заявку с `set_git_remote`"
        )
    token = _load_token()
    if not token:
        raise ContourError(
            "токен для remote не найден в защищённом хранилище — заявку с "
            "`set_git_remote` нужно подать заново"
        )
    set_git_remote(root, pending["url"], token)
    _clear_pending_remote()
    return {"url": pending["url"]}


def set_git_remote(root: Path, url: str, token: str) -> None:
    """Настроить remote (без токена в URL) и сохранить токен отдельно, вне root.

    Токен НИКОГДА не попадает ни в ``git remote``, ни в файл внутри ``root``
    (а значит — никогда не закоммитится); он лежит в файле ``0600`` под
    ``HERMES_HOME`` исполнителя и подставляется в URL только на время вызова
    :func:`push`.
    """
    ensure_repo(root)
    _store_token(token)
    remotes = _run_git(root, ["remote"], check=False).stdout.split()
    if "origin" in remotes:
        _run_git(root, ["remote", "set-url", "origin", url])
    else:
        _run_git(root, ["remote", "add", "origin", url])


def push(root: Path) -> str:
    """Отправить историю в настроенный remote. Текст результата НИКОГДА не
    содержит токен — даже при ошибке (``git`` иногда эхом отражает URL в
    диагностике; поэтому ``stderr`` намеренно не попадает в текст ошибки).

    Пункт 6 обзора: токен раньше подставлялся ПРЯМО В URL аргумента ``git
    push`` — виден в ``ps``/списке процессов ЛЮБОМУ локальному
    пользователю по умолчанию, без специальных прав. Токен передаётся ЧЕРЕЗ
    ОКРУЖЕНИЕ дочернего процесса, не через argv: ``git`` умеет читать
    конфиг из ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n``
    (git >= 2.31) — сюда уходит ``http.extraHeader`` с заголовком
    ``Authorization: Basic ...``, а сам ``git push`` получает ЧИСТЫЙ URL
    remote'а без единого встроенного секрета.
    """
    if not _git_available():
        raise ContourError("git не найден в PATH")
    got = _run_git(root, ["remote", "get-url", "origin"], check=False)
    remote_url = got.stdout.strip() if got.returncode == 0 else ""
    if not remote_url:
        pending = read_pending_remote()
        if pending:
            raise ContourError(
                f"remote {pending['url']} заявлен, но ждёт активации человеком — "
                "запустите `hermes contour approve-remote`, прежде чем push сможет работать"
            )
        return "удалённый репозиторий не настроен — push пропущен"
    token = _load_token()
    if not token:
        raise ContourError("токен для push не найден — сначала set_git_remote")
    branch_res = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    branch = branch_res.stdout.strip() or "main"
    auth_header = "Authorization: Basic " + base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": auth_header,
    })
    result = subprocess.run(
        ["git", "-C", str(root), "push", "origin", f"HEAD:{branch}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if result.returncode != 0:
        # НЕ включаем result.stderr дословно: git иногда повторяет в
        # диагностике сам URL с уже подставленным токеном.
        raise ContourError(f"git push не удался (код {result.returncode}) — см. журнал git на хосте")
    return "история отправлена в удалённый репозиторий"


# ---------------------------------------------------------------------------
# Применение плана
# ---------------------------------------------------------------------------


@dataclass
class OpResult:
    index: int
    kind: str
    ok: bool
    detail: str


@dataclass
class ApplyResult:
    ops: list = field(default_factory=list)
    backups: list = field(default_factory=list)
    commit: Optional[str] = None


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S%f")


#: Содержимое маркера, которым помечается ``<root>/skills`` как папка,
#: управляемая контуром — см. :func:`ensure_skills_dir_marker`. Русская
#: проза внутри — расчёт на то, что человек, который найдёт файл руками
#: (или откроет его из любопытства), сразу поймёт, что это и почему его
#: не нужно трогать. Содержимое НЕ проверяется кодом (см. докстринг
#: ``agent.skill_utils.is_contour_managed_skill_path`` — значение имеет
#: только сам факт существования файла), так что менять этот текст можно
#: свободно, лишь бы файл продолжал существовать.
_SKILLS_DIR_MARKER_CONTENT = """\
Эта папка — общая база навыков компании, которой управляет `hermes
contour` (hermes_cli/trix_contour.py), а не личное хранилище отдельного
профиля. Файлы сюда попадают ТОЛЬКО через `publish_skill` + `hermes
contour approve-skill` (человек за терминалом) — не редактируйте и не
удаляйте их вручную, и не удаляйте этот файл: по его наличию агентам
запрещено писать в эту папку напрямую через `skill_manage`, независимо
от того, из какого профиля вызов (см. спека 21 §9, blocker 1 обзора).
"""


def ensure_skills_dir_marker(root: Path) -> Path:
    """Пометить ``<root>/skills`` как управляемую контуром папку —
    идемпотентно: создаёт папку, если её ещё нет, и пишет маркер, только
    если его ещё нет (никогда не перезаписывает существующий — правка
    человеком его текста, если он вообще на это пойдёт, не должна
    откатываться следующим тиком исполнителя).

    Вызывается из ДВУХ мест, чтобы ни один путь развёртывания не остался
    без маркера: ``hermes business setup`` (свежая машина — папка ещё не
    существует вовсе) и первое же :func:`apply_planned` (машина уже
    развёрнута до появления этой защиты — маркер доедет тем же тиком,
    которым исполнитель в следующий раз применит хоть что-то, без ручной
    миграции). Идемпотентность обоих вызовов означает, что порядок и
    количество повторных вызовов не имеют значения.
    """
    root = Path(root)
    skills_dir = root / SKILLS_DIR_NAME
    skills_dir.mkdir(parents=True, exist_ok=True)
    from agent.skill_utils import CONTOUR_MANAGED_SKILLS_MARKER

    marker = skills_dir / CONTOUR_MANAGED_SKILLS_MARKER
    if not marker.exists():
        atomic_write_text(marker, _SKILLS_DIR_MARKER_CONTENT)
    return marker


#: Имя папки-приёмника кандидатов навыков внутри ``<root>/skills`` — НЕ
#: живая папка, агенты её не видят (см. ``EXCLUDED_SKILL_DIRS`` в
#: ``agent/skill_utils.py``, которая исключает её из обхода И для
#: ``~/.hermes/skills``, И для любого ``skills.external_dirs`` — а
#: ``<root>/skills`` именно им и становится, см. :func:`apply_planned`).
#: Пункт 3 обзора: сюда попадает КАЖДЫЙ ``publish_skill`` — публикация
#: становится живой только через :func:`approve_skill`, которую запускает
#: человек, а не агент (``hermes contour approve-skill``; агенту
#: недостижимо — команда начинается с ``hermes `` и попадает под
#: ``approvals.deny`` профиля ``system_admin``, см. спека 21 §5/§9).
_PENDING_SKILLS_DIR_NAME = ".pending"


def _refuse_if_symlink_inside(path: Path, what: str) -> None:
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in list(dirnames) + list(filenames):
            found = Path(dirpath) / name
            if found.is_symlink():
                raise ContourError(f"{found} — символическая ссылка {what}, отказ")


def _copy_skill_dir(src: Path, dest_parent: Path, name: str) -> Path:
    """Атомарно скопировать ``src`` в ``dest_parent/name`` (заменяя, если уже
    есть), с symlink-safety на обоих концах — общий код для «кандидат ->
    pending» (:func:`_publish_skill_to_pending`) и «pending -> живое»
    (:func:`approve_skill`).

    Пункт 2 обзора: ``copytree(..., symlinks=True)`` — раньше
    ``symlinks=False`` разыменовывало КАЖДУЮ ссылку и копировало содержимое
    её ЦЕЛИ; при символическом src-каталоге (не проверявшемся отдельно
    раньше) это давало прямую утечку произвольного файла с диска в
    git-отслеживаемую ``<root>/skills``. С ``symlinks=True`` ссылка
    копируется КАК ссылка — а затем результат целиком сканируется на
    предмет «хоть одна ссылка попала внутрь» и полностью удаляется при
    находке: закрывает и «символическая ссылка появилась между сканом и
    copytree» (TOCTOU), и «copytree сама создала неожиданную ссылку».
    """
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = dest_parent / name
    tmp = dest_parent / f".tmp-{name}-{uuid.uuid4().hex}"
    shutil.copytree(src, tmp, symlinks=True)
    try:
        _refuse_if_symlink_inside(tmp, "в результате копирования")
    except ContourError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if dest.is_dir():
        shutil.rmtree(dest)
    os.replace(tmp, dest)
    return dest


def _content_digest(dir_path: Path) -> str:
    """Детерминированный sha256 по ВСЕМ файлам ``dir_path`` (относительный
    путь + байты, отсортировано) — привязывает одобрение к ТОЧНО тем
    байтам, которые показали человеку (blocker 3 обзора спеки 21 §9,
    TOCTOU: см. :func:`approve_skill`)."""
    h = hashlib.sha256()
    for p in sorted(dir_path.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(dir_path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _publish_skill_to_pending(root: Path, from_profile: str, skill: str) -> tuple:
    """Скопировать кандидат навыка в ``<root>/skills/.pending/<skill>/`` —
    НЕ в живую папку (пункт 3 обзора). Живым навык становится только через
    :func:`approve_skill`, которую запускает человек — см. докстринг
    :data:`_PENDING_SKILLS_DIR_NAME` и спека 21 §9 («publish_skill —
    единственное исключение: ... показывает текст человеку»). Возвращает
    ``(текст SKILL.md, путь к pending-копии, дайджест содержимого)`` — текст
    и дайджест уходят целиком в файл результата заявки
    (:func:`run_executor`), чтобы человек увидел их, даже не заходя на
    машину руками. Дайджест — см. :func:`_content_digest` и докстринг
    :func:`approve_skill` за тем, зачем он нужен (TOCTOU, blocker 3
    обзора).
    """
    from hermes_cli.profiles import get_profile_dir

    src = get_profile_dir(from_profile) / "skills" / skill
    # Повторная проверка (включая САМ src, не только детей — пункт 2
    # обзора) прямо перед копированием — защита от TOCTOU между
    # validate_request и apply_planned (источник живёт в песочнице
    # профиля, которую сам агент мог успеть переписать).
    if src.is_symlink():
        raise ContourError(f"{src} стал символической ссылкой — отказ")
    _refuse_if_symlink_inside(src, "внутри навыка")
    pending_root = root / SKILLS_DIR_NAME / _PENDING_SKILLS_DIR_NAME
    dest = _copy_skill_dir(src, pending_root, skill)
    skill_md_text = (dest / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    digest = _content_digest(dest)
    return skill_md_text, dest, digest


def pending_skill_preview(root: Path, skill: str) -> dict:
    """Прочитать (не мутируя ничего) текст и дайджест кандидата, ожидающего
    одобрения — то, что ``hermes contour approve-skill`` обязан показать
    человеку СВЕЖИМ (не из старого файла результата заявки) прямо перед
    вопросом «одобряете?». Делает те же проверки на символические ссылки,
    что и :func:`approve_skill`, — библиотека решает, CLI только печатает
    (см. докстринг ``hermes_cli/trix_contour_cli.py``).
    """
    root = Path(root)
    _validate_segment(skill, "`skill`")
    skills_root = root / SKILLS_DIR_NAME
    pending = skills_root / _PENDING_SKILLS_DIR_NAME / skill
    if pending.is_symlink():
        raise ContourError(f"{pending} — символическая ссылка, отказ")
    if not pending.is_dir():
        raise ContourError(f"нет навыка «{skill}», ожидающего одобрения, в {pending}")
    _refuse_if_symlink_inside(pending, "в ожидающем навыке")
    skill_md = pending / "SKILL.md"
    if not skill_md.is_file():
        raise ContourError(f"{pending} не содержит SKILL.md")
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    digest = _content_digest(pending)
    return {"skill": skill, "path": str(pending), "text": text, "digest": digest}


def approve_skill(root: Path, skill: str, *, expected_digest: Optional[str] = None) -> dict:
    """Человек одобряет ожидающий навык — переносит его из
    ``<root>/skills/.pending/<skill>`` в живую ``<root>/skills/<skill>`` и
    коммитит (пункт 3 обзора). Единственный код-путь, которым содержимое
    ``.pending/`` вообще становится видимым системным промптам агентов
    компании: до этого вызова оно лежит только в git-истории рабочего
    каталога (``.pending`` в ``EXCLUDED_SKILL_DIRS``, см.
    ``agent/skill_utils.py`` — исключена из обхода и для локальных
    ``~/.hermes/skills``, и для ``skills.external_dirs``, которой
    ``<root>/skills`` и становится). Вызывается ТОЛЬКО из
    ``hermes contour approve-skill`` — человеком за терминалом; агент не
    может выполнить эту команду сам (``*hermes *`` в ``approvals.deny``
    профиля ``system_admin``).

    **TOCTOU, blocker 3 обзора.** Заявка тикает раз в минуту, и человек не
    читает результат и не набирает `approve-skill` мгновенно — между
    показом текста и одобрением есть окно. Отравленный агент мог показать
    ДОБРОКАЧЕСТВЕННЫЙ текст, дождаться, что человек согласен, и в этом
    самом окне подать ВТОРОЙ `publish_skill` тем же именем — вторая заявка
    молча перезаписывает `.pending/<skill>` (см. :func:`_copy_skill_dir`),
    и старая версия `_cmd_approve_skill` промотировала бы в живую папку
    уже ПОДМЕНЁННОЕ содержимое, ни разу не показав его. ``expected_digest``
    привязывает одобрение к КОНКРЕТНЫМ байтам: CLI считает его прямо перед
    вопросом человеку (:func:`pending_skill_preview`) и передаёт сюда;
    если к моменту фактического вызова содержимое ``.pending/`` уже другое
    (дайджест не совпадает) — отказ, а не тихая промоция подменённого
    текста. Без ``expected_digest`` (обратная совместимость/скрипты) эта
    проверка пропускается — вызывающий код сам отвечает за то, что
    показывает актуальный текст.
    """
    root = Path(root)
    _validate_segment(skill, "`skill`")
    if not _git_available():
        raise ContourError("git не найден в PATH — история контура вестись не может")
    skills_root = root / SKILLS_DIR_NAME
    pending = skills_root / _PENDING_SKILLS_DIR_NAME / skill
    if pending.is_symlink():
        raise ContourError(f"{pending} — символическая ссылка, отказ")
    if not pending.is_dir():
        raise ContourError(f"нет навыка «{skill}», ожидающего одобрения, в {pending}")
    _refuse_if_symlink_inside(pending, "в ожидающем навыке")
    if expected_digest is not None:
        actual_digest = _content_digest(pending)
        if expected_digest.strip() != actual_digest:
            raise ContourError(
                f"кандидат навыка «{skill}» изменился с момента, когда его показали "
                f"человеку (ожидался дайджест {expected_digest.strip()}, сейчас "
                f"{actual_digest}) — покажите текст заново и одобряйте по нему"
            )
    dest = skills_root / skill
    is_update = dest.is_dir()
    _copy_skill_dir(pending, skills_root, skill)
    shutil.rmtree(pending, ignore_errors=True)
    ensure_repo(root)
    verb = "update" if is_update else "add"
    message = f"contour: approve-skill\n\n- [ok] publish_skill: {verb} {skill} (одобрено человеком)"
    commit = commit_all(root, message)
    return {"skill": skill, "is_update": is_update, "commit": commit}


def _op_public_detail(p: PlannedOp) -> str:
    """Текстовое описание операции для коммита/аудита — БЕЗ токена и БЕЗ
    полного текста SKILL.md (тот уходит только в файл результата заявки,
    не в git-сообщение коммита, — см. :func:`_publish_skill_to_pending`)."""
    if p.kind == "create_folder":
        return "/".join(p.rel_parts)
    if p.kind in ("grant", "revoke"):
        extra = f" mode={p.mode}" if p.mode else ""
        return f"{p.profile} <- {'/'.join(p.rel_parts)}{extra}"
    if p.kind == "publish_skill":
        return f"{p.skill} from {p.from_profile} -> .pending/ (ждёт hermes contour approve-skill)"
    if p.kind == "set_git_remote":
        return f"remote={p.url}"  # НИКОГДА не включать p.token
    return "?"


def _build_commit_message(planned, results_by_index: dict) -> str:
    kinds = []
    for p in planned:
        if p.kind not in kinds:
            kinds.append(p.kind)
    lines = [f"contour: {', '.join(kinds)}", ""]
    for p in planned:
        r = results_by_index[p.index]
        mark = "ok" if r.ok else "FAILED"
        lines.append(f"- [{mark}] {p.kind}: {_op_public_detail(p)}")
    return "\n".join(lines)


def apply_planned(planned: list, root: Path) -> ApplyResult:
    """Применить УЖЕ провалидированный план. Отказ git'а на PATH — это отказ
    ВСЕГО применения (ничего не создаётся, ничего не пишется): история —
    не опциональная возможность этого модуля, а его контракт (пункт брифа
    «Refuse to operate ... fail loudly rather than silently skipping
    history»). Любой другой отказ (I/O, подмена конфига, обнаруженная в
    момент записи) — частичный: остальные операции продолжают применяться,
    и по каждой возвращается точный результат.
    """
    root = Path(root)
    if not _git_available():
        raise ContourError("git не найден в PATH — история контура вестись не может, ничего не применяю")

    _check_no_symlink_components(root)
    root.mkdir(parents=True, exist_ok=True)
    ensure_repo(root)
    # Идемпотентно — "первый прогон контура" на машине, где хостовый
    # исполнитель уже был развёрнут ДО появления этой защиты (см. докстринг
    # ensure_skills_dir_marker): маркер доедет тем же тиком, а не потребует
    # ручной миграции.
    ensure_skills_dir_marker(root)

    results_by_index: dict = {}
    backups = []
    any_mutation = False

    # 1) create_folder — независимые друг от друга по конструкции.
    for p in planned:
        if p.kind != "create_folder":
            continue
        try:
            dest = _resolve_under_root(root, list(p.rel_parts))
            dest.mkdir(parents=True, exist_ok=True)
            any_mutation = True
            results_by_index[p.index] = OpResult(p.index, p.kind, True, f"папка {dest} готова")
        except Exception as e:
            results_by_index[p.index] = OpResult(p.index, p.kind, False, str(e))

    # 2) grant/revoke — один конфиг-файл на профиль, один rewrite_config
    #    вызов на все операции этого профиля разом (иначе повторная запись
    #    одного и того же файла в рамках одного apply рисковала бы гонкой
    #    сама с собой).
    by_profile: dict = {}
    for p in planned:
        if p.kind in ("grant", "revoke"):
            by_profile.setdefault(p.profile, []).append(p)

    for profile, ops in by_profile.items():
        try:
            cfg_path = _profile_config_path(profile)
            text = _read_raw_text(cfg_path)
            data = yaml.safe_load(text) or {}
            if not isinstance(data, dict):
                raise ContourError(f"{cfg_path}: верхний уровень конфига — не словарь")
            _check_config_not_tampered(profile, data)  # повтор — конфиг мог измениться со времени validate_request

            volumes = list((data.get("terminal") or {}).get("docker_volumes") or [])

            # Обзор: docker_volumes раньше не входил в проверку на подмену
            # вовсе — `_check_config_not_tampered` сравнивает только три
            # ключа terminal.* (extra_args/forward_env/credential_files), а
            # не сами маунты. Оператор, вручную поставивший свой маунт на
            # ТУ ЖЕ точку монтирования (например, `/company`, до первого
            # `grant` контура), молча терял его на первом же `grant`/
            # `revoke`: код ниже фильтрует volumes по совпадению
            # container_path и просто выбрасывает найденное, а apply
            # рапортует «конфиг обновлён» как успех. Отличаем «мы сами
            # когда-то смонтировали это» (host resolve'ится под root) от
            # «постороннее» (не под root) — и отказываем на постороннем,
            # вместо того чтобы тихо его вытеснить.
            real_root = os.path.realpath(root)
            foreign = []
            for p in ops:
                for v in volumes:
                    if _volume_target(v) != p.container_path:
                        continue
                    host = _volume_host(v)
                    if host is None:
                        continue
                    real_host = os.path.realpath(host)
                    if real_host != real_root and not real_host.startswith(real_root + os.sep):
                        foreign.append((p.container_path, v))
            if foreign:
                detail = "; ".join(f"{cp} <- {vol!r}" for cp, vol in foreign)
                raise ContourError(
                    f"на точке(ах) монтирования профиля «{profile}» уже стоит маунт, не "
                    f"принадлежащий контуру ({detail}) — похоже на маунт, поставленный "
                    "оператором вручную; правка отклонена, чтобы не вытеснить его молча. "
                    "Снимите его вручную из config.yaml, если он больше не нужен."
                )

            has_grant = False
            for p in ops:
                volumes = [v for v in volumes if _volume_target(v) != p.container_path]
                if p.kind == "grant":
                    has_grant = True
                    host = _resolve_under_root(root, list(p.rel_parts))
                    volumes.append(f"{host}:{p.container_path}:{p.mode}")

            expected = {("terminal", "docker_volumes"): volumes}
            # Пункт 4 обзора: revoke раньше ДОПИСЫВАЛ skills.external_dirs
            # безусловно, тем же кодом путём, что и grant — то есть можно
            # было форсировать общую папку навыков профилю, у которого
            # администратор её явно снял, просто вызвав revoke на что
            # угодно ещё в его конфиге. Трогаем skills.external_dirs
            # ТОЛЬКО если в этом же вызове был хотя бы один grant.
            if has_grant:
                external = list((data.get("skills") or {}).get("external_dirs") or [])
                skills_host = str(root / SKILLS_DIR_NAME)
                if skills_host not in external:
                    external = external + [skills_host]
                expected[("skills", "external_dirs")] = external

            new_text = rewrite_config(text, expected)

            if new_text != text:
                backup_path = cfg_path.with_name(cfg_path.name + f".bak-{_timestamp()}")
                atomic_write_text(backup_path, text, newline="")
                backups.append(backup_path)
                atomic_write_text(cfg_path, new_text, newline="", preserve_mode=True)
                any_mutation = True
                for p in ops:
                    results_by_index[p.index] = OpResult(p.index, p.kind, True, f"конфиг «{profile}» обновлён")
            else:
                for p in ops:
                    results_by_index[p.index] = OpResult(
                        p.index, p.kind, True, f"конфиг «{profile}» уже в нужном состоянии"
                    )
        except Exception as e:
            for p in ops:
                results_by_index[p.index] = OpResult(p.index, p.kind, False, str(e))

    # 3) publish_skill — лендит в .pending/, НЕ в живую папку (пункт 3
    #    обзора). Полный текст SKILL.md идёт прямо в detail этой операции —
    #    run_executor пишет detail дословно в файл результата заявки, так
    #    человек видит текст навыка, не заходя на машину руками.
    for p in planned:
        if p.kind != "publish_skill":
            continue
        try:
            skill_md_text, pending_path, digest = _publish_skill_to_pending(root, p.from_profile, p.skill)
            any_mutation = True
            results_by_index[p.index] = OpResult(
                p.index, p.kind, True,
                f"навык «{p.skill}» скопирован в {pending_path} — ЖДЁТ ОДОБРЕНИЯ ЧЕЛОВЕКА "
                f"(`hermes contour approve-skill {p.skill}`), НЕ опубликован.\n"
                f"Дайджест содержимого (изменится, если кандидат подменят до одобрения): {digest}\n"
                "Текст SKILL.md:\n" + "-" * 40 + f"\n{skill_md_text}\n" + "-" * 40,
            )
        except Exception as e:
            results_by_index[p.index] = OpResult(p.index, p.kind, False, str(e))

    # 4) set_git_remote — заявляет remote как ОЖИДАЮЩИЙ, не активирует его
    #    (blocker 2 обзора спеки 21 §9): реальный `git remote add/set-url`
    #    происходит только из `hermes contour approve-remote`, которую
    #    запускает человек. Токен сохраняем сразу (как и раньше) — он уже
    #    провалидирован :func:`_plan_set_git_remote`, и заявка `night`
    #    должна пережить свою собственную заготовку токена так же, как и
    #    остальные операции.
    for p in planned:
        if p.kind != "set_git_remote":
            continue
        try:
            _store_token(p.token)
            _write_pending_remote(p.url, datetime.now())
            any_mutation = True
            results_by_index[p.index] = OpResult(
                p.index, p.kind, True,
                f"remote {p.url} заявлен и ЖДЁТ ОДОБРЕНИЯ ЧЕЛОВЕКА "
                "(`hermes contour approve-remote`) — не активирован. "
                "Токен сохранён в защищённое хранилище.",
            )
        except Exception as e:
            results_by_index[p.index] = OpResult(p.index, p.kind, False, str(e))

    commit = None
    if any_mutation:
        message = _build_commit_message(planned, results_by_index)
        commit = commit_all(root, message)

    ordered_results = [results_by_index[p.index] for p in planned]
    return ApplyResult(ops=ordered_results, backups=backups, commit=commit)


# ---------------------------------------------------------------------------
# Исполнитель — точка входа для cron (`no_agent`)
# ---------------------------------------------------------------------------


def _in_night_window(now: datetime) -> bool:
    return NIGHT_WINDOW_START_HOUR <= now.hour < NIGHT_WINDOW_END_HOUR


def _audit_log_path() -> Path:
    return _executor_state_dir() / "audit.log"


def _append_audit(entry: dict) -> None:
    """Дописать строку в журнал аудита. ВНЕ root — агент, которому доступен
    ``root`` через маунты своей песочницы, физически не может дотянуться до
    ``HERMES_HOME`` исполнителя, а значит не может ни прочитать, ни стереть,
    ни переписать собственный журнал."""
    path = _audit_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _extract_raw_git_tokens(request: Any) -> list:
    """Достать значения ``token`` из ЛЮБОГО ``set_git_remote`` в
    НЕПРОВЕРЕННОМ запросе — структурным разбором, БЕЗ полной грамматики
    :func:`validate_request` (то же самое поле, которое потом
    провалидирует :func:`_plan_set_git_remote`).

    Нужно вызывать это ДО валидации (пункт 6 обзора): токен обязан попасть
    в защищённый файл и быть стёрт из файла заявки на диске сразу же — даже
    если весь остальной запрос потом откажет (``refused``) или применение
    отложится до ночи (``pending``) — иначе именно эти два исхода archived
    заявку с токеном открытым текстом (реальный найденный дефект, не
    гипотеза). Уже стёртые значения (плейсхолдер) не считаются — иначе
    повторное чтение отложенной заявки затёрло бы защищённый файл этим же
    плейсхолдером вместо настоящего токена.
    """
    tokens = []
    if not isinstance(request, dict):
        return tokens
    ops = request.get("ops")
    if not isinstance(ops, list):
        return tokens
    for op in ops:
        if not isinstance(op, dict) or op.get("op") != "set_git_remote":
            continue
        token = op.get("token")
        if isinstance(token, str) and token and token != _TOKEN_SCRUB_PLACEHOLDER:
            tokens.append(token)
    return tokens


def _extract_storable_git_tokens(request: Any) -> list:
    """Тот же разбор, что :func:`_extract_raw_git_tokens`, но только для
    операций, чей ``url`` уже имеет форму, которую примет
    :func:`_plan_set_git_remote` (``_HTTPS_URL_RE``).

    Обзор: раньше ЛЮБОЙ найденный токен немедленно СОХРАНЯЛСЯ в защищённый
    файл (:func:`_store_token`) — до единого вызова :func:`validate_request`.
    Это нужно ради `when: night` (см. докстринг :func:`_extract_raw_git_tokens`
    — токен обязан пережить собственный scrub заявки), но у этого была
    цена: заведомо кривая заявка (не https, встроенные логин/пароль в URL —
    то же самое, что потом откажет `_plan_set_git_remote`) всё равно
    затирала РАБОЧИЙ токен, лежавший в защищённом файле с прошлого раза,
    прежде чем запрос вообще успевал дойти до `validate_request` и
    отказать целиком. Разделяем: СТИРАТЬ из файла заявки нужно любой
    похожий на токен текст (см. вызывающий код в :func:`run_executor`) —
    иначе он останется лежать открытым текстом в архиве отказа; но
    ХРАНИТЬ стоит только то, что уже прошло эту лёгкую, но реальную
    проверку формы `url` — она не заменяет :func:`validate_request`
    целиком (`when`/профили/`_check_binding` тут ни при чём), но
    достаточна, чтобы отличить «этот токен относится к операции, которая
    В ПРИНЦИПЕ может быть валидной» от «этот текст — мусор из отравленной
    заявки».
    """
    tokens = []
    if not isinstance(request, dict):
        return tokens
    ops = request.get("ops")
    if not isinstance(ops, list):
        return tokens
    for op in ops:
        if not isinstance(op, dict) or op.get("op") != "set_git_remote":
            continue
        token = op.get("token")
        url = op.get("url")
        if (
            isinstance(token, str) and token and token != _TOKEN_SCRUB_PLACEHOLDER
            and isinstance(url, str) and _HTTPS_URL_RE.match(url)
        ):
            tokens.append(token)
    return tokens


def _scrub_tokens_in_place(request_path: Path, tokens: list) -> None:
    """Стереть переданные значения токена(ов) из файла запроса на диске,
    заменив их плейсхолдером. Принимает голые строковые значения (не
    ``PlannedOp``) — вызывается ДО валидации, когда плана ещё не
    существует, см. :func:`run_executor`."""
    tokens = [t for t in tokens if t]
    if not tokens:
        return
    try:
        text = request_path.read_text(encoding="utf-8")
    except OSError:
        return
    for t in tokens:
        text = text.replace(t, _TOKEN_SCRUB_PLACEHOLDER)
    try:
        atomic_write_text(request_path, text)
    except OSError:
        pass


_TOKEN_FIELD_RE = re.compile(r'"token"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _scrub_tokens_by_regex_in_place(request_path: Path) -> None:
    """Запрос не разобрался как валидный JSON вовсе — структурно токен не
    достать (:func:`_extract_raw_git_tokens` требует уже распарсенный
    ``dict``). Ищем ``"token": "..."`` регэкспом по сырому тексту: не
    идеальный разбор (не умеет управляющие последовательности сложнее
    ``\\"``), но лучше, чем оставить токен лежать открытым текстом в
    архиве отказа — а архивируется именно эта ветка (:func:`run_executor`,
    JSON не распарсился)."""
    try:
        text = request_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    tokens = []
    for m in _TOKEN_FIELD_RE.finditer(text):
        raw = m.group(1)
        try:
            value = json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            value = raw
        if value and value != _TOKEN_SCRUB_PLACEHOLDER:
            tokens.append(value)
    if not tokens:
        return
    for t in tokens:
        text = text.replace(t, _TOKEN_SCRUB_PLACEHOLDER)
    try:
        atomic_write_text(request_path, text)
    except OSError:
        pass


def _consume_request(request_path: Path, now: datetime) -> None:
    ts = now.strftime("%Y%m%dT%H%M%S%f")
    dest = request_path.with_name(request_path.name + f".processed-{ts}")
    try:
        os.replace(request_path, dest)
    except OSError:
        pass


def _write_result_file(path: Path, result: dict) -> None:
    lines = [f"Итог: {result['outcome']}", result["detail"], ""]
    for op in result.get("ops", []):
        mark = "✓" if op["ok"] else "✗"
        lines.append(f"  {mark} [{op['index']}] {op['op']}: {op['detail']}")
    if result.get("commit"):
        lines.append("")
        lines.append(f"Коммит: {result['commit']}")
    try:
        atomic_write_text(path, "\n".join(lines) + "\n")
    except OSError:
        pass


def run_executor(
    request_path: Path,
    *,
    root: Path = DEFAULT_ROOT,
    now: Optional[datetime] = None,
    result_path: Optional[Path] = None,
) -> dict:
    """Прочитать файл запроса, применить (если время пришло), отчитаться,
    убрать запрос с дороги, дописать аудит. Предназначен для cron-скрипта
    профиля-администратора (``no_agent``).

    Возвращает ``dict`` с ``outcome`` (``no_request`` / ``pending`` /
    ``refused`` / ``applied`` / ``partial``), ``detail`` (текст по-русски),
    ``ops`` (список результатов операций) и ``commit`` (sha или ``None``).
    """
    request_path = Path(request_path)
    now = now or datetime.now()
    if result_path is None:
        result_path = request_path.with_name(request_path.name + ".result.txt")

    if not request_path.is_file():
        return {"outcome": "no_request", "detail": "файла запроса нет — делать нечего", "ops": [], "commit": None}

    raw_bytes = request_path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()

    def _finish(outcome: str, detail: str, *, commit=None, ops_report=None, consume: bool = True) -> dict:
        result = {"outcome": outcome, "detail": detail, "ops": ops_report or [], "commit": commit}
        _write_result_file(result_path, result)
        if consume:
            _consume_request(request_path, now)
        _append_audit({
            "ts": now.isoformat(),
            "sha256": digest,
            "ops": ops_report or [],
            "outcome": outcome,
            "commit": commit,
        })
        return result

    try:
        request = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        # Пункт 6 обзора: даже когда JSON не разобрался вовсе, `"token":
        # "..."` может быть виден просто текстом внутри — стираем регэкспом
        # ДО того, как эта же ветка заархивирует файл заявки (см. ниже).
        _scrub_tokens_by_regex_in_place(request_path)
        return _finish("refused", f"запрос не разобрался как JSON: {e}")

    # Пункт 6 обзора: токен стирается из файла заявки СРАЗУ здесь — ДО
    # любой ветки (когда/валидация/применение), а не только на пути
    # успеха. Раньше `refused` и `pending` оставляли токен открытым текстом
    # на диске: `refused` архивировал заявку как есть, а `pending`
    # оставлял её лежать на месте до ночного окна. Значение уже сохранено
    # в защищённый файл к этому моменту — если применение всё же случится
    # (сейчас или после ночного окна), :func:`_plan_set_git_remote`
    # прочитает настоящий токен ОТТУДА, а не из (к тому времени уже
    # стёртого) файла заявки.
    #
    # Blocker 2 обзора спеки 21: СТИРАТЬ из файла заявки нужно любой похожий
    # на токен текст, независимо от того, разбирается ли его `url` —
    # иначе он останется лежать открытым текстом в архиве отказа. Но
    # ХРАНИТЬ (перезаписывать защищённый файл, из которого потом читает
    # push) стоит только токены с уже правильной формой url
    # (:func:`_extract_storable_git_tokens`) — иначе заведомо кривая заявка
    # (не https, логин/пароль в URL) молча затирает РАБОЧИЙ токен от
    # прошлого раза ещё ДО того, как `validate_request` успевает отказать
    # ей целиком.
    raw_tokens_for_scrub = _extract_raw_git_tokens(request)
    raw_tokens_for_store = _extract_storable_git_tokens(request)
    if raw_tokens_for_store:
        for t in raw_tokens_for_store:
            _store_token(t)
    if raw_tokens_for_scrub:
        _scrub_tokens_in_place(request_path, raw_tokens_for_scrub)

    when = request.get("when") if isinstance(request, dict) else None
    if when == "night" and not _in_night_window(now):
        # НЕ consume: запрос остаётся ждать своего ночного окна. Аудит и
        # результат всё равно пишутся — саппорт должен видеть, что запрос
        # получен и ожидает, а не потерян.
        return _finish("pending", "ночное окно ещё не наступило — запрос остаётся ждать", consume=False)

    try:
        planned, when = validate_request(request, root=root)
    except ContourError as e:
        return _finish("refused", f"запрос отклонён целиком: {e}")

    try:
        result = apply_planned(planned, root)
    except ContourError as e:
        return _finish("refused", f"применение отказано: {e}")

    ops_report = [{"index": r.index, "op": r.kind, "ok": r.ok, "detail": r.detail} for r in result.ops]
    outcome = "applied" if all(r.ok for r in result.ops) else "partial"
    detail = "запрос применён" if outcome == "applied" else "запрос применён частично — см. ops"
    return _finish(outcome, detail, commit=result.commit, ops_report=ops_report)


# ---------------------------------------------------------------------------
# Только чтение: status() и check()
# ---------------------------------------------------------------------------


def status(root: Path = DEFAULT_ROOT) -> dict:
    """Кто что видит — из конфигов профилей. Без побочных эффектов.

    Читает МНОГО профильных конфигов по явному пути (не свой собственный
    процесс) — ровно случай «multi-profile probes», для которого
    ``read_user_config_raw()`` и принимает необязательный ``config_path``.
    """
    from hermes_cli.config import read_user_config_raw
    from hermes_cli.profiles import list_profiles

    root = Path(root)
    real_root = os.path.realpath(root)
    out: dict = {}
    for info in list_profiles():
        cfg_path = info.path / "config.yaml"
        if not cfg_path.is_file():
            continue
        try:
            data = read_user_config_raw(cfg_path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        volumes = (data.get("terminal") or {}).get("docker_volumes") or []
        entries = []
        for v in volumes:
            if not isinstance(v, str):
                continue
            try:
                host, at, mode = v.rsplit(":", 2)
            except ValueError:
                continue
            real_host = os.path.realpath(host)
            if real_host == real_root or real_host.startswith(real_root + os.sep):
                rel = os.path.relpath(real_host, real_root)
                entries.append({"folder": rel, "container_path": at, "mode": mode})
        if entries:
            out[info.name] = entries
    return out


# ---------------------------------------------------------------------------
# Миграция со старой (широкой) грамматики — человеческий выход из тупика.
# ---------------------------------------------------------------------------


def revoke_literal_mount(profile: str, container_path: str) -> dict:
    """Человеческий эскейп-хэтч: снять ЛЮБОЙ маунт профиля по точке
    монтирования внутри контейнера, независимо от того, узнаёт ли его
    закрытая грамматика :func:`_validate_grantable_folder`.

    Обзор нашёл тупик миграции: доступ, выданный ПРЕЖНЕЙ, более широкой
    грамматикой прототипа (``scripts/contour.py`` до этого модуля) —
    произвольная вложенная папка вроде ``dept/sales/raw`` или бывший бакет
    ``templates`` — записан в ``terminal.docker_volumes`` профиля как обычная
    строка ``host:at:mode``. Закрытая грамматика этого модуля принимает
    РОВНО две формы папки (``company``, ``dept/<X>``, см. докстринг
    :func:`_validate_grantable_folder`), поэтому обычный агентский `revoke`
    (:func:`_plan_revoke`) отказывает на форме `folder`, даже не начиная
    искать соответствующий маунт — то есть уже выданный по старой грамматике
    доступ теперь НЕЛЬЗЯ забрать штатным путём вообще, только руками.

    Это НЕ часть закрытой грамматики агента (не вызывается из
    :func:`validate_request`, не достижимо из JSON заявки) — команда
    принимает произвольную строку `container_path` без единой из проверок
    биндинга папки к профилю, которые делают `grant`/`revoke` безопасными
    против TOCTOU на путях монтирования (см. докстринг
    :func:`_validate_grantable_folder`). Доверяем этому только человеку за
    терминалом (`hermes contour revoke-mount`), который сам знает, какую
    именно точку монтирования сейчас безопасно снять — не агенту.
    """
    profile = _check_profile_exists(profile)
    if not isinstance(container_path, str) or not container_path.strip():
        raise ContourError("`container_path` должен быть непустой строкой")
    cfg_path = _profile_config_path(profile)
    text = _read_raw_text(cfg_path)
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ContourError(f"{cfg_path}: верхний уровень конфига — не словарь")
    _check_config_not_tampered(profile, data)

    volumes = list((data.get("terminal") or {}).get("docker_volumes") or [])
    remaining = [v for v in volumes if _volume_target(v) != container_path]
    if len(remaining) == len(volumes):
        return {"profile": profile, "container_path": container_path, "changed": False}

    new_text = rewrite_config(text, {("terminal", "docker_volumes"): remaining})
    changed = new_text != text
    if changed:
        backup_path = cfg_path.with_name(cfg_path.name + f".bak-{_timestamp()}")
        atomic_write_text(backup_path, text, newline="")
        atomic_write_text(cfg_path, new_text, newline="", preserve_mode=True)
    return {"profile": profile, "container_path": container_path, "changed": changed}


def _docker_mounts(profile: str) -> Optional[list]:
    if not shutil.which("docker"):
        return None
    ps = subprocess.run(
        [
            "docker", "ps", "-aq",
            "--filter", "label=hermes-agent=1",
            "--filter", f"label=hermes-profile={profile}",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    ids = [x for x in (ps.stdout or "").split() if x]
    if ps.returncode != 0 or not ids:
        return None
    ins = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Mounts}}", ids[0]],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if ins.returncode != 0:
        return None
    try:
        return json.loads(ins.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return None


def check(root: Path = DEFAULT_ROOT) -> dict:
    """Сверить config.yaml с маунтами живых контейнеров. Только чтение —
    ``docker ps``/``docker inspect``, ни одной мутирующей команды."""
    root = Path(root)
    real_root = os.path.realpath(root)
    st = status(root)
    report: dict = {}
    for profile, entries in st.items():
        mounts = _docker_mounts(profile)
        if mounts is None:
            report[profile] = {"live": None, "entries": entries}
            continue
        live = {(m.get("Source"), m.get("Destination"), "rw" if m.get("RW") else "ro") for m in mounts}
        checked = []
        for e in entries:
            host = str(Path(real_root) / e["folder"])
            ok = (host, e["container_path"], e["mode"]) in live
            checked.append({**e, "live_ok": ok})
        report[profile] = {"live": True, "entries": checked}
    return report
