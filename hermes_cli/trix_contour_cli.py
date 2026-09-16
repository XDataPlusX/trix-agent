"""CLI-подкоманда ``hermes contour <verb>`` — тонкая обёртка над
``hermes_cli/trix_contour.py``.

Ничего не решает сама: каждая проверка (root, символические ссылки, привязка
dept/company к профилю, подмена конфига) уже сделана в библиотеке. Этот
модуль только разбирает argparse (как ``hermes_cli/curator.py``) и печатает
результат по-русски — рычаг rung 2 «footprint ladder» из CLAUDE.md: CLI +
навык, а не новый модельный инструмент.

Регистрируется лениво из ``hermes_cli/main.py`` — как ``curator``: импорт
``trix_contour`` (а через него — ``hermes_cli.profiles``, ``yaml`` и т.д.)
происходит только внутри обработчиков подкоманд, а не при построении
argparse-дерева.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _root_arg(args) -> Path:
    from hermes_cli.trix_contour import DEFAULT_ROOT

    return Path(args.root) if getattr(args, "root", None) else DEFAULT_ROOT


def _cmd_status(args) -> int:
    from hermes_cli import trix_contour as tc

    root = _root_arg(args)
    data = tc.status(root)
    if not data:
        print(f"Контур {root}: ни один профиль ничего не смонтировал.")
    else:
        for profile in sorted(data):
            print(f"Профиль {profile}:")
            for e in data[profile]:
                print(f"  {e['folder']} -> {e['container_path']} ({e['mode']})")
    pending_remote = tc.read_pending_remote()
    if pending_remote:
        print(
            f"Remote {pending_remote['url']} заявлен и ждёт активации — "
            "`hermes contour approve-remote`."
        )
    return 0


def _cmd_check(args) -> int:
    from hermes_cli import trix_contour as tc

    root = _root_arg(args)
    report = tc.check(root)
    if not report:
        print(f"Контур {root}: сверять нечего — ни один профиль ничего не смонтировал.")
        return 0
    bad = 0
    for profile in sorted(report):
        info = report[profile]
        print(f"Профиль {profile}:")
        if info["live"] is None:
            print("  · контейнера нет или docker недоступен — маунты проверю после первой команды агента")
            continue
        for e in info["entries"]:
            ok = e["live_ok"]
            bad += not ok
            mark = "✓" if ok else "✗"
            print(f"  {mark} {e['folder']} -> {e['container_path']} ({e['mode']})" + ("" if ok else " — пересоздайте песочницу"))
    print("\n" + ("Всё сходится." if not bad else f"Расхождений: {bad}."))
    return 0 if not bad else 1


def _cmd_apply_request(args) -> int:
    from hermes_cli import trix_contour as tc

    root = _root_arg(args)
    result = tc.run_executor(Path(args.request), root=root)
    print(f"Итог: {result['outcome']}")
    print(result["detail"])
    for op in result.get("ops", []):
        mark = "✓" if op["ok"] else "✗"
        print(f"  {mark} [{op['index']}] {op['op']}: {op['detail']}")
    if result.get("commit"):
        print(f"Коммит: {result['commit']}")
    return 0 if result["outcome"] in ("applied", "pending", "no_request") else 1


def _cmd_push(args) -> int:
    from hermes_cli import trix_contour as tc

    root = _root_arg(args)
    try:
        print(tc.push(root))
        return 0
    except tc.ContourError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


def _cmd_revoke_mount(args) -> int:
    """Человеческий эскейп-хэтч для миграции: снять любой маунт по точке
    монтирования, включая доступ, выданный прежней (уже нерабочей) широкой
    грамматикой — обычный `revoke` в JSON-заявке агента отказывает на такой
    форме папки и не может его увидеть вовсе (см. `revoke_literal_mount`)."""
    from hermes_cli import trix_contour as tc

    try:
        result = tc.revoke_literal_mount(args.profile, args.container_path)
    except tc.ContourError as e:
        print(f"Отказ: {e}", file=sys.stderr)
        return 1
    if result["changed"]:
        print(
            f"Маунт {result['container_path']} у профиля «{result['profile']}» снят. "
            "Пересоздайте песочницу профиля, чтобы это вступило в силу."
        )
    else:
        print(f"У профиля «{result['profile']}» и так не было маунта на {result['container_path']}.")
    return 0


def _cmd_approve_skill(args) -> int:
    """Человек одобряет навык, ожидающий публикации в общей папке (пункт 3
    обзора спеки 21: `publish_skill` больше не публикует сам — только
    складывает кандидат в `.pending/`; здесь человек, за терминалом, а не
    агент, переносит его в живую папку).

    Blocker 3 обзора: показывает ПОЛНЫЙ текст SKILL.md, считанный только
    что (не из старого файла результата заявки), требует явного
    подтверждения (`--yes` — для неинтерактивного вызова), и передаёт
    только что посчитанный дайджест в `approve_skill()` — если между этим
    чтением и фактической промоцией кандидат в `.pending/` подменят
    вторым `publish_skill`, `approve_skill()` откажет вместо того, чтобы
    молча промотировать подменённое содержимое. `--digest` — необязательная
    сверка для сценария, где человек уже видел текст раньше (из файла
    результата заявки) и хочет подтвердить, что кандидат с тех пор не
    менялся, ПРЕЖДЕ чем читать его заново.
    """
    from hermes_cli import trix_contour as tc

    root = _root_arg(args)
    try:
        preview = tc.pending_skill_preview(root, args.skill)
    except tc.ContourError as e:
        print(f"Отказ: {e}", file=sys.stderr)
        return 1

    print(f"Навык «{args.skill}», ожидающий одобрения ({preview['path']}):")
    print("-" * 40)
    print(preview["text"])
    print("-" * 40)
    print(f"Дайджест содержимого: {preview['digest']}")

    if args.digest and args.digest.strip() != preview["digest"]:
        print(
            "Отказ: указанный --digest не совпадает с текущим содержимым — кандидат "
            "изменился с момента, когда его показали. Посмотрите текст заново (выше) "
            "и одобряйте по нему.",
            file=sys.stderr,
        )
        return 1

    if not args.yes:
        try:
            answer = input(f"Опубликовать этот текст как общий навык «{args.skill}»? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes", "да"):
            print("Отменено — навык не опубликован.")
            return 1

    try:
        result = tc.approve_skill(root, args.skill, expected_digest=preview["digest"])
    except tc.ContourError as e:
        print(f"Отказ: {e}", file=sys.stderr)
        return 1
    verb = "обновлён" if result["is_update"] else "опубликован"
    print(f"Навык «{result['skill']}» {verb} и стал общим для компании.")
    if result.get("commit"):
        print(f"Коммит: {result['commit']}")
    return 0


def _cmd_approve_remote(args) -> int:
    """Человек активирует remote, заявленный агентом через `set_git_remote`
    (blocker 2 обзора спеки 21 §9) — тот же барьер, что уже стоит на
    `publish_skill`: заявка только запоминает URL, реальный `git remote
    add/set-url` происходит только отсюда."""
    from hermes_cli import trix_contour as tc

    root = _root_arg(args)
    try:
        result = tc.approve_remote(root)
    except tc.ContourError as e:
        print(f"Отказ: {e}", file=sys.stderr)
        return 1
    print(f"Remote {result['url']} активирован.")
    return 0


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Собрать ``hermes contour <verb>`` — вызывается из ``main.py`` при
    построении общего парсера, тем же способом, что и ``hermes curator``."""
    parent.set_defaults(func=lambda a: (parent.print_help(), 0)[1])
    parent.add_argument(
        "--root",
        help="Переопределить корень контура (только для теста/отладки — в бою root не меняется)",
    )

    subs = parent.add_subparsers(dest="contour_command")

    p_status = subs.add_parser("status", help="Кто что видит — из конфигов профилей")
    p_status.set_defaults(func=_cmd_status)

    p_check = subs.add_parser("check", help="Сверить конфиги профилей с маунтами живых контейнеров")
    p_check.set_defaults(func=_cmd_check)

    p_apply = subs.add_parser(
        "apply-request",
        help="Применить файл запроса (закрытая грамматика — см. references/model.md навыка trix-file-contour)",
    )
    p_apply.add_argument("request", help="Путь к JSON-файлу запроса")
    p_apply.set_defaults(func=_cmd_apply_request)

    p_push = subs.add_parser("push", help="Отправить историю контура в настроенный remote")
    p_push.set_defaults(func=_cmd_push)

    p_approve = subs.add_parser(
        "approve-skill",
        help="Человек одобряет навык, ожидающий публикации (.pending/) — переносит его в живую общую папку",
    )
    p_approve.add_argument("skill", help="Имя навыка (директория внутри skills/.pending/)")
    p_approve.add_argument(
        "--digest",
        help="Сверить с дайджестом, который видели раньше (из файла результата заявки), "
        "прежде чем показывать текст заново",
    )
    p_approve.add_argument(
        "--yes", action="store_true",
        help="Не спрашивать подтверждение интерактивно (для неинтерактивного вызова)",
    )
    p_approve.set_defaults(func=_cmd_approve_skill)

    p_approve_remote = subs.add_parser(
        "approve-remote",
        help="Человек активирует remote, заявленный агентом через set_git_remote",
    )
    p_approve_remote.set_defaults(func=_cmd_approve_remote)

    p_revoke_mount = subs.add_parser(
        "revoke-mount",
        help="Снять ЛЮБОЙ маунт по точке монтирования — путь миграции для грантов старой "
        "(широкой) грамматики, которые обычный revoke больше не распознаёт",
    )
    p_revoke_mount.add_argument("profile", help="Имя профиля")
    p_revoke_mount.add_argument("container_path", help="Точка монтирования внутри контейнера, например /dept")
    p_revoke_mount.set_defaults(func=_cmd_revoke_mount)
