"""
F1/F2 Bulgaria Telegram Bot
Features: Live data (OpenF1), standings, Nikola Tsolov tracking,
          per-user notification preferences, morning briefing.
"""

import logging
import json
import html as _html
import os
import sqlite3
import threading
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta, time as dt_time
import pytz
from typing import Optional, List, Dict, Any, Tuple

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
TOKEN = os.environ.get('F1_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')  # from @BotFather - set F1_BOT_TOKEN

BG_TZ                   = pytz.timezone('Europe/Sofia')
DB_FILE                 = 'f1bot.db'
CACHE_DURATION          = timedelta(hours=6)
REMINDER_CHECK_INTERVAL = 60    # seconds between reminder checks
LIVE_CHECK_INTERVAL     = 90    # seconds between live session polls
MAX_REMINDER_LOOKAHEAD  = 65    # minutes – max window to look ahead for reminders
DEFAULT_REMINDER_MIN    = 30    # default reminder lead time in minutes
TSOLOV_SURNAME          = 'Tsolov'  # match substring (case-insensitive)

BG_DAY_NAMES = {
    0: 'Понеделник', 1: 'Вторник', 2: 'Сряда',
    3: 'Четвъртък',  4: 'Петък',   5: 'Събота', 6: 'Неделя'
}


def esc(s) -> str:
    """Escape a string for Telegram HTML parse mode."""
    return _html.escape(str(s))


def esc(s: object) -> str:
    """Escape dynamic content for Telegram HTML parse mode."""
    return _html.escape(str(s))

# Session filter presets: empty list = all sessions
SESSION_PRESETS = {
    'all':        {'label': 'Всички',     'sessions': []},
    'race_quali': {'label': 'Race+Quali', 'sessions': ['Race','Qualifying','Sprint','Sprint Race','Feature Race']},
    'race_only':  {'label': 'Само Race',  'sessions': ['Race','Feature Race']},
}

# Typical session durations (minutes) used to detect when a session has ended
SESSION_DURATIONS = {
    'FP1': 65, 'FP2': 65, 'FP3': 65,
    'Sprint Qualifying': 30, 'Qualifying': 65,
    'Sprint': 35, 'Race': 130,
    'Practice': 45, 'Sprint Race': 45, 'Feature Race': 65,
}

# ══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY CACHES
# ══════════════════════════════════════════════════════════════════════════════
calendar_cache = {
    'f1': {'data': None, 'last_updated': None},
    'f2': {'data': None, 'last_updated': None},
}
standings_cache = {
    'f1_drivers':      {'data': None, 'last_updated': None},
    'f1_constructors': {'data': None, 'last_updated': None},
    'f2_drivers':      {'data': None, 'last_updated': None},
}
tsolov_cache: Dict = {}   # last known Tsolov stats
live_state: Dict = {
    'is_live':      False,
    'session_key':  None,
    'session_name': None,
    'race_name':    None,
    'drivers':      {},   # driver_number -> {name, abbreviation, team}
}
sent_notifications: set = set()   # prevents duplicate pushes
db_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE – per-user settings
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    with db_lock:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id          INTEGER PRIMARY KEY,
                subscribed       INTEGER DEFAULT 1,
                reminder_min     INTEGER DEFAULT 30,
                session_preset   TEXT    DEFAULT 'all',
                tsolov_alerts    INTEGER DEFAULT 1,
                morning_briefing INTEGER DEFAULT 1,
                results_push     INTEGER DEFAULT 1,
                created_at       TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        con.commit()
        con.close()


def _get_con():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def ensure_user(chat_id: int):
    with db_lock:
        con = _get_con()
        con.execute('INSERT OR IGNORE INTO users (chat_id) VALUES (?)', (chat_id,))
        con.commit()
        con.close()


def get_user(chat_id: int) -> Dict:
    ensure_user(chat_id)
    with db_lock:
        con = _get_con()
        row = con.execute(
            'SELECT subscribed, reminder_min, session_preset, tsolov_alerts, morning_briefing, results_push '
            'FROM users WHERE chat_id = ?', (chat_id,)
        ).fetchone()
        con.close()
    if row:
        return {
            'subscribed':       bool(row[0]),
            'reminder_min':     row[1],
            'session_preset':   row[2],
            'tsolov_alerts':    bool(row[3]),
            'morning_briefing': bool(row[4]),
            'results_push':     bool(row[5]),
        }
    return {'subscribed': False, 'reminder_min': DEFAULT_REMINDER_MIN,
            'session_preset': 'all', 'tsolov_alerts': True,
            'morning_briefing': True, 'results_push': True}


def set_user(chat_id: int, **kwargs):
    ensure_user(chat_id)
    allowed = {'subscribed','reminder_min','session_preset','tsolov_alerts','morning_briefing','results_push'}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    values = list(updates.values()) + [chat_id]
    with db_lock:
        con = _get_con()
        con.execute(f'UPDATE users SET {set_clause} WHERE chat_id = ?', values)
        con.commit()
        con.close()


def get_all_subscribers() -> List[Tuple[int, Dict]]:
    with db_lock:
        con = _get_con()
        rows = con.execute(
            'SELECT chat_id, subscribed, reminder_min, session_preset, '
            'tsolov_alerts, morning_briefing, results_push '
            'FROM users WHERE subscribed = 1'
        ).fetchall()
        con.close()
    return [(r[0], {
        'subscribed': bool(r[1]), 'reminder_min': r[2], 'session_preset': r[3],
        'tsolov_alerts': bool(r[4]), 'morning_briefing': bool(r[5]), 'results_push': bool(r[6]),
    }) for r in rows]


def get_tsolov_subscribers() -> List[int]:
    with db_lock:
        con = _get_con()
        rows = con.execute(
            'SELECT chat_id FROM users WHERE subscribed=1 AND tsolov_alerts=1'
        ).fetchall()
        con.close()
    return [r[0] for r in rows]


def remove_subscriber(chat_id: int):
    set_user(chat_id, subscribed=0)

# ══════════════════════════════════════════════════════════════════════════════
# TIME UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def convert_utc_to_bg(date_str: str, time_str: str) -> str:
    try:
        dt_str = f"{date_str} {time_str}"
        fmt = "%Y-%m-%d %H:%M:%SZ" if time_str.endswith('Z') else "%Y-%m-%d %H:%M:%S"
        return pytz.utc.localize(datetime.strptime(dt_str, fmt)).astimezone(BG_TZ).strftime('%H:%M')
    except Exception as e:
        logger.error(f"convert_utc_to_bg: {e}")
        return "15:00"


def convert_utc_to_bg_dt(date_str: str, time_str: str) -> Optional[datetime]:
    try:
        dt_str = f"{date_str} {time_str}"
        fmt = "%Y-%m-%d %H:%M:%SZ" if time_str.endswith('Z') else "%Y-%m-%d %H:%M:%S"
        return pytz.utc.localize(datetime.strptime(dt_str, fmt)).astimezone(BG_TZ)
    except Exception as e:
        logger.error(f"convert_utc_to_bg_dt: {e}")
        return None


