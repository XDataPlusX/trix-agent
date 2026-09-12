"""Перенос правок КОММЕНТАРИЕВ клиентского config.yaml на работающие машины.

``trix_config_sync`` переносит только отсутствующие КЛЮЧИ и по инварианту не
меняет ни одной существующей строки — поэтому исправленный текст пояснений до
клиента доехать не может. При переходе на полный конфиг (сентябрь 2026) это
стало не косметикой, а ложью в файле: шапка утверждала «здесь только то, что
отличается от значений по умолчанию» над файлом, в который досев дописал все
85 секций.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def test_the_table_never_touches_a_value(tmp_path):
    """В таблице только комментарии: ни одной строки с данными.

    Это главная страховка модуля. Замена, задевшая строку со значением,
    молча поменяла бы клиенту настройку — ровно то, чего досев не делает
    никогда и чего не должен делать и этот путь.
    """
    from hermes_cli.trix_config_comments import COMMENT_REWRITES

    for label, old, new in COMMENT_REWRITES:
        for side, text in (("старый", old), ("новый", new)):
            for line in text.splitlines():
                if not line.strip():
                    continue
                assert line.lstrip().startswith("#"), (
                    f"{label}: {side} текст содержит строку с данными: {line!r}"
                )


def test_every_rewrite_has_a_label_and_changes_something():
    from hermes_cli.trix_config_comments import COMMENT_REWRITES

    seen = set()
    for label, old, new in COMMENT_REWRITES:
        assert label and label not in seen, f"метка пустая или повторяется: {label!r}"
        seen.add(label)
        assert old, f"{label}: пустой старый текст — заменять нечего"
        assert old != new, f"{label}: замена ничего не меняет"


def test_applied_to_the_released_template_it_yields_the_current_one(tmp_path, previous_release_config_template):
    """Главный сквозной инвариант: старый шаблон + правки = текущие пояснения.

    Именно этого требует «у всех одинаково»: клиент, обновившийся с прошлого
    релиза, обязан читать те же пояснения, что видит свежая установка.
    Сравниваются не файлы целиком (у клиента вдобавок досеяны секции), а
    наличие нового текста и отсутствие старого.
    """
    from hermes_cli.trix_config_comments import (
        COMMENT_REWRITES,
        sync_client_comments,
    )

    path = tmp_path / "config.yaml"
    path.write_text(previous_release_config_template, encoding="utf-8")

    applied, unmatched = sync_client_comments(path)

    assert applied, (
        "не применилось ни одной правки — похоже, отправная точка уже "
        "обновлена, и тест ничего не проверяет"
    )
    assert len(applied) == len(COMMENT_REWRITES), (
        f"применилось {len(applied)} правок из {len(COMMENT_REWRITES)}: "
        f"{applied} — остальные не нашли свой старый текст в релизном шаблоне"
    )
    text = path.read_text(encoding="utf-8")
    for label, old, new in COMMENT_REWRITES:
        assert old not in text, f"{label}: старый текст остался"
        if new:
            assert new in text, f"{label}: нового текста нет"


def test_a_second_run_changes_nothing(tmp_path, previous_release_config_template):
    """Идемпотентность: после первой миграции старого текста в файле нет."""
    from hermes_cli.trix_config_comments import sync_client_comments

    path = tmp_path / "config.yaml"
    path.write_text(previous_release_config_template, encoding="utf-8")

    assert sync_client_comments(path)[0]
    after_first = path.read_text(encoding="utf-8")
    assert sync_client_comments(path)[0] == []
    assert path.read_text(encoding="utf-8") == after_first


def test_a_comment_the_client_edited_is_left_alone(tmp_path, previous_release_config_template):
    """Клиент правил пояснение сам — не трогаем и не угадываем."""
    from hermes_cli.trix_config_comments import sync_client_comments

    path = tmp_path / "config.yaml"
    edited = previous_release_config_template.replace(
        "# Trix Agent — конфигурация.", "# Мой конфиг, руки прочь."
    )
    path.write_text(edited, encoding="utf-8")

    applied, unmatched = sync_client_comments(path)

    text = path.read_text(encoding="utf-8")
    assert "# Мой конфиг, руки прочь." in text
    assert "шапка файла" not in applied, "правленую шапку переписали поверх клиента"


def test_the_file_still_parses_and_keeps_every_value(tmp_path, previous_release_config_template):
    """Значения после правки комментариев совпадают дословно."""
    from hermes_cli.trix_config_comments import sync_client_comments

    path = tmp_path / "config.yaml"
    original = previous_release_config_template
    path.write_text(original, encoding="utf-8")

    sync_client_comments(path)

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == yaml.safe_load(original)


def test_crlf_file_gets_no_stray_newline(tmp_path, previous_release_config_template):
    """Файл, правленный блокнотом, не получает ни одного голого \\n."""
    from hermes_cli.trix_config_comments import sync_client_comments

    path = tmp_path / "config.yaml"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(previous_release_config_template.replace("\n", "\r\n"))

    assert sync_client_comments(path)[0]

    data = path.read_bytes()
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b""), (
        "появился голый \\n — файл заговорил на двух языках переводов строк"
    )


def test_a_missing_or_unreadable_file_is_a_no_op(tmp_path):
    """Падать нельзя: модуль вызывается из обновления."""
    from hermes_cli.trix_config_comments import sync_client_comments

    assert sync_client_comments(tmp_path / "nope.yaml") == ([], [])
    broken = tmp_path / "broken.yaml"
    broken.write_bytes(b"\xff\xfe\x00not utf-8")
    assert sync_client_comments(broken) == ([], [])


def test_the_shipped_template_carries_no_old_text():
    """В текущем шаблоне старых формулировок уже нет.

    Иначе миграция «поправила» бы свежую установку, а оператор увидел бы в
    выводе обновления правки на машине, где их быть не должно.
    """
    from hermes_cli.trix_config_comments import COMMENT_REWRITES

    text = TEMPLATE.read_text(encoding="utf-8")
    for label, old, _new in COMMENT_REWRITES:
        assert old not in text, (
            f"{label}: старая формулировка всё ещё в поставляемом шаблоне"
        )


def test_an_edited_header_is_reported_as_unmatched(tmp_path, previous_release_config_template):
    """Клиент правил пояснение сам — это состояние наблюдаемо.

    Само оно не чинится: дословной замены нет, а угадывать нельзя. Но
    оператор обязан иметь возможность узнать, что у машины осталась старая
    шапка над новым содержимым — иначе обновление молчит, а проверка
    паритета этого не видит.
    """
    from hermes_cli.trix_config_comments import sync_client_comments

    path = tmp_path / "config.yaml"
    path.write_text(
        previous_release_config_template.replace(
            "# Trix Agent — конфигурация.", "# Мой конфиг, руки прочь."
        ),
        encoding="utf-8",
    )

    applied, unmatched = sync_client_comments(path)

    assert "шапка файла" not in applied
    assert "шапка файла" in unmatched, (
        "правленая шапка не попала в отчёт — состояние осталось незаметным"
    )


def test_a_read_only_config_is_left_untouched(tmp_path, previous_release_config_template):
    """Файл, снятый клиентом в «только чтение», не переписываем.

    То же правило, что и у досева секций: уважаем защиту, поставленную
    руками. ``atomic_write_text`` пишет через каталог и сам по себе права
    файла не проверяет, поэтому проверка нужна явная.
    """
    import os
    import stat

    from hermes_cli.trix_config_comments import sync_client_comments

    path = tmp_path / "config.yaml"
    path.write_text(previous_release_config_template, encoding="utf-8")
    before = path.read_bytes()
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    if os.access(path, os.W_OK):  # под root os.access отвечает утвердительно
        pytest.skip("запущено с правами, для которых 444 не запрет")

    applied, _unmatched = sync_client_comments(path)

    assert applied == []
    assert path.read_bytes() == before
