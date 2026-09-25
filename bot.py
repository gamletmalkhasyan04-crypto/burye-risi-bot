#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот для автопостинга новостей БК "Бурые Рыси" (ABL, ablforpeople.com) в Telegram-канал.

Режимы:
  --mode announce  -> анонс игр на ближайшие 7 дней (пятница)
  --mode recap     -> итоги игр за последние 7 дней + топ-3 игрока
                      "Бурые Рыси" по каждой игре, по данным со страницы
                      /protocol (понедельник)

Переменные окружения:
  TELEGRAM_BOT_TOKEN  -- токен бота (обязательно)
  DRY_RUN=1           -- собрать и напечатать пост, но не отправлять в Telegram
  ANTHROPIC_API_KEY   -- необязательно. Если задан, живой текст поста
                         (заголовок/вступление/концовка) пишет Claude под
                         конкретный случай (соперник/результат/лидеры матча).
                         Если не задан или запрос не удался -- пост всё
                         равно уходит, но с более простым фиксированным
                         текстом (см. ANNOUNCE_SYSTEM_PROMPT/RECAP_SYSTEM_PROMPT
                         и *_static ниже). Модель по умолчанию задаётся
                         CLAUDE_TEXT_MODEL -- поменяйте, если понадобится
                         (см. https://docs.claude.com/en/docs/about-claude/models).
  CLAUDE_TEXT_MODEL   -- необязательно, id модели для генерации текста
                         (по умолчанию см. CLAUDE_TEXT_MODEL ниже в коде)

Заметки / известные ограничения:
  - Сайт ABL не отдаёт публичного JSON API, поэтому парсинг идёт по HTML
    карточек матчей (каждая карточка -- это <a href="/game/ID">...</a>).
    Если ABL поменяет вёрстку, парсер может перестать находить игры --
    в этом случае скрипт просто ничего не публикует (см. safe-guard ниже)
    и это будет видно в логах workflow-запуска на GitHub.
  - К каждому посту прикладывается скриншот карточки игры с сайта ABL
    (см. screenshot_game_card) -- рендерится в 3x разрешении для чёткости.
    Если скриншот не удаётся сделать, бот присылает обычный текст вместо
    того, чтобы промолчать (см. build_announce_text / build_recap_fallback_text).
"""

import os
import re
import sys
import json
import time
import argparse
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup, NavigableString

TEAM_ID = 13921
TEAM_NAME = "Бурые Рыси"
TEAM_URL = f"https://ablforpeople.com/team/{TEAM_ID}"
TELEGRAM_CHAT = "@burye_risi"
TOP_PLAYERS_N = 3

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


# ABL сейчас рендерит дату и время как ДВА отдельных текстовых узла
# ("19 сентября," и "18:20"), а не одной строкой "19 сентября,18:20" --
# поэтому дата и время матчатся отдельными regex-ами по соседним строкам.
DATE_RE = re.compile(r"^(\d{1,2})\s+([а-яёА-ЯЁ]+),?$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
ROUND_RE = re.compile(r"^\d+\s*тур", re.IGNORECASE)
SCORE_RE = re.compile(r"^(\d{1,3}):(\d{1,3})$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# ----------------------------------------------------------------------------
# Разбор статистики игроков со страницы /protocol (для recap).
#
# Логика перенесена и проверена в top_players.py на реальном логе
# debug-inspect-protocol (игра 158253) -- см. историю там же для деталей.
# У ABL нет <table> в вёрстке, только плоский видимый текст страницы, где
# на каждую из двух команд идёт: [Название команды], 26 строк-подписей
# колонок (HEADER_ANCHOR), строка-итог команды, затем строки игроков
# (иногда с необязательной строкой амплуа между именем и статистикой).
# ----------------------------------------------------------------------------

PLAYER_STATS_HEADER_ANCHOR = [
    "Очки",
    "2 ОЧКА", "Поп.", "Брос.", "%",
    "3 ОЧКА", "Поп.", "Брос.", "%",
    "ШТРАФНОЙ", "Поп.", "Брос.", "%",
    "ПОДБОР", "Напад.", "Защ.", "Всего",
    "Передачи", "Блоки", "Перехваты", "Потери",
    "Фолы", "Фолы соперника",
    "Эффектив.", "Мин.", "+-",
]

PLAYER_STATS_COLUMNS = [
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
PLAYER_STATS_ROW_LEN = len(PLAYER_STATS_COLUMNS)  # 22

PLAYER_NAME_RE = re.compile(r"^[А-ЯЁA-Z][а-яёa-zА-ЯЁA-Z\-.\s]+$")


def find_player_stats_header_anchors(lines):
    n = len(PLAYER_STATS_HEADER_ANCHOR)
    return [
        i for i in range(len(lines) - n + 1)
        if lines[i:i + n] == PLAYER_STATS_HEADER_ANCHOR
    ]


def _try_player_stats_values(lines, s):
    if s + PLAYER_STATS_ROW_LEN > len(lines):
        return None
    values = lines[s: s + PLAYER_STATS_ROW_LEN]
    for (col_name, pattern), val in zip(PLAYER_STATS_COLUMNS, values):
        if not re.match(pattern, val):
            return None
    return {col_name: val for (col_name, _), val in zip(PLAYER_STATS_COLUMNS, values)}


def parse_player_stats_row(lines, start):
    """Строка игрока = имя [+ необязательная строка амплуа] + 22 значения."""
    if start >= len(lines):
        return None
    name = lines[start]
    if not PLAYER_NAME_RE.match(name):
        return None

    stats = _try_player_stats_values(lines, start + 1)
    if stats is not None:
        return {"name": name, **stats}, start + 1 + PLAYER_STATS_ROW_LEN

    if start + 1 < len(lines) and not re.match(PLAYER_STATS_COLUMNS[0][1], lines[start + 1]):
        stats = _try_player_stats_values(lines, start + 2)
        if stats is not None:
            return {"name": name, **stats}, start + 2 + PLAYER_STATS_ROW_LEN

    return None


def parse_player_stats_team_block(lines, header_idx, stop_before):
    team_name = lines[header_idx - 1] if header_idx > 0 else "?"
    j = header_idx + len(PLAYER_STATS_HEADER_ANCHOR)
    players = []
    scan_limit = min(stop_before, len(lines))
    while j < scan_limit:
        parsed = parse_player_stats_row(lines, j)
        if parsed:
            player, j = parsed
            players.append(player)
        else:
            j += 1
    return team_name, players


def parse_protocol_player_stats(lines):
    """Вернуть {название_команды: [игроки]} для обеих команд, либо кинуть
    RuntimeError, если якоря-заголовки не нашлись (сайт поменял вёрстку)."""
    anchors = find_player_stats_header_anchors(lines)
    if len(anchors) < 2:
        raise RuntimeError(
            f"не нашёл 2 блока статистики команд на /protocol (нашёл {len(anchors)})"
        )
    teams = {}
    for idx, header_idx in enumerate(anchors):
        stop_before = anchors[idx + 1] - 1 if idx + 1 < len(anchors) else len(lines)
        team_name, players = parse_player_stats_team_block(lines, header_idx, stop_before)
        teams[team_name] = players
    return teams


def pick_top_players(players, top_n=TOP_PLAYERS_N):
    return sorted(players, key=lambda p: (int(p["eff"]), int(p["pts"])), reverse=True)[:top_n]


TOP_PLAYER_MEDALS = ["🥇", "🥈", "🥉", "4.", "5."]


def fetch_top_players(game_url, timeout_ms=30000):
    """Открыть /protocol данной игры и вернуть список топ-N игроков
    TEAM_NAME (сырые dict со статистикой), либо None, если что-то пошло не
    так (страница не открылась, не нашли команду и т.п.) -- recap в этом
    случае просто уйдёт без блока статистики, а не сломается целиком."""
    from playwright.sync_api import sync_playwright

    protocol_url = game_url.rstrip("/") + "/protocol"
    try:
        text = None
        last_error = None
        for attempt in range(1, 3):  # 1 повтор при обычном сетевом сбое
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    try:
                        page = browser.new_page(viewport={"width": 1400, "height": 2000})
                        page.goto(protocol_url, wait_until="networkidle", timeout=timeout_ms)
                        page.wait_for_timeout(3000)
                        text = page.inner_text("body")
                    finally:
                        browser.close()
                break
            except Exception as e:
                last_error = e
                print(f"WARN: попытка {attempt}/2 открыть {protocol_url} не удалась: {e}", file=sys.stderr)
        if text is None:
            raise last_error

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        teams = parse_protocol_player_stats(lines)
        our_players = teams.get(TEAM_NAME)
        if not our_players:
            print(
                f"WARN: команда '{TEAM_NAME}' не найдена в статистике {protocol_url} "
                f"(нашлись: {list(teams.keys())})",
                file=sys.stderr,
            )
            return None
        return pick_top_players(our_players)
    except Exception as e:
        print(f"WARN: не удалось получить статистику игроков для {protocol_url}: {e}", file=sys.stderr)
        return None


# ----------------------------------------------------------------------------
# Живой текст поста через Claude API (необязательно, см. ANTHROPIC_API_KEY
# в шапке файла). Жёсткие факты (счёт, дата, имена, цифры статистики)
# ВСЕГДА собираются кодом отдельно и не отдаются модели на откуп -- в
# промпт эти факты передаются готовыми, а модель пишет только заголовок/
# вступление/концовку. Так пост не может случайно получить придуманную
# статистику или серию побед, даже если модель что-то перепутает.
# ----------------------------------------------------------------------------

CLAUDE_TEXT_MODEL = os.environ.get("CLAUDE_TEXT_MODEL", "claude-3-5-sonnet-20241022")

STYLE_NOTE = (
    "Ты ведёшь Telegram-канал баскетбольной команды-любителей «Бурые Рыси» "
    "(лига ABL). Пиши по-русски, энергично, гордо и тепло, как капитан "
    "команды обращается к своим болельщикам: короткие ударные фразы, "
    "обращение \"мы\", немного эмодзи (🔥🐾🏀💪⚡🎯🎉 и похожие, не больше "
    "4-5 на весь текст), никакого канцелярита и никаких хэштегов (их "
    "добавляют отдельно). Используй ТОЛЬКО факты, которые даны в запросе -- "
    "никогда не придумывай счёт, статистику, место в таблице или серию игр, "
    "которых там нет."
)

AI_SECTION_KEYS = ["TITLE", "INTRO", "OUTRO"]

ANNOUNCE_SYSTEM_PROMPT = (
    STYLE_NOTE + "\n\n"
    "Сейчас нужно написать пост-анонс перед игрой (уходит в пятницу перед "
    "выходными). Он должен заряжать болельщиков перед матчем.\n\n"
    "Ответь СТРОГО в этом формате и больше ничего не пиши:\n"
    "TITLE: короткий яркий заголовок с эмодзи по краям (одна строка)\n"
    "INTRO: 2-4 предложения куража перед игрой, не больше ~350 символов\n"
    "OUTRO: одна короткая кричалка-призыв поддержать команду, не больше ~80 символов"
)

RECAP_SYSTEM_PROMPT = (
    STYLE_NOTE + "\n\n"
    "Сейчас нужно написать пост-итог после сыгранного матча (уходит в "
    "понедельник). Опиши, как прошла игра, опираясь только на данные "
    "результата и лидеров матча из запроса.\n\n"
    "Ответь СТРОГО в этом формате и больше ничего не пиши:\n"
    "TITLE: короткий яркий заголовок с эмодзи по краям (одна строка)\n"
    "INTRO: 2-4 предложения о том, как прошла игра, не больше ~350 символов\n"
    "OUTRO: одна короткая фраза-итог/призыв, не больше ~80 символов"
)


def call_claude_for_copy(system_prompt, user_prompt, max_tokens=500):
    """Запросить у Claude API живой текст поста. Возвращает сырой текст
    ответа или None при любой проблеме (нет ключа, сеть, статус, пустой
    ответ) -- вызывающий код в этом случае просто берёт запасной
    статический текст, публикация никогда не блокируется этим шагом."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_TEXT_MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            part.get("text", "") for part in data.get("content", [])
            if part.get("type") == "text"
        )
        return text.strip() or None
    except Exception as e:
        print(f"WARN: вызов Claude API не удался, беру запасной текст: {e}", file=sys.stderr)
        return None


