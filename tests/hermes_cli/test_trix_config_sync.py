"""Досев недостающих секций в уже существующий клиентский config.yaml.

Требование: дописываем только отсутствующее, не трогая ни одной
существующей строки (байт в байт — включая перевод строки), вместе с
русскими комментариями, и не воскрешая то, что клиент удалил сам.
"""

import difflib
import re
import stat
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _old_lines_are_untouched(before: str, after: str) -> bool:
    """True, если новый файл отличается от старого ТОЛЬКО добавлениями."""
    diff = difflib.ndiff(before.splitlines(), after.splitlines())
    return all(not line.startswith(("-", "?")) for line in diff)


def _skipped_paths(skipped) -> list:
    """``skipped`` is a list of ``(path, reason)`` — just the paths."""
    return [p for p, _ in skipped]


def _skipped_reasons(skipped) -> list:
    """``skipped`` is a list of ``(path, reason)`` — just the reasons."""
    return [r for _, r in skipped]


def _skipped_reason(skipped, path: str):
    for p, reason in skipped:
        if p == path:
            return reason
    return None


@pytest.fixture
def client_config(tmp_path):
    """Конфиг «старого» клиента: наш шаблон без секций, добавленных спекой 9."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "# Trix Agent — конфигурация.\n"
        "terminal:\n"
        "  # Команды агента выполняются в контейнере.\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    return path


def test_missing_root_section_is_added(client_config):
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    added, skipped = sync_missing_client_sections(client_config, TEMPLATE)
    assert "display" in added
    assert skipped == []

    text = client_config.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["display"]["cleanup_progress"] is True


def test_missing_leaf_inside_an_existing_section_is_added(client_config):
    """terminal: у клиента уже есть — а docker_extra_args внутри него нет.
    Врезка обязана попасть ВНУТРЬ существующего блока."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    sync_missing_client_sections(client_config, TEMPLATE)
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert data["terminal"]["docker_extra_args"] == ["-p", "18000-18009:18000-18009"]
    assert data["terminal"]["backend"] == "docker"


def test_russian_comments_travel_with_the_block(client_config):
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    sync_missing_client_sections(client_config, TEMPLATE)
    text = client_config.read_text(encoding="utf-8")
    assert "Что клиент видит в Telegram, пока агент работает." in text


def test_not_a_single_existing_line_changes(client_config):
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    before = client_config.read_text(encoding="utf-8")
    sync_missing_client_sections(client_config, TEMPLATE)
    after = client_config.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after), (
        "врезка изменила или удалила существующие строки — "
        "это худшее, что можно сделать с клиентским конфигом"
    )


def test_client_values_always_win(client_config):
    """Клиент поменял значение — досев обязан его не трогать."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    text = client_config.read_text(encoding="utf-8").replace(
        "search_backend: ddgs", "search_backend: searxng"
    )
    client_config.write_text(text, encoding="utf-8")

    sync_missing_client_sections(client_config, TEMPLATE)
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert data["web"]["search_backend"] == "searxng"


def test_second_run_changes_nothing(client_config):
    """Идемпотентность: досев на уже досеянном файле — пустой список и
    побайтово тот же файл."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    sync_missing_client_sections(client_config, TEMPLATE)
    once = client_config.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(client_config, TEMPLATE)
    assert added == []
    assert skipped == []
    assert client_config.read_text(encoding="utf-8") == once


def test_sync_never_touches_the_client_config_version(client_config):
    """Инвариант: досев не трогает версию клиента — какой бы она ни была.

    Не сравниваем с DEFAULT_CONFIG — это был бы change-detector: тест
    покраснел бы при первом же бампе версии, хотя досев к версии вообще
    не прикасается. Сравниваем с тем, что уже лежало в фикстуре.
    """
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    original_version = yaml.safe_load(client_config.read_text(encoding="utf-8"))["_config_version"]

    sync_missing_client_sections(client_config, TEMPLATE)
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert data["_config_version"] == original_version


def test_unreadable_template_is_a_no_op(client_config, tmp_path):
    """Отсутствующий шаблон не имеет права испортить клиентский файл."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    before = client_config.read_text(encoding="utf-8")
    added, skipped = sync_missing_client_sections(client_config, tmp_path / "нет-такого.yaml")
    assert added == []
    assert skipped == []
    assert client_config.read_text(encoding="utf-8") == before


def test_deeper_than_two_levels_is_skipped_not_inserted(tmp_path):
    """gateway.media_retention_hours уже есть у клиента (пустой) — а
    gateway.media_retention_hours.documents внутри него нет. Это третий
    уровень: не вставляется наугад, а возвращается как пропущенный."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "gateway:\n"
        "  media_retention_hours: {}\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )

    from hermes_cli.trix_config_sync import _SKIP_TOO_DEEP

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    assert "gateway.media_retention_hours.documents" in _skipped_paths(skipped)
    assert _skipped_reason(skipped, "gateway.media_retention_hours.documents") == _SKIP_TOO_DEEP
    assert "gateway.media_retention_hours.documents" not in added

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert not (data["gateway"].get("media_retention_hours") or {}).get("documents")


def test_totally_empty_section_is_filled_at_the_template_indent(tmp_path):
    """display: присутствует у клиента, но БЕЗ ЕДИНОЙ содержательной строки.

    Round 1 filled this at the template's indent; round 3 skipped it
    because "there is no client indent to derive from". Round 4 restores
    the filling on a different, correct footing: the rule is *never
    contradict a shape the client already has*, and a block with no
    content lines has no shape at all — there is nothing to contradict, so
    the template's own indent is safe by construction. (What round 3 was
    really protecting against was a block whose children ARE there and are
    indented differently — that case is still respected, see
    test_client_indent_wider_than_template_is_respected.)

    Coverage is not lost by the flip: what these empty-block tests actually
    guard is "the file still parses and no existing line moved", and both
    assertions stay — only the expected `added`/`skipped` side changes.
    """
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "display:\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    assert "display.cleanup_progress" in added
    assert not any(p.startswith("display.") for p in _skipped_paths(skipped))

    after = path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after), "существующие строки не тронуты"
    data = yaml.safe_load(after)  # must not raise
    assert data["display"]["cleanup_progress"] is True
    # Отступ взят из шаблона (2), потому что своего у клиента не было.
    assert re.search(r"(?m)^  cleanup_progress: true$", after)


