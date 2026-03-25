import time
import json
import hashlib
import logging
import requests
import ctypes
from logging.handlers import RotatingFileHandler
from playwright.sync_api import sync_playwright

# --- ANTI-SLEEP OVERRIDE ---
# Because IT policies restrict normal Sleep settings, this asks the Windows
# Kernel to forcibly keep the system awake endlessly as long as this script runs
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
# ---------------------------

# --- LOGGING SETUP ---
logger = logging.getLogger("cricket")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

file_handler = RotatingFileHandler("scraper.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
file_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)
# ---------------------

# --- LOAD CONFIG ---
with open("config.json", "r") as f:
    CONFIG = json.load(f)

KVDB_URL           = CONFIG["kvdb_url"]
POLL_LIVE          = CONFIG.get("poll_interval_live", 10)
POLL_STANDBY       = CONFIG.get("poll_interval_standby", 60)
NO_LIVE_TIMEOUT    = CONFIG.get("no_live_timeout", 15)
HEADLESS           = CONFIG.get("headless", False)

# Build tournament URL from id + slug — you only need to update these two fields in config.json
TOURNAMENT_ID      = CONFIG["tournament_id"]
TOURNAMENT_SLUG    = CONFIG["tournament_slug"]
TOURNAMENT_URL     = f"https://cricheroes.com/tournament/{TOURNAMENT_ID}/{TOURNAMENT_SLUG}"
# -------------------


def _payload_hash(data: dict) -> str:
    """Return a stable hash of the parts of the payload that matter for change detection."""
    key = json.dumps({
        "score":   data.get("score"),
        "batsmen": data.get("batsmen"),
        "bowlers": data.get("bowlers"),
        "ended":   data.get("ended"),
    }, sort_keys=True)
    return hashlib.md5(key.encode()).hexdigest()


def push_to_kvdb(data: dict) -> bool:
    """PUT data to KVDB with exponential-backoff retry (max 3 attempts).
    Automatically stamps every payload with 'last_updated' (Unix seconds)
    so the overlay can detect a dead scraper.
    """
    data = {**data, "last_updated": time.time()}
    delays = [2, 4, 8]
    for attempt, delay in enumerate(delays, start=1):
        try:
            resp = requests.put(KVDB_URL, json=data, timeout=10)
            if resp.status_code < 300:
                return True
            logger.warning(f"KVDB returned {resp.status_code} (attempt {attempt}/3)")
        except requests.RequestException as e:
            logger.warning(f"KVDB write failed (attempt {attempt}/3): {e}")
        if attempt < len(delays):
            time.sleep(delay)
    logger.error("KVDB write failed after 3 attempts — score update dropped")
    return False


def extract_score_from_next_data(page):
    """Extract score data directly from Next.js __NEXT_DATA__ JSON — 100% reliable."""
    try:
        next_data = page.evaluate('() => window.__NEXT_DATA__')
        if not next_data:
            return None

        pageProps = next_data.get('props', {}).get('pageProps', {})
        summary = pageProps.get('summaryData', {}).get('data', {})
        if not summary:
            summary = pageProps.get('miniScorecard', {}).get('data', {})

        if not summary:
            return None

        team_a = summary.get('team_a', {})
        team_b = summary.get('team_b', {})

        team_a_name = team_a.get('name', 'Team A')
        team_b_name = team_b.get('name', 'Team B')

        # Determine the current batting team from current_inning
        current_inning = summary.get('current_inning', 1)
        status = summary.get('status', '')
        match_result = summary.get('match_result', '')

        # Get the scores
        team_a_summary = team_a.get('summary', '--')  # e.g. "113/8"
        team_b_summary = team_b.get('summary', '--')  # e.g. "80/7"

        # Get overs for current batting team
        current_overs = ""
        if current_inning == 1 and team_a.get('innings'):
            current_overs = team_a['innings'][0].get('summary', {}).get('over', '')
        elif current_inning == 2 and team_b.get('innings'):
            current_overs = team_b['innings'][0].get('summary', {}).get('over', '')

        # Build the display score
        target_runs = None
        if current_inning == 2 and team_b_summary != '--':
            # 2nd innings: show batting team score with target info
            display_score = f"{team_b_summary} {current_overs}"
            target_runs = int(team_a_summary.split('/')[0]) + 1 if '/' in team_a_summary else None
            display_status = f"Target: {target_runs}" if target_runs else f"{team_a_name}: {team_a_summary}"
        else:
            # 1st innings
            display_score = f"{team_a_summary} {current_overs}"
            display_status = "1st Innings"

        # Extract batsmen and bowlers if available
        batsmen_data = summary.get('batsmen', {})
        bowlers_data = summary.get('bowlers', {})

        parsed_batsmen = []
        parsed_bowlers = []

        if batsmen_data:
            sb = batsmen_data.get('sb')
            nsb = batsmen_data.get('nsb')
            if sb: parsed_batsmen.append({'name': sb.get('name', ''), 'runs': sb.get('runs', 0), 'balls': sb.get('balls', 0), 'striker': True})
            if nsb: parsed_batsmen.append({'name': nsb.get('name', ''), 'runs': nsb.get('runs', 0), 'balls': nsb.get('balls', 0), 'striker': False})

        if bowlers_data:
            # sb usually means current active bowler
            sb = bowlers_data.get('sb')
            if sb: parsed_bowlers.append({'name': sb.get('name', ''), 'overs': f"{sb.get('overs', 0)}.{sb.get('balls', 0)}", 'runs': sb.get('runs', 0), 'wickets': sb.get('wickets', 0)})

        # Check if match ended
        match_summary_obj = summary.get('match_summary', {})
        result_text = match_summary_obj.get('summary', '')

        if match_result == 'Resulted' or 'won' in result_text.lower():
            return {
                'team1': team_a_name,
                'team2': team_b_name,
                'score': f"{team_a_summary} vs {team_b_summary}",
                'status': result_text,
                'ended': True,
                'batsmen': parsed_batsmen,
                'bowlers': parsed_bowlers
            }

        return {
            'team1': team_a_name,
            'team2': team_b_name,
            'score': display_score,
            'status': display_status,
            'target': target_runs,           # None for 1st innings, int for 2nd
            'innings': current_inning,
            'team1_score': team_a_summary,   # 1st innings total, useful in 2nd innings display
            'ended': False,
            'batsmen': parsed_batsmen,
            'bowlers': parsed_bowlers
        }
    except Exception as e:
        logger.error(f"Error extracting __NEXT_DATA__: {e}")
        return None


