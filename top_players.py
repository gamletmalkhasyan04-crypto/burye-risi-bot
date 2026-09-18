#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Топ-3 игрока "Бурые Рыси" по итогам конкретного матча -> пост в Telegram.

Использование:
  python top_players.py <url страницы протокола или страницы игры> [--top N]

Примеры url (оба варианта ок, /protocol подставится сам):
  https://ablforpeople.com/game/158253
  https://ablforpeople.com/game/158253/protocol

Переменные окружения:
  TELEGRAM_BOT_TOKEN  -- токен бота (обязательно, если не DRY_RUN)
  DRY_RUN=1           -- не отправлять в Telegram; вместо этого напечатать в
                         лог (а) итоговый текст поста и (б) JSON с полным
                         разбором обеих команд -- чтобы можно было ГЛАЗАМИ
                         сверить, что парсер понял таблицу правильно, прежде
                         чем реально постить в канал.

Как устроен парсинг (страница рендерится на JS, поэтому open через настоящий
браузер, как в inspect_protocol.py):
  На странице /protocol для каждой из двух команд идёт блок:
    <Название команды>
    <26 строк-подписей колонок: "Очки", "2 ОЧКА", "Поп.", "Брос.", "%", ...>
    <строка-итог по команде (не игрок)>
    <строки игроков: имя + 21 значение>
  Подписи колонок -- фиксированный "якорь", который мы ищем в плоском
  видимом тексте страницы (page.inner_text("body")). Ряд игрока опознаём не
  по фиксированной позиции, а по тому, что все 21 значение после имени
  проходят по типу (число / проценты / мм:сс / +-N) -- так парсер не
  ломается, если строка-итог команды окажется на пару токенов длиннее или
  короче, чем мы предполагаем.

  Если ABL поменяет вёрстку и якорь-заголовки не найдутся -- скрипт НЕ
  постит наугад, а падает с понятной ошибкой (см. safe-guard в main()).