def bg_day(date_obj) -> str:
    return BG_DAY_NAMES[date_obj.weekday()]

# ══════════════════════════════════════════════════════════════════════════════
# CALENDAR  (Jolpica API)
# ══════════════════════════════════════════════════════════════════════════════

def generate_session_times(race_date: str, race_time: str) -> Dict:
    try:
        race_dt = pytz.utc.localize(
            datetime.strptime(f"{race_date} {race_time}", "%Y-%m-%d %H:%M:%SZ")
        ).astimezone(BG_TZ)
        return {
            'FP1':        {'time': (race_dt-timedelta(days=2,hours=1,minutes=30)).strftime('%H:%M'),
                           'datetime_bg': race_dt-timedelta(days=2,hours=1,minutes=30), 'tv': 'Diema Sport'},
            'FP2':        {'time': (race_dt-timedelta(days=2)).strftime('%H:%M'),
                           'datetime_bg': race_dt-timedelta(days=2), 'tv': 'Diema Sport'},
            'FP3':        {'time': (race_dt-timedelta(days=1,hours=1)).strftime('%H:%M'),
                           'datetime_bg': race_dt-timedelta(days=1,hours=1), 'tv': 'Diema Sport'},
            'Qualifying': {'time': (race_dt-timedelta(days=1)).strftime('%H:%M'),
                           'datetime_bg': race_dt-timedelta(days=1), 'tv': 'Diema Sport'},
            'Race':       {'time': race_dt.strftime('%H:%M'),
                           'datetime_bg': race_dt, 'tv': 'Diema Sport'},
        }
    except Exception as e:
        logger.error(f"generate_session_times: {e}")
        return {
            'FP1': {'time': '14:00', 'tv': 'Diema Sport'},
            'FP2': {'time': '17:00', 'tv': 'Diema Sport'},
            'FP3': {'time': '13:00', 'tv': 'Diema Sport'},
            'Qualifying': {'time': '16:00', 'tv': 'Diema Sport'},
            'Race': {'time': '15:00', 'tv': 'Diema Sport'},
        }


def fetch_f1_calendar(year: int = None) -> List[Dict]:
    if year is None:
        year = datetime.now().year
    cache = calendar_cache['f1']
    if cache['data'] and cache['last_updated'] and \
            datetime.now() - cache['last_updated'] < CACHE_DURATION:
        return cache['data']
    try:
        resp = requests.get(f"https://api.jolpi.ca/ergast/f1/{year}.json", timeout=15)
        if resp.status_code != 200:
            return get_fallback_f1_calendar()
        races = resp.json().get('MRData', {}).get('RaceTable', {}).get('Races', [])
        if not races:
            return get_fallback_f1_calendar()

        calendar = []
        for race in races:
            race_time = race.get('time', '15:00:00Z')
            race_date = race.get('date', '')
            is_sprint = 'Sprint' in race and 'ThirdPractice' not in race
            sessions = {}

            def _add(key, api_key, default_time, _sessions=sessions, _race=race):
                if api_key in _race:
                    s = _race[api_key]
                    d, t = s.get('date'), s.get('time', default_time)
                    _sessions[key] = {
                        'time': convert_utc_to_bg(d, t),
                        'datetime_bg': convert_utc_to_bg_dt(d, t),
                        'tv': 'Diema Sport'
                    }

            _add('FP1', 'FirstPractice', '12:00:00Z')
            if is_sprint:
                _add('Sprint Qualifying', 'SecondPractice', '15:00:00Z')
                _add('Sprint', 'Sprint', '15:00:00Z')
            else:
                _add('FP2', 'SecondPractice', '15:00:00Z')
                _add('FP3', 'ThirdPractice', '12:00:00Z')
            _add('Qualifying', 'Qualifying', '15:00:00Z')
            sessions['Race'] = {
                'time': convert_utc_to_bg(race_date, race_time),
                'datetime_bg': convert_utc_to_bg_dt(race_date, race_time),
                'tv': 'Diema Sport'
            }
            if len(sessions) == 1:
                sessions = generate_session_times(race_date, race_time)

            calendar.append({
                'round': int(race.get('round', 0)),
                'country': race.get('raceName', '').replace(' Grand Prix', ''),
                'circuit': race.get('Circuit', {}).get('circuitName', ''),
                'date': race_date, 'time': race_time,
                'sessions': sessions, 'is_sprint': is_sprint,
            })

        cache['data'] = calendar
        cache['last_updated'] = datetime.now()
        logger.info(f"Fetched {len(calendar)} F1 races")
        return calendar
    except Exception as e:
        logger.error(f"fetch_f1_calendar: {e}")
        return get_fallback_f1_calendar()


def fetch_f2_calendar(year: int = None) -> List[Dict]:
    if year is None:
        year = datetime.now().year
    cache = calendar_cache['f2']
    if cache['data'] and cache['last_updated'] and \
            datetime.now() - cache['last_updated'] < CACHE_DURATION:
        return cache['data']
    try:
        f1_cal = fetch_f1_calendar(year)
        f2_hosts = [
            'Bahrain','Sakhir','Saudi Arabia','Jeddah','Melbourne','Australia',
            'Imola','Emilia','Monaco','Monte Carlo','Barcelona','Catalunya',
            'Silverstone','British','Hungaroring','Hungarian','Spa','Belgium',
            'Monza','Italy','Baku','Azerbaijan','Singapore','Austin','Americas',
            'Mexico','Qatar','Abu Dhabi','Yas Marina'
        ]
        calendar, rnd = [], 1
        for race in f1_cal:
            cir, cou = race.get('circuit',''), race.get('country','')
            if any(loc in cir or loc in cou for loc in f2_hosts):
                rd = race['date']
                sat = datetime.strptime(rd, '%Y-%m-%d').date() - timedelta(days=1)
                sun = datetime.strptime(rd, '%Y-%m-%d').date()
                def mk(d, h, m):
                    dt = BG_TZ.localize(datetime(d.year, d.month, d.day, h, m))
                    return {'time': dt.strftime('%H:%M'), 'datetime_bg': dt, 'tv': 'Diema Sport'}
                calendar.append({
                    'round': rnd, 'country': cou, 'circuit': cir,
                    'date': rd, 'is_sprint': False,
                    'sessions': {
                        'Qualifying':   mk(sat, 10,  0),
                        'Sprint Race':  mk(sat, 15, 15),
                        'Feature Race': mk(sun, 11,  5),
                    }
                })
                rnd += 1
        cache['data'] = calendar
        cache['last_updated'] = datetime.now()
        return calendar
    except Exception as e:
        logger.error(f"fetch_f2_calendar: {e}")
        return []


