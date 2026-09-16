"""Строка "агент ещё работает" для клиентских поверхностей.

Живёт здесь, а не в ``gateway/run.py``, по тому же принципу, что
``trix_sandbox_guard.py`` и ``trix_tool_names.py``: продуктовая
формулировка — наша, апстримный файл — не наш. ``gateway/run.py`` — 27
тысяч строк, которые мы регулярно подтягиваем сверху; каждая наша функция
внутри него оплачивается конфликтами при каждом мёрже. Здесь функция ни с
чем не конфликтует, зовётся однострочным импортом на месте вызова и
тестируется без запуска шлюза.
"""

from __future__ import annotations


def build_heartbeat_text(minutes: int, tool_name: str | None, lang: str | None = None) -> str:
    """Строка "агент ещё работает" для мессенджера.

    Называет ДЕЛО ("читаю файл"), а не имя инструмента: клиент — не
    программист, и слово ``read_file`` ему ничего не говорит. Неизвестный
    инструмент даёт строку без хвоста — показывать английское имя хуже,
    чем не показывать ничего.

    ``_ACTIONS`` (``hermes_cli.trix_tool_names``) хранит только русские
    формулировки. Подставлять их в строку любого другого языка дало бы
    смесь ("Working — 3 min — читаю файл"), поэтому дело подставляем
    только когда РЕЗОЛВЯЩИЙСЯ язык — русский; для всех остальных языков
    отдаём терсовую строку без хвоста, как для неизвестного инструмента.
    ``agent.i18n.resolve_language`` резолвит язык той же логикой, что и
    ``t()`` — сверяемся с ней явно, а не полагаемся, что вызов ``t()``
    ниже случайно выбрал русский каталог.
    """
    from agent.i18n import resolve_language, t
    from hermes_cli.trix_tool_names import russian_tool_action

    is_russian = resolve_language(lang) == "ru"
    action = russian_tool_action(tool_name) if (tool_name and is_russian) else None
    if action:
        return t("trix.status.working_with_action", lang=lang,
                 minutes=minutes, action=action)
    return t("trix.status.working", lang=lang, minutes=minutes)
