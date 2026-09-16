"""Бан за перебор пароля не должен запирать поддержку на полсуток.

Это инвариант ПОСТАВКИ, а не поведения кода: значение живёт в рецепте
установки, объекта времени выполнения у него нет. Проверять такое чтением
файла в этом проекте допустимо — так же, как «pyproject объявляет tzdata с
маркером».

Повод, 2026-09-07: в рецепте стоял `bantime = 12h`. Пять подключений за
десять минут закрыли инженеру доступ к машине на полсуток; снять бан
можно было только через консоль в панели, потому что по SSH уже не зайти.
На машине клиента это означало бы, что поддержка теряет доступ ровно
тогда, когда он нужен срочно.
"""

import re
from pathlib import Path

import pytest

RECIPE = (Path(__file__).resolve().parents[2] / "docs" / "product"
          / "cloud_init" / "hermes_install_v2.sh")


def _jail() -> str:
    text = RECIPE.read_text(encoding="utf-8")
    m = re.search(r"cat << 'EOF' > /etc/fail2ban/jail\.local\n(.*?)\nEOF",
                  text, re.S)
    assert m, "блок jail.local в рецепте не найден"
    return m.group(1)


def _minutes(value: str) -> float:
    """Перевести значение fail2ban (10m, 12h, 600) в минуты."""
    v = value.strip()
    if v.endswith("h"):
        return float(v[:-1]) * 60
    if v.endswith("m"):
        return float(v[:-1])
    if v.endswith("d"):
        return float(v[:-1]) * 1440
    return float(v) / 60  # голое число — секунды


class TestBanIsShortEnoughToRecoverFrom:
    def test_bantime_does_not_exceed_the_distro_default(self):
        """Сток Debian/Ubuntu — 10 минут. Дольше запирает человека, а не бот."""
        m = re.search(r"^bantime\s*=\s*(\S+)", _jail(), re.M)
        assert m, "bantime не задан"
        assert _minutes(m.group(1)) <= 10, (
            f"bantime {m.group(1)} — дольше стокового; поддержка потеряет "
            "доступ к машине клиента и снимать бан придётся через консоль"
        )

    def test_protection_is_still_on(self):
        """Смягчили срок, а не выключили защиту."""
        jail = _jail()
        assert re.search(r"^\[sshd\]", jail, re.M)
        assert re.search(r"^enabled\s*=\s*true", jail, re.M)

    def test_retry_budget_stays_tight(self):
        """Смысл бана — сбить темп автоподбору; бюджет попыток остаётся малым."""
        m = re.search(r"^maxretry\s*=\s*(\d+)", _jail(), re.M)
        assert m and int(m.group(1)) <= 5

    def test_findtime_is_not_widened_to_compensate(self):
        """Иначе короткий бан обходится растянутым окном наблюдения."""
        m = re.search(r"^findtime\s*=\s*(\S+)", _jail(), re.M)
        assert m and _minutes(m.group(1)) <= 10