def get_fallback_f1_calendar() -> List[Dict]:
    """Minimal hardcoded fallback – only used when Jolpica API is unreachable."""
    logger.warning("Using fallback F1 calendar (Jolpica unreachable)")
    # 2026 estimated dates – refresh with /refresh when API is back
    races_raw = [
        ("1",  "Australian",     "Albert Park Circuit",      "2026-03-15", "05:00:00Z", False),
        ("2",  "Chinese",        "Shanghai International",   "2026-03-22", "07:00:00Z", False),
        ("3",  "Japanese",       "Suzuka Circuit",           "2026-04-05", "05:00:00Z", False),
        ("4",  "Bahrain",        "Bahrain International",    "2026-04-19", "13:00:00Z", False),
        ("5",  "Saudi Arabian",  "Jeddah Corniche",          "2026-04-26", "17:00:00Z", False),
        ("6",  "Miami",          "Miami International",      "2026-05-10", "19:00:00Z", True),
        ("7",  "Emilia Romagna", "Autodromo Enzo e Dino",    "2026-05-24", "13:00:00Z", False),
        ("8",  "Monaco",         "Circuit de Monaco",        "2026-05-31", "13:00:00Z", False),
        ("9",  "Spanish",        "Circuit de Barcelona",     "2026-06-14", "13:00:00Z", False),
        ("10", "Canadian",       "Circuit Gilles Villeneuve","2026-06-21", "18:00:00Z", False),
        ("11", "Austrian",       "Red Bull Ring",            "2026-06-28", "13:00:00Z", True),
        ("12", "British",        "Silverstone Circuit",      "2026-07-05", "13:00:00Z", False),
        ("13", "Belgian",        "Circuit de Spa",           "2026-07-26", "13:00:00Z", True),
        ("14", "Hungarian",      "Hungaroring",              "2026-08-02", "13:00:00Z", False),
        ("15", "Dutch",          "Circuit Zandvoort",        "2026-08-30", "13:00:00Z", False),
        ("16", "Italian",        "Autodromo Nazionale Monza","2026-09-06", "13:00:00Z", False),
        ("17", "Azerbaijan",     "Baku City Circuit",        "2026-09-20", "11:00:00Z", False),
        ("18", "Singapore",      "Marina Bay Street",        "2026-09-27", "12:00:00Z", False),
        ("19", "United States",  "Circuit of the Americas",  "2026-10-18", "19:00:00Z", True),
        ("20", "Mexico City",    "Autodromo Hermanos",       "2026-10-25", "20:00:00Z", False),
        ("21", "São Paulo",      "Autodromo Jose Carlos",    "2026-11-08", "17:00:00Z", True),
        ("22", "Las Vegas",      "Las Vegas Strip Circuit",  "2026-11-21", "06:00:00Z", False),
        ("23", "Qatar",          "Lusail International",     "2026-11-29", "17:00:00Z", True),
        ("24", "Abu Dhabi",      "Yas Marina Circuit",       "2026-12-06", "13:00:00Z", False),
    ]
    calendar = []
    for rnd, country, circuit, date, time, is_sprint in races_raw:
        calendar.append({
            "round": int(rnd),
            "country": country,
            "circuit": circuit,
            "date": date,
            "time": time,
            "is_sprint": is_sprint,
            "sessions": generate_session_times(date, time),
        })
    return calendar


def get_next_race(calendar: List[Dict]) -> Optional[Dict]:
    today = datetime.now(BG_TZ).date()
    for race in calendar:
        if datetime.strptime(race['date'], '%Y-%m-%d').date() >= today:
            return race
    return None


def get_all_upcoming_sessions(series: str) -> List[Dict]:
    now = datetime.now(BG_TZ)
    cal = fetch_f1_calendar() if series == 'F1' else fetch_f2_calendar()
    items = []
    for race in cal:
        for sname, sinfo in race['sessions'].items():
            dt = sinfo.get('datetime_bg')
            if dt and dt > now:
                items.append({'race': race, 'session_name': sname,
                              'session_info': sinfo, 'dt_bg': dt, 'series': series})
    return sorted(items, key=lambda x: x['dt_bg'])