"""
import os
import re
import sys
import json
import argparse

import requests
from playwright.sync_api import sync_playwright

TEAM_NAME = "Бурые Рыси"
TELEGRAM_CHAT = "@burye_risi"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Фиксированная последовательность подписей колонок, как она реально видна
# на странице (см. inspect_protocol.py -> debug-inspect-protocol run).
HEADER_ANCHOR = [
    "Очки",
    "2 ОЧКА", "Поп.", "Брос.", "%",
    "3 ОЧКА", "Поп.", "Брос.", "%",
    "ШТРАФНОЙ", "Поп.", "Брос.", "%",
    "ПОДБОР", "Напад.", "Защ.", "Всего",
    "Передачи", "Блоки", "Перехваты", "Потери",
    "Фолы", "Фолы соперника",
    "Эффектив.", "Мин.", "+-",
]

# Имя колонки -> regex, которому должно соответствовать значение игрока в
# этой позиции. Порядок соответствует HEADER_ANCHOR (без самого "Очки",
# он тоже входит как первая колонка).
COLUMNS = [
    ("pts", r"^-?\d+$"),
    ("fg2_m", r"^\d+$"), ("fg2_a", r"^\d+$"), ("fg2_pct", r"^(\d+(\.\d+)?%|-)$"),
    ("fg3_m", r"^\d+$"), ("fg3_a", r"^\d+$"), ("fg3_pct", r"^(\d+(\.\d+)?%|-)$"),
    ("ft_m", r"^\d+$"), ("ft_a", r"^\d+$"), ("ft_pct", r"^(\d+(\.\d+)?%|-)$"),
    ("reb_off", r"^\d+$"), ("reb_def", r"^\d+$"), ("reb_tot", r"^\d+$"),
    ("ast", r"^\d+$"), ("blk", r"^\d+$"), ("stl", r"^\d+$"), ("tov", r"^\d+$"),
    ("pf", r"^\d+$"), ("pf_drawn", r"^\d+$"),
    ("eff", r"^-?\d+$"),
    ("min", r"^\d{1,3}:\d{2}$|^-$"),
    ("plus_minus", r"^[+-]?\d+$"),
]
ROW_LEN = len(COLUMNS)  # 21

NAME_LIKE_RE = re.compile(r"^[А-ЯЁA-Z][а-яёa-zА-ЯЁA-Z\-.\s]+$")
SCORE_RE = re.compile(r"^(\d{1,3})\s*:\s*(\d{1,3})$")
INT_RE = re.compile(r"^\d+$")


def normalize_url(raw):
    raw = raw.strip().rstrip("/")
    if raw.endswith("/protocol"):
        return raw
    return raw + "/protocol"


def game_base_url(protocol_url):
    return protocol_url[: -len("/protocol")] if protocol_url.endswith("/protocol") else protocol_url


def fetch_body_lines(url, timeout_ms=30000):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 2000})
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            text = page.inner_text("body")
        finally:
            browser.close()
    return [l.strip() for l in text.split("\n") if l.strip()]


def find_header_anchors(lines):
    """Вернуть список индексов i, где lines[i:i+len(HEADER_ANCHOR)] ==
    HEADER_ANCHOR -- по одному на каждую команду."""
    n = len(HEADER_ANCHOR)
    anchors = []
    for i in range(len(lines) - n + 1):
        if lines[i:i + n] == HEADER_ANCHOR:
            anchors.append(i)
    return anchors


def _try_values(lines, s):
    """Проверить, что lines[s:s+ROW_LEN] -- валидный набор значений строки
    игрока (по типу каждой колонки). Вернуть dict или None."""
    if s + ROW_LEN > len(lines):
        return None
    values = lines[s: s + ROW_LEN]
    for (col_name, pattern), val in zip(COLUMNS, values):
        if not re.match(pattern, val):
            return None
    return {col_name: val for (col_name, _), val in zip(COLUMNS, values)}


def parse_player_row(lines, start):
    """Попробовать разобрать строку игрока начиная с lines[start] (имя).
    Вернуть (player_dict, следующий_индекс) или None, если не подошло.

    На странице ABL у ЧАСТИ игроков после имени идёт ещё строка с амплуа
    ("Разыгрывающий защитник", "Легкий форвард" и т.п.), у остальных --
    нет. Поэтому пробуем сначала без неё, а если не сошлось по типам --
    считаем следующую строку амплуа и пробуем ещё раз, сдвинувшись на 1.
    """
    if start >= len(lines):
        return None
    name = lines[start]
    if not NAME_LIKE_RE.match(name):
        return None

    stats = _try_values(lines, start + 1)
    if stats is not None:
        row = {"name": name}
        row.update(stats)
        return row, start + 1 + ROW_LEN

    # Возможно, lines[start + 1] -- строка амплуа (не число, не проценты,
    # не мм:сс) -- пробуем пропустить её и разобрать статистику дальше.
    if start + 1 < len(lines) and not re.match(COLUMNS[0][1], lines[start + 1]):
        stats = _try_values(lines, start + 2)
        if stats is not None:
            row = {"name": name, "position": lines[start + 1]}
            row.update(stats)
            return row, start + 2 + ROW_LEN

    return None


def parse_team_block(lines, header_idx, stop_before):
    """header_idx -- индекс начала HEADER_ANCHOR. stop_before -- индекс,
    дальше которого не читаем (следующий якорь или конец текста)."""
    team_name = lines[header_idx - 1] if header_idx > 0 else "?"
    j = header_idx + len(HEADER_ANCHOR)
    players = []
    # Пропускаем строку-итог команды и любой "мусор" между заголовками и
    # первым игроком: пробуем распознать строку игрока в каждой позиции,
    # пока не найдём совпадение или не упрёмся в stop_before / разумный лимит.
    scan_limit = min(stop_before, len(lines))
    while j < scan_limit:
        parsed = parse_player_row(lines, j)
        if parsed:
            player, j = parsed
            players.append(player)
        else:
            j += 1
    return team_name, players


def parse_protocol(lines):
    anchors = find_header_anchors(lines)
    if len(anchors) < 2:
        raise RuntimeError(
            f"Не нашёл 2 блока статистики команд на странице (нашёл {len(anchors)}). "
            "Возможно, ABL поменял вёрстку -- нужно перезапустить inspect_protocol.py "
            "и поправить HEADER_ANCHOR."
        )
    teams = {}
    for idx, header_idx in enumerate(anchors):
        stop_before = anchors[idx + 1] - 1 if idx + 1 < len(anchors) else len(lines)
        team_name, players = parse_team_block(lines, header_idx, stop_before)
        teams[team_name] = players
    return teams


def parse_game_summary(lines):
    """Найти "Команда А, счётА, 1, X:Y, 2, X:Y, ..., Команда Б, счётБ" --
    возвращает (team_a, score_a, team_b, score_b) или (None, None, None, None)."""
    for i in range(len(lines) - 1):
        if not INT_RE.match(lines[i]):
            continue
        # lines[i-1] похоже на название команды, lines[i] -- её счёт
        if i == 0:
            continue
        team_a = lines[i - 1]
        score_a = lines[i]
        j = i + 1
        # период-блоки: "1", "19 : 9", "2", "13 : 11", ...
        while j + 1 < len(lines) and INT_RE.match(lines[j]) and SCORE_RE.match(lines[j + 1]):
            j += 2
        if j < len(lines) and j + 1 < len(lines) and NAME_LIKE_RE.match(lines[j]) and INT_RE.match(lines[j + 1]):
            team_b = lines[j]
            score_b = lines[j + 1]
            if team_b != team_a:
                return team_a, int(score_a), team_b, int(score_b)
    return None, None, None, None


def pick_top(players, top_n):
    def key(p):
        return (int(p["eff"]), int(p["pts"]))
    return sorted(players, key=key, reverse=True)[:top_n]


MEDALS = ["🥇", "🥈", "🥉", "4.", "5."]


def build_post(our_team, players, opponent, our_score, opp_score, url):
    if our_score is not None and opp_score is not None:
        if our_score > opp_score:
            result_line = f"🟢 Победа {our_score}:{opp_score}"
        elif our_score < opp_score:
            result_line = f"🔴 Поражение {our_score}:{opp_score}"
        else:
            result_line = f"⚪ Ничья {our_score}:{opp_score}"
        header = f"{result_line} над «{opponent}»" if opponent else result_line
    else:
        header = f"🏀 {our_team}" + (f" — {opponent}" if opponent else "")

    lines = [f"⭐ Топ-3 игрока «{our_team}»", header, ""]
    for medal, p in zip(MEDALS, players):
        lines.append(
            f"{medal} {p['name']} — {p['pts']} очков, {p['reb_tot']} подборов, "
            f"{p['ast']} передач, эффективность {p['eff']} (мин. {p['min']})"
        )
    lines.append("")
    if url:
        lines.append(url)
    lines.append("#БурыеРыси #ABL")
    return "\n".join(lines)


def send_telegram_message(token, text):
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    ok = False
    try:
        data = resp.json()
        ok = data.get("ok", False)
        if not ok:
            print("Telegram API error:", data, file=sys.stderr)
    except ValueError:
        print("Telegram API non-JSON response:", resp.status_code, resp.text, file=sys.stderr)
    resp.raise_for_status()
    if not ok:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL страницы игры или протокола (ablforpeople.com/game/<id>[/protocol])")
    parser.add_argument("--top", type=int, default=3)
    args = parser.parse_args()

    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token and not dry_run:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    protocol_url = normalize_url(args.url)
    print(f"Opening: {protocol_url}")
    lines = fetch_body_lines(protocol_url)

    teams = parse_protocol(lines)
    print("----- PARSED TEAMS (для проверки) -----")
    print(json.dumps(teams, ensure_ascii=False, indent=2))
    print("----------------------------------------")

    if TEAM_NAME not in teams:
        print(
            f"ERROR: команда '{TEAM_NAME}' не найдена среди разобранных команд: "
            f"{list(teams.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)

    our_players = teams[TEAM_NAME]
    if not our_players:
        print(f"ERROR: у команды '{TEAM_NAME}' не нашлось ни одного игрока", file=sys.stderr)
        sys.exit(1)

    top = pick_top(our_players, args.top)

    team_a, score_a, team_b, score_b = parse_game_summary(lines)
    opponent = our_score = opp_score = None
    if team_a and team_b:
        if team_a == TEAM_NAME:
            opponent, our_score, opp_score = team_b, score_a, score_b
        elif team_b == TEAM_NAME:
            opponent, our_score, opp_score = team_a, score_b, score_a

    post_text = build_post(TEAM_NAME, top, opponent, our_score, opp_score, game_base_url(protocol_url))

    print("----- POST TEXT -----")
    print(post_text)
    print("----------------------")

    if dry_run:
        print("DRY_RUN=1: сообщение НЕ отправлено в Telegram.")
        return

    send_telegram_message(token, post_text)
    print("Sent to Telegram OK.")


if __name__ == "__main__":
    main()
