#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разовый диагностический скрипт (не часть бота).

Открывает страницу /protocol конкретной игры в настоящем браузере
(с выполнением JS) и печатает в лог всё, что реально есть на странице:
весь видимый текст, а также содержимое любых <table>. Нужен, чтобы
понять, как именно устроена статистика по игрокам на сайте ABL, прежде
чем писать код, который будет её оттуда доставать для бота.

Использование: python inspect_protocol.py <url>
(по умолчанию берёт пример, который прислал Гамлет)
"""
import sys
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://ablforpeople.com/game/158253/protocol"


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Opening: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 2000})
        page.goto(url, wait_until="networkidle", timeout=30000)
        # даём странице ещё немного времени -- если данные подгружаются
        # отдельным запросом уже после networkidle
        page.wait_for_timeout(3000)

        print("\n===== PAGE TITLE =====")
        print(page.title())

        print("\n===== FULL HTML LENGTH =====")
        html = page.content()
        print(len(html))

        print("\n===== VISIBLE BODY TEXT =====")
        print(page.inner_text("body"))

        tables = page.query_selector_all("table")
        print(f"\n===== FOUND {len(tables)} <table> ELEMENTS =====")
        for i, t in enumerate(tables):
            print(f"\n--- table {i} ---")
            print(t.inner_text())

        # на случай если разметка вообще без <table> -- поищем что-то
        # похожее на строки с именами игроков по распространённым словам
        print("\n===== ELEMENTS MENTIONING 'очк' (очки/points) =====")
        hits = page.locator("text=/очк/i")
        count = hits.count()
        print(f"count={count}")
        for i in range(min(count, 40)):
            try:
                print(repr(hits.nth(i).inner_text()))
            except Exception as e:
                print(f"(error reading item {i}: {e})")

        browser.close()


if __name__ == "__main__":
    main()
