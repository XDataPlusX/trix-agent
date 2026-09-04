"""Шаг npm в обновлении обязан иметь потолок времени.

Без него `hermes update` у клиента замирает молча и надолго. Замерено на
клиентской машине 2026-09-05: `npm ci` встал на 16,5 минут на ОДНОМ
пакете — в логе npm `997189ms attempt #2`, а `ss` показывал чёрную дыру
TCP к одному адресу CDN, при том что `curl` с той же машины отвечал за
0,9 секунды.

И это не гипотетика: в журнале обновлений той же машины прогон
0.1.3 → 0.1.4 обрывается ровно на строке «Updating Node.js
dependencies...» без строки завершения. Код тогда уже лёг (git идёт
раньше), а зависимости нет — машина простояла вечер на наполовину
применённом обновлении.

Проверяется поведение: что происходит, когда npm не отвечает.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import main as hermes_main


@pytest.fixture
def sleeper(tmp_path):
    """Подставной «npm», который просто висит.

    Отдельным скриптом, а не заглушкой: смысл проверки в том, что
    настоящий подпроцесс, который не отвечает, будет остановлен, — а не в
    том, что мы правильно позвали mock.
    """
    script = tmp_path / "hangs.py"
    script.write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
    return [sys.executable, str(script)]


class TestATimeLimitExists:
    def test_the_default_limit_is_shorter_than_the_gateway_watcher(self):
        """Потолок обязан быть МЕНЬШЕ получаса, который отведён наблюдателю
        в шлюзе, — иначе наблюдатель успевает высказаться клиенту раньше,
        чем шаг честно сдастся."""
        assert hermes_main._NPM_STEP_TIMEOUT_SECONDS < 30 * 60

    def test_the_limit_is_generous_enough_for_an_honest_install(self):
        """И заведомо больше честной установки: полный `npm ci` этого дерева
        укладывается в 2-4 минуты даже на слабой VM."""
        assert hermes_main._NPM_STEP_TIMEOUT_SECONDS >= 10 * 60


class TestAHangingNpmIsStopped:
    @pytest.mark.parametrize("capture_output", [True, False])
    def test_a_wedged_command_returns_instead_of_hanging(
        self, sleeper, tmp_path, capture_output
    ):
        result = hermes_main._run_npm_watching_for_engine_failure(
            sleeper,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=capture_output,
            timeout=1,
        )
        assert result.returncode == 124

    @pytest.mark.parametrize("capture_output", [True, False])
    def test_the_reason_reaches_the_caller(self, sleeper, tmp_path, capture_output):
        """Молчаливый отказ был бы не лучше зависания: читающий вывод
        обязан понять, что произошло и что делать."""
        result = hermes_main._run_npm_watching_for_engine_failure(
            sleeper,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=capture_output,
            timeout=1,
        )
        assert "не уложился" in (result.stderr or "")
        assert "код уже обновлён" in (result.stderr or "")

    def test_the_child_process_is_actually_killed(self, sleeper, tmp_path):
        """Осиротевший потомок продолжил бы держать сеть и файлы уже после
        того, как обновление ушло дальше."""
        import time

        before = _sleeping_children()
        hermes_main._run_npm_watching_for_engine_failure(
            sleeper,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=False,
            timeout=1,
        )
        time.sleep(0.5)
        assert _sleeping_children() <= before


class TestNormalRunsAreUntouched:
    def test_a_fast_command_still_returns_its_own_result(self, tmp_path):
        result = hermes_main._run_npm_watching_for_engine_failure(
            [sys.executable, "-c", "import sys; sys.stderr.write('EBADENGINE\\n')"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0
        assert "EBADENGINE" in result.stderr

    def test_streaming_mode_still_tees_stderr_to_the_caller(self, tmp_path):
        """Ради этого функция и существует: восстановление EBADENGINE
        читает stderr даже когда вывод идёт клиенту живьём."""
        result = hermes_main._run_npm_watching_for_engine_failure(
            [sys.executable, "-c", "import sys; sys.stderr.write('EBADENGINE\\n')"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=False,
            timeout=60,
        )
        assert "EBADENGINE" in result.stderr


def _sleeping_children() -> int:
    out = subprocess.run(
        ["ps", "-eo", "command"], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout
    return out.count("hangs.py")


class TestTheLimitAppliesWithoutBeingAsked:
    """Проверки выше передают потолок явно и остались бы зелёными, даже
    если убрать его применение по умолчанию — то есть если защита исчезнет
    для настоящего вызова из обновления, который аргумента не передаёт.
    Поймано при проверке «краснеет ли тест без правки»: удаление умолчания
    не покраснило ничего."""

    @pytest.mark.parametrize("capture_output", [True, False])
    def test_a_hanging_command_is_stopped_with_no_timeout_argument(
        self, sleeper, tmp_path, monkeypatch, capture_output
    ):
        monkeypatch.setattr(hermes_main, "_NPM_STEP_TIMEOUT_SECONDS", 1)
        result = hermes_main._run_npm_watching_for_engine_failure(
            sleeper,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=capture_output,
        )
        assert result.returncode == 124

    def test_none_still_means_no_limit_at_all(self, tmp_path):
        """`None` — законный способ сказать «без потолка», и он не должен
        случайно подхватывать умолчание."""
        result = hermes_main._run_npm_watching_for_engine_failure(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            timeout=None,
        )
        assert result.returncode == 0