def test_section_with_only_a_comment_can_still_be_filled(tmp_path):
    """Поведение то же, что и в круге 3 (блок заполняется), но ОСНОВАНИЕ
    другое, и это важно.

    Round 3 filled this because the comment's own indent was accepted as
    the block's child indent. That reasoning is what broke the file in
    round 4's finding: a comment's indent means nothing to the YAML parser,
    so it can never be the authority on form. The correct reason is that a
    comment carries no shape at all — so this block is in exactly the same
    position as a totally empty one, and gets the template's indent.
    """
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "display:\n"
        "  # заметка клиента, но ни одного ключа\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )

    added, _ = sync_missing_client_sections(path, TEMPLATE)
    assert "display.cleanup_progress" in added

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["display"]["cleanup_progress"] is True


def test_comment_only_section_ignores_the_comments_own_indent(tmp_path):
    """The same comment-only block, but with the comment indented 6 spaces.

    Round 3 would have derived "6" from it and written the new keys there.
    That happens to parse here, which is exactly why it survived three
    rounds — but it is the same wrong rule that DOES corrupt the file as
    soon as a real child sits next to the comment. The comment's indent
    must be ignored outright: the block has no shape, so the template's
    indent (2) is used.
    """
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "display:\n"
        "      # заметка клиента, отбитая как попало\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, _ = sync_missing_client_sections(path, TEMPLATE)
    assert "display.cleanup_progress" in added

    after = path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after)
    data = yaml.safe_load(after)  # must not raise
    assert data["display"]["cleanup_progress"] is True
    assert re.search(r"(?m)^  cleanup_progress: true$", after), (
        "новые ключи обязаны встать на отступ шаблона (2), а не на отступ "
        "комментария (6) — комментарий формы не несёт"
    )
    assert not re.search(r"(?m)^      cleanup_progress: true$", after)


def test_deleted_leaf_inside_existing_section_is_not_resurrected(client_config):
    """Клиент стирает то, что мы только что дописали внутрь существующей
    секции — второй прогон обязан НЕ вернуть это обратно."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    sync_missing_client_sections(client_config, TEMPLATE)
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert data["terminal"]["docker_extra_args"]  # sanity: и правда дописалось

    # Клиент вручную убирает блок docker_extra_args (ключ + два элемента
    # списка), оставляя остальную секцию terminal как есть.
    lines = client_config.read_text(encoding="utf-8").splitlines()
    out = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "docker_extra_args:":
            i += 3  # ключ + "- -p" + "- 18000-18009:18000-18009"
            continue
        out.append(lines[i])
        i += 1
    client_config.write_text("\n".join(out) + "\n", encoding="utf-8")

    added2, _ = sync_missing_client_sections(client_config, TEMPLATE)
    assert "terminal.docker_extra_args" not in added2

    data2 = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert "docker_extra_args" not in data2["terminal"]


def test_deleted_key_inside_a_whole_added_root_section_is_not_resurrected(client_config):
    """Клиент удаляет подключ внутри секции, которая приехала ЦЕЛИКОМ (как
    новый корневой ключ), а не была довставлена по одному ключу — защита
    от воскрешения обязана работать и для этого случая."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    sync_missing_client_sections(client_config, TEMPLATE)
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert "browser" in data  # sanity: секция приехала целиком

    text = client_config.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip() != 'backend: "off"']
    client_config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    added2, _ = sync_missing_client_sections(client_config, TEMPLATE)
    assert "browser.backend" not in added2

    data2 = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert not (data2.get("browser") or {}).get("backend")


def test_crlf_client_file_is_preserved_byte_for_byte(tmp_path):
    """Файл, отредактированный блокнотом (CRLF), не имеет права получить ни
    одного постороннего "\\n" — ни на старых строках, ни на новых."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    original = (
        "# Trix Agent — конфигурация.\r\n"
        "terminal:\r\n"
        "  backend: docker\r\n"
        "  cwd: /workspace\r\n"
        "  container_memory: 3072\r\n"
        "  container_cpu: 2\r\n"
        "  docker_extra_args:\r\n"
        "    - -p\r\n"
        "    - 18000-18009:18000-18009\r\n"
        "\r\n"
        "web:\r\n"
        "  search_backend: ddgs\r\n"
        "\r\n"
        "_config_version: 34\r\n"
    )
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(original)
    before_bytes = path.read_bytes()

    added, _ = sync_missing_client_sections(path, TEMPLATE)
    assert added  # что-то новое (display, gateway, ...) дописалось

    after_bytes = path.read_bytes()
    assert after_bytes.startswith(before_bytes), (
        "старые строки не должны меняться — новый файл начинается со старого"
    )
    appended = after_bytes[len(before_bytes):]
    assert b"\r\n" in appended
    assert b"\n" not in appended.replace(b"\r\n", b""), (
        "в дописанной части не должно остаться ни одного голого \\n — "
        "весь файл обязан говорить на одном языке переводов строк"
    )
    assert "Что клиент видит в Telegram" in appended.decode("utf-8")


def test_no_trailing_newline_stays_that_way(tmp_path):
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    content = (
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "_config_version: 34"  # намеренно без завершающего \n
    )
    path.write_bytes(content.encode("utf-8"))

    added, _ = sync_missing_client_sections(path, TEMPLATE)
    assert added
    after = path.read_bytes()
    assert not after.endswith(b"\n"), (
        "исходный файл не заканчивался переводом строки — он не должен "
        "появиться из ниоткуда"
    )


def test_read_only_config_is_left_untouched(client_config):
    """Защиту, которую клиент поставил руками (chmod), досев обязан уважать."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    before = client_config.read_text(encoding="utf-8")
    client_config.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        added, skipped = sync_missing_client_sections(client_config, TEMPLATE)
        assert added == []
        assert skipped == []
        assert client_config.read_text(encoding="utf-8") == before
    finally:
        client_config.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Review round 2 — Critical: an existing section with an inline value
# (scalar/null/flow-mapping) must never get block-style children spliced
# under it — that produces invalid YAML ("expected <block end>, but found").
# ---------------------------------------------------------------------------