HEARTBEAT_INTERVAL = 90  # Seconds — push even if no score change, to keep last_updated fresh


def scrape_live_match(page, match_url):
    logger.info(f"--- SCRAPING LIVE MATCH ---")
    logger.info(f"URL: {match_url}")
    page.goto(match_url, timeout=60000)

    last_hash = ""
    last_push_time = 0.0
    while True:
        try:
            page.wait_for_timeout(3000)

            data = extract_score_from_next_data(page)

            if data:
                if data.get('ended'):
                    logger.info(f"Match ended! {data['status']}")
                    push_to_kvdb(data)
                    logger.info(f"Final: {data['score']} - {data['status']}")
                    return

                current_hash = _payload_hash(data)
                now = time.time()
                heartbeat_due = (now - last_push_time) >= HEARTBEAT_INTERVAL

                if current_hash != last_hash:
                    push_to_kvdb(data)
                    logger.info(f"Broadcasted -> {data['team1']} vs {data['team2']}: {data['score']} ({data['status']})")
                    last_hash = current_hash
                    last_push_time = now
                elif heartbeat_due:
                    push_to_kvdb(data)
                    logger.debug(f"Heartbeat push ({data['score']})")
                    last_push_time = now
                else:
                    logger.debug(f"No change ({data['score']})")
            else:
                logger.warning(f"Could not extract score data")

        except Exception as e:
            logger.error(f"Error reading score: {e}")

        logger.debug(f"Wait {POLL_LIVE}s...")
        time.sleep(POLL_LIVE)
        # Re-navigate instead of reload to avoid Cloudflare blocks
        try:
            page.goto(match_url, timeout=60000)
            page.wait_for_timeout(5000)
        except Exception as e:
            logger.warning(f"Re-navigation timeout/error, retrying next cycle: {e}")


def main():
    logger.info("=======================================")
    logger.info("  CRICHEROES TOURNAMENT AUTO-TRACKER   ")
    logger.info("=======================================")
    logger.info(f"Anti-Sleep Override: ENABLED")
    logger.info(f"Tournament: {TOURNAMENT_URL}")
    logger.info(f"KVDB: {KVDB_URL}")
    logger.info(f"Headless: {HEADLESS}")
    logger.info("Starting browser...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        no_live_count = 0  # Tracks consecutive standby checks with no live match

        while True:
            if "/scorecard/" in TOURNAMENT_URL:
                logger.info(f"\nDirectly scraping LIVE match:\n{TOURNAMENT_URL}")
                scrape_live_match(page, TOURNAMENT_URL)
                no_live_count = 0  # Reset after a match finishes
            else:
                logger.info(f"\nChecking Tournament Page for LIVE matches...")
                page.goto(TOURNAMENT_URL)
                page.wait_for_timeout(5000)  # Wait for page to load fully

                # Find all links on the page that point to a live match
                # Must contain the tournament slug to avoid following matches
                # from other tournaments that may appear on the same page.
                live_match_url = None
                links = page.locator('a').all()
                for link in links:
                    href = link.get_attribute('href')
                    if (href
                            and "/scorecard/" in href
                            and "live" in href.lower()
                            and TOURNAMENT_SLUG in href):
                        if href.startswith("/"):
                            live_match_url = "https://cricheroes.com" + href
                        elif not href.startswith("http"):
                            live_match_url = "https://cricheroes.com/" + href
                        else:
                            live_match_url = href
                        break

                if live_match_url:
                    no_live_count = 0  # Reset — a new live match is found!
                    logger.info(f"LIVE match found: {live_match_url}")
                    scrape_live_match(page, live_match_url)
                else:
                    no_live_count += 1
                    logger.info(f"No LIVE matches found. ({no_live_count}/{NO_LIVE_TIMEOUT} minutes waited)")

                    if no_live_count >= NO_LIVE_TIMEOUT:
                        # 15 minutes of no live match — signal all done
                        logger.info("Match Day appears to have concluded. Hiding overlay...")
                        payload = {
                            "team1": "",
                            "team2": "",
                            "score": "",
                            "status": "",
                            "all_done": True,
                            "no_live": True
                        }
                        push_to_kvdb(payload)
                        # Keep polling but don't keep incrementing past timeout
                        no_live_count = NO_LIVE_TIMEOUT
                    else:
                        # Still within timeout window — show standby state
                        payload = {
                            "team1": "Next Match",
                            "team2": "Starting Soon",
                            "score": "--/--",
                            "status": "Standby",
                            "no_live": True,
                            "all_done": False
                        }
                        push_to_kvdb(payload)

                    logger.info(f"Checking again in {POLL_STANDBY} seconds...")
                    time.sleep(POLL_STANDBY)


if __name__ == "__main__":
    main()
