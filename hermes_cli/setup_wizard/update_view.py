"""Страница «Обновление» в мастере настройки — вторая дверь к `/update`.

Зачем она есть. Обновление продукта до сих пор запускалось ровно одной
командой в чате. Пока чат работает, этого хватает; но чинить клиенту
приходится как раз то, из-за чего чат и не работает — а починка едет к
нему обновлением. Клиент с отвалившимся ботом оказывался заперт: ему
некуда нажать, чтобы получить уже выпущенную нами правку, и оставалось
только написать в поддержку и ждать человека.

Мастер — единственная дверь, которая от состояния бота не зависит: это
отдельный systemd-юнит на той же машине, со своим паролем из письма.
Поэтому кнопка живёт здесь.

**Что здесь принципиально, а что случайно.**

* Страница ничего не делает сама: и запуск, и чтение исхода живут в
  ``hermes_cli/update_launch.py``, потому что тот же исход должен видеть
  чат. Здесь только показ и подтверждение.
* Подтверждение обязательно. Обновление перезапускает и бота, и сам
  мастер; нажатие мимо не должно ронять клиенту работающую машину.
* Страница обязана пережить **собственную смерть**. `hermes update`
  перезапускает мастер, то есть процесс, отдающий эту страницу,
  оборвётся посреди опроса. Поэтому опрос молча терпит сетевые ошибки и
  продолжает стучаться, пока сервер не поднимется, — иначе клиент увидит
  «страница недоступна» ровно в тот момент, когда всё идёт правильно.
* Итог показывается словами. «Код возврата 1» — это не ответ клиенту, у
  которого нет консоли; ему нужно знать, обновилось или нет и что делать
  дальше.
"""

from __future__ import annotations

import asyncio
import html
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from hermes_cli import update_launch
from hermes_cli.setup_wizard.page import (
    _header_intro,
    _rail_address,
    _rail_html,
    render_shell,
)

logger = logging.getLogger(__name__)

_MSG_ALREADY_RUNNING = "Обновление уже идёт. Дождитесь, пожалуйста, его окончания."
_MSG_NO_COMMAND = (
    "Не удалось запустить обновление на этой машине. "
    "Напишите в поддержку XDataPlus — это наша сторона, не ваша."
)

#: Итоговые фразы. Клиент читает их вместо кода возврата.
_MSG_DONE = (
    "Готово — продукт обновлён. Бот перезапущен и снова на связи; "
    "если он молчит, напишите ему любое сообщение."
)
#: Нажали, а обновлять было нечего. Отдельная фраза, потому что сказать
#: «продукт обновлён» там, где ничего не менялось, — это соврать: клиент
#: решит, что получил починку, которой не получал, и перестанет искать
#: настоящую причину.
_MSG_ALREADY_LATEST = (
    "Обновлять было нечего — на машине и так стоит последняя версия. "
    "Ничего не изменилось, бот работает как работал."
)
#: Кончилось хорошо, но сменилась версия или нет — определить не вышло.
#: Молчим о том, чего не знаем.
_MSG_DONE_UNKNOWN = (
    "Готово, обновление завершилось без ошибок. Бот перезапущен; "
    "если он молчит, напишите ему любое сообщение."
)
_MSG_FAILED = (
    "Обновление не завершилось. Машина продолжает работать на прежней "
    "версии — ничего не сломано. Напишите в поддержку XDataPlus."
)


def _current_version_text() -> str:
    """Версия так, как её называет `/version` в чате.

    Именно та же функция, а не своя копия: две двери, называющие разные
    версии одной машины, — это вопрос в поддержку.
    """
    try:
        from hermes_cli.banner import format_version_reply_text

        return format_version_reply_text()
    except Exception:  # pragma: no cover — версия не должна ронять страницу
        logger.debug("не удалось определить версию", exc_info=True)
        return "Trix Agent"


def _availability_text() -> str:
    """«Есть новая» / «стоит последняя» — или честное «не смогли узнать».

    Проверка ходит в сеть и на машине клиента может не дойти (прокси,
    блокировки). Молчать об этом нельзя: клиент нажмёт «Обновить» и не
    поймёт, почему ничего не изменилось.
    """
    try:
        from hermes_constants import get_hermes_home

        from hermes_cli.banner import check_for_updates

        # Ответ кэшируется на шесть часов — это правильно для баннера
        # при запуске, который просто не должен тормозить. Здесь наоборот:
        # у страницы ровно одна задача — сказать, есть ли новая версия, и
        # шестичасовой кэш означал бы «установлена последняя» человеку,
        # которому мы час назад выпустили починку. Он бы не нажал кнопку.
        (get_hermes_home() / ".update_check").unlink(missing_ok=True)
        behind = check_for_updates()
    except Exception:
        logger.debug("проверка обновлений не удалась", exc_info=True)
        behind = None

    if behind is None:
        return "Проверить наличие новой версии сейчас не удалось."
    if behind == 0:
        return "Установлена последняя версия."
    if behind < 0:
        return "Доступна новая версия."
    return f"Доступна новая версия (изменений с вашей: {behind})."


def render_update_page(host: str | None = None) -> str:
    """HTML страницы обновления — через общий каркас мастера."""
    version = html.escape(_current_version_text())
    availability = html.escape(_availability_text())

    content_html = (
        '<div class="content" id="content">'
        "<h1>Обновление</h1>"
        '<p class="lead">Здесь можно обновить продукт на этой машине. '
        "То же самое делает команда <code>/update</code> в чате — это просто "
        "вторая дверь, на случай если бот не отвечает.</p>"
        '<div class="field-group">'
        f'<p id="update-version"><strong>{version}</strong></p>'
        f'<p id="update-availability">{availability}</p>'
        "</div>"
        '<div id="update-warn" class="field-group">'
        "<p>Во время обновления бот и эта страница ненадолго перезапустятся. "
        "Обычно это занимает одну–две минуты. Машину выключать не нужно, "
        "страницу закрывать не обязательно — она сама дождётся конца.</p>"
        "</div>"
        '<div class="step-nav" id="update-actions">'
        '<button type="button" id="update-start" class="accent">Обновить</button>'
        "</div>"
        '<div id="update-confirm" hidden>'
        "<p>Обновить сейчас? Бот перезапустится.</p>"
        '<div class="step-nav">'
        '<button type="button" id="update-cancel">Отмена</button>'
        '<button type="button" id="update-confirm-yes" class="accent">Да, обновить</button>'
        "</div>"
        "</div>"
        '<div id="update-progress" hidden>'
        '<p id="update-progress-text">Обновляю…</p>'
        "</div>"
        '<div id="update-result" hidden><p id="update-result-text"></p></div>'
        "</div>"
    )

    script = """
(function () {
  var startBtn = document.getElementById('update-start');
  var confirmBox = document.getElementById('update-confirm');
  var yesBtn = document.getElementById('update-confirm-yes');
  var cancelBtn = document.getElementById('update-cancel');
  var actions = document.getElementById('update-actions');
  var progress = document.getElementById('update-progress');
  var progressText = document.getElementById('update-progress-text');
  var result = document.getElementById('update-result');
  var resultText = document.getElementById('update-result-text');

  function show(el) { el.hidden = false; }
  function hide(el) { el.hidden = true; }

  startBtn.addEventListener('click', function () { hide(actions); show(confirmBox); });
  cancelBtn.addEventListener('click', function () { hide(confirmBox); show(actions); });

  function finish(text) {
    hide(progress);
    resultText.textContent = text;
    show(result);
  }

  // Опрос намеренно терпит ЛЮБУЮ сетевую ошибку и продолжает: мастер
  // перезапускается сам собой в середине обновления, и оборванный
  // запрос здесь -- признак нормального хода дела, а не сбоя.
  function poll() {
    fetch('/api/update/status', {cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.state === 'running') {
          progressText.textContent = 'Обновляю… ' + (data.note || '');
          setTimeout(poll, 3000);
          return;
        }
        if (data.state === 'done') { finish(data.message); return; }
        if (data.state === 'failed') { finish(data.message); return; }
        setTimeout(poll, 3000);
      })
      .catch(function () {
        progressText.textContent = 'Обновляю… перезапускаю службы';
        setTimeout(poll, 3000);
      });
  }

  yesBtn.addEventListener('click', function () {
    hide(confirmBox);
    show(progress);
    fetch('/api/update/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, data: d}; }); })
      .then(function (res) {
        if (!res.ok) { finish(res.data.error || 'Не удалось запустить обновление.'); return; }
        setTimeout(poll, 3000);
      })
      .catch(function () { setTimeout(poll, 3000); });
  });
})();
"""

    rail_html = _rail_html(
        _rail_address(host),
        _header_intro(host),
        subtitle="Обновление",
        wizard_entry_visible=True,
    )
    return render_shell(
        title="Обновление", rail_html=rail_html, content_html=content_html, script=script
    )


def _state_payload() -> dict:
    state = update_launch.read_update_state()
    payload = {"state": state.state, "exit_code": state.exit_code}
    if state.state == "done":
        if state.changed is True:
            payload["message"] = _MSG_DONE
        elif state.changed is False:
            payload["message"] = _MSG_ALREADY_LATEST
        else:
            payload["message"] = _MSG_DONE_UNKNOWN
    elif state.state == "failed":
        payload["message"] = _MSG_FAILED
    return payload


def register_update_routes(app: FastAPI) -> None:
    """Повесить страницу и два её маршрута на приложение мастера.

    Вызывается один раз из ``create_app()`` — после установки общего
    стека middleware, поэтому ни авторизацию, ни проверку Origin здесь
    переизобретать не нужно: `/api/update/*` попадает под тот же
    ``_OriginGuardMiddleware``, что и остальные мутирующие маршруты.
    """

    @app.get("/update", response_class=HTMLResponse)
    async def update_page(request: Request) -> HTMLResponse:
        from hermes_cli.setup_wizard.app import _host_from_request

        host = _host_from_request(request)
        html_text = await asyncio.to_thread(render_update_page, host)
        return HTMLResponse(content=html_text)

    @app.post("/api/update/start")
    async def update_start(request: Request) -> JSONResponse:
        try:
            argv = await asyncio.to_thread(update_launch.start_detached_update)
        except update_launch.UpdateAlreadyRunning:
            return JSONResponse(status_code=409, content={"error": _MSG_ALREADY_RUNNING})
        except update_launch.HermesCommandNotFound:
            logger.error("мастер не нашёл, чем запустить обновление")
            return JSONResponse(status_code=500, content={"error": _MSG_NO_COMMAND})
        logger.info("обновление запущено из мастера: %s", " ".join(argv))
        return JSONResponse(content={"started": True})

    @app.get("/api/update/status")
    async def update_status() -> JSONResponse:
        return JSONResponse(content=await asyncio.to_thread(_state_payload))