def _client_with_display_value(tmp_path, display_line: str) -> Path:
    """A client config.yaml with ``display_line`` verbatim as the display key."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        f"{display_line}\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "display_line, expected_display",
    [
        ("display: null", None),
        ("display: ~", None),
        ("display: {}", {}),
        ("display: {cleanup_progress: true}", {"cleanup_progress": True}),
    ],
    ids=["null", "tilde", "empty-flow-map", "populated-flow-map"],
)
def test_inline_display_value_is_never_corrupted(tmp_path, display_line, expected_display):
    """Splicing block-style children under an inline-valued parent line
    would produce invalid YAML. The path must be skipped, not inserted,
    and the file must still parse afterwards with its content unchanged."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = _client_with_display_value(tmp_path, display_line)
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)

    assert not any(a.startswith("display.") for a in added), (
        f"a display.* path was inserted under {display_line!r} — this is "
        "exactly the YAML-corrupting regression from review round 2"
    )
    assert any(p.startswith("display.") for p in _skipped_paths(skipped)), (
        f"{display_line!r} should route every display.* path to skipped, "
        "not silently drop it"
    )

    after = path.read_text(encoding="utf-8")
    # Nothing was spliced under the display line or anywhere else that
    # already existed — only whole new root sections can have been
    # appended at the very end, so the old text is an exact PREFIX of the
    # new one.
    assert after.startswith(before), (
        "existing content (including the display line itself) changed — "
        "nothing should have been spliced under an inline-valued parent"
    )

    data = yaml.safe_load(after)  # must not raise
    assert data["display"] == expected_display


def test_bare_display_key_is_filled_when_truly_empty(client_config):
    """Same flip as test_totally_empty_section_is_filled_at_the_template_indent,
    kept under its own name so the round-1 → round-3 → round-4 history of
    this exact edge case stays discoverable by search. A bare ``display:``
    with nothing under it has no shape to contradict, so it gets filled —
    and, as always, the file must still parse and no old line may move."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = client_config.parent / "config_bare_display.yaml"
    text = client_config.read_text(encoding="utf-8").replace(
        "_config_version: 34\n", "display:\n\n_config_version: 34\n"
    )
    path.write_text(text, encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    assert "display.cleanup_progress" in added
    assert not any(p.startswith("display.") for p in _skipped_paths(skipped))

    after = path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(text, after)
    data = yaml.safe_load(after)  # must not raise
    assert data["display"]["cleanup_progress"] is True


def test_binary_garbage_in_seeded_state_file_does_not_crash_sync(client_config):
    """A corrupt (non-UTF-8) sidecar must degrade to 'nothing seeded yet',
    not propagate an exception out of sync_missing_client_sections()."""
    from hermes_cli.trix_config_sync import _seeded_state_path, sync_missing_client_sections

    state_path = _seeded_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(b"\xff\xfe\x00\xff not valid utf-8 or json")

    added, skipped = sync_missing_client_sections(client_config, TEMPLATE)
    assert "display" in added


# ---------------------------------------------------------------------------
# Review round 3 — Critical: formatting decisions (line separator, parent
# line shape, and now INDENT) must be derived from the CLIENT file only —
# the template supplies content, never how to format it. Three rounds
# caught the same class of bug in three different places: the line
# separator, the parent line's shape, and now the indent of its children.
# ---------------------------------------------------------------------------


def test_client_indent_wider_than_template_is_respected(tmp_path):
    """Client hand-indented an existing ``display:`` section with 4 spaces
    instead of the template's 2. Inserted content must be re-indented to
    MATCH the client's own 4 — using the template's 2 verbatim (as before
    this fix) produces ``expected <block end>, but found ...``."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "display:\n"
        '    tool_progress: "off"\n'
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    assert "display.cleanup_progress" in added
    assert "display.platforms" in added
    assert "display.interim_assistant_messages" in added
    assert "display.tool_progress" not in added  # уже есть у клиента

    after = path.read_text(encoding="utf-8")
    # Вставка идёт ВНУТРИ файла (после display:, перед _config_version),
    # а не только в конец — строгий префикс здесь не годится, нужен тот же
    # построчный diff-инвариант, что и в test_not_a_single_existing_line_changes.
    assert _old_lines_are_untouched(before, after), "существующие строки не тронуты"

    data = yaml.safe_load(after)  # must not raise — доказывает, что переотбивка сработала
    assert data["display"]["tool_progress"] == "off"
    assert data["display"]["cleanup_progress"] is True
    assert data["display"]["platforms"]["telegram"]["streaming"] is False

    # Каждая вставленная строка внутри display — на клиентском отступе (4),
    # а не на шаблонном (2). Вложенные на один уровень глубже строки
    # (platforms.telegram, platforms.telegram.streaming) сдвинуты той же
    # дельтой (+2), то есть 6 и 8 соответственно.
    assert re.search(r"(?m)^    cleanup_progress: true$", after)
    assert re.search(r"(?m)^    interim_assistant_messages: false$", after)
    assert re.search(r"(?m)^    platforms:$", after)
    assert re.search(r"(?m)^      telegram:$", after)
    assert re.search(r"(?m)^        streaming: false$", after)
    # И ничего из этого не осталось на шаблонном отступе (2).
    assert not re.search(r"(?m)^  cleanup_progress: true$", after)


def test_client_indent_narrower_than_template_is_respected(tmp_path):
    """Symmetric case, with a synthetic template whose indent (4) is WIDER
    than the client's existing indent (2) — the client's own indent must
    still win, regardless of which side happens to be the bigger number."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    template_path = tmp_path / "template.yaml"
    template_path.write_text(
        "display:\n"
        "    # Комментарий шаблона про первый ключ.\n"
        '    tool_progress: "off"\n'
        "    cleanup_progress: true\n",
        encoding="utf-8",
    )

    client_path = tmp_path / "config.yaml"
    client_path.write_text(
        "display:\n"
        '  tool_progress: "off"\n'
        "\n"
        "_config_version: 1\n",
        encoding="utf-8",
    )
    before = client_path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(client_path, template_path)
    assert "display.cleanup_progress" in added

    after = client_path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after)
    data = yaml.safe_load(after)  # must not raise
    assert data["display"]["cleanup_progress"] is True
    assert re.search(r"(?m)^  cleanup_progress: true$", after)
    assert not re.search(r"(?m)^    cleanup_progress: true$", after)


def test_flow_mapping_continuation_on_next_line_is_skipped(tmp_path):
    """The parent line itself is bare (passes the round-2 check), but its
    sole "child" line is actually a flow-mapping CONTINUATION of the
    parent's own value (``display:\\n  {cleanup_progress: true}``), not a
    block-mapping key. Inserting a block-style sibling after it breaks the
    file exactly like round 2's cases did."""
    from hermes_cli.trix_config_sync import _SKIP_AMBIGUOUS_INDENT, sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        "display:\n"
        "  {cleanup_progress: true}\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    assert not any(p.startswith("display.") for p in added)
    assert _skipped_reason(skipped, "display.tool_progress") == _SKIP_AMBIGUOUS_INDENT

    after = path.read_text(encoding="utf-8")
    assert after.startswith(before)
    data = yaml.safe_load(after)  # must not raise
    assert data["display"] == {"cleanup_progress": True}