def parse_ai_sections(text, keys=AI_SECTION_KEYS):
    """Достать TITLE:/INTRO:/OUTRO: из ответа модели. Возвращает dict
    только если ВСЕ нужные секции нашлись и не пустые -- иначе None, и
    вызывающий код падает обратно на статический текст (лучше простой
    пост, чем обрезанный/сломанный AI-текст)."""
    if not text:
        return None
    alt = "|".join(keys)
    pattern = re.compile(
        rf"^(?:{alt}):\s*(.*?)(?=^(?:{alt}):|\Z)", re.MULTILINE | re.DOTALL
    )
    labels = re.findall(rf"^({alt}):", text, re.MULTILINE)
    values = pattern.findall(text)
    result = {
        label: value.strip()
        for label, value in zip(labels, values)
    }
    if all(result.get(k) for k in keys):
        return result
    return None


def compute_team_form(games, upto_dt=None):
    """Факты о недавней форме команды по уже сыгранным играм, которые
    реально нашлись на странице команды (это может быть неполная история
    сезона, если сайт что-то не показывает -- поэтому наружу отдаём только
    текущую серию побед/поражений, а не итоговый счёт побед за сезон,
    который легко может оказаться неполным/неверным).

    upto_dt -- учитывать только игры до этого момента включительно
    (для анонса -- без самой предстоящей игры; для recap -- включая
    только что сыгранную). Возвращает None, если сыгранных игр не нашлось."""
    cutoff = upto_dt or datetime.now(MSK)
    played = [
        g for g in games
        if team_involved(g) and not is_upcoming(g) and g["datetime"] <= cutoff
    ]
    if not played:
        return None
    played.sort(key=lambda g: g["datetime"])

    results = []
    for g in played:
        if g["team_a"] == TEAM_NAME:
            us, opp = g["score_a"], g["score_b"]
        else:
            us, opp = g["score_b"], g["score_a"]
        results.append("W" if us > opp else ("L" if us < opp else "D"))

    streak_type = results[-1]
    streak_count = 0
    for r in reversed(results):
        if r == streak_type:
            streak_count += 1
        else:
            break
    return {"games_known": len(played), "streak_type": streak_type, "streak_count": streak_count}