# ══════════════════════════════════════════════════════════════════════════════
# F1 STANDINGS  (Jolpica API)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_f1_standings(kind: str = 'drivers') -> Optional[List[Dict]]:
    cache_key = f'f1_{kind}'
    cache = standings_cache[cache_key]
    if cache['data'] and cache['last_updated'] and \
            datetime.now() - cache['last_updated'] < CACHE_DURATION:
        return cache['data']
    try:
        year = datetime.now().year
        endpoint = 'driverStandings' if kind == 'drivers' else 'constructorStandings'
        resp = requests.get(f"https://api.jolpi.ca/ergast/f1/{year}/{endpoint}.json", timeout=15)
        if resp.status_code != 200:
            return None
        lists = resp.json().get('MRData', {}).get('StandingsTable', {}).get('StandingsLists', [])
        if not lists:
            return None

        standings = []
        if kind == 'drivers':
            for item in lists[0].get('DriverStandings', []):
                drv = item.get('Driver', {})
                con = (item.get('Constructors') or [{}])[0]
                standings.append({
                    'pos': item.get('position','?'),
                    'driver': f"{drv.get('givenName','')} {drv.get('familyName','')}".strip(),
                    'code': drv.get('code','???'),
                    'team': con.get('name',''),
                    'points': item.get('points','0'),
                    'wins': item.get('wins','0'),
                })
        else:
            for item in lists[0].get('ConstructorStandings', []):
                con = item.get('Constructor', {})
                standings.append({
                    'pos': item.get('position','?'),
                    'team': con.get('name',''),
                    'points': item.get('points','0'),
                    'wins': item.get('wins','0'),
                })

        cache['data'] = standings
        cache['last_updated'] = datetime.now()
        logger.info(f"Fetched F1 {kind} standings ({len(standings)} entries)")
        return standings
    except Exception as e:
        logger.error(f"fetch_f1_standings({kind}): {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# F2 STANDINGS + NIKOLA TSOLOV
# ══════════════════════════════════════════════════════════════════════════════

def fetch_f2_standings() -> Optional[List[Dict]]:
    cache = standings_cache['f2_drivers']
    if cache['data'] and cache['last_updated'] and \
            datetime.now() - cache['last_updated'] < CACHE_DURATION:
        return cache['data']

    year = datetime.now().year
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}

    # Try FIA F2 Umbraco API patterns (common CMS for fiaformula2.com)
    endpoints = [
        f"https://www.fiaformula2.com/Umbraco/Api/DriverStandingsApi/GetDriverStandings?seasonId={year}",
        f"https://www.fiaformula2.com/Umbraco/Api/RaceResultsApi/GetDriverStandings?seasonId={year}",
        f"https://api.formula2.com/api/standings/driver/{year}",
    ]
    for url in endpoints:
        try:
            resp = requests.get(url, timeout=10, headers=headers)
            if resp.status_code == 200:
                raw = resp.json()
                items = raw if isinstance(raw, list) else \
                    raw.get('DriverStandingsItems', raw.get('Items', raw.get('standings', [])))
                if items:
                    standings = []
                    for i, item in enumerate(items, 1):
                        first = item.get('FirstName', item.get('firstName', item.get('first_name', '')))
                        last  = item.get('LastName',  item.get('lastName',  item.get('last_name',  '')))
                        standings.append({
                            'pos':    str(item.get('Position', item.get('Pos', item.get('position', i)))),
                            'driver': f"{first} {last}".strip() or item.get('name', item.get('Driver', '')),
                            'team':   item.get('TeamName', item.get('teamName', item.get('team', ''))),
                            'points': str(item.get('Points', item.get('points', 0))),
                            'wins':   str(item.get('Wins',   item.get('wins',   0))),
                        })
                    if standings:
                        cache['data'] = standings
                        cache['last_updated'] = datetime.now()
                        logger.info(f"Fetched F2 standings: {len(standings)} drivers")
                        return standings
        except Exception as e:
            logger.debug(f"F2 endpoint {url}: {e}")

    # Fallback: HTML scrape via BeautifulSoup (optional dependency)
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(
            f"https://www.fiaformula2.com/Standings/Driver",
            timeout=10, headers={**headers, 'Accept': 'text/html'}
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table')
            if table:
                standings = []
                for row in table.find_all('tr')[1:]:
                    cols = [td.get_text(strip=True) for td in row.find_all('td')]
                    if len(cols) >= 3:
                        standings.append({
                            'pos': cols[0], 'driver': cols[1],
                            'team': cols[2] if len(cols) > 3 else '',
                            'points': cols[-1], 'wins': '0'
                        })
                if standings:
                    cache['data'] = standings
                    cache['last_updated'] = datetime.now()
                    return standings
    except ImportError:
        logger.info("BeautifulSoup not installed – pip install beautifulsoup4 for HTML scraping")
    except Exception as e:
        logger.warning(f"F2 HTML scrape: {e}")

    logger.warning("F2 standings unavailable from all sources")
    return None


def get_tsolov_entry(standings: List[Dict]) -> Optional[Dict]:
    for e in standings:
        if TSOLOV_SURNAME.lower() in e.get('driver', '').lower():
            return e
    return None


def format_tsolov_info() -> str:
    year = datetime.now().year
    lines = [f"🇧🇬 <b>Никола Цолов — F2 {year}</b>\n"]
    standings = fetch_f2_standings()
    if standings:
        entry = get_tsolov_entry(standings)
        if entry:
            lines.append(f"📊 <b>Класиране:</b> P{esc(entry['pos'])}  |  {esc(entry['points'])} т.  |  {esc(entry['wins'])} победи")
            lines.append(f"🏎️ <b>Отбор:</b> {esc(entry['team'])}\n")
        else:
            lines.append("ℹ️ Цолов не е намерен в текущото класиране.\n")
    else:
        lines.append("⚠️ F2 класирането не е налично в момента.\n")

    if tsolov_cache.get('last_result'):
        lr = tsolov_cache['last_result']
        lines.append(f"🏁 <b>Последен резултат:</b> {esc(lr['session'])} — P{esc(lr['pos'])} ({esc(lr['race'])})")

    cal = fetch_f2_calendar()
    nxt = get_next_race(cal)
    if nxt:
        rd = datetime.strptime(nxt['date'], '%Y-%m-%d')
        days = (rd.date() - datetime.now(BG_TZ).date()).days
        lines.append(f"\n📅 <b>Следващо F2:</b> {esc(nxt['country'])} — {rd.strftime('%d.%m.%Y')}")
        if days == 0:     lines.append("⏰ <b>ДНЕС!</b>")
        elif days == 1:   lines.append("⏰ <b>Утре</b>")
        elif days > 0:    lines.append(f"⏰ След {days} дни")

    if standings:
        lines.append("\n<b>Топ 5 F2:</b><pre>")
        for e in standings[:5]:
            marker = ">> " if TSOLOV_SURNAME.lower() in e.get('driver','').lower() else "   "
            lines.append(f"{marker}P{e['pos']}  {e['driver']:<22}  {e['points']} т.")
        lines.append("</pre>")

    return '\n'.join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# OPENF1 – Live data
# ══════════════════════════════════════════════════════════════════════════════

OPENF1 = "https://api.openf1.org/v1"


def openf1_get(endpoint: str, params: dict = None) -> Optional[Any]:
    try:
        resp = requests.get(f"{OPENF1}/{endpoint}", params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"OpenF1 {endpoint}: {e}")
    return None


def get_live_f1_session() -> Optional[Dict]:
    """Return currently active F1 session from OpenF1, or None."""
    sessions = openf1_get('sessions', {'session_key': 'latest'})
    if not sessions:
        return None
    s = sessions[0] if isinstance(sessions, list) else sessions
    now_utc = datetime.now(pytz.utc)
    try:
        start = datetime.fromisoformat(s['date_start'].replace('Z', '+00:00'))
        end_str = s.get('date_end')
        if end_str:
            end = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            if start <= now_utc <= end:
                return s
        else:
            if start <= now_utc <= start + timedelta(hours=4):
                return s
    except Exception as e:
        logger.warning(f"get_live_f1_session: {e}")
    return None


def fetch_driver_map(session_key) -> Dict[int, Dict]:
    drivers = openf1_get('drivers', {'session_key': session_key})
    result = {}
    if drivers:
        for d in drivers:
            num = d.get('driver_number')
            if num:
                result[int(num)] = {
                    'name':  f"{d.get('first_name','')} {d.get('last_name','')}".strip(),
                    'abbr':  d.get('name_acronym', '???'),
                    'team':  d.get('team_name', ''),
                }
    return result


def fetch_live_positions(session_key) -> List[Dict]:
    data = openf1_get('position', {'session_key': session_key})
    if not data:
        return []
    latest: Dict = {}
    for entry in data:
        num = entry.get('driver_number')
        if num is not None:
            if num not in latest or entry.get('date','') > latest[num].get('date',''):
                latest[num] = entry
    return sorted(latest.values(), key=lambda x: x.get('position', 99))


def fetch_live_intervals(session_key) -> Dict[int, str]:
    data = openf1_get('intervals', {'session_key': session_key})
    result: Dict = {}
    if data:
        latest: Dict = {}
        for entry in data:
            num = entry.get('driver_number')
            if num and (num not in latest or entry.get('date','') > latest[num].get('date','')):
                latest[num] = entry
        for num, entry in latest.items():
            gap = entry.get('gap_to_leader')
            if gap is not None:
                result[int(num)] = f"+{gap:.3f}s" if isinstance(gap, (int,float)) and gap > 0 else "LEADER"
    return result


def format_live_standings(session_key, session_name: str, race_name: str) -> str:
    positions  = fetch_live_positions(session_key)
    intervals  = fetch_live_intervals(session_key)
    driver_map = live_state.get('drivers') or fetch_driver_map(session_key)
    if not positions:
        return f"🔴 <b>{esc(race_name)} — {esc(session_name)}</b>\n\nОще няма данни."
    lines = [f"🔴 <b>LIVE: {esc(race_name)} — {esc(session_name)}</b>\n<pre>"]
    for pe in positions[:20]:
        num  = pe.get('driver_number')
        pos  = pe.get('position', '?')
        info = driver_map.get(int(num) if num else 0, {})
        abbr = info.get('abbr', f"#{num}")
        team = info.get('team', '')[:16]
        gap  = intervals.get(int(num) if num else 0, '')
        gap_str = f"  {gap}" if gap and gap != 'LEADER' else ''
        lines.append(f"P{str(pos):>2}  {abbr:<4}  {team:<16}{gap_str}")
    lines.append(f"</pre>\n<i>Обновено: {datetime.now(BG_TZ).strftime('%H:%M:%S')}</i>")
    return '\n'.join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════

def format_race_info(race: Dict, series: str = "F1") -> str:
    race_date  = datetime.strptime(race['date'], '%Y-%m-%d')
    days_until = (race_date.date() - datetime.now(BG_TZ).date()).days
    is_sprint  = race.get('is_sprint', False)

    text  = f"🏁 <b>{esc(series)} — {esc(race['country'])}</b>\n"
    text += f"📍 {esc(race['circuit'])}\n"
    text += f"📅 {race_date.strftime('%d.%m.%Y')}\n"

    if days_until == 0:     text += "⏰ <b>ДНЕС!</b>\n\n"
    elif days_until == 1:   text += "⏰ <b>Утре</b>\n\n"
    elif days_until > 0:    text += f"⏰ След {days_until} дни\n\n"
    else:                   text += "\n"

    if series == "F1" and is_sprint:
        text += "⚡ <b>Спринт уикенд</b>\n\n"

    text += "<b>Програма (Българско време):</b>\n\n"

    rd = race_date.date()
    if series == "F1":
        fri, sat, sun = rd-timedelta(days=2), rd-timedelta(days=1), rd
        day_map = {fri: [], sat: [], sun: []}
        placement = ({'FP1': fri, 'Sprint Qualifying': fri,
                      'Sprint': sat, 'Qualifying': sat, 'Race': sun}
                     if is_sprint else
                     {'FP1': fri, 'FP2': fri,
                      'FP3': sat, 'Qualifying': sat, 'Race': sun})
        for sn, si in race['sessions'].items():
            day_map[placement.get(sn, sun)].append((sn, si))
    else:
        sat = rd - timedelta(days=1)
        day_map = {sat: [], rd: []}
        for sn, si in race['sessions'].items():
            day_map[sat if sn in ('Practice','Qualifying','Sprint Race') else rd].append((sn, si))

    for day in sorted(day_map):
        sessions = sorted(day_map[day], key=lambda x: x[1]['time'])
        if sessions:
            text += f"<b>{esc(bg_day(day))}, {day.strftime('%d.%m')}</b>\n"
            for sn, si in sessions:
                text += f" • {esc(sn)}: {esc(si['time'])} ч. ({esc(si['tv'])})\n"
            text += "\n"
    return text


def format_f1_standings(standings: List[Dict], kind: str = 'drivers') -> str:
    year = datetime.now().year
    if kind == 'drivers':
        lines = [f"<b>🏆 F1 Пилоти — {year}</b>\n<pre>"]
        for e in standings[:20]:
            lines.append(f"P{str(e['pos']):>2}  {e['code']:<4}  {e['driver']:<22}  {str(e['points']):>5} т.")
        lines.append("</pre>")
    else:
        lines = [f"<b>🏆 F1 Отбори — {year}</b>\n<pre>"]
        for e in standings[:10]:
            lines.append(f"P{str(e['pos']):>2}  {e['team']:<28}  {str(e['points']):>5} т.")
        lines.append("</pre>")
    return '\n'.join(lines)


def format_settings(chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    s = get_user(chat_id)
    preset_label = SESSION_PRESETS.get(s['session_preset'], SESSION_PRESETS['all'])['label']

    text = (
        f"⚙️ <b>Твоите настройки</b>\n\n"
        f"🔔 Абонамент:        {'ВКЛ ✅' if s['subscribed'] else 'ИЗКЛ ❌'}\n"
        f"⏰ Напомняне:        {s['reminder_min']} мин. преди\n"
        f"📋 Сесии:            {esc(preset_label)}\n"
        f"🏎️ Цолов известия:   {'ВКЛ ✅' if s['tsolov_alerts'] else 'ИЗКЛ ❌'}\n"
        f"☀️ Сутрешен брифинг: {'ВКЛ ✅' if s['morning_briefing'] else 'ИЗКЛ ❌'}\n"
        f"📊 Push резултати:   {'ВКЛ ✅' if s['results_push'] else 'ИЗКЛ ❌'}"
    )

    def chk(val, target): return f"✓{target}" if str(val) == str(target) else str(target)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'🔕 Спри известията' if s['subscribed'] else '🔔 Абонирай се'}",
            callback_data='toggle_sub')],
        [InlineKeyboardButton(chk(s['reminder_min'],15)+" мин", callback_data='remind_15'),
         InlineKeyboardButton(chk(s['reminder_min'],30)+" мин", callback_data='remind_30'),
         InlineKeyboardButton(chk(s['reminder_min'],60)+" мин", callback_data='remind_60')],
        [InlineKeyboardButton(('✓ ' if s['session_preset']=='all' else '')+'Всички',       callback_data='filter_all'),
         InlineKeyboardButton(('✓ ' if s['session_preset']=='race_quali' else '')+'Race+Quali', callback_data='filter_race_quali'),
         InlineKeyboardButton(('✓ ' if s['session_preset']=='race_only' else '')+'Race',   callback_data='filter_race_only')],
        [InlineKeyboardButton(f"{'✅' if s['tsolov_alerts'] else '❌'} Цолов известия",    callback_data='toggle_tsolov'),
         InlineKeyboardButton(f"{'✅' if s['morning_briefing'] else '❌'} Брифинг",        callback_data='toggle_briefing')],
        [InlineKeyboardButton(f"{'✅' if s['results_push'] else '❌'} Push резултати",     callback_data='toggle_results')],
        [InlineKeyboardButton("« Главно меню", callback_data='back_to_menu')],
    ])
    return text, keyboard

# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏁 Следващо F1",   callback_data='next_f1'),
         InlineKeyboardButton("🏎️ Следващо F2",  callback_data='next_f2')],
        [InlineKeyboardButton("📅 F1 Календар",  callback_data='full_f1'),
         InlineKeyboardButton("📅 F2 Календар",  callback_data='full_f2')],
        [InlineKeyboardButton("🏆 F1 Пилоти",    callback_data='standings_drivers'),
         InlineKeyboardButton("🏆 F1 Отбори",    callback_data='standings_constructors')],
        [InlineKeyboardButton("🇧🇬 Никола Цолов", callback_data='tsolov'),
         InlineKeyboardButton("🔴 На живо",       callback_data='live')],
        [InlineKeyboardButton("📺 TV канали",     callback_data='tv_info'),
         InlineKeyboardButton("⚙️ Настройки",     callback_data='settings')],
        [InlineKeyboardButton("🔄 Обнови данни",  callback_data='refresh_data')],
    ])

# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED JOBS
# ══════════════════════════════════════════════════════════════════════════════

async def send_to_user(context, chat_id: int, text: str, parse_mode: str = 'HTML') -> bool:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return True
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ('blocked','deactivated','not found','chat not found','forbidden')):
            logger.warning(f"Removing inactive subscriber {chat_id}: {e}")
            remove_subscriber(chat_id)
        else:
            logger.error(f"send_to_user {chat_id}: {e}")
        return False


