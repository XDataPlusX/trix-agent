"""Обновление не имеет права уносить с собой распознавание речи.

На машинах с процессором ниже `x86-64-v2` рецепт установки ставит
numpy 1.x — колёса 2.x там не запускаются вовсе. `hermes update`
доустанавливает зависимости и возвращает 2.x поверх подобранного.

Поймано живьём 2026-09-05: после чистой установки голос работал
(проверено настоящим звуком), после обновления `import numpy` падает с
«your machine doesn't support X86_V2». Молча — клиент узнал бы, только
когда бот перестал бы понимать голосовые.

**Результат сторожа собирается настоящим типом, а не заглушкой.** Первая
редакция этих тестов подсовывала самодельный класс со своими именами
полей — и пропустила то, что отчёт читал несуществующие атрибуты и не
печатал НИЧЕГО ни при какой починке. Заглушка проверяла выдумку автора,
а не продукт.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from hermes_cli.trix_numpy_guard import _HEALTH_PROBE, NumpyGuardResult
from hermes_cli.trix_update_numpy import numpy_report_lines, repair_numpy_after_update


def _result(*, checked=True, ok=True, repaired=False, message=""):
    return NumpyGuardResult(checked=checked, ok=ok, repaired=repaired, message=message)


def test_a_healthy_machine_gets_no_extra_noise():
    """Ничего не чинили — отчёт об обновлении не обрастает строчками."""
    assert numpy_report_lines(_result(ok=True, repaired=False)) == []


def test_a_machine_without_numpy_says_nothing_either():
    assert numpy_report_lines(_result(checked=False, ok=True, repaired=False)) == []


def test_a_repaired_machine_is_told_what_happened():
    lines = numpy_report_lines(_result(ok=True, repaired=True))
    joined = " ".join(lines).lower()
    assert lines, "починку обязаны показать"
    assert "numpy" in joined
    assert "распознавание речи" in joined


def test_an_unrepairable_machine_gets_the_honest_bad_news_and_a_next_step():
    """Честный отказ дороже тихого успеха — и он обязан говорить, что делать."""
    lines = numpy_report_lines(_result(ok=False, repaired=True))
    joined = " ".join(lines)
    assert "НЕ будет" in joined
    assert "hermes doctor --fix" in joined
    assert "остальное" in joined, "клиент должен понимать, что сломано не всё"


def test_every_branch_of_the_report_reads_fields_that_actually_exist():
    """Защита от повторения найденного дефекта: имена полей — не выдумка.

    Отчёт читал `attempted_install`/`importable`, которых у результата нет,
    и потому молчал при любой починке. Здесь проверяется, что каждое имя,
    от которого зависит ветвление, реально есть у типа.
    """
    fields = set(NumpyGuardResult.__dataclass_fields__)
    assert {"checked", "ok", "repaired"} <= fields
    # И что хотя бы одна ветка отчёта действительно срабатывает.
    assert numpy_report_lines(_result(ok=True, repaired=True))


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


# --- проверка здоровья numpy -------------------------------------------------


def _run_probe(extra_path: str | None) -> int:
    env = dict(os.environ)
    if extra_path:
        env["PYTHONPATH"] = extra_path
    return subprocess.run(
        [sys.executable, "-c", _HEALTH_PROBE],
        capture_output=True,
        text=True,
        env=env,
    ).returncode


def test_a_real_numpy_passes_the_health_probe():
    pytest.importorskip("numpy")
    assert _run_probe(None) == 0


def test_a_gutted_numpy_directory_fails_the_health_probe(tmp_path):
    """Каталог-обманка обязан читаться как «сломан», а не как «здоров».

    Ровно это нашлось на живой машине, когда установщик не смог дочистить
    старый каталог пакета: `import numpy` проходил, `numpy.__version__`
    отсутствовал, `pip list` numpy не показывал. Голый импорт объявлял
    такую машину здоровой, и сторож её не чинил.
    """
    (tmp_path / "numpy").mkdir()
    (tmp_path / "numpy" / "__init__.py").write_text("", encoding="utf-8")
    assert _run_probe(str(tmp_path)) != 0


def test_a_numpy_that_raises_on_import_fails_the_health_probe(tmp_path):
    """Та самая поломка старого процессора: импорт падает целиком."""
    (tmp_path / "numpy").mkdir()
    (tmp_path / "numpy" / "__init__.py").write_text(
        textwrap.dedent(
            """
            raise RuntimeError(
                "NumPy was built with baseline optimizations (X86_V2) but "
                "your machine doesn't support (X86_V2)."
            )
            """
        ),
        encoding="utf-8",
    )
    assert _run_probe(str(tmp_path)) != 0


def test_a_numpy_whose_maths_is_broken_fails_the_health_probe(tmp_path):
    """Версия на месте, счёт — нет. Голосовому слою нужен именно счёт."""
    (tmp_path / "numpy").mkdir()
    (tmp_path / "numpy" / "__init__.py").write_text(
        textwrap.dedent(
            """
            __version__ = "1.26.4"

            def zeros(*a, **k):
                raise RuntimeError("скомпилированное ядро не поднялось")
            """
        ),
        encoding="utf-8",
    )
    assert _run_probe(str(tmp_path)) != 0