def form_hint_line(form):
    """Одна строка с формой команды для промпта, только если серия из 2+
    игр -- иначе пусто (не стоит упоминания)."""
    if not form or form["streak_count"] < 2:
        return ""
    word = {"W": "побед", "L": "поражений", "D": "ничьих"}[form["streak_type"]]
    return f"Наша текущая серия (по последним известным играм): {form['streak_count']} {word} подряд.\n"


def player_highlights(p):
    """Заметные достижения игрока, которые можно честно посчитать по уже
    распарсенным цифрам (не выдумка модели, а код) -- дабл-дабл/трипл-дабл,
    идеальная точность из-за дуги при разумном числе попыток."""
    try:
        pts, reb, ast = int(p["pts"]), int(p["reb_tot"]), int(p["ast"])
        stl, blk = int(p["stl"]), int(p["blk"])
        fg3_m, fg3_a = int(p["fg3_m"]), int(p["fg3_a"])
    except (KeyError, ValueError):
        return []
    highlights = []
    doubles = sum(1 for v in (pts, reb, ast, stl, blk) if v >= 10)
    if doubles >= 3:
        highlights.append("трипл-дабл")
    elif doubles == 2:
        highlights.append("дабл-дабл")
    if fg3_a >= 3 and fg3_m == fg3_a:
        highlights.append(f"{fg3_m}/{fg3_a} трёхочковых")
    return highlights