# ── 1. Smart per-user reminders ───────────────────────────────────────────────
async def job_reminders(context):
    users = get_all_subscribers()
    if not users:
        return
    now = datetime.now(BG_TZ)
    sessions_soon = []
    for series in ('F1', 'F2'):
        for item in get_all_upcoming_sessions(series):
            mins = (item['dt_bg'] - now).total_seconds() / 60
            if 0 < mins <= MAX_REMINDER_LOOKAHEAD:
                sessions_soon.append((item, mins))

    for item, mins_until in sessions_soon:
        race         = item['race']
        session_name = item['session_name']
        series       = item['series']
        race_key     = f"{series}_{race.get('round',0)}"

        for chat_id, settings in users:
            user_min = settings.get('reminder_min', DEFAULT_REMINDER_MIN)
            if abs(mins_until - user_min) > 0.5:   # ±30s window
                continue
            notif_key = f"rem_{chat_id}_{race_key}_{session_name}"
            if notif_key in sent_notifications:
                continue

            # Check user's session filter
            preset  = settings.get('session_preset', 'all')
            allowed = SESSION_PRESETS[preset]['sessions']
            sent_notifications.add(notif_key)
            if allowed and session_name not in allowed:
                continue  # filtered out for this user

            tv = item['session_info'].get('tv', 'Diema Sport')
            text = (
                f"🔔 <b>Напомняне — {esc(series)}</b>\n\n"
                f"🏁 <b>{esc(race['country'])}</b> — {esc(session_name)}\n"
                f"⏰ Започва в <b>{item['dt_bg'].strftime('%H:%M')}</b> "
                f"(след ~{user_min} мин)\n"
                f"📺 На живо: {tv}\n\n"
                "<i>Готови ли сте?</i> 🏎️"
            )
            await send_to_user(context, chat_id, text)


# ── 2. Morning race-day briefing at 09:00 BG ──────────────────────────────────
async def job_morning_briefing(context):
    today = datetime.now(BG_TZ).date()
    sessions_today = []
    for series in ('F1', 'F2'):
        for item in get_all_upcoming_sessions(series):
            if item['dt_bg'].date() == today:
                sessions_today.append(item)
    if not sessions_today:
        return

    lines = ["☀️ <b>Добро утро! Днес има сесии:</b>\n"]
    current_race = None
    for item in sorted(sessions_today, key=lambda x: x['dt_bg']):
        if item['race']['country'] != current_race:
            current_race = item['race']['country']
            lines.append(f"\n🏁 *{item['series']} — {current_race}*")
        lines.append(f" • {item['session_name']}: {item['dt_bg'].strftime('%H:%M')} ч.")
    lines.append("\nХубав ден! 🏎️")
    text = '\n'.join(lines)

    for chat_id, settings in get_all_subscribers():
        if settings.get('morning_briefing', True):
            await send_to_user(context, chat_id, text)


# ── 3. Live session monitor + post-session results push ───────────────────────
async def job_live_monitor(context):
    global live_state
    session = get_live_f1_session()

    if session:
        sk    = session.get('session_key')
        sname = session.get('session_name', '?')
        rname = session.get('meeting_name', session.get('circuit_short_name', '?'))

        if not live_state['is_live']:
            # Session just started
            live_state.update({
                'is_live': True, 'session_key': sk,
                'session_name': sname, 'race_name': rname,
                'drivers': fetch_driver_map(sk),
            })
            notif_key = f"live_start_{sk}"
            if notif_key not in sent_notifications:
                sent_notifications.add(notif_key)
                text = (
                    f"🔴 <b>НА ЖИВО: {esc(rname)} — {esc(sname)}</b>\n\n"
                    "Сесията започна!\n"
                    "Виж класирането с /live\n"
                    "📺 Diema Sport"
                )
                for chat_id, settings in get_all_subscribers():
                    if settings.get('results_push', True):
                        await send_to_user(context, chat_id, text)
    else:
        if live_state['is_live']:
            # Session just ended – push final results
            sk    = live_state['session_key']
            sname = live_state['session_name']
            rname = live_state['race_name']

            notif_key = f"live_result_{sk}"
            if sk and notif_key not in sent_notifications:
                sent_notifications.add(notif_key)
                result_text = format_live_standings(sk, sname, rname)
                result_text = result_text.replace('🔴 <b>LIVE:', '🏁 <b>РЕЗУЛТАТ:')
                for chat_id, settings in get_all_subscribers():
                    if settings.get('results_push', True):
                        await send_to_user(context, chat_id, result_text)

            await _check_tsolov_f2_result(context)

        live_state['is_live'] = False


async def _check_tsolov_f2_result(context):
    """After an F2 session's estimated end time, push Tsolov's result."""
    now = datetime.now(BG_TZ)
    cal = fetch_f2_calendar()
    for race in cal:
        for sname, sinfo in race['sessions'].items():
            dt = sinfo.get('datetime_bg')
            if not dt:
                continue
            duration = SESSION_DURATIONS.get(sname, 60)
            end_time = dt + timedelta(minutes=duration)
            # Session ended in the last 10 minutes
            if timedelta(0) < now - end_time < timedelta(minutes=10):
                notif_key = f"tsolov_{race['round']}_{sname}"
                if notif_key in sent_notifications:
                    continue
                sent_notifications.add(notif_key)

                # Refresh F2 standings
                standings_cache['f2_drivers']['data'] = None
                standings = fetch_f2_standings()
                if not standings:
                    return

                entry = get_tsolov_entry(standings)
                if not entry:
                    return

                tsolov_cache['last_result'] = {
                    'session': sname, 'race': race['country'], 'pos': entry['pos']
                }
                text = (
                    f"🇧🇬 <b>Никола Цолов — {esc(sname)}</b>\n\n"
                    f"🏁 {esc(race['country'])}\n"
                    f"📊 <b>Класиране:</b> P{esc(entry['pos'])}\n"
                    f"🏆 <b>Точки:</b> {esc(entry['points'])}\n"
                    f"🏎️ <b>Отбор:</b> {esc(entry['team'])}"
                )
                for chat_id in get_tsolov_subscribers():
                    await send_to_user(context, chat_id, text)

