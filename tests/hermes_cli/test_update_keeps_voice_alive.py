"""Обновление не имеет права уносить с собой распознавание речи.

На машинах с процессором ниже `x86-64-v2` рецепт установки ставит
numpy 1.x — колёса 2.x там не запускаются вовсе. `hermes update`
переставляет зависимости по замку и возвращает 2.x поверх подобранного.

Поймано живьём 2026-09-05: после чистой установки голос работал
(проверено настоящим звуком), после обновления `import numpy` падает с
«your machine doesn't support X86_V2». Молча — клиент узнал бы, только
когда бот перестал бы понимать голосовые.
"""

import pytest

from hermes_cli.trix_update_numpy import numpy_report_lines, repair_numpy_after_update


class _Result:
    def __init__(self, present=True, importable=True, attempted_install=False):
        self.present = present
        self.importable = importable
        self.attempted_install = attempted_install
        self.message = ""


def test_a_healthy_machine_gets_no_extra_noise():
    """Ничего не чинили — отчёт об обновлении не обрастает строчками."""
    assert numpy_report_lines(_Result(importable=True, attempted_install=False)) == []


def test_a_machine_without_numpy_says_nothing_either():
    assert numpy_report_lines(_Result(present=False, attempted_install=False)) == []


def test_a_repaired_machine_is_told_what_happened():
    lines = numpy_report_lines(_Result(importable=True, attempted_install=True))
    joined = " ".join(lines).lower()
    assert lines, "починку обязаны показать"
    assert "numpy" in joined
    assert "распознавание речи" in joined


def test_an_unrepairable_machine_gets_the_honest_bad_news_and_a_next_step():
    """Честный отказ дороже тихого успеха — и он обязан говорить, что делать."""
    lines = numpy_report_lines(_Result(importable=False, attempted_install=True))
    joined = " ".join(lines)
    assert "НЕ будет" in joined
    assert "hermes doctor --fix" in joined
    assert "остальное" in joined, "клиент должен понимать, что сломано не всё"


def test_the_repair_never_raises_even_when_the_guard_explodes(monkeypatch):
    """Обновление уже прошло: провал починки не смеет выглядеть его провалом."""
    import hermes_cli.trix_numpy_guard as guard

    def explode(python=None):
        raise RuntimeError("сторож упал")

    monkeypatch.setattr(guard, "ensure_runnable_numpy", explode, raising=True)
    assert repair_numpy_after_update() is None


def test_update_completion_runs_the_guard(monkeypatch, capsys):
    """Сквозная проверка: объявление успеха обновления зовёт сторожа.

    Это и есть дефект — сторож в продукте был, но обновление его не звало.
    """
    from hermes_cli import update_cmd
    import hermes_cli.trix_update_numpy as mod

    called = []
    monkeypatch.setattr(
        mod, "repair_numpy_after_update", lambda *a, **k: called.append(True)
    )
    update_cmd._print_update_completion("✓ Update complete!")

    assert called == [True], "обновление объявило успех, не проверив numpy"
    assert "✓ Update complete!" in capsys.readouterr().out


def test_the_guard_runs_before_the_success_line(monkeypatch, capsys):
    """Порядок важен: сначала чиним и говорим, потом объявляем успех.

    Иначе предупреждение уезжает ПОД «✓ Update complete!» и читается как
    приписка к уже объявленному успеху.
    """
    from hermes_cli import update_cmd
    import hermes_cli.trix_update_numpy as mod

    monkeypatch.setattr(
        mod, "repair_numpy_after_update", lambda *a, **k: print("  ⚠ строка сторожа")
    )
    update_cmd._print_update_completion("✓ Update complete!")

    out = capsys.readouterr().out
    assert out.index("строка сторожа") < out.index("Update complete")
