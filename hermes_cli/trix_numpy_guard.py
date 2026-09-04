"""numpy, который на этой машине действительно запускается.

Колёса numpy 2.x собраны под базовую линию x86-64-v2. На процессоре ниже
(а именно такой приезжает у нашего хостера по умолчанию —
`QEMU Virtual CPU version 2.5+`, проверено на четырёх машинах из четырёх)
они не импортируются вовсе: `import numpy` падает с «NumPy was built with
baseline optimizations». За numpy стоят распознавание речи
(faster-whisper) и локальные голоса (KittenTTS, Piper) — то есть на таком
процессоре у клиента молча мертва вся голосовая часть.

**Почему одной правки в рецепте оказалось мало.** Рецепт при создании
машины ставит `numpy<2`, и это работает. Но любой ПОЗДНИЙ шаг, который
тянет пакет с зависимостью от numpy, возвращает 2.x обратно — и ломает
всё заново, молча. Снято на живой машине 2026-09-05: клиент выбрал в
мастере KittenTTS (заработал), потом Piper — установка Piper подтянула
numpy 2.x, и озвучка снова отвалилась с той же ошибкой.

Поэтому починка обязана быть НЕ разовой, а восстанавливающейся: после
каждого шага, который мог тронуть numpy, проверяем, импортируется ли он,
и если нет — возвращаем работающую ветку.

Проверяем ИМПОРТОМ, а не разбором флагов процессора. Флаги отвечают на
вопрос «должен ли numpy заработать», а нам нужен ответ на «работает ли он
сейчас» — и он один и тот же на любой причине поломки, включая ту, о
которой мы ещё не знаем.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class NumpyGuardResult:
    """Что вышло из попытки вернуть numpy в рабочее состояние."""

    checked: bool          # numpy вообще установлен и его проверяли
    ok: bool               # numpy импортируется сейчас
    repaired: bool         # пришлось откатывать версию
    message: str


# Проверка здоровья numpy. Голого `import numpy` НЕ достаточно.
#
# Снято на живой машине 2026-09-05: после `uv pip install -e .[all]`
# поверх откаченного numpy на диске остался каталог-обманка — `import
# numpy` проходит, `numpy.__version__` отсутствует, `pip list` numpy не
# показывает вовсе. Сторож считал такую машину здоровой и не чинил её,
# а faster-whisper на ней, разумеется, не работал.
#
# Поэтому спрашиваем то, что нужно вызывающему: версию (её нет у пустого
# каталога-пакета) и одно арифметическое действие (оно поднимает
# скомпилированное ядро — ровно то, что не запускается на процессоре без
# x86-64-v2).
_HEALTH_PROBE = (
    "import numpy, sys;"
    " v = numpy.__version__;"
    " sys.exit(0 if (v and float(numpy.zeros(3).sum()) == 0.0) else 1)"
)


def _numpy_imports(python: str | None = None) -> bool:
    """Пригоден ли numpy к работе в целевом интерпретаторе.

    Отдельным процессом, а не `import numpy` здесь: сломанный numpy
    падает `RuntimeError` при импорте, и повторить попытку в том же
    процессе после починки уже нельзя — модуль остаётся в кэше
    полуимпортированным.

    Имя историческое; проверяется не сам факт импорта, а работоспособ-
    ность — см. ``_HEALTH_PROBE``.
    """
    exe = python or sys.executable
    try:
        result = subprocess.run(
            [exe, "-c", _HEALTH_PROBE],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except Exception:
        return False
    return result.returncode == 0


def _numpy_present(python: str | None = None) -> bool:
    exe = python or sys.executable
    try:
        result = subprocess.run(
            [exe, "-c", "import importlib.util as u, sys;"
                        " sys.exit(0 if u.find_spec('numpy') else 1)"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except Exception:
        return False
    return result.returncode == 0


def ensure_runnable_numpy(python: str | None = None) -> NumpyGuardResult:
    """Вернуть numpy в состояние «импортируется», если он сломан.

    Ничего не делает, когда numpy не установлен вовсе (значит он этой
    машине не нужен) или когда он и так работает — то есть на нормальном
    процессоре это чистый no-op ценой одного импорта.

    Никогда не бросает: это восстановительный шаг, и его провал не должен
    ронять установку инструмента, ради которой он вызван.
    """
    exe = python or sys.executable
    try:
        if not _numpy_present(exe):
            return NumpyGuardResult(False, True, False, "numpy не установлен — проверять нечего.")
        if _numpy_imports(exe):
            return NumpyGuardResult(True, True, False, "numpy импортируется.")

        # Откат на ветку 1.x — ту, что собрана под базовую линию без
        # x86-64-v2. Опустить пин безопасно: `numpy>=2` не требует ни один
        # пакет поставки (проверено по замку), точные пины стоят только в
        # необязательных наборах `voice` и `wake`.
        installer = _installer_command(exe)
        if installer is None:
            return NumpyGuardResult(
                True, False, False,
                "numpy не импортируется, и починить нечем: не нашёлся ни uv, ни pip.",
            )
        # Явная кодировка: читаем вывод ЧУЖОГО инструмента (uv/pip), и
        # посторонний байт в нём уронил бы починку UnicodeDecodeError-ом —
        # ровно тем, ради чего в репозитории стоит сторож на этот класс.
        subprocess.run(
            installer, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )

        if _numpy_imports(exe):
            return NumpyGuardResult(
                True, True, True,
                "numpy не запускался на этом процессоре — вернул версию 1.x, работает.",
            )
        return NumpyGuardResult(
            True, False, True,
            "numpy не запускается на этом процессоре, и откат на 1.x не помог.",
        )
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        return NumpyGuardResult(False, False, False, f"Проверка numpy не выполнилась: {exc}")


def _installer_command(python: str) -> list[str] | None:
    """Чем ставить: управляемый uv, иначе pip целевого интерпретатора."""
    try:
        from hermes_cli.managed_uv import ensure_uv

        uv_bin = str(ensure_uv() or "")
        if uv_bin:
            return [uv_bin, "pip", "install", "--python", python, "numpy<2"]
    except Exception:
        pass
    try:
        subprocess.run(
            [python, "-m", "pip", "--version"],
            capture_output=True, timeout=30, check=True,
        )
    except Exception:
        return None
    return [python, "-m", "pip", "install", "numpy<2"]
