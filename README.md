# 🏎️ F1/F2 Bulgaria: Telegram bot

**A Bulgarian-language Telegram bot for Formula 1 and Formula 2 fans.** It gives you the calendar in Sofia time, standings, results, live session data, reminders before every session, and special tracking of Bulgarian driver **Nikola Tsolov**.

![Python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21-26A5E4)
![License](https://img.shields.io/badge/license-MIT-green)

> Пълнофункционален Telegram бот за Формула 1 и Формула 2 на български: календар, класирания, резултати, live данни, известия и проследяване на Никола Цолов.

## Features

- 📅 **Calendar**: every session of the season, in Bulgarian time (`Europe/Sofia`), with Bulgarian day names.
- ⏭️ **Next race** at a glance.
- 🏆 **Standings**: drivers' and constructors' championships.
- 🏁 **Results** for races, qualifying and sprints, optionally **pushed automatically** after each session.
- 🔴 **Live data** during sessions (positions and more) from [OpenF1](https://openf1.org).
- 🔔 **Reminders** before sessions, with the lead time and session filter (all sessions / race + qualifying + sprint / race only) set **per user**.
- ☀️ **Morning briefing** at 09:00 on every day that has a session.
- 🇧🇬 **Formula 2 + Nikola Tsolov**: F2 calendar and standings, plus dedicated alerts for Tsolov.
- ⚙️ Everything is configured through **inline buttons** in `/settings`. Preferences are stored in SQLite, and API data is cached for 6 hours.

### Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Welcome and help |
| `/next` | Next race weekend with all session times |
| `/calendar` | Season calendar |
| `/standings` | Driver & constructor standings |
| `/live` | Live data for the current session |
| `/tsolov` | Nikola Tsolov in F2 |
| `/settings` | Reminders, session filter, briefing, result push, Tsolov alerts |
| `/subscribe`, `/unsubscribe` | Turn notifications on/off |
| `/refresh` | Clear the cache and reload data |

---

## Installation

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy its **token**.
2. Install **Python 3.9+**, then:
   ```bash
   git clone https://github.com/deniskostadinov1995/f1-bulgaria-bot.git
   cd f1-bulgaria-bot
   pip install -r requirements.txt
   ```
3. Provide the token as an environment variable:
   ```powershell
   # Windows (permanent; open a new terminal afterwards)
   setx F1_BOT_TOKEN "123456789:AA..."
   ```
   ```bash
   # macOS / Linux
   export F1_BOT_TOKEN="123456789:AA..."
   ```
4. Run it:
   ```bash
   python f1bot.py
   ```
   On Windows you can also double-click `run.bat`.

The bot creates `f1bot.db` (subscribers and their settings) next to the script. It must keep running for reminders and live updates to work, so use a PC that stays on, a small VPS, or a process manager such as [PyLauncher Pro](https://github.com/deniskostadinov1995/pylauncher-pro).

### Settings in the code

The constants at the top of `f1bot.py` control the cache duration (6 h), reminder and live polling intervals, the default reminder lead time (30 min), the time zone and the tracked driver (`TSOLOV_SURNAME`).

## Data sources

- [Jolpica F1 API](https://github.com/jolpica/jolpica-f1) (the Ergast successor): calendar, results, standings
- [OpenF1](https://openf1.org): live session data
- Formula 2 official API / website: F2 calendar and standings

This is an unofficial fan project and is not associated with Formula 1, FIA Formula 2 or their partners.

## License

[MIT](LICENSE) © Denis Kostadinov
