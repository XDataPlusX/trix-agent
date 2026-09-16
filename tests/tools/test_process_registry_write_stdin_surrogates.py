"""Sibling regression test for #79178: background-PTY stdin must round-trip
surrogateescape content instead of crashing on the strict UTF-8 encode."""
import shlex
import time

import pytest

from tools.process_registry import ProcessRegistry


def test_write_stdin_pty_surrogateescape_roundtrip(tmp_path):
    registry = ProcessRegistry()
    out = tmp_path / "out.bin"
    script = tmp_path / "read_stdin.py"
    # readline(): a PTY never delivers EOF, so read one line (canonical mode
    # delivers it after the newline we send).
    script.write_text(
        f"import sys\nopen({str(out)!r}, 'wb').write(sys.stdin.buffer.readline())\n"
    )
    session = registry.spawn_local(
        f"python3 {shlex.quote(str(script))}",
        cwd=str(tmp_path),
        use_pty=True,
    )
    if session._pty is None:
        registry.kill_process(session.id)
        pytest.skip("ptyprocess not available; PTY path not exercised")
    try:
        result = registry.write_stdin(
            session.id, b"\xff".decode("utf-8", "surrogateescape") + "\n"
        )
        assert result["status"] == "ok", result
        # Ждём СОДЕРЖИМОЕ, а не существование файла, и ждём щедро.
        #
        # Два изъяна прежней редакции, оба видны только под нагрузкой (тест
        # покраснел в полном параллельном прогоне и прошёл в одиночку):
        #
        # 1. `out.exists()` становится истинным в момент ОТКРЫТИЯ файла на
        #    запись, то есть до того, как в него что-то попало. Между
        #    открытием и записью проверка могла прочитать пустоту и
        #    сравнить её с ожидаемым — гонка, а не проверка.
        # 2. Десять секунд на запуск процесса в PTY при двадцати рабочих
        #    процессах раннера — не запас, а лотерея.
        #
        # Ждём ровно того, что проверяем: пока прочитанное не совпадёт с
        # ожидаемым. Тогда успех наступает сразу, а потолок нужен только
        # чтобы не висеть вечно при настоящей поломке.
        expected = b"\xff\n"
        deadline = time.monotonic() + 60
        actual = b""
        while time.monotonic() < deadline:
            try:
                actual = out.read_bytes()
            except OSError:
                actual = b""
            if actual == expected:
                break
            time.sleep(0.05)
        assert actual == expected
    finally:
        registry.kill_process(session.id)