# ══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_chat.id)
    await update.message.reply_text(
        "🏁 <b>Добре дошли в F1/F2 Bulgaria Bot!</b>\n\n"
        "• 📅 Календар и програма в Българско време\n"
        "• 🏆 F1 класирания (пилоти & отбори)\n"
        "• 🇧🇬 Никола Цолов — F2 фокус\n"
        "• 🔴 Данни на живо (OpenF1 API)\n"
        "• 🔔 Персонализирани напомняния\n"
        "• ☀️ Сутрешен брифинг в гоночни дни\n\n"
        "Изберете опция:",
        reply_markup=get_main_keyboard(), parse_mode='HTML'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🏁 Команди</b>\n\n"
        "/start — Главно меню\n"
        "/next — Следващо F1 състезание\n"
        "/calendar — Календар\n"
        "/standings — F1 класирания\n"
        "/tsolov — Никола Цолов (F2)\n"
        "/live — На живо класиране\n"
        "/settings — Твоите настройки\n"
        "/subscribe — Абонирай се\n"
        "/unsubscribe — Спри известията\n"
        "/refresh — Обнови данните\n"
        "/help — Помощ\n\n"
        "<b>Данни:</b> Jolpica API + OpenF1 API\n"
        "<b>F2 данни:</b> FIA Formula 2 website",
        parse_mode='HTML'
    )


async def next_race_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = get_next_race(fetch_f1_calendar())
    await update.message.reply_text(
        format_race_info(nxt, "F1") if nxt else "Няма предстоящи F1 състезания.",
        parse_mode='HTML'
    )


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("F1", callback_data='full_f1'),
           InlineKeyboardButton("F2", callback_data='full_f2')]]
    await update.message.reply_text("Изберете:", reply_markup=InlineKeyboardMarkup(kb))


async def standings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("🏆 Пилоти",  callback_data='standings_drivers'),
           InlineKeyboardButton("🏆 Отбори",  callback_data='standings_constructors')]]
    await update.message.reply_text("Изберете:", reply_markup=InlineKeyboardMarkup(kb))


async def tsolov_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Зареждане...")
    kb = [[InlineKeyboardButton("🔄 Обнови", callback_data='tsolov'),
           InlineKeyboardButton("« Меню",   callback_data='back_to_menu')]]
    await update.message.reply_text(
        format_tsolov_info(), reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML'
    )