# ---------------------------------------------------------------------------
# Review round 4 — Critical: a COMMENT is not a source of form. The rule
# is "never contradict a shape the client already has", and a comment's
# indent is invisible to the YAML parser, so it is not a shape. Counting
# comments into the block's minimum indent sent the splice to the comment's
# level and made the whole config unreadable — on an ordinary two-space
# file with one sloppily indented comment.
# ---------------------------------------------------------------------------


def _client_with_display_block(tmp_path, display_block: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(
        "terminal:\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  container_memory: 3072\n"
        "  container_cpu: 2\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        f"{display_block}"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "display_block, child_indent",
    [
        # The reviewer's own repro: an ordinary two-space file whose only
        # oddity is one comment nudged to column 1. Rounds 1-3 spliced at
        # indent 1 and the config stopped parsing.
        ('display:\n # заметка\n  tool_progress: "off"\n', 2),
        # Same thing with the comment AFTER the real child.
        ('display:\n  tool_progress: "off"\n # заметка\n', 2),
        # And with the client's children on four spaces while a comment
        # sits on two — the comment must not drag the splice down to 2.
        ('display:\n  # заметка\n    tool_progress: "off"\n', 4),
        # Comment deeper than the children: still ignored entirely.
        ('display:\n  tool_progress: "off"\n        # заметка\n', 2),
    ],
    ids=["comment-first-line", "comment-after-children", "comment-shallower", "comment-deeper"],
)
def test_a_stray_comment_never_decides_the_child_indent(tmp_path, display_block, child_indent):
    """Whatever the comment's indent, the splice follows the REAL child."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = _client_with_display_block(tmp_path, display_block)
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    assert "display.cleanup_progress" in added
    assert not any(p.startswith("display.") for p in _skipped_paths(skipped))

    after = path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after), "существующие строки не тронуты"

    data = yaml.safe_load(after)  # must not raise — это и есть пойманный дефект
    assert data["display"]["tool_progress"] == "off"
    assert data["display"]["cleanup_progress"] is True
    assert data["display"]["platforms"]["telegram"]["streaming"] is False

    pad = " " * child_indent
    assert re.search(rf"(?m)^{pad}cleanup_progress: true$", after)
    assert re.search(rf"(?m)^{pad}platforms:$", after)


def test_client_child_indent_ignores_comment_lines():
    """The reviewer's unit-level confirmation, kept as a test.

    Before round 4 this returned 2 — the comment's indent — while the
    block's real children sit at 4. Anything derived from that number
    splices into the wrong column.
    """
    from hermes_cli.trix_config_sync import _client_child_indent

    assert _client_child_indent(["display:", "    a: 1", "  # c"], 0, 3) == 4


# ---------------------------------------------------------------------------
# Review round 4 — Minor: a client file closed with the YAML document-end
# marker (``...``). Appending root sections after it makes a SECOND
# document, and yaml.safe_load then refuses the whole file with
# "expected '<document start>'". Insertion position is a form decision
# like any other and must come from the client's document structure.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "end_marker",
    ["...", "...  # конец конфига", "...   "],
    ids=["plain", "with-comment", "trailing-spaces"],
)
def test_document_end_marker_does_not_become_a_second_document(tmp_path, end_marker):
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n  backend: docker\n  cwd: /workspace\n" f"{end_marker}\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    assert "display" in added, "секции обязаны доехать, а не уйти в skipped целиком"

    after = path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after), "существующие строки не тронуты"

    data = yaml.safe_load(after)  # must not raise — это и есть пойманный дефект
    assert data["display"]["cleanup_progress"] is True
    assert data["terminal"]["backend"] == "docker"

    # Маркер остался ровно один и остался ПОСЛЕДНИМ содержательным элементом:
    # всё дописанное встало перед ним, а не за ним.
    lines = [ln for ln in after.splitlines() if ln.strip()]
    assert lines.count(end_marker) == 1
    assert lines[-1] == end_marker


def test_document_end_marker_still_allows_second_level_splicing(tmp_path):
    """The marker also must not confuse the in-block splice: `terminal:`
    exists, `docker_extra_args` does not, and the block ends at the marker."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n  backend: docker\n  cwd: /workspace\n...\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, _ = sync_missing_client_sections(path, TEMPLATE)
    assert "terminal.docker_extra_args" in added

    after = path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after)
    data = yaml.safe_load(after)  # must not raise
    assert data["terminal"]["docker_extra_args"] == ["-p", "18000-18009:18000-18009"]
    assert data["terminal"]["backend"] == "docker"


def test_dots_inside_a_block_are_not_a_document_end_marker(tmp_path):
    """``...`` only closes the document at column 0. An indented value that
    happens to be three dots is ordinary data and must not move insertion."""
    from hermes_cli.trix_config_sync import _root_insert_index

    lines = ["terminal:", "  cwd: ...", "web:", "  search_backend: ddgs"]
    assert _root_insert_index(lines) == len(lines)
    assert _root_insert_index(lines + ["..."]) == len(lines)


