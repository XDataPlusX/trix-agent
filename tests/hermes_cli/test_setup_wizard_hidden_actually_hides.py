"""Что страница прячет атрибутом `hidden`, то и должно исчезать с экрана.

Схватка одна и та же каждый раз: `[hidden] { display: none }` приходит из
таблицы стилей БРАУЗЕРА, а любое наше `display: flex/grid/...` — правило
автора, и автор браузера всегда перебивает. Поэтому элемент, которому
скрипт честно поставил `hidden = true`, продолжает рисоваться.

Этот класс дефектов в `page.py` ловили уже четыре раза поштучно:
`.field-row` (строка «способ подключения» не пряталась), `.botlink`,
`.verdict` и — найдено клиентом на живой машине 2026-09-04 — `.stages li`:
шаг «Устанавливаем инструменты» оставался на экране, хотя в этой отправке
устанавливать было нечего. Клиент читал это как «шаг пропустили и всё
зависло».

Здесь проверяется не пятый экземпляр, а инвариант: **если правило автора
задаёт элементу `display`, и этот элемент вообще может быть спрятан, то
для него обязано существовать `[hidden]`-переопределение.** Пятый случай
покраснеет сам, без нового теста.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest


# --- разбор отрисованной страницы -----------------------------------------


class _Element:
    __slots__ = ("tag", "el_id", "classes", "hideable", "ancestors")

    def __init__(self, tag, el_id, classes, hideable, ancestors):
        self.tag = tag
        self.el_id = el_id
        self.classes = classes
        self.hideable = hideable
        self.ancestors = ancestors


class _Collector(HTMLParser):
    """Плоский список элементов с их предками — этого хватает на те формы
    селекторов, которые страница реально использует."""

    _VOID = {"br", "hr", "img", "input", "meta", "link", "source", "path", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._stack: list[_Element] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        el = _Element(
            tag=tag,
            el_id=a.get("id"),
            classes=frozenset((a.get("class") or "").split()),
            hideable="hidden" in a,
            ancestors=tuple(self._stack),
        )
        self.elements.append(el)
        if tag not in self._VOID:
            self._stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return


# --- крошечный сопоставитель селекторов ------------------------------------


def _parse_compound(part: str):
    """`.cls`, `#id`, `tag`, `tag.cls`, `.a.b` -> (tag|None, id|None, {classes})."""
    m = re.fullmatch(r"([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)", part)
    if not m:
        return None
    tag = m.group(1)
    el_id = None
    classes = set()
    for token in re.findall(r"[.#][\w-]+", m.group(2) or ""):
        (classes.add(token[1:]) if token[0] == "." else None)
        if token[0] == "#":
            el_id = token[1:]
    return tag, el_id, frozenset(classes)


def _compound_matches(el: _Element, compound) -> bool:
    tag, el_id, classes = compound
    if tag and el.tag != tag:
        return False
    if el_id and el.el_id != el_id:
        return False
    return classes <= el.classes


def _selector_matches_any(selector: str, elements) -> bool | None:
    """True/False, либо None — «форму селектора не разобрали»."""
    parts = selector.split()
    compounds = [_parse_compound(p) for p in parts]
    if any(c is None for c in compounds):
        return None
    target = compounds[-1]
    ancestors_needed = compounds[:-1]
    for el in elements:
        if not _compound_matches(el, target):
            continue
        chain = list(el.ancestors)
        ok = True
        for need in ancestors_needed:
            while chain and not _compound_matches(chain[0], need):
                chain.pop(0)
            if not chain:
                ok = False
                break
            chain.pop(0)
        if ok:
            return True
    return False


# --- сам инвариант ---------------------------------------------------------


def _css_display_rules(html: str):
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # комментарии не селекторы
    out = []
    for raw_selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        m = re.search(r"(?:^|;|\s)display\s*:\s*([a-z-]+)", body)
        if not m:
            continue
        for selector in raw_selector.split(","):
            out.append((selector.strip(), m.group(1)))
    return out


def _hideable_ids(html: str) -> set[str]:
    """Что страница вообще прячет: статический `hidden` плюс цели скриптов."""
    ids = set()
    for match in re.finditer(r'id="([\w-]+)"[^>]*\shidden[\s>]', html):
        ids.add(match.group(1))
    for match in re.finditer(r'setHidden\(\s*"([\w-]+)"', html):
        ids.add(match.group(1))
    return ids


@pytest.fixture(scope="module")
def page_html():
    from hermes_cli.setup_wizard.page import render_page

    return render_page()


def test_every_hideable_element_with_a_display_rule_has_a_hidden_override(page_html):
    collector = _Collector()
    collector.feed(page_html)
    elements = collector.elements

    hideable_ids = _hideable_ids(page_html)
    for el in elements:
        if el.el_id and el.el_id in hideable_ids:
            el.hideable = True

    # Элемент считается скрываемым и тогда, когда скрываем его СОСЕД по
    # тому же селектору: `.stages li` — один класс на четыре строки, из
    # которых прячут одну. Правило пишется на селектор, не на элемент.
    hideable = [el for el in elements if el.hideable]
    assert hideable, "на странице не нашлось ни одного скрываемого элемента — тест ничего не проверил"

    rules = _css_display_rules(page_html)
    assert rules, "в отрисованной странице не нашлось ни одного правила с display"

    overrides = {sel for sel, value in rules if "[hidden]" in sel and value == "none"}

    unparsed = []
    offenders = []
    for selector, value in rules:
        if value == "none" or "[hidden]" in selector:
            continue
        verdict = _selector_matches_any(selector, hideable)
        if verdict is None:
            unparsed.append(selector)
            continue
        if not verdict:
            continue
        if f"{selector}[hidden]" not in overrides:
            offenders.append(f"{selector} {{ display: {value} }}")

    assert not unparsed, (
        "сопоставитель не разобрал эти селекторы — молча пропустить их нельзя, "
        f"иначе инвариант перестанет что-либо охранять: {unparsed}"
    )
    assert not offenders, (
        "эти элементы страница прячет атрибутом hidden, но правило автора задаёт им "
        "display — браузерное [hidden]{display:none} оно перебивает, и элемент "
        "останется на экране. Добавьте `<селектор>[hidden] { display: none; }`:\n  "
        + "\n  ".join(offenders)
    )


def test_the_stage_row_that_started_this_is_covered(page_html):
    """Точечная проверка найденного клиентом случая — чтобы инвариант выше
    нельзя было «починить», ослабив сопоставитель."""
    # Ищем настоящее правило, а не первое текстовое упоминание селектора:
    # объясняющий комментарий рядом ловился поиском подстроки раньше него.
    match = re.search(r"\.stages li\[hidden\]\s*\{([^}]*)\}", page_html)
    assert match, "в отрисованной странице нет правила .stages li[hidden]"
    assert "display: none" in match.group(1)
