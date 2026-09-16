#!/usr/bin/env python3
"""Досевает в клиентский шаблон config.yaml всё, чего в нём нет.

**Зачем.** Решение владельца (сентябрь 2026): клиент должен видеть ВСЕ
настройки в одном файле, а не только те, где мы отошли от стандартного
значения. Раньше ``assets/config/trix-config.yaml`` содержал 16 корневых
секций из 85 — остальное работало на встроенных значениях, но в файле их
не было видно, и чтобы что-то поменять, приходилось знать имя ключа
заранее.

**Ничего своего этот скрипт не придумывает.** Он склеивает два уже
существующих механизма:

1. ``save_config(DEFAULT_CONFIG, strip_defaults=False)`` — апстримный
   генератор полного конфига. За ним стоит ``hermes config edit``: когда
   ``config.yaml`` у пользователя нет, апстрим создаёт его именно так —
   все 85 секций, только значения, без комментариев.
2. ``sync_missing_client_sections`` — наш досев: дописывает отсутствующее,
   НЕ меняя ни одной существующей строки. Тот же код, который на машине
   клиента приводит старый конфиг к новому шаблону. Собирая шаблон им же,
   мы прогоняем по нему ровно тот путь, по которому поедут клиенты.

Поэтому правка шаблона руками остаётся законной: наши значения и русские
комментарии — источник истины, скрипт только добивает недостающее.

Разделитель между кураторской частью и остальным — обычный комментарий
внутри шаблона, не работа скрипта. Новые секции всегда дописываются в конец
документа, а метка версии переносится в самый низ, поэтому всё досеянное
оказывается ниже разделителя само, без отдельной логики размещения.

Запуск::

    python3 scripts/build_trix_config.py            # досеять и записать
    python3 scripts/build_trix_config.py --check    # только проверить

``--check`` ничего не пишет и возвращает код 1, если в шаблоне чего-то не
хватает. На него опирается тест полноты: после слияния с апстримом, который
добавил новый ключ, тест краснеет, и шаблон доводится этим же скриптом.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"

_VERSION_KEY = "_config_version"


def _full_default_dump(workdir: Path) -> Path:
    """Полный конфиг апстримным генератором. Возвращает путь к файлу."""
    os.environ["HERMES_HOME"] = str(workdir)
    # Импорт ПОСЛЕ установки HERMES_HOME: модульные константы кэшируют
    # get_hermes_home() на импорте (см. CLAUDE.md, раздел про профили).
    from hermes_cli.config import DEFAULT_CONFIG, save_config

    save_config(DEFAULT_CONFIG, strip_defaults=False)
    produced = workdir / "config.yaml"
    dump = workdir / "full-default-dump.yaml"
    shutil.move(str(produced), str(dump))
    return dump


def _all_paths(data, prefix: tuple = ()) -> list:
    """Все пути (кортежи ключей) в разобранном отображении."""
    out = []
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        path = prefix + (key,)
        out.append(path)
        out.extend(_all_paths(value, path))
    return out


def _missing_against_defaults(template_path: Path) -> list:
    """Пути из DEFAULT_CONFIG, которых в шаблоне нет. Без записи."""
    import yaml

    from hermes_cli.config_defaults import DEFAULT_CONFIG

    data = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    have = set(_all_paths(data))
    return [path for path in _all_paths(DEFAULT_CONFIG) if path not in have]


def _block_bounds(lines: list, key: str) -> tuple:
    """``(start, end)`` корневого блока ``key`` вместе с его комментариями."""
    from hermes_cli.trix_config_sync import _block_extent, _find_key_line

    idx = _find_key_line(lines, key, 0, 0, len(lines))
    if idx is None:
        return None
    return _block_extent(lines, idx, 0)


def _move_version_to_the_end(lines: list) -> list:
    """Перенести блок ``_config_version`` в самый конец файла.

    Досев дописывает новые корневые секции в конец ДОКУМЕНТА, то есть после
    служебной метки версии. Метке место последней — иначе при каждом
    прогоне она уезжает в середину файла.
    """
    bounds = _block_bounds(lines, _VERSION_KEY)
    if bounds is None:
        return lines
    start, end = bounds
    block = lines[start:end]
    rest = lines[:start] + lines[end:]
    while rest and rest[-1].strip() == "":
        rest.pop()
    return rest + [""] + block


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="только проверить полноту шаблона, ничего не записывать",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT))

    if args.check:
        missing = _missing_against_defaults(TEMPLATE)
        if not missing:
            print("шаблон полон: все пути DEFAULT_CONFIG на месте")
            return 0
        print(f"в шаблоне не хватает {len(missing)} путей:", file=sys.stderr)
        for path in missing[:40]:
            print("   " + ".".join(str(p) for p in path), file=sys.stderr)
        if len(missing) > 40:
            print(f"   … и ещё {len(missing) - 40}", file=sys.stderr)
        print(
            "\nдовести: python3 scripts/build_trix_config.py",
            file=sys.stderr,
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="trix-config-build-") as tmp:
        workdir = Path(tmp)
        dump = _full_default_dump(workdir)

        from hermes_cli.trix_config_sync import sync_missing_client_sections

        added, skipped = sync_missing_client_sections(TEMPLATE, dump)

        lines = _move_version_to_the_end(
            TEMPLATE.read_text(encoding="utf-8").splitlines()
        )
        TEMPLATE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"досеяно путей: {len(added)}")
    if skipped:
        print(f"ПРОПУЩЕНО: {len(skipped)}", file=sys.stderr)
        for path, reason in skipped:
            print(f"   {path} — {reason}", file=sys.stderr)

    missing = _missing_against_defaults(TEMPLATE)
    if missing:
        print(
            f"шаблон всё ещё неполон: {len(missing)} путей "
            f"(первый: {'.'.join(str(p) for p in missing[0])})",
            file=sys.stderr,
        )
        return 1
    total = len(TEMPLATE.read_text(encoding="utf-8").splitlines())
    print(f"шаблон полон, {total} строк")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