def format_top_players_block(top_players):
    out = ["🎯 Лидеры матча:"]
    for medal, p in zip(TOP_PLAYER_MEDALS, top_players):
        bits = f"{p['pts']} очков, {p['reb_tot']} подборов, {p['ast']} передач"
        hl = player_highlights(p)
        if hl:
            bits += " (" + ", ".join(hl) + ")"
        out.append(f"{medal} {p['name']}: {bits}")
    return "\n".join(out)


def generate_announce_ai(g, form):
    opponent = g["team_b"] if g["team_a"] == TEAM_NAME else g["team_a"]
    user_prompt = (
        f"Соперник: {opponent}\n"
        f"Дата и время: {fmt_date(g['datetime'])}, {g['datetime'].strftime('%H:%M')} МСК\n"
        f"{g['round']} ({g['division']})\n"
        f"{form_hint_line(form)}"
        f"Не упоминай точное место/адрес проведения и не выдумывай его -- "
        f"в посте и так будет ссылка с деталями."
    )
    return parse_ai_sections(call_claude_for_copy(ANNOUNCE_SYSTEM_PROMPT, user_prompt))


def generate_recap_ai(g, our_score, opp_score, opponent, result_word, top_players, form):
    player_lines = []
    for p in top_players:
        hl = player_highlights(p)
        extra = f" ({', '.join(hl)})" if hl else ""
        player_lines.append(
            f"- {p['name']}: {p['pts']} очков, {p['reb_tot']} подборов, "
            f"{p['ast']} передач, эффективность {p['eff']}{extra}"
        )
    user_prompt = (
        f"Результат: {result_word} {our_score}:{opp_score} против {opponent}\n"
        f"{g['round']} ({g['division']})\n"
        f"Лидеры матча:\n" + "\n".join(player_lines) + "\n"
        f"{form_hint_line(form)}"
    )
    return parse_ai_sections(call_claude_for_copy(RECAP_SYSTEM_PROMPT, user_prompt))


