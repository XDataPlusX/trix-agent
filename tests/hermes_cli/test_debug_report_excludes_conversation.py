"""Отчёт `/debug` не должен выносить переписку клиента с агентом.

Продуктовое основание: отчёт существует, чтобы клиент мог переслать его в
поддержку. Всё, что в него попало, клиент отдаёт третьей стороне — значит
границей приватности является не файл на диске (он и так `0600`), а текст,
который отчёт возвращает наружу.

Проверка идёт через настоящий ход разговора: реальный ``AIAgent`` против
mock-провайдера в процессе, настоящий ``setup_logging()`` в изолированный
``HERMES_HOME``, настоящий ``collect_debug_report()``. Не мок логгера и не
чтение исходников — иначе тест не увидит новое место, где текст клиента
попадёт в журнал.

Что тест поймал при написании (2026-09-06): ``agent/turn_context.py``
пишет в `agent.log` на уровне INFO строку ``conversation turn: ...
msg='<первые 80 символов сообщения клиента>'``, и она доезжала до отчёта
целиком. Докстринг ``_save_report_locally`` называл это («plaintext
conversation content»), но проверки за этим не стояло. Теперь стоит.

Что тест НЕ покрывает и покрывать не берётся: превью вывода инструментов
(``agent/tool_executor.py`` пишет ``Tool %s returned error … result_preview``
на WARNING). Это не переписка, а то, что агент делал, — отдельный вопрос
с отдельной ценой, записан в «Что открыто» STATUS.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# Маркеры выбраны так, чтобы не совпасть ни с одним словом из журнала:
# случайное совпадение сделало бы тест зелёным по неверной причине.
_USER_MARKER = "USERMARKER4c72xq"
_ASSISTANT_MARKER = "ASSISTANTMARKER9f31zw"


class _MockHandler(BaseHTTPRequestHandler):
    """Провайдер в процессе: отдаёт один потоковый ответ и молчит в лог."""

    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        resp = type(self).response_queue.pop(0) if type(self).response_queue else _text_resp("DONE")
        content = resp["choices"][0]["message"].get("content") or ""

        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):
        pass


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


@pytest.fixture()
def report_after_a_real_turn(tmp_path, monkeypatch):
    """Прогоняет один настоящий ход и возвращает (отчёт, каталог логов)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from hermes_logging import flush_log_queue, setup_logging

    # mode="gateway" — тот же режим, в котором продукт и работает у клиента:
    # к agent.log добавляется gateway.log, и оба попадают в отчёт.
    logs_dir = setup_logging(hermes_home=home, log_level="INFO", mode="gateway", force=True)

    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
        provider="openai-compat", model="test-model",
        max_iterations=4, enabled_toolsets=[], quiet_mode=True,
        skip_context_files=True, skip_memory=True, save_trajectories=False,
        platform="telegram",
    )
    _MockHandler.response_queue.append(_text_resp(f"{_ASSISTANT_MARKER} ответ агента про личное дело"))
    try:
        result = agent.run_conversation(
            f"{_USER_MARKER} мой личный вопрос, который клиент не хотел бы никому пересылать",
            conversation_history=[],
            task_id="t",
        )
    finally:
        flush_log_queue()
        srv.shutdown()

    # Ход обязан был состояться: если провайдер не ответил, журнал пуст и
    # любая проверка «маркера нет» станет зелёной по неверной причине.
    assert _ASSISTANT_MARKER in str(result.get("final_response", ""))

    from hermes_cli.debug import collect_debug_report

    return collect_debug_report(log_lines=200), logs_dir


class TestDebugReportExcludesConversation:
    def test_the_turn_really_reached_the_logs(self, report_after_a_real_turn):
        """Защита от зелёного впустую.

        Проверки ниже утверждают отсутствие. Они ничего не значат, если
        отчёт собран из пустых журналов, поэтому сначала убеждаемся, что
        ход разговора в журнал попал: секция agent.log в отчёте есть и она
        не «(file empty)», а сам журнал упоминает состоявшийся ход.
        """
        report, logs_dir = report_after_a_real_turn
        assert "--- agent.log" in report
        agent_section = report.split("--- agent.log", 1)[1]
        assert "(file empty)" not in agent_section.split("---", 1)[0]
        assert "conversation turn:" in report, (
            "в отчёте нет следа хода разговора — журнал пуст, и проверки "
            "отсутствия ничего не доказывают"
        )
        raw_log = (logs_dir / "agent.log").read_text(errors="replace")
        assert _USER_MARKER in raw_log, (
            "сообщение клиента не дошло даже до локального журнала — "
            "значит тест проверяет не тот путь"
        )

    def test_user_message_does_not_reach_the_report(self, report_after_a_real_turn):
        """Текста клиента в отчёте нет — ни целиком, ни куском.

        Именно это свойство мы обещаем, когда просим клиента переслать
        отчёт в поддержку.
        """
        report, _ = report_after_a_real_turn
        leaking = [line for line in report.splitlines() if _USER_MARKER in line]
        assert not leaking, f"сообщение клиента попало в отчёт: {leaking[:3]}"

    def test_assistant_reply_does_not_reach_the_report(self, report_after_a_real_turn):
        """Вторая половина переписки — ответ агента."""
        report, _ = report_after_a_real_turn
        leaking = [line for line in report.splitlines() if _ASSISTANT_MARKER in line]
        assert not leaking, f"ответ агента попал в отчёт: {leaking[:3]}"


class TestConversationPreviewScrubber:
    """Быстрая пара к E2E-тесту выше: сам вычищающий шаг, на строке журнала.

    E2E доказывает, что путь целиком не течёт; эти три проверяют, что
    вычистка снимает ровно превью и не трогает технику вокруг, — иначе
    поддержка получит отчёт без строк, ради которых он и собирается.
    """

    def test_strips_the_message_preview(self):
        from hermes_cli.debug import _redact_log_text

        line = (
            "2026-09-06 15:57:45,648 INFO [20260906_155745] agent.turn_context: "
            "conversation turn: session=s1 model=m provider=p platform=telegram "
            "history=0 msg='личный вопрос клиента'"
        )
        out = _redact_log_text(line)
        assert "личный вопрос клиента" not in out
        # То, ради чего строка и нужна поддержке, остаётся.
        assert "conversation turn:" in out
        assert "session=s1" in out
        assert "platform=telegram" in out

    def test_strips_a_double_quoted_preview(self):
        """repr() берёт двойные кавычки, когда в тексте есть апостроф."""
        from hermes_cli.debug import _redact_log_text

        line = "agent.turn_context: conversation turn: session=s1 msg=\"it's private\""
        assert "it's private" not in _redact_log_text(line)

    def test_leaves_ordinary_technical_lines_alone(self):
        from hermes_cli.debug import _redact_log_text

        line = (
            "2026-09-06 15:57:45,648 WARNING agent.conversation_loop: API call failed "
            "(attempt 1/3) error_type=EmptyStreamError model=test-model"
        )
        assert _redact_log_text(line) == line