def test_a_parseable_client_file_is_still_parseable_after_the_seed(tmp_path, monkeypatch):
    """The whole defect class, as one property instead of four case tests.

    Every round so far ended the same way: some unforeseen shape of the
    client file made ``yaml.safe_load`` fail after the seed, and the
    client's config stopped being readable by anything. This sweeps the
    axes that produced all four findings — document markers, parent line
    shape, child indent, stray comments at every indent, block scalars,
    blank-line separation — and asserts the two invariants that actually
    matter, for every combination:

      1. a file that parsed BEFORE the seed still parses AFTER it;
      2. not one pre-existing line changed.

    Run against round 3's code this fails on 1008 of the 1980
    combinations; the four named tests above pin down the specific shapes
    the reviewer found, and this one guards the shapes nobody has thought
    of yet.

    The template here is a compact stand-in rather than the shipped one:
    the axis under test is the shape of the CLIENT file, and re-parsing
    the real 200-line template 1980 times costs ~18s for no extra signal.
    It keeps the structural variety that matters to the splice — comments
    above keys, a nested mapping, a list value, a block scalar. The four
    named tests above run against the real TEMPLATE.
    """
    import itertools

    from hermes_cli.trix_config_sync import sync_missing_client_sections

    template = tmp_path / "template.yaml"
    template.write_text(
        "terminal:\n"
        "  # Комментарий про backend.\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "  # Комментарий про список портов.\n"
        "  docker_extra_args:\n"
        "    - -p\n"
        "    - 18000-18009:18000-18009\n"
        "\n"
        "display:\n"
        "  # Комментарий про вложенную секцию.\n"
        "  platforms:\n"
        "    telegram:\n"
        "      streaming: false\n"
        '  tool_progress: "off"\n'
        "  cleanup_progress: true\n"
        "\n"
        "platform_hints:\n"
        "  telegram:\n"
        "    append: >\n"
        "      Многострочный текст подсказки, который обязан переехать\n"
        "      целиком и не развалить файл.\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )

    heads = ["", "---\n", "# заголовок клиента\n"]
    parents = [
        "terminal:\n  backend: docker\n",
        "terminal:\n    backend: docker\n",
        "terminal:\n # note\n  backend: docker\n",
        "terminal:\n  backend: docker\n # note\n",
        "terminal:\n  # note\n",
        "terminal:\n",
        "terminal: {}\n",
        "terminal: null\n",
        "terminal: {backend: docker}\n",
        "terminal:\n  {backend: docker}\n",
        "terminal:\n  note: |\n    text\n  backend: docker\n",
    ]
    displays = [
        "",
        "display:\n",
        'display:\n  tool_progress: "off"\n',
        "display:\n      # note\n",
        "display: ~\n",
        'display:\n   tool_progress: "off"\n   # note\n',
    ]
    tails = ["", "_config_version: 34\n", "_config_version: 34\n...\n", "...\n", "note: >\n  хвост\n"]
    seps = ["\n", ""]

    checked = 0
    for n, (head, parent, display, tail, sep) in enumerate(
        itertools.product(heads, parents, displays, tails, seps)
    ):
        text = head + parent + sep + display + sep + tail
        if not text.strip():
            continue
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            continue  # already unreadable before we touched it — out of scope

        case_dir = tmp_path / f"case{n}"
        case_dir.mkdir()
        # Fresh HERMES_HOME per case: the anti-resurrection sidecar is
        # global, and a shared one would make every case after the first
        # seed nothing at all — the sweep would pass vacuously.
        monkeypatch.setenv("HERMES_HOME", str(case_dir / "home"))
        path = case_dir / "config.yaml"
        path.write_text(text, encoding="utf-8")

        sync_missing_client_sections(path, template)
        after = path.read_text(encoding="utf-8")

        try:
            yaml.safe_load(after)
        except yaml.YAMLError as exc:
            raise AssertionError(
                f"досев сделал клиентский конфиг нечитаемым.\n"
                f"--- было ---\n{text}\n--- стало ---\n{after}\n--- ошибка ---\n{exc}"
            ) from exc
        assert _old_lines_are_untouched(text, after), (
            f"досев изменил существующие строки.\n--- было ---\n{text}\n--- стало ---\n{after}"
        )
        checked += 1

    assert checked > 1000, f"sweep выродился до {checked} комбинаций — проверять стало нечего"


def test_client_child_indent_degrades_gracefully_on_tabs():
    """A tab-indented line can't be measured as a numeric space-indent —
    ``_indent_of`` only counts leading spaces, so a tab-indented line
    silently reads as indent 0. In the real call path a line like that
    never even enters the block's range (``_block_extent`` already
    excludes anything not deeper than the parent's own indent), but this
    exercises the helper directly to prove it degrades to 'cannot derive'
    rather than miscounting indent 0 as a real, insertable indent."""
    from hermes_cli.trix_config_sync import _client_child_indent

    lines = ["display:", "\tcleanup_progress: true", ""]
    assert _client_child_indent(lines, 0, 2) is None


# ---------------------------------------------------------------------------
# Review round 5 — Critical: the line scanner and the YAML parser can
# disagree about WHICH LINE OWNS A KEY. A multi-line quoted scalar whose
# continuation sits at column 0 is textually indistinguishable from a root
# key. No sixth textual special case closes that class; only parsing the
# RESULT and comparing it against what the client had does.
# ---------------------------------------------------------------------------


def _client_with_multiline_scalar(tmp_path, quote: str) -> Path:
    """A client config whose scalar CONTAINS a line that reads as ``web:``.

    ``agent.system_prompt_extra`` is a multi-line quoted scalar. Its second
    line is ``web:`` at column 0 — owned by the string as far as the parser
    is concerned, and a root key as far as any line scanner is concerned.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n"
        f"  system_prompt_extra: {quote}Ты Trix.\n"
        "web:\n"
        f"Всегда отвечай по-русски.{quote}\n"
        "web:\n"
        "  search_backend: ddgs\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double-quoted", "single-quoted"])
def test_multiline_scalar_that_looks_like_a_root_key_is_never_written(tmp_path, quote):
    """Both variants must leave the file byte-for-byte untouched.

    The double-quoted variant used to end up unparseable. The single-quoted
    one is worse: it stayed parseable while the client's own value was
    silently rewritten — neither _old_lines_are_untouched nor the 1980-shape
    sweep can see that, because every line really is still there. Only a
    value-level check catches it.
    """
    from hermes_cli.trix_config_sync import _SKIP_VERIFY_FAILED, sync_missing_client_sections

    path = _client_with_multiline_scalar(tmp_path, quote)
    before_bytes = path.read_bytes()
    before_value = yaml.safe_load(before_bytes.decode("utf-8"))["agent"]["system_prompt_extra"]

    added, skipped = sync_missing_client_sections(path, TEMPLATE)

    assert added == [], "ничего не должно было записаться"
    assert path.read_bytes() == before_bytes, "файл обязан остаться нетронутым байт в байт"

    after = path.read_text(encoding="utf-8")
    data = yaml.safe_load(after)  # must not raise
    assert data["agent"]["system_prompt_extra"] == before_value, (
        "значение клиента переписано — это та самая тихая порча, ради "
        "которой и добавлена проверка результата перед записью"
    )
    assert _SKIP_VERIFY_FAILED in _skipped_reasons(skipped), (
        "отказ обязан быть объяснён, а не выглядеть как «всё на месте»"
    )


def test_seeded_state_is_untouched_when_the_write_is_refused(tmp_path, monkeypatch):
    """A refused write must not leave 28 paths marked as delivered.

    Marking them would make the damage permanent: the client fixes the
    config by hand and doctor --fix would never deliver those settings,
    because it believes it already did.
    """
    import json

    from hermes_cli.trix_config_sync import _seeded_state_path, sync_missing_client_sections

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    path = _client_with_multiline_scalar(tmp_path, '"')

    added, _ = sync_missing_client_sections(path, TEMPLATE)
    assert added == []

    state = _seeded_state_path()
    seeded = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    assert seeded == {}, f"досев отметил доставленным то, что не записал: {sorted(seeded)}"


def test_verify_rejects_a_result_that_changes_an_existing_value():
    """The gate itself, exercised directly on values rather than on files."""
    from hermes_cli.trix_config_sync import _verify

    client = {"web": {"search_backend": "searxng"}}

    # Adds a key, keeps every existing value -> accepted, nothing ineffective.
    assert _verify("web:\n  search_backend: searxng\ndisplay:\n  a: 1\n", client, ["display"]) == []
    # Rewrites the client's value -> rejected outright.
    assert _verify("web:\n  search_backend: ddgs\ndisplay:\n  a: 1\n", client, ["display"]) is None
    # Drops the client's key -> rejected.
    assert _verify("display:\n  a: 1\n", client, ["display"]) is None
    # Unparseable -> rejected.
    assert _verify("web:\n  search_backend: searxng\n display: {\n", client, ["display"]) is None
    # Parses and preserves, but the claimed path never materialised.
    assert _verify("web:\n  search_backend: searxng\n", client, ["display"]) == ["display"]


def test_verify_allows_an_empty_block_to_become_a_mapping():
    """The one legitimate value change: a bare ``key:`` (None) being filled."""
    from hermes_cli.trix_config_sync import _verify

    client = {"display": None}
    assert _verify("display:\n  cleanup_progress: true\n", client, ["display.cleanup_progress"]) == []
    # None must still not be allowed to become a scalar or a list.
    assert _verify("display: 5\n", client, []) is None
    assert _verify("display:\n  - a\n", client, []) is None


def test_true_is_not_accepted_as_one():
    """Type-strict comparison: ``True == 1`` in Python, but not for us."""
    from hermes_cli.trix_config_sync import _preserves_client_values

    assert _preserves_client_values({"a": True}, {"a": True})
    assert not _preserves_client_values({"a": True}, {"a": 1})
    assert not _preserves_client_values({"a": 1}, {"a": 1.0})


# ---------------------------------------------------------------------------
# Review round 5 — Minor 1: a path that is neither added nor skipped is
# invisible. The operator sees "configuration is up to date" while the
# settings never arrived, with no diagnostic anywhere.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first_line",
    ["terminal :", '"terminal":', "﻿terminal:"],
    ids=["space-before-colon", "quoted-key", "bom"],
)
def test_a_parent_the_scanner_cannot_find_is_reported_not_dropped(tmp_path, first_line):
    """All three parse fine as YAML, so the parsed config says the parent
    exists — but the line scanner's ``key:`` pattern does not match them."""
    from hermes_cli.trix_config_sync import _SKIP_PARENT_NOT_FOUND, sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(f"{first_line}\n  backend: docker\n", encoding="utf-8")

    assert "terminal" in yaml.safe_load(path.read_text(encoding="utf-8")), (
        "премиса теста: для парсера родитель ЕСТЬ"
    )

    added, skipped = sync_missing_client_sections(path, TEMPLATE)

    missing_under_terminal = [p for p in _skipped_paths(skipped) if p.startswith("terminal.")]
    assert missing_under_terminal, (
        "недоехавшие подключи terminal.* не должны исчезать бесследно — "
        "ни в added, ни в skipped, ни в логе"
    )
    for path_str in missing_under_terminal:
        assert _skipped_reason(skipped, path_str) == _SKIP_PARENT_NOT_FOUND
    assert "terminal.docker_extra_args" not in added


def test_every_missing_path_is_accounted_for(tmp_path, monkeypatch):
    """Contract: a path is either added, or skipped WITH a reason. Never
    neither. This is the invariant the silent ``continue`` violated."""
    from hermes_cli.trix_config_sync import (
        _collect_missing_paths,
        sync_missing_client_sections,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal :\n"  # scanner can't match it
        "  backend: docker\n"
        "\n"
        "display: null\n"  # inline parent
        "\n"
        "gateway:\n"
        "  media_retention_hours: {}\n"  # too deep
        "\n"
        "web:\n"
        "  search_backend: ddgs\n",
        encoding="utf-8",
    )
    template_data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    client_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = set(_collect_missing_paths(template_data, client_data))

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    accounted = set(added) | set(_skipped_paths(skipped))

    assert expected - accounted == set(), (
        f"пути потерялись молча: {sorted(expected - accounted)}"
    )
    assert all(reason for _, reason in skipped), "у каждого пропуска обязана быть причина"


# ---------------------------------------------------------------------------
# Review round 5 — Minor 2: the module's contract is "any error -> empty
# result, file untouched". A non-UTF-8 client file broke it, because
# _read_raw_text was guarded by ``except OSError`` and UnicodeDecodeError
# is a ValueError. The sidecar was hardened against exactly this in round
# 2; the config file itself was not.
# ---------------------------------------------------------------------------


def test_non_utf8_client_config_returns_empty_instead_of_raising(tmp_path):
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_bytes(b"\xff\xfe\x00terminal: docker\n")
    before = path.read_bytes()

    added, skipped = sync_missing_client_sections(path, TEMPLATE)  # must not raise
    assert added == []
    assert skipped == []
    assert path.read_bytes() == before


def test_non_utf8_template_returns_empty_instead_of_raising(client_config):
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    bad_template = client_config.parent / "template.yaml"
    bad_template.write_bytes(b"\xff\xfe\x00display: x\n")
    before = client_config.read_bytes()

    added, skipped = sync_missing_client_sections(client_config, bad_template)
    assert added == []
    assert client_config.read_bytes() == before


# ---------------------------------------------------------------------------
# Review round 5 — Minor 3: the sidecar must mark only what actually
# reached the parsed config. A splice that lands in a text block the parser
# does not own changes nothing, and marking it "delivered" makes the miss
# permanent.
# ---------------------------------------------------------------------------


def test_a_splice_with_no_parsed_effect_is_not_marked_delivered(tmp_path, monkeypatch):
    """Duplicated root key: the parser takes the LAST ``terminal:`` block,
    the scanner finds the FIRST. Splicing into the first changes nothing.

    Three things must hold: the ineffective paths are not written (the file
    must not grow run after run), they are reported with a reason, and the
    sidecar does not claim them — so once the client removes the duplicate,
    doctor --fix still delivers them.
    """
    import json

    from hermes_cli.trix_config_sync import (
        _SKIP_NO_EFFECT,
        _seeded_state_path,
        sync_missing_client_sections,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n  backend: docker\n\nweb:\n  search_backend: ddgs\n\nterminal:\n  extra: 1\n",
        encoding="utf-8",
    )

    added, skipped = sync_missing_client_sections(path, TEMPLATE)
    first_run = path.read_text(encoding="utf-8")

    assert not any(p.startswith("terminal.") for p in added)
    assert _skipped_reason(skipped, "terminal.backend") == _SKIP_NO_EFFECT
    # Sections that DO take effect still get delivered — one dead path must
    # not block the whole batch.
    assert "display" in added

    seeded = json.loads(_seeded_state_path().read_text(encoding="utf-8"))
    assert not any(k.startswith("terminal.") for k in seeded), (
        f"sidecar отметил доставленным то, что до парсера не доехало: {sorted(seeded)}"
    )

    # Second run must not append the same dead block again.
    sync_missing_client_sections(path, TEMPLATE)
    assert path.read_text(encoding="utf-8") == first_run, (
        "файл растёт с каждым прогоном — врезка в мёртвый блок повторяется"
    )
    assert yaml.safe_load(first_run)["terminal"] == {"extra": 1}


def test_the_path_is_delivered_once_the_client_removes_the_duplicate(tmp_path, monkeypatch):
    """The point of not marking it: it stays deliverable."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.yaml"
    path.write_text(
        "terminal:\n  backend: docker\n\nweb:\n  search_backend: ddgs\n\nterminal:\n  extra: 1\n",
        encoding="utf-8",
    )
    sync_missing_client_sections(path, TEMPLATE)

    # Клиент убирает дубль.
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    cut = [i for i, ln in enumerate(lines) if ln == "terminal:"][-1]
    del lines[cut : cut + 2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    added, _ = sync_missing_client_sections(path, TEMPLATE)
    assert "terminal.docker_extra_args" in added, (
        "путь был помечен доставленным при первом прогоне и больше не доедет"
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["terminal"]["docker_extra_args"] == ["-p", "18000-18009:18000-18009"]


# --------------------------------------------------------------------------
# Порты доезжают конфигом, но не контейнером.
#
# ``-p`` подставляется только при СОЗДАНИИ контейнера
# (``tools/environments/docker.py``), а песочница клиента живёт месяцами.
# Досев при этом вписывает И порты, И подсказку агенту — а подсказка едет в
# промпт со следующего же сообщения и велит агенту называть собеседнику
# публичный адрес. Без предупреждения клиент узнаёт о рассинхроне от
# собеседника, у которого ссылка не открылась.
# --------------------------------------------------------------------------


def test_notice_fires_when_ports_were_seeded():
    from hermes_cli.trix_config_sync import DOCKER_PORTS_PATH, sandbox_recreate_notice

    notice = sandbox_recreate_notice(["display", DOCKER_PORTS_PATH])
    assert notice and "пересоздайте песочницу" in notice.lower()


def test_no_notice_when_ports_were_not_seeded():
    """Молчание — тоже контракт: досев без портов не должен пугать клиента."""
    from hermes_cli.trix_config_sync import sandbox_recreate_notice

    assert sandbox_recreate_notice(["display", "approvals"]) is None
    assert sandbox_recreate_notice([]) is None
    assert sandbox_recreate_notice(None) is None


def test_ports_path_is_actually_in_the_template():
    """Инвариант: путь, на который смотрит уведомление, обязан существовать в
    шаблоне — иначе оно не выстрелит НИКОГДА и это не будет видно."""
    from hermes_cli.trix_config_sync import DOCKER_PORTS_PATH

    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    section, key = DOCKER_PORTS_PATH.split(".")
    assert key in data[section]


def _run_update_sync(monkeypatch, config_path):
    """Прогнать досев ровно так, как его зовёт ``hermes update``."""
    from hermes_cli import config as config_mod
    from hermes_cli.update_cmd import _sync_trix_config_sections

    monkeypatch.setattr(config_mod, "get_config_path", lambda *a, **k: config_path)
    monkeypatch.setattr(config_mod, "get_project_root", lambda *a, **k: REPO_ROOT)
    _sync_trix_config_sections(quiet=False)


def test_update_prints_the_notice_when_it_seeds_ports(client_config, monkeypatch, capsys):
    _run_update_sync(monkeypatch, client_config)

    out = capsys.readouterr().out
    assert "docker_extra_args" in out
    assert "пересоздайте песочницу" in out.lower()


def _run_doctor_sync(config_path):
    """Прогнать досев ровно так, как его зовёт ``hermes doctor --fix``."""
    from hermes_cli.doctor import _sync_trix_config_sections

    return _sync_trix_config_sections(config_path, REPO_ROOT)


def test_doctor_prints_the_notice_when_it_seeds_ports(client_config, capsys):
    """Проверяет ``_sync_trix_config_sections`` саму по себе, в изоляции от
    ``run_doctor()`` — вызывает её напрямую через ``_run_doctor_sync``, минуя
    настоящий доктор целиком. Это НЕ проверка того, что ``run_doctor()``
    действительно зовёт досев: эта граница закрыта отдельным тестом,
    ``test_run_doctor_with_fix_actually_calls_the_sync`` ниже, который гоняет
    настоящий ``run_doctor(Namespace(fix=True))``."""
    fixed = _run_doctor_sync(client_config)

    out = capsys.readouterr().out
    assert fixed == 1
    assert "docker_extra_args" in out
    assert "пересоздайте песочницу" in out.lower()


def test_doctor_stays_quiet_when_ports_were_already_there(client_config, capsys):
    text = client_config.read_text(encoding="utf-8")
    client_config.write_text(
        text.replace(
            "  cwd: /workspace\n",
            "  cwd: /workspace\n  docker_extra_args:\n    - \"-p\"\n"
            "    - \"18000-18009:18000-18009\"\n",
        ),
        encoding="utf-8",
    )
    _run_doctor_sync(client_config)

    out = capsys.readouterr().out
    assert "Дописаны недостающие настройки" in out, (
        "досев обязан был дописать ОСТАЛЬНЫЕ секции — иначе тест доказывает "
        "лишь то, что он не сработал вовсе"
    )
    assert "пересоздайте песочницу" not in out.lower()


def test_run_doctor_with_fix_actually_calls_the_sync(tmp_path, monkeypatch):
    """The real entry point (``hermes doctor --fix``), not the direct-call
    helper above. ``_run_doctor_sync`` calls ``_sync_trix_config_sections``
    directly and proves nothing about whether ``run_doctor()`` itself still
    wires that call in — commenting out the ``fixed_count +=
    _sync_trix_config_sections(...)`` line inside ``run_doctor()`` leaves
    every other test in this module green. Only running the real
    ``run_doctor(Namespace(fix=True))`` and checking the on-disk config
    actually got the missing sections closes that gap.
    """
    import io
    import contextlib
    import sys
    import types
    from argparse import Namespace

    import hermes_cli.doctor as doctor_mod
    from hermes_cli.config import DEFAULT_CONFIG

    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    version = DEFAULT_CONFIG["_config_version"]
    config_path = home / "config.yaml"
    config_path.write_text(
        "# Trix Agent — конфигурация.\n"
        "terminal:\n"
        "  # Команды агента выполняются в контейнере.\n"
        "  backend: docker\n"
        "  cwd: /workspace\n"
        "\n"
        "web:\n"
        "  search_backend: ddgs\n"
        "\n"
        f"_config_version: {version}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))
    # PROJECT_ROOT is deliberately left pointing at the real repo root: the
    # curated template being spliced in (assets/config/trix-config.yaml)
    # only resolves from there. Faking it (as most other run_doctor tests
    # in tests/hermes_cli/test_doctor.py do, since they don't exercise
    # --fix's config-sync branch) would make the sync a silent no-op and
    # this test would pass for the wrong reason.

    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=True))

    out = buf.getvalue()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert "display" in data, (
        "run_doctor(fix=True) did not seed the missing template sections "
        "into config.yaml — the wiring inside run_doctor() itself is what "
        "this test guards, not sync_missing_client_sections() directly"
    )
    assert data["terminal"]["docker_extra_args"] == ["-p", "18000-18009:18000-18009"]
    assert "Дописаны недостающие настройки" in out
    assert "пересоздайте песочницу" in out.lower()


def test_update_stays_quiet_when_ports_were_already_there(client_config, monkeypatch, capsys):
    text = client_config.read_text(encoding="utf-8")
    client_config.write_text(
        text.replace(
            "  cwd: /workspace\n",
            "  cwd: /workspace\n  docker_extra_args:\n    - \"-p\"\n"
            "    - \"18000-18009:18000-18009\"\n",
        ),
        encoding="utf-8",
    )
    _run_update_sync(monkeypatch, client_config)

    out = capsys.readouterr().out
    assert "пересоздайте песочницу" not in out.lower()


# --- Часовой пояс (спека 11) -------------------------------------------


def test_timezone_key_and_its_explanation_reach_an_installed_machine(client_config):
    """Уже установленная машина получает строку и объяснение, но не ответ.

    Досев умеет добавлять корневой скаляр так же, как секцию, — и это
    единственное, что нужно: значение остаётся пустым, потому что пояс
    знает только клиент.
    """
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    before = client_config.read_text(encoding="utf-8")
    added, skipped = sync_missing_client_sections(client_config, TEMPLATE)

    assert "timezone" in added
    text = client_config.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["timezone"] == ""
    assert "часовой пояс" in text.lower()
    assert _old_lines_are_untouched(before, text)


def test_seeding_a_timezone_never_changes_behaviour_on_an_installed_machine(client_config):
    """Досев довозит объяснение, а не смену пояса.

    Пустое значение читается ровно как отсутствие ключа — системное время
    машины. Это и делает досев безопасным там, где смена пояса задним
    числом небезопасна.
    """
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    sync_missing_client_sections(client_config, TEMPLATE)
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert not (data["timezone"] or "").strip()


def test_an_answered_timezone_is_never_overwritten(client_config):
    """Клиент уже ответил — досев обязан пройти мимо."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    client_config.write_text(
        client_config.read_text(encoding="utf-8") + '\ntimezone: "Asia/Yekaterinburg"\n',
        encoding="utf-8",
    )
    added, skipped = sync_missing_client_sections(client_config, TEMPLATE)

    assert "timezone" not in added
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert data["timezone"] == "Asia/Yekaterinburg"