class TestPlaceholderHandsThePortBack:
    """Заглушка на 8443 обязана уйти раньше, чем мастер займёт порт.

    Тоже инвариант ПОСТАВКИ: это порядок шагов в рецепте, у него нет
    объекта времени выполнения. Читать файл здесь допустимо ровно по той
    же причине, что и выше.

    Повод, 2026-09-08: письмо с паролем уходит клиенту из синхронной части
    рецепта, а `trix-setup.service` встаёт на 110-й секунде из 116 —
    клиент, открывший ссылку сразу, получал «не удаётся подключиться».
    Порт до конца установки держит nginx со страницей «машина готовится».

    Чем это опасно, если порядок сломать: nginx останется на 8443,
    `install-service` поднимет мастер, тот не сможет забиндиться и упадёт —
    и клиент застрянет на странице «готовится» НАВСЕГДА. Отказ соединения
    на две минуты хуже не бывает; вечная заглушка — бывает.
    """

    @staticmethod
    def _text() -> str:
        return RECIPE.read_text(encoding="utf-8")

    def test_placeholder_is_raised_and_later_removed(self):
        text = self._text()
        assert "sites-enabled/trix-placeholder" in text, "заглушка из рецепта пропала"
        assert "rm -f /etc/nginx/sites-enabled/trix-placeholder" in text, (
            "заглушка ставится, но нигде не снимается — мастер не займёт порт"
        )

    @staticmethod
    def _handover_block(text: str) -> str:
        """Блок ПЕРЕДАЧИ порта, а не ветка отката в блоке установки.

        Симлинк удаляется в рецепте дважды: в откате, когда `nginx -t` не
        принял конфиг (там reload не нужен — nginx плохой конфиг и не
        загружал), и в передаче порта мастеру. Поиск по первому вхождению
        измерял бы откат; первая версия этого теста так и сделала.
        """
        start = text.index("if [ -e /etc/nginx/sites-enabled/trix-placeholder ]")
        return text[start:start + 900]

    def test_the_port_is_freed_before_the_wizard_takes_it(self):
        """Снятие обязано стоять ВЫШЕ install-service по тексту рецепта."""
        text = self._text()
        freed = text.index("if [ -e /etc/nginx/sites-enabled/trix-placeholder ]")
        taken = text.index("hermes setup-wizard install-service")
        assert freed < taken, (
            "заглушка снимается ПОСЛЕ того, как мастер пытается занять 8443 — "
            "мастер не сможет забиндиться, и клиент останется на странице "
            "«машина готовится» навсегда"
        )

    def test_nginx_is_reloaded_when_the_port_is_handed_over(self):
        """Удалённый симлинк без reload порт не освобождает."""
        block = self._handover_block(self._text())
        assert "rm -f /etc/nginx/sites-enabled/trix-placeholder" in block
        assert "reload nginx" in block or "restart nginx" in block, (
            "симлинк убран, но nginx не перечитан — 8443 остаётся за заглушкой"
        )

    def test_the_placeholder_asks_for_the_same_password(self):
        """Решение владельца 2026-09-08: пароль спрашивается с первой секунды.

        Клиент вводит логин с паролем из письма один раз и дальше видит
        либо страницу «готовится», либо мастер — смотря успела ли
        установка. Наружу при этом не торчит ничего.

        realm обязан совпадать с мастером, иначе браузер спросит пароль
        второй раз, когда порт сменит хозяина.
        """
        text = self._text()
        site = text[text.index("PLACEHOLDER_SITE"):]
        site = site[:site.index("PLACEHOLDER_SITE", 10)]
        assert "auth_basic" in site, "заглушка открыта всему интернету"
        assert 'auth_basic "Trix Setup"' in site, (
            "realm разошёлся с мастером — браузер спросит пароль дважды"
        )

    def test_the_htpasswd_is_built_from_the_wizard_password(self):
        """Второй копии пароля быть не должно — только тот же самый."""
        text = self._text()
        assert '"$WIZARD_PASSWORD"' in text[:text.index("PLACEHOLDER_HTML")], (
            "htpasswd собирается не из пароля мастера"
        )

    def test_the_password_never_reaches_the_log(self):
        """Рецепт идёт под `set -x`: строку с паролем обязано обрамлять
        `set +x`, иначе пароль осядет в /var/log."""
        text = self._text()
        i = text.index("openssl passwd -apr1")
        before = text[:i]
        assert before.rstrip().endswith("set +x") or "set +x" in before[-200:], (
            "генерация htpasswd не закрыта от set -x"
        )

    def test_the_htpasswd_is_removed_with_the_site(self):
        """Иначе хеш пароля мастера остаётся на диске после установки."""
        block = self._handover_block(self._text())
        assert ".trix-placeholder-htpasswd" in block, (
            "htpasswd переживает передачу порта"
        )
