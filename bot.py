import os
import sys
import json
import time
import subprocess
import requests
import telebot

# --- LOAD CONFIG ---
with open("config.json", "r") as f:
    CONFIG = json.load(f)

BOT_TOKEN         = CONFIG["telegram_bot_token"]
KVDB_URL          = CONFIG["kvdb_url"]
ALLOWED_CHAT_ID   = CONFIG.get("telegram_chat_id")   # None = allow anyone

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def guard(message):
    """Return True (and warn) if the sender is not authorised."""
    if ALLOWED_CHAT_ID and str(message.chat.id) != str(ALLOWED_CHAT_ID):
        bot.reply_to(message, "⛔ Not authorised.")
        return True
    return False


def is_scraper_running() -> bool:
    result = subprocess.run(
        'wmic process where "commandline like \'%%scraper.py%%\'" get name',
        shell=True, capture_output=True, text=True
    )
    return "python" in result.stdout.lower()


def kill_scraper():
    os.system('wmic process where "name=\'python3.13.exe\' and commandline like \'%%scraper.py%%\'" delete >nul 2>&1')
    os.system('wmic process where "name=\'python.exe\'     and commandline like \'%%scraper.py%%\'" delete >nul 2>&1')


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    if guard(message):
        return
    bot.reply_to(message,
        "*Cricket Overlay Bot* 🏏\n\n"
        "/start\_scraper — Start the live scraper\n"
        "/stop\_scraper  — Stop the scraper\n"
        "/status        — Current score from overlay\n"
        "/help          — Show this message"
    )


@bot.message_handler(commands=["start_scraper"])
def cmd_start(message):
    if guard(message):
        return
    if is_scraper_running():
        bot.reply_to(message, "⚠️ Scraper is already running.")
        return
    subprocess.Popen(
        [sys.executable, "scraper.py"],
        cwd=SCRIPT_DIR,
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )
    bot.reply_to(message, "✅ Scraper started! It will auto-detect the live match.")


@bot.message_handler(commands=["stop_scraper"])
def cmd_stop(message):
    if guard(message):
        return
    if not is_scraper_running():
        bot.reply_to(message, "ℹ️ Scraper is not running.")
        return
    kill_scraper()
    bot.reply_to(message, "⛔ Scraper stopped.")


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if guard(message):
        return
    try:
        resp = requests.get(KVDB_URL + "?_=" + str(int(time.time())), timeout=10)
        if resp.status_code != 200:
            bot.reply_to(message, f"❌ KVDB error: {resp.status_code}")
            return

        data = resp.json()
        running = is_scraper_running()

        if not running:
            bot.reply_to(message, "🔴 Scraper is not running.\nUse /start\\_scraper to start it.")
            return

        scraper_icon = "🟢 Scraper running"

        if data.get("all_done"):
            bot.reply_to(message, f"{scraper_icon}\n\nMatch day concluded. Overlay hidden.")
            return

        if data.get("no_live"):
            bot.reply_to(message, f"{scraper_icon}\n\n⏳ No live match yet — standing by.")
            return

        if data.get("ended"):
            bot.reply_to(message,
                f"{scraper_icon}\n\n"
                f"🏁 *Match Ended*\n"
                f"{data.get('team1')} vs {data.get('team2')}\n"
                f"{data.get('score')}\n"
                f"_{data.get('status')}_"
            )
            return

        lines = [
            scraper_icon,
            "",
            f"🏏 *{data.get('team1')} vs {data.get('team2')}*",
            f"Score: *{data.get('score')}*",
            f"Status: {data.get('status')}",
        ]
        if data.get("target"):
            lines.append(f"Target: *{data.get('target')}*")

        batsmen = data.get("batsmen", [])
        bowlers = data.get("bowlers", [])
        if batsmen:
            lines.append("")
            for b in batsmen:
                star = " ✱" if b.get("striker") else ""
                lines.append(f"🏏 {b['name']}{star}: {b['runs']} ({b['balls']})")
        if bowlers:
            for bw in bowlers:
                lines.append(f"🎯 {bw['name']}: {bw['wickets']}/{bw['runs']} ({bw['overs']})")

        stale = (time.time() - data.get("last_updated", 0)) > 180
        if stale:
            lines.append("\n⚠️ Data may be stale")

        bot.reply_to(message, "\n".join(lines))

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Bot started. Listening for commands...")
    if ALLOWED_CHAT_ID:
        print(f"Restricted to chat_id: {ALLOWED_CHAT_ID}")
    else:
        print("Warning: telegram_chat_id not set — bot responds to anyone.")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