def test_telegram_network_section_is_seeded_when_missing(client_config):
    """Клиент без блока telegram: получает всю секцию с комментариями.

    Настройки сети Телеграма ради этого и вынесены на корневой уровень:
    досев умеет вставлять корневой ключ и ключ второго уровня, а
    ``platforms.telegram.extra.network`` — четвёртый, туда он не дошёл бы.
    """
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    before = client_config.read_text(encoding="utf-8")
    added, skipped = sync_missing_client_sections(client_config, TEMPLATE)

    assert "telegram" in added
    assert "telegram" not in _skipped_paths(skipped)

    after = client_config.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after)

    data = yaml.safe_load(after)
    assert set(data["telegram"]["network"]) == {
        "pool_size", "pool_timeout", "connect_timeout",
        "read_timeout", "write_timeout", "media_write_timeout",
    }
    # Комментарии — единственная документация этих чисел для клиента.
    assert "Отдельный таймаут на отправку ФАЙЛОВ" in after


def test_telegram_network_is_seeded_into_an_existing_telegram_block(tmp_path):
    """У клиента уже есть telegram: с другими ключами — врезка идёт внутрь."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    path = tmp_path / "config.yaml"
    path.write_text(
        "telegram:\n"
        "  require_mention: false\n"
        "\n"
        "_config_version: 34\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_missing_client_sections(path, TEMPLATE)

    assert "telegram.network" in added
    assert "telegram.network" not in _skipped_paths(skipped)

    after = path.read_text(encoding="utf-8")
    assert _old_lines_are_untouched(before, after)

    data = yaml.safe_load(after)
    assert data["telegram"]["require_mention"] is False
    assert data["telegram"]["network"]["pool_timeout"] == 8


def test_seeded_telegram_network_matches_the_adapter_defaults(client_config):
    """Досев не имеет права изменить поведение: числа шаблона — это в
    точности сегодняшние умолчания клиента Telegram в коде."""
    from hermes_cli.trix_config_sync import sync_missing_client_sections
    from plugins.platforms.telegram.telegram_network import (
        _NETWORK_SPEC,
        resolve_http_request_kwargs,
    )

    sync_missing_client_sections(client_config, TEMPLATE)
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))

    seeded = resolve_http_request_kwargs({"network": data["telegram"]["network"]}, env={})
    bare = {spec[1]: spec[3] for spec in _NETWORK_SPEC}
    assert seeded == bare, (
        "секция в шаблоне обещает клиенту одно, а код без неё делает другое"
    )