async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_live_f1_session()
    kb = [[InlineKeyboardButton("🔄 Обнови", callback_data='live'),
           InlineKeyboardButton("« Меню",   callback_data='back_to_menu')]]
    if not session:
        items = get_all_upcoming_sessions('F1')
        if items:
            nxt  = items[0]
            mins = int((nxt['dt_bg'] - datetime.now(BG_TZ)).total_seconds() / 60)
            text = (
                f"📡 Няма активна F1 сесия.\n\n"
                f"<b>Следваща:</b> {esc(nxt['race']['country'])} — {esc(nxt['session_name'])}\n"
                f"⏰ {nxt['dt_bg'].strftime('%d.%m %H:%M')} ч."
                + (f" (след {mins} мин)" if mins < 120 else "")
            )
        else:
            text = "📡 Няма активни или предстоящи F1 сесии."
    else:
        sk = session.get('session_key')
        if not live_state.get('drivers'):
            live_state['drivers'] = fetch_driver_map(sk)
        text = format_live_standings(sk, session.get('session_name','?'), session.get('meeting_name','?'))
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = format_settings(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_user(chat_id, subscribed=1)
    await update.message.reply_text(
        f"🔔 <b>Абонирани сте!</b>\nНапомняния {DEFAULT_REMINDER_MIN} мин. преди сесиите.\n"
        "Настройте предпочитанията си с /settings",
        parse_mode='HTML'
    )


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user(update.effective_chat.id, subscribed=0)
    await update.message.reply_text("🔕 Отписахте се от известията.")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Обновяване...")
    for key in calendar_cache: calendar_cache[key]['data'] = None
    for key in standings_cache: standings_cache[key]['data'] = None
    f1  = fetch_f1_calendar()
    f2  = fetch_f2_calendar()
    std = fetch_f1_standings('drivers')
    await update.message.reply_text(
        f"✅ <b>Обновено!</b>\n\n• F1: {len(f1)} кръга\n• F2: {len(f2)} кръга\n"
        f"• Класиране: {'✅' if std else '⚠️ неналично'}"
    )

# ══════════════════════════════════════════════════════════════════════════════
# BUTTON HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()
    d = query.data

    try:
        back = [[InlineKeyboardButton("« Назад", callback_data='back_to_menu')]]

        # ── Navigation ────────────────────────────────────────────────────────
        if d == 'back_to_menu':
            await query.edit_message_text("Изберете опция:", reply_markup=get_main_keyboard())
            return

        # ── Settings ──────────────────────────────────────────────────────────
        if d == 'settings':
            text, kb = format_settings(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
            return

        if d == 'toggle_sub':
            s = get_user(chat_id)
            set_user(chat_id, subscribed=0 if s['subscribed'] else 1)
            text, kb = format_settings(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
            return

        if d.startswith('remind_'):
            mins = int(d.split('_')[1])
            set_user(chat_id, subscribed=1, reminder_min=mins)
            text, kb = format_settings(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
            return

        if d.startswith('filter_'):
            preset = d[len('filter_'):]
            if preset in SESSION_PRESETS:
                set_user(chat_id, subscribed=1, session_preset=preset)
            text, kb = format_settings(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
            return

        if d == 'toggle_tsolov':
            s = get_user(chat_id)
            set_user(chat_id, tsolov_alerts=0 if s['tsolov_alerts'] else 1)
            text, kb = format_settings(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
            return

        if d == 'toggle_briefing':
            s = get_user(chat_id)
            set_user(chat_id, morning_briefing=0 if s['morning_briefing'] else 1)
            text, kb = format_settings(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
            return

        if d == 'toggle_results':
            s = get_user(chat_id)
            set_user(chat_id, results_push=0 if s['results_push'] else 1)
            text, kb = format_settings(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode='HTML')
            return

        # ── Content ───────────────────────────────────────────────────────────
        if d == 'next_f1':
            await query.edit_message_text("⏳ Зареждане...")
            nxt  = get_next_race(fetch_f1_calendar())
            text = format_race_info(nxt, "F1") if nxt else "Няма предстоящи F1 състезания."
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back), parse_mode='HTML')

        elif d == 'next_f2':
            await query.edit_message_text("⏳ Зареждане...")
            nxt  = get_next_race(fetch_f2_calendar())
            text = format_race_info(nxt, "F2") if nxt else "Няма предстоящи F2 състезания."
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back), parse_mode='HTML')

        elif d == 'full_f1':
            await query.edit_message_text("⏳ Зареждане...")
            cal   = fetch_f1_calendar()
            today = datetime.now(BG_TZ).date()
            text  = f"<b>📅 F1 Календар {datetime.now().year}</b>\n\n"
            for race in cal:
                rd  = datetime.strptime(race['date'], '%Y-%m-%d')
                st  = "✅" if rd.date() < today else "🏁" if rd.date() == today else "📅"
                sp  = " ⚡" if race.get('is_sprint') else ""
                text += f"{st} R{race['round']}: {race['country']}{sp} — {rd.strftime('%d.%m.%Y')}\n"
            text += f"\n<i>Общо: {len(cal)} кръга | ⚡ Спринт</i>"
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back), parse_mode='HTML')

        elif d == 'full_f2':
            await query.edit_message_text("⏳ Зареждане...")
            cal   = fetch_f2_calendar()
            today = datetime.now(BG_TZ).date()
            text  = f"<b>📅 F2 Календар {datetime.now().year}</b>\n\n"
            for race in cal:
                rd  = datetime.strptime(race['date'], '%Y-%m-%d')
                st  = "✅" if rd.date() < today else "🏁" if rd.date() == today else "📅"
                text += f"{st} R{race['round']}: {race['country']} — {rd.strftime('%d.%m.%Y')}\n"
            text += f"\n<i>Общо: {len(cal)} кръга</i>"
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back), parse_mode='HTML')

        elif d == 'standings_drivers':
            await query.edit_message_text("⏳ Зареждане...")
            data = fetch_f1_standings('drivers')
            text = format_f1_standings(data, 'drivers') if data else "⚠️ Класирането не е налично."
            btn  = [[InlineKeyboardButton("🏆 Отбори", callback_data='standings_constructors'),
                     InlineKeyboardButton("« Назад",   callback_data='back_to_menu')]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btn), parse_mode='HTML')

        elif d == 'standings_constructors':
            await query.edit_message_text("⏳ Зареждане...")
            data = fetch_f1_standings('constructors')
            text = format_f1_standings(data, 'constructors') if data else "⚠️ Класирането не е налично."
            btn  = [[InlineKeyboardButton("🏆 Пилоти", callback_data='standings_drivers'),
                     InlineKeyboardButton("« Назад",   callback_data='back_to_menu')]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btn), parse_mode='HTML')

        elif d == 'tsolov':
            await query.edit_message_text("⏳ Зареждане...")
            btn = [[InlineKeyboardButton("🔄 Обнови", callback_data='tsolov'),
                    InlineKeyboardButton("« Назад",  callback_data='back_to_menu')]]
            await query.edit_message_text(
                format_tsolov_info(), reply_markup=InlineKeyboardMarkup(btn), parse_mode='HTML'
            )

        elif d == 'live':
            session = get_live_f1_session()
            btn = [[InlineKeyboardButton("🔄 Обнови", callback_data='live'),
                    InlineKeyboardButton("« Назад",  callback_data='back_to_menu')]]
            if not session:
                items = get_all_upcoming_sessions('F1')
                if items:
                    nxt  = items[0]
                    mins = int((nxt['dt_bg'] - datetime.now(BG_TZ)).total_seconds() / 60)
                    text = (
                        f"📡 Няма активна сесия.\n\n"
                        f"<b>Следваща:</b> {esc(nxt['race']['country'])} — {esc(nxt['session_name'])}\n"
                        f"⏰ {nxt['dt_bg'].strftime('%d.%m %H:%M')} ч."
                        + (f" (след {mins} мин)" if mins < 120 else "")
                    )
                else:
                    text = "📡 Няма активни или предстоящи F1 сесии."
            else:
                sk = session.get('session_key')
                if not live_state.get('drivers'):
                    live_state['drivers'] = fetch_driver_map(sk)
                text = format_live_standings(
                    sk, session.get('session_name','?'), session.get('meeting_name','?')
                )
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btn), parse_mode='HTML')

        elif d == 'tv_info':
            text = (
                "<b>📺 TV излъчване</b>\n\n"
                "<b>Diema Sport / Diema Sport 2 / Diema Sport 3</b>\n"
                "Всички F1 и F2 на живо\n\n"
                "<b>Онлайн:</b> play.diemaxtra.bg\n"
                "<b>Мобилно:</b> iOS / Android\n\n"
                "<b>Доставчици:</b>\n"
                "• Vivacom — 14.99 лв/мес.\n"
                "• Bulsatcom, A1, Yettel\n\n"
                "<b>HD | Коментар на български</b>\n"
                "www.diema.bg"
            )
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back), parse_mode='HTML')

        elif d == 'refresh_data':
            for key in calendar_cache: calendar_cache[key]['data'] = None
            for key in standings_cache: standings_cache[key]['data'] = None
            await query.edit_message_text("🔄 Обновяване...")
            f1  = fetch_f1_calendar()
            f2  = fetch_f2_calendar()
            std = fetch_f1_standings('drivers')
            text = (
                f"✅ <b>Обновено!</b>\n\n• F1: {len(f1)} кръга\n• F2: {len(f2)} кръга\n"
                f"• Класиране: {'✅' if std else '⚠️'}\n\n"
                f"<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>"
            )
            await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode='HTML')

    except Exception as e:
        import traceback
        logger.error(f"button_handler [{d}]: {e}\n{traceback.format_exc()}")
        try:
            await query.edit_message_text(
                f"⚠️ Грешка: {_html.escape(str(e)[:120])}\n\nОпитайте пак.",
                reply_markup=get_main_keyboard(),
                parse_mode='HTML'
            )
        except Exception as e2:
            logger.error(f"button_handler fallback also failed: {e2}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    logger.error(f"Update caused error: {context.error}\n{''.join(traceback.format_tb(context.error.__traceback__)) if context.error else ''}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    if TOKEN == 'YOUR_BOT_TOKEN_HERE':
        print("\n" + "="*60)
        print("ERROR: Set the F1_BOT_TOKEN environment variable (token from @BotFather)!")
        print("="*60 + "\n")
        return

    init_db()

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_command))
    app.add_handler(CommandHandler("next",        next_race_command))
    app.add_handler(CommandHandler("calendar",    calendar_command))
    app.add_handler(CommandHandler("standings",   standings_command))
    app.add_handler(CommandHandler("tsolov",      tsolov_command))
    app.add_handler(CommandHandler("live",        live_command))
    app.add_handler(CommandHandler("settings",    settings_command))
    app.add_handler(CommandHandler("subscribe",   subscribe_command))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    app.add_handler(CommandHandler("refresh",     refresh_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    # ── Scheduled jobs ────────────────────────────────────────────────────────
    # Reminder check  – every 60 s
    app.job_queue.run_repeating(job_reminders,      interval=REMINDER_CHECK_INTERVAL, first=15)
    # Live session poll – every 90 s
    app.job_queue.run_repeating(job_live_monitor,   interval=LIVE_CHECK_INTERVAL,     first=20)
    # Morning briefing  – daily at 09:00 BG time
    app.job_queue.run_daily(
        job_morning_briefing,
        time=dt_time(hour=9, minute=0, tzinfo=BG_TZ)
    )

    # Pre-fetch
    logger.info("Pre-fetching data...")
    f1  = fetch_f1_calendar()
    f2  = fetch_f2_calendar()
    std = fetch_f1_standings('drivers')

    print("\n" + "="*60)
    print("F1/F2 Bulgaria Bot — started")
    print("="*60)
    print(f"  F1 races     : {len(f1)}")
    print(f"  F2 races     : {len(f2)}")
    print(f"  F1 standings : {'OK' if std else 'unavailable'}")
    print(f"  Reminders    : check every {REMINDER_CHECK_INTERVAL}s")
    print(f"  Live poll    : every {LIVE_CHECK_INTERVAL}s")
    print(f"  Briefing     : daily 09:00 BG time")
    print("\nPress Ctrl+C to stop\n")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()