"""Учётные данные в userinfo URL не должны переживать сборку /debug-отчёта.

Найдено 2026-09-12 на живом отчёте: пароль прокси из `.env` клиента лежал
в нём открытым текстом три раза — при том что отчёт собирается с
``redact=True`` по умолчанию. Причина не в выключенной редакции, а в том,
что общий ``redact_sensitive_text`` намеренно пропускает web-URL насквозь
(#34029): ломались magic links и OAuth-коллбэки, которые агент обязан
пройти — их токены живут в QUERY-строке, и слепая маскировка по имени
параметра рвала скиллы посреди работы.

Вместе с query-строкой тем же коммитом выключили и форму ``user:pass@``,
хотя сломанный сценарий был не про неё. Отчёт — не round-trip: его никто
не проходит по ссылке, он уходит человеку в поддержку. Поэтому он ОПТ-ИНИТСЯ
в те два редактора, которые общий проход оставляет выключенными, ровно как
это уже делает CDP-путь (``redact_cdp_endpoint_for_log``).

Глобальное поведение при этом не меняется — это отдельно закреплено ниже,
чтобы правка не съехала в откат #34029.
"""

from __future__ import annotations

import pytest

from agent.redact import redact_sensitive_text
from hermes_cli.debug import _redact_log_text, collect_share_bundle


_PROXY_PASSWORD = "n0t-a-real-password"


class TestReportRedactsUserinfoCredentials:
    """То, ради чего правка: пароль в userinfo не доживает до отчёта."""

    def test_http_proxy_password_is_masked(self):
        line = f"Proxy detected: http://user20:{_PROXY_PASSWORD}@192.0.2.10:42567"
        out = _redact_log_text(line)
        assert _PROXY_PASSWORD not in out

    def test_socks5_proxy_password_is_masked(self):
        line = f"socks5://u:{_PROXY_PASSWORD}@192.0.2.10:1080"
        assert _PROXY_PASSWORD not in _redact_log_text(line)

    def test_https_basic_auth_password_is_masked(self):
        line = f"https://u:{_PROXY_PASSWORD}@example.com/x"
        assert _PROXY_PASSWORD not in _redact_log_text(line)

    def test_scheme_host_and_port_survive(self):
        """Отчёт присылают, чтобы чинить связность. Замазать хост и порт —
        значит сделать его бесполезным ровно в том месте, ради которого он
        и нужен: маскируется пароль, а не адрес."""
        out = _redact_log_text(
            f"http://user20:{_PROXY_PASSWORD}@192.0.2.10:42567"
        )
        assert "http://" in out
        assert "192.0.2.10:42567" in out
        assert "user20" in out

    def test_query_string_secret_in_a_log_line_is_masked_too(self):
        """У отчёта нет round-trip'а, поэтому он опт-инится и в query —
        так же, как CDP-путь. Это НЕ откат #34029: глобальный проход
        остаётся прежним, см. TestGlobalPassthroughIsUnchanged."""
        out = _redact_log_text("GET https://example.com/cb?code=SECRETVALUE123")
        assert "SECRETVALUE123" not in out


class TestNoFalsePositives:
    """Формы, на которых такие правки обычно ломаются."""

    def test_short_user_without_password_is_left_alone(self):
        """``https://git@host`` — userinfo есть, пароля нет, правка про
        двоеточие его не касается.

        Две оговорки, обе про ЧУЖИЕ правила — потому берём форму, которой
        ни одно из них не касается, иначе тест проверял бы не то:
        * голая userinfo от 8 символов маскируется и без этой правки —
          правилом #6396 для ``scheme://TOKEN@host`` (имя от токена там
          неотличимо, порог в 8 символов — осознанный компромисс);
        * ``user@domain.tld`` съедает почтовое правило самого отчёта
          (``_EMAIL_ADDRESS_RE``): клиентские адреса в отчёт не попадают.
        Поэтому имя короткое, а хост — IP.
        """
        out = _redact_log_text("https://git@192.0.2.10/x")
        assert "git@192.0.2.10" in out

    def test_empty_password_is_left_alone(self):
        """``http://user:@host`` — форма легальная, и группа пароля требует
        хотя бы один символ, так что правило её не трогает: пустоту не
        схлопываем в ``***``, будто там что-то было."""
        assert _redact_log_text("http://user:@192.0.2.10/x") == "http://user:@192.0.2.10/x"

    def test_plain_url_passes_through(self):
        line = "GET https://example.com/v1/items HTTP/1.1"
        assert _redact_log_text(line) == line

    def test_email_in_query_is_not_treated_as_userinfo(self):
        """``@`` в query — не userinfo. Проверяется тем, что хост цел."""
        out = _redact_log_text("https://example.com/s?q=user@example.org")
        assert "example.com" in out


class TestGlobalPassthroughIsUnchanged:
    """Страховка от того, что правка расползётся в откат #34029.

    Сломанный тогда сценарий — magic links и OAuth-коллбэки, которые агент
    round-trip'ит через историю: живой вызов уходит с настоящим URL, а
    следующий ход видит ``***``. Глобальный проход обязан остаться таким же.
    """

    def test_oauth_callback_still_passes_through_globally(self):
        url = "https://example.com/cb?code=SECRETVALUE123&state=xyz"
        assert redact_sensitive_text(url, force=True) == url

    def test_presigned_url_still_passes_through_globally(self):
        url = "https://s3.example.com/o?X-Amz-Signature=abc123def456&X-Amz-Expires=900"
        assert redact_sensitive_text(url, force=True) == url

    @pytest.mark.parametrize(
        "url",
        [
            f"http://user20:{_PROXY_PASSWORD}@192.0.2.10:42567",
            f"https://u:{_PROXY_PASSWORD}@example.com/x",
        ],
    )
    def test_userinfo_still_passes_through_globally(self, url):
        """Глобальный проход userinfo НЕ трогает — опт-ин живёт у отчёта.

        Инвариант, а не снимок: он фиксирует, что правка сделана на
        surface'е отчёта, а не в общем редакторе. Если кто-то решит
        включить это глобально, он пройдёт мимо и обдуманно — сломав
        этот тест, а не тихо.
        """
        assert redact_sensitive_text(url, force=True) == url

    def test_db_connection_string_was_already_masked_and_stays_so(self):
        out = redact_sensitive_text(
            f"postgres://u:{_PROXY_PASSWORD}@h/db", force=True
        )
        assert _PROXY_PASSWORD not in out

    def test_bare_token_userinfo_was_already_masked_and_stays_so(self):
        out = redact_sensitive_text(
            "https://ghp_AAAABBBBCCCCDDDD@github.com/x.git", force=True
        )
        assert "ghp_AAAABBBBCCCCDDDD" not in out


class TestNothingLeavesTheMachineCarryingCredentials:
    """Проверка на уровне того, что реально уходит, а не одного хелпера.

    ``collect_share_bundle`` — единственный сборщик: и локальная печать, и
    (отключённый продуктовым решением) путь публикации берут содержимое
    из него. Тест на ``_redact_log_text`` доказывает, что правило работает;
    этот доказывает, что оно стоит НА ПУТИ — то есть что между логом и
    отчётом нет обходной дороги.

    Это и был механизм утечки 2026-09-12: редакция была включена, правило
    существовало, но на нужном пути его не было.
    """

    _PW = "Pr0xyL1veSecret42"

    def _bundle_over_log(self, tmp_path, monkeypatch, log_body: str) -> str:
        logs = tmp_path / "logs"
        logs.mkdir(parents=True)
        (logs / "agent.log").write_text(log_body, encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        return repr(collect_share_bundle(log_lines=50, redact=True))

    def test_proxy_password_in_a_log_never_reaches_the_bundle(
        self, tmp_path, monkeypatch
    ):
        blob = self._bundle_over_log(
            tmp_path,
            monkeypatch,
            "INFO telegram: Proxy detected; passing explicitly to HTTPXRequest: "
            f"http://user20:{self._PW}@192.0.2.10:42567\n"
            f"INFO other: socks5://u:{self._PW}@192.0.2.10:1080\n",
        )
        assert self._PW not in blob

    def test_bundle_keeps_host_and_port(self, tmp_path, monkeypatch):
        """Связность чинят по адресу и порту. Замазать их — значит отдать
        клиенту отчёт, бесполезный ровно там, где он нужен."""
        blob = self._bundle_over_log(
            tmp_path,
            monkeypatch,
            f"INFO telegram: http://user20:{self._PW}@192.0.2.10:42567\n",
        )
        assert "192.0.2.10:42567" in blob

    def test_upload_is_refused_at_the_network_boundary(self):
        """Вторая линия обороны продукта: даже если путь публикации кто-то
        воскресит слиянием, сеть отказывает сама. Проверяем вызовом, а не
        чтением — именно так этот отказ и должен себя вести."""
        from hermes_cli.debug import upload_to_pastebin

        with pytest.raises(RuntimeError, match="never uploads"):
            upload_to_pastebin("secret content")