def fetch_team_page(retries=3, backoff_seconds=8):
    """Скачать страницу команды с несколькими попытками -- ABL иногда
    просто не отвечает вовремя (обычный разовый сетевой сбой), и без
    повторов бот в этом случае ничего не постит до следующего расписания."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(TEAM_URL, headers=HEADERS, timeout=30)
            print(f"DEBUG: HTTP status = {resp.status_code}")
            print(f"DEBUG: HTML length = {len(resp.text)}")
            print(f"DEBUG: HTML start = {resp.text[:500]!r}")
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_error = e
            print(
                f"WARN: попытка {attempt}/{retries} получить страницу команды "
                f"не удалась: {e}",
                file=sys.stderr,
            )
            if attempt < retries:
                time.sleep(backoff_seconds)
    raise last_error


def extract_lines(a_tag):
    """Собрать текстовые "строки" карточки матча в порядке появления.

    Названия команд в вёрстке ABL -- это лого, т.е. <img alt="Название">,
    а не текст. Обычный a_tag.get_text() такие узлы полностью
    игнорирует, поэтому команды нужно доставать отдельно из alt
    у <img>, вперемешку с обычными текстовыми узлами.
    """
    parts = []
    for desc in a_tag.descendants:
        if isinstance(desc, NavigableString):
            t = str(desc).strip()
            if t:
                parts.append(t)
        elif getattr(desc, "name", None) == "img":
            alt = (desc.get("alt") or "").strip()
            if alt:
                parts.append(alt)
    return parts


def parse_card(a_tag, now):
    lines = extract_lines(a_tag)
    if not lines:
        return None

    href_path = a_tag.get("href", "")
    href = href_path
    if href and not href.startswith("http"):
        href = "https://ablforpeople.com" + href

    date_idx = None
    for i in range(len(lines) - 1):
        if DATE_RE.match(lines[i]) and TIME_RE.match(lines[i + 1]):
            date_idx = i
            break
    if date_idx is None:
        return None

    dm = DATE_RE.match(lines[date_idx])
    tm = TIME_RE.match(lines[date_idx + 1])
    day, month_name = dm.groups()
    hh, mm = tm.groups()
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

    rest = lines[date_idx + 2:]
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
        "href_path": href_path,
    }


def collect_games(html):
    soup = BeautifulSoup(html, "html.parser")
    now = datetime.now(MSK)
    games = []
    seen_urls = set()
    candidates = soup.find_all("a", href=re.compile(r"/game/\d+"))
    print(f"DEBUG: found {len(candidates)} candidate <a> tags")
    for i, a in enumerate(candidates[:5]):
        print(f"DEBUG: candidate {i} href={a.get('href')!r}")
        print(f"DEBUG: candidate {i} text={a.get_text(separator=' | ', strip=True)!r}")
    for a in candidates:
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


def select_upcoming(games):
    now = datetime.now(MSK)
    window_end = now + timedelta(days=7)
    upcoming = [
        g for g in games
        if team_involved(g) and is_upcoming(g) and now <= g["datetime"] <= window_end
    ]
    upcoming.sort(key=lambda g: g["datetime"])
    return upcoming


def select_played(games):
    now = datetime.now(MSK)
    window_start = now - timedelta(days=7)
    played = [
        g for g in games
        if team_involved(g) and not is_upcoming(g) and window_start <= g["datetime"] <= now
    ]
    played.sort(key=lambda g: g["datetime"])
    return played


TELEGRAM_CAPTION_SAFE_LIMIT = 1000  # у Telegram лимит 1024 на подпись к фото


def game_caption_announce_static(g):
    """Фиксированный текст без Claude -- запасной вариант, если
    ANTHROPIC_API_KEY не задан, запрос не удался или AI-текст не прошёл
    проверку формата/длины."""
    opponent = g["team_b"] if g["team_a"] == TEAM_NAME else g["team_a"]
    return (
        f"🏀 {TEAM_NAME} — {opponent}\n"
        f"📅 {fmt_date(g['datetime'])}, {g['datetime'].strftime('%H:%M')} МСК\n"
        f"🏆 {g['round']} ({g['division']})\n\n"
        f"Приходите поддержать «{TEAM_NAME}»! 🐾\n"
        f"{g['url']}"
    )


def game_caption_announce(g, form):
    facts = f"📅 {fmt_date(g['datetime'])}, {g['datetime'].strftime('%H:%M')} МСК\n🏆 {g['round']} ({g['division']})"
    ai = generate_announce_ai(g, form)
    if ai:
        caption = f"{ai['TITLE']}\n\n{ai['INTRO']}\n\n{facts}\n\n{ai['OUTRO']}\n{g['url']}"
        if len(caption) <= TELEGRAM_CAPTION_SAFE_LIMIT:
            return caption
        print("WARN: AI-текст анонса вышел за лимит длины, беру запасной", file=sys.stderr)
    return game_caption_announce_static(g)


def result_word_and_emoji(our_score, opp_score):
    if our_score > opp_score:
        return "Победа", "🟢"
    if our_score < opp_score:
        return "Поражение", "🔴"
    return "Ничья", "⚪"


def game_caption_recap_static(g, stats_block):
    our_score, opp_score, opponent = recap_scores(g)
    result, emoji = result_word_and_emoji(our_score, opp_score)
    header = (
        f"{emoji} {result}\n"
        f"🏀 {TEAM_NAME} {our_score}:{opp_score} {opponent}\n"
        f"🏆 {g['round']} ({g['division']}) · {fmt_date(g['datetime'])}"
    )
    stats_part = f"\n\n{stats_block}" if stats_block else ""
    return f"{header}{stats_part}\n\n{g['url']}\n\n#БурыеРыси #ABL"


def recap_scores(g):
    if g["team_a"] == TEAM_NAME:
        return g["score_a"], g["score_b"], g["team_b"]
    return g["score_b"], g["score_a"], g["team_a"]


def game_caption_recap(g, top_players, form):
    our_score, opp_score, opponent = recap_scores(g)
    result, emoji = result_word_and_emoji(our_score, opp_score)
    stats_block = format_top_players_block(top_players) if top_players else None

    if top_players:
        ai = generate_recap_ai(g, our_score, opp_score, opponent, result, top_players, form)
        if ai:
            facts = f"📊 Итог встречи:\n«{TEAM_NAME}» {our_score} : {opp_score} {opponent}"
            parts = [ai["TITLE"], "", ai["INTRO"], "", facts]
            if stats_block:
                parts += ["", stats_block]
            parts += ["", ai["OUTRO"], g["url"], "", "#БурыеРыси #ABL"]
            caption = "\n".join(parts)
            if len(caption) <= TELEGRAM_CAPTION_SAFE_LIMIT:
                return caption
            print("WARN: AI-текст итогов вышел за лимит длины, беру запасной", file=sys.stderr)

    return game_caption_recap_static(g, stats_block)


def build_announce_items(games):
    """Список (игра, подпись) для всех предстоящих игр на ближайшие 7 дней --
    по одной карточке-фото на каждую игру."""
    items = []
    for g in select_upcoming(games):
        form = compute_team_form(games, upto_dt=g["datetime"])
        items.append((g, game_caption_announce(g, form)))
    return items


def build_recap_items(games):
    """Список (игра, подпись) для всех сыгранных за последние 7 дней игр.
    Для каждой игры дополнительно пытаемся достать топ-3 игрока "Бурые
    Рыси" со страницы /protocol -- если не получится, подпись просто
    уйдёт без этого блока (см. fetch_top_players)."""
    items = []
    for g in select_played(games):
        top_players = fetch_top_players(g["url"])
        form = compute_team_form(games, upto_dt=g["datetime"])
        items.append((g, game_caption_recap(g, top_players, form)))
    return items


def build_announce_text(games):
    """Текстовый fallback (без фото) на случай, если скриншот карточки
    не удался -- чтобы канал всё равно получил анонс."""
    upcoming = select_upcoming(games)
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


def build_recap_fallback_text(items):
    """Текстовый fallback (без фото) на случай, если скриншот карточки
    не удался -- чтобы канал всё равно получил дайджест итогов.

    Принимает уже собранные (game, caption) из build_recap_items(), а не
    сами games -- иначе пришлось бы второй раз ходить на /protocol за
    статистикой игроков для каждой игры."""
    if not items:
        return None
    # game_caption_recap() уже содержит хэштеги в конце -- для сводки из
    # нескольких игр оставляем их только один раз, в конце всего текста.
    blocks = [caption.rsplit("\n\n#БурыеРыси #ABL", 1)[0] for _, caption in items]
    return "\n\n".join(blocks) + "\n\n#БурыеРыси #ABL"


def screenshot_game_card(href_path, timeout_ms=30000):
    """Сделать скриншот именно того блока на странице команды, который
    соответствует игре с данным href (например "/game/159011").

    Специально не рисуем карточку сами (шрифты/цвета/лого пришлось бы
    поддерживать вручную), а вместо этого открываем реальную страницу ABL в
    headless-браузере и вырезаем нужный элемент -- так фото всегда будет
    выглядеть так же, как на сайте, даже если ABL поменяет вёрстку/дизайн.
    """
    from playwright.sync_api import sync_playwright

    last_error = None
    for attempt in range(1, 3):  # 1 повтор при обычном сетевом сбое
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    # device_scale_factor рендерит страницу в более высоком
                    # разрешении (как экран "retina"), а не просто растягивает
                    # готовую картинку -- поэтому лого и текст на карточке
                    # получаются чёткими, а не размытыми при увеличении.
                    page = browser.new_page(
                        viewport={"width": 1280, "height": 1600},
                        device_scale_factor=3,
                    )
                    page.goto(TEAM_URL, wait_until="networkidle", timeout=timeout_ms)
                    locator = page.locator(f'a[href="{href_path}"]').first
                    locator.wait_for(state="visible", timeout=timeout_ms)
                    locator.scroll_into_view_if_needed()
                    # даём время дорисоваться лого команд (они у ABL иногда
                    # подгружаются с небольшой задержкой уже после networkidle)
                    page.wait_for_timeout(1000)
                    return locator.screenshot()
                finally:
                    browser.close()
        except Exception as e:
            last_error = e
            print(f"WARN: попытка {attempt}/2 сделать скриншот карточки не удалась: {e}", file=sys.stderr)
    raise last_error


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
    _check_telegram_response(resp)


def _check_telegram_response(resp):
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


def send_telegram_photo(token, photo_bytes, caption):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT, "caption": caption},
        files={"photo": ("game.png", photo_bytes, "image/png")},
        timeout=30,
    )
    _check_telegram_response(resp)


def send_telegram_media_group(token, items):
    """items: список (photo_bytes, caption); отправляется одним альбомом,
    у каждой фотографии своя подпись. Telegram ограничивает альбом 2-10
    вложениями, поэтому лишнее обрезаем (маловероятный край случая)."""
    items = items[:10]
    media = []
    files = {}
    for i, (photo_bytes, caption) in enumerate(items):
        key = f"file{i}"
        media.append({"type": "photo", "media": f"attach://{key}", "caption": caption})
        files[key] = (f"{key}.png", photo_bytes, "image/png")

    url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT, "media": json.dumps(media, ensure_ascii=False)},
        files=files,
        timeout=60,
    )
    _check_telegram_response(resp)


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
        items = build_announce_items(games)
        fallback_text = build_announce_text(games)
    else:
        items = build_recap_items(games)
        fallback_text = build_recap_fallback_text(items)

    if not items:
        print(f"No content to post for mode={args.mode}. Skipping (safe default, nothing sent).")
        return

    print("----- POST ITEMS -----")
    for g, caption in items:
        print(f"* {g['url']}")
        print(caption)
        print()
    print("----------------------")

    # Скриншоты делаем всегда, в том числе и при DRY_RUN=1 -- так можно
    # проверить, что вырезка карточки с сайта реально работает, ещё до того
    # как что-то уйдёт в Telegram. Сохраняем их в screenshots/, чтобы файлы
    # можно было посмотреть как артефакт запуска workflow.
    os.makedirs("screenshots", exist_ok=True)
    photos = []
    for i, (g, caption) in enumerate(items):
        try:
            photo_bytes = screenshot_game_card(g["href_path"])
            fname = f"screenshots/{i:02d}_{g['href_path'].strip('/').replace('/', '_')}.png"
            with open(fname, "wb") as f:
                f.write(photo_bytes)
            print(f"Screenshot OK: {g['url']} -> {fname} ({len(photo_bytes)} bytes)")
        except Exception as e:
            print(f"WARN: failed to screenshot card for {g['url']}: {e}", file=sys.stderr)
            photo_bytes = None
        photos.append((photo_bytes, caption))

    if dry_run:
        print("DRY_RUN=1: сообщение(-я) НЕ отправлены в Telegram.")
        return

    if all(p is None for p, _ in photos):
        # Скриншот совсем не удался (например, сайт изменил вёрстку и
        # селектор перестал находить карточку) -- отправляем обычный текст,
        # чтобы канал всё равно получил анонс/дайджест.
        print("WARN: all screenshots failed, falling back to text-only message.", file=sys.stderr)
        send_telegram_message(token, fallback_text)
    elif len(photos) == 1:
        photo_bytes, caption = photos[0]
        if photo_bytes:
            send_telegram_photo(token, photo_bytes, caption)
        else:
            send_telegram_message(token, caption)
    else:
        media_items = [(p, c) for p, c in photos if p]
        text_only = [c for p, c in photos if not p]
        if media_items:
            send_telegram_media_group(token, media_items)
        for c in text_only:
            send_telegram_message(token, c)

    print("Sent to Telegram OK.")


if __name__ == "__main__":
    main()
