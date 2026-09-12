#!/usr/bin/env python3
"""Замер каналов, от которых зависит установка Trix Agent.

Зачем: установка одного и того же релиза на ОДНОЙ машине занимала от
1 мин 56 с до 11 минут при неизменных 1 мин 24 с процессорного времени.
Работы всегда одинаково — меняется только ожидание сети. Скрипт отвечает
на вопрос, какой именно источник тормозит и тормозит ли канал вообще.

Как пользоваться:

    python3 trix_net_probe.py             # один проход, ~1-2 минуты
    python3 trix_net_probe.py --rounds 6  # шесть проходов с паузой
    python3 trix_net_probe.py --json      # машиночитаемо

Ничего не устанавливает и ничего не меняет на машине: только стандартная
библиотека Python 3 и запросы на чтение. Качает по 24 МБ с источника
диапазонным запросом, файлы не сохраняет.

Что означает результат:

* медленно ВЕЗДЕ, включая нейтральные точки отсчёта — это общий канал
  машины, вопрос к провайдеру про полосу и загрузку узла;
* нейтральные быстрые, а конкретные источники медленные — это маршрут
  или пиринг до этих сетей, вопрос к провайдеру про транзит;
* всё быстро — значит в момент замера канал здоров, и повторять надо
  сериями (`--rounds`), потому что провал бывает плавающим.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 24 * 1024 * 1024  # сколько тянем с каждого источника
TIMEOUT = 60

# Источники, которые установка Trix Agent действительно использует.
# «Нейтральные» нужны как точка отсчёта: если и они медленные, дело не в
# маршруте до конкретной сети, а в канале машины целиком.
SOURCES = [
    ("PyPI (зависимости Python)", "https://files.pythonhosted.org/packages/"
     "source/n/numpy/numpy-1.26.4.tar.gz", "install"),
    # 16 МБ: на пакете в 2 МБ передача кончается раньше, чем TCP
    # разгоняется, и скорость выходит заниженной.
    ("npm (браузерные пакеты)", "https://registry.npmjs.org/@swc/"
     "core-linux-x64-gnu/-/core-linux-x64-gnu-1.10.7.tgz", "install"),
    ("GitHub — код продукта", "https://codeload.github.com/nodejs/node/"
     "tar.gz/refs/tags/v22.14.0", "install"),
    ("GitHub — архив браузеров", "https://github.com/XDataPlusX/trix-agent/"
     "releases/download/browsers-chromium-1243/"
     "trix-browsers-chromium-1243-linux-x64.tar.gz", "install"),
    ("nodejs.org (сам Node)", "https://nodejs.org/dist/v22.14.0/"
     "node-v22.14.0-linux-x64.tar.xz", "install"),
    ("HuggingFace (модель речи)", "https://huggingface.co/Systran/"
     "faster-whisper-base/resolve/main/model.bin", "install"),
    ("cdn.playwright.dev (браузер)", "https://cdn.playwright.dev/builds/cft/"
     "153.0.8010.12/linux64/chrome-linux64.zip", "install"),
    # Точки отсчёта. Одна внутри страны, одна снаружи: если внутренняя
    # быстрая, а внешние медленные — проблема на транзите провайдера, а не
    # в его полосе. Продукт этими адресами не пользуется, они только для
    # сравнения.
    ("Selectel, РФ — точка отсчёта", "https://speedtest.selectel.ru/100MB",
     "reference"),
    ("OVH, Европа — точка отсчёта", "https://proof.ovh.net/files/100Mb.dat",
     "reference"),
]


def _resolve(host: str) -> tuple[float | None, str]:
    """Время DNS-ответа и первый адрес."""
    start = time.monotonic()
    try:
        info = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return None, f"DNS не ответил: {exc}"
    return (time.monotonic() - start) * 1000, info[0][4][0]


def _connect_ms(host: str) -> float | None:
    """Время установки TCP-соединения на 443."""
    start = time.monotonic()
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT):
            pass
    except OSError:
        return None
    return (time.monotonic() - start) * 1000


def measure(url: str) -> dict:
    """Скачать CHUNK байт и вернуть замер. Файл никуда не сохраняется."""
    host = urllib.parse.urlsplit(url).hostname or ""
    dns_ms, addr = _resolve(host)
    if dns_ms is None:
        return {"ok": False, "error": addr, "dns_ms": None}

    request = urllib.request.Request(
        url,
        headers={"Range": f"bytes=0-{CHUNK - 1}", "User-Agent": "trix-net-probe/1"},
    )
    ctx = ssl.create_default_context()
    read = 0
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=ctx) as resp:
            first_byte = None
            while read < CHUNK:
                block = resp.read(256 * 1024)
                if not block:
                    break
                if first_byte is None:
                    first_byte = (time.monotonic() - start) * 1000
                read += len(block)
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        return {"ok": False, "error": str(exc)[:90], "dns_ms": dns_ms, "addr": addr}

    elapsed = time.monotonic() - start
    if elapsed <= 0 or read == 0:
        return {"ok": False, "error": "пустой ответ", "dns_ms": dns_ms, "addr": addr}
    return {
        "ok": True,
        "mbps": read / elapsed / 1024 / 1024,
        "read_mb": read / 1024 / 1024,
        "seconds": elapsed,
        "dns_ms": dns_ms,
        "ttfb_ms": first_byte,
        "addr": addr,
    }


def run_round(verbose: bool = True) -> dict:
    """Один проход по всем источникам.

    Повтор ровно один и только после ошибки: разовый обрыв TLS случается и
    на здоровом канале, а инструмент, который из-за него объявляет источник
    недоступным, отправит техников чинить не то. Медленный, но успешный
    замер не повторяется никогда — иначе мы бы мерили не канал, а удачу.
    """
    results = {}
    for name, url, kind in SOURCES:
        if verbose:
            print(f"  {name:<34} …", end="", flush=True, file=sys.stderr)
        res = measure(url)
        if not res["ok"]:
            time.sleep(2)
            retried = measure(url)
            if retried["ok"]:
                retried["note"] = f"со второй попытки (первая: {res['error']})"
            res = retried
        res["kind"] = kind
        results[name] = res
        if verbose:
            if res["ok"]:
                tail = f"   {res.get('note', '')}".rstrip()
                line = (f"  {name:<34} {res['mbps']:6.1f} МБ/с   "
                        f"({res['read_mb']:.0f} МБ за {res['seconds']:.1f} с){tail}")
            else:
                line = f"  {name:<34} ОШИБКА: {res['error']}"
            print("\r" + " " * 44, end="", file=sys.stderr)
            print(line)
    return results


def verdict(rounds: list[dict]) -> list[str]:
    """Короткий вывод, который можно переслать техподдержке.

    Считает по ХУДШЕМУ проходу, а не по среднему. Для установки важна не
    медиана, а та минута, в которую она попала: сколько канал дал тогда,
    столько она и заняла. Первая версия сравнивала медианы и на данных
    2026-09-08 написала «канал здоров», хотя PyPI в одном из трёх проходов
    просел с 14.7 до 1.6 МБ/с — а это ровно тот провал, из-за которого одна
    установка идёт 2 минуты, а следующая 11.
    """
    def stats(name: str):
        vals = [r[name]["mbps"] for r in rounds if r.get(name, {}).get("ok")]
        if not vals:
            return None
        return {"min": min(vals), "med": statistics.median(vals),
                "max": max(vals), "n": len(vals)}

    install = {n: stats(n) for n, _, k in SOURCES if k == "install"}
    reference = {n: stats(n) for n, _, k in SOURCES if k == "reference"}
    domestic = next((v for n, v in reference.items() if "РФ" in n), None)

    inst = {n: v for n, v in install.items() if v}
    lines = []
    if not inst:
        return ["ВЕРДИКТ: ни один источник не ответил — проверьте связность машины."]

    worst_med = statistics.median([v["min"] for v in inst.values()])
    swings = {n: v for n, v in inst.items()
              if v["n"] >= 2 and v["max"] > 4 * max(v["min"], 0.1)}

    dom_ok = domestic and domestic["min"] >= 10

    if worst_med < 3 and not dom_ok:
        lines.append("ВЕРДИКТ: медленно ВЕЗДЕ, включая точку отсчёта внутри страны.")
        lines.append("Это полоса машины, а не маршрут до конкретной сети.")
        lines.append("Вопрос провайдеру: полоса на этой VM и загрузка узла.")
    elif dom_ok and swings:
        lines.append("ВЕРДИКТ: полоса есть, но международный транзит НЕСТАБИЛЕН.")
        lines.append(f"Внутри страны ровно (не ниже {domestic['min']:.0f} МБ/с), "
                     "а внешние источники проваливаются и восстанавливаются.")
        lines.append("Именно это и растягивает установку: она занимает столько,")
        lines.append("сколько канал дал в ТУ минуту, а не в среднем.")
        lines.append("Вопрос провайдеру: транзит и пиринг до зарубежных сетей.")
    elif dom_ok and worst_med < 3:
        lines.append("ВЕРДИКТ: внутри страны быстро, наружу медленно во всех проходах.")
        lines.append("Это маршрут или пиринг до зарубежных сетей, не полоса.")
        lines.append("Вопрос провайдеру: транзит до этих сетей.")
    else:
        lines.append("ВЕРДИКТ: за время замера провалов не было.")
        lines.append("Провал плавающий — повторите сериями в разное время: --rounds 6")

    if swings:
        lines.append("")
        lines.append("Провалы между проходами (это и ломает установку):")
        for n, v in sorted(swings.items(), key=lambda kv: kv[1]["min"]):
            lines.append(f"  {n}: проседал до {v['min']:.1f} МБ/с "
                         f"при {v['max']:.1f} в лучшем проходе")

    lines.append("")
    lines.append("Худшая минута по каждому источнику (по ней и считается установка):")
    for n, v in sorted(inst.items(), key=lambda kv: kv[1]["min"]):
        lines.append(f"  {n:<34} {v['min']:6.1f} МБ/с")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Замер каналов, от которых зависит установка Trix Agent")
    ap.add_argument("--rounds", type=int, default=1, help="сколько проходов")
    ap.add_argument("--pause", type=int, default=30, help="пауза между проходами, с")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args()

    verbose = not args.json
    if verbose:
        print(f"Замер каналов установки. Проходов: {args.rounds}, "
              f"по {CHUNK // 1024 // 1024} МБ с источника.")
        print("Ничего не устанавливается и не сохраняется.\n")

    rounds = []
    for i in range(args.rounds):
        if verbose and args.rounds > 1:
            print(f"── проход {i + 1} из {args.rounds} "
                  f"({time.strftime('%H:%M:%S')}) ──")
        rounds.append(run_round(verbose))
        if i + 1 < args.rounds:
            if verbose:
                print(f"  пауза {args.pause} с…\n")
            time.sleep(args.pause)

    if args.json:
        print(json.dumps({"rounds": rounds, "verdict": verdict(rounds)},
                         ensure_ascii=False, indent=2))
        return 0

    print()
    for line in verdict(rounds):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
