#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот для автопостинга новостей БК "Бурые Рыси" (ABL, ablforpeople.com) в Telegram-канал.

Режимы:
  --mode announce  -> анонс игр на ближайшие 7 дней (пн)
  --mode recap     -> итоги игр за последние 7 дней (пт)

Переменные окружения:
  TELEGRAM_BOT_TOKEN  -- токен бота (обязательно)
  DRY_RUN=1           -- собрать и напечатать пост, но не отправлять в Telegram

Заметки / известные ограничения:
  - Сайт ABL не отдаёт публичного JSON API, поэтому парсинг идёт по HTML
    карточек матчей (каждая карточка -- это <a href="/game/ID">...</a>).
    Если ABL поменяет вёрстку, парсер может перестать находить игры --
    в этом случае скрипт просто ничего не публикует (см. safe-guard ниже)
    и это будет видно в логах workflow-запуска на GitHub.
  - Автоматическая публикация фото пока НЕ реализована: у ABL нет надёжного
    отдельного фотогалереи с привязкой к матчу (в основном видео), а постить
    случайные/неверные картинки в канал клуба -- плохая идея. Если у клуба
    есть свой источник фото (Google Drive, Я.Диск и т.п.), это можно добавить
    отдельно.
"""

import os
import re
import sys
import argparse
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

TEAM_ID = 13921
TEAM_NAME = "Бурые Рыси"
TEAM_URL = f"https://ablforpeople.com/team/{TEAM_ID}"
TELEGRAM_CHAT = "@burye_risi"

MSK = timezone(timedelta(hours=3))

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
RU_MONTHS_NOM = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

DATE_RE = re.compile(r"(\d{1,2})\s+([а-яёА-ЯЁ]+),(\d{1,2}):(\d{2})")
ROUND_RE = re.compile(r"^\d+\s*тур", re.IGNORECASE)
SCORE_RE = re.compile(r"^(\d{1,3}):(\d{1,3})$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def fetch_team_page():
    resp = requests.get(TEAM_URL, headers=HEADERS, timeout=20)
    print(f"DEBUG: HTTP status = {resp.status_code}")
    print(f"DEBUG: HTML length = {len(resp.text)}")
    print(f"DEBUG: HTML start = {resp.text[:500]!r}")
    resp.raise_for_status()
    return resp.text


def parse_card(a_tag, now):
    text = a_tag.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None

    href = a_tag.get("href", "")
    if href and not href.startswith("http"):
        href = "https://ablforpeople.com" + href

    date_idx = None
    for i, l in enumerate(lines):
        if DATE_RE.search(l):
            date_idx = i
            break
    if date_idx is None:
        return None

    m = DATE_RE.search(lines[date_idx])
    day, month_name, hh, mm = m.groups()
    month = RU_MONTHS.get(month_name.lower())
    if not month:
        return None

    year = now.year
    try:
        game_dt = datetime(year, month, int(day), int(hh), int(mm), tzinfo=MSK)
    except ValueError:
        return None

    delta_days = (game_dt - now).days
    if delta_days < -200:
        game_dt = game_dt.replace(year=year + 1)
    elif delta_days > 200:
        game_dt = game_dt.replace(year=year - 1)

    division = lines[0] if date_idx >= 1 else ""

    rest = lines[date_idx + 1:]
    round_idx = None
    for i, l in enumerate(rest):
        if ROUND_RE.match(l):
            round_idx = i
            break
    if round_idx is None:
        return None

    matchup_lines = rest[:round_idx]
    round_label = rest[round_idx]

    score_a = score_b = None
    team_a = team_b = None

    if len(matchup_lines) == 1 and "-" in matchup_lines[0]:
        parts = matchup_lines[0].split("-", 1)
        if len(parts) == 2:
            team_a, team_b = parts[0].strip(), parts[1].strip()
    elif len(matchup_lines) >= 3:
        team_a = matchup_lines[0]
        sm = SCORE_RE.match(matchup_lines[1])
        team_b = matchup_lines[2]
        if sm:
            score_a, score_b = int(sm.group(1)), int(sm.group(2))

    if not team_a or not team_b:
        return None

    return {
        "division": division,
        "datetime": game_dt,
        "team_a": team_a,
        "team_b": team_b,
        "score_a": score_a,
        "score_b": score_b,
        "round": round_label,
        "url": href,
    }


def collect_games(html):
    soup = BeautifulSoup(html, "html.parser")
    now = datetime.now(MSK)
    games = []
    seen_urls = set()
    for a in soup.find_all("a", href=re.compile(r"/game/\d+")):
        g = parse_card(a, now)
        if g and g["url"] not in seen_urls:
            seen_urls.add(g["url"])
            games.append(g)
    return games


def team_involved(game):
    return TEAM_NAME in (game["team_a"], game["team_b"])


def is_upcoming(game):
    return game["score_a"] is None or game["score_b"] is None


def fmt_date(dt):
    return f"{dt.day} {RU_MONTHS_NOM[dt.month]}"


def build_announce_text(games):
    now = datetime.now(MSK)
    window_end = now + timedelta(days=7)
    upcoming = [
        g for g in games
        if team_involved(g) and is_upcoming(g) and now <= g["datetime"] <= window_end
    ]
    upcoming.sort(key=lambda g: g["datetime"])
    if not upcoming:
        return None

    if len(upcoming) == 1:
        g = upcoming[0]
        opponent = g["team_b"] if g["team_a"] == TEAM_NAME else g["team_a"]
        text = (
            f"🏀 На этой неделе играем!\n\n"
            f"📅 {fmt_date(g['datetime'])}, {g['datetime'].strftime('%H:%M')} МСК\n"
            f"⚔️ {TEAM_NAME} — {opponent}\n"
            f"🏆 {g['round']} ({g['division']})\n\n"
            f"Приходите поддержать «{TEAM_NAME}»! 🐾\n"
            f"{g['url']}"
        )
        return text

    lines = ["🏀 Игры «Бурые Рыси» на этой неделе:\n"]
    for g in upcoming:
        opponent = g["team_b"] if g["team_a"] == TEAM_NAME else g["team_a"]
        lines.append(
            f"📅 {fmt_date(g['datetime'])}, {g['datetime'].strftime('%H:%M')} МСК "
            f"— {opponent} ({g['round']})"
        )
    lines.append(f"\nПриходите поддержать «{TEAM_NAME}»! 🐾")
    return "\n".join(lines)


def build_recap_text(games):
    now = datetime.now(MSK)
    window_start = now - timedelta(days=7)
    played = [
        g for g in games
        if team_involved(g) and not is_upcoming(g) and window_start <= g["datetime"] <= now
    ]
    played.sort(key=lambda g: g["datetime"])
    if not played:
        return None

    blocks = []
    for g in played:
        if g["team_a"] == TEAM_NAME:
            our_score, opp_score, opponent = g["score_a"], g["score_b"], g["team_b"]
        else:
            our_score, opp_score, opponent = g["score_b"], g["score_a"], g["team_a"]

        if our_score > opp_score:
            result = "🟢 Победа"
        elif our_score < opp_score:
            result = "🔴 Поражение"
        else:
            result = "⚪ Ничья"

        blocks.append(
            f"{result}\n"
            f"🏀 {TEAM_NAME} {our_score}:{opp_score} {opponent}\n"
            f"🏆 {g['round']} ({g['division']}) · {fmt_date(g['datetime'])}\n"
            f"{g['url']}"
        )

    text = "\n\n".join(blocks) + f"\n\n#БурыеРыси #ABL"
    return text


def send_telegram_message(token, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "disable_web_page_preview": True,
        },
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
    parser.add_argument("--mode", choices=["announce", "recap"], required=True)
    args = parser.parse_args()

    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token and not dry_run:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    try:
        html = fetch_team_page()
    except Exception as e:
        print(f"Failed to fetch team page: {e}", file=sys.stderr)
        sys.exit(1)

    games = collect_games(html)
    print(f"Parsed {len(games)} game cards from team page.")

    if args.mode == "announce":
        text = build_announce_text(games)
    else:
        text = build_recap_text(games)

    if not text:
        print(f"No content to post for mode={args.mode}. Skipping (safe default, nothing sent).")
        return

    print("----- POST TEXT -----")
    print(text)
    print("----------------------")

    if dry_run:
        print("DRY_RUN=1: сообщение НЕ отправлено в Telegram.")
        return

    send_telegram_message(token, text)
    print("Sent to Telegram OK.")


if __name__ == "__main__":
    main()

