"""
evm_watch.py - ping Telegram when an EVM Wheels MUV hits a new all-time low price,
plus a daily "still alive" summary so a silent failure can't go unnoticed.

Built for GitHub Actions. All state is ONE integer: the repository variable
ALL_TIME_LOW (the cheapest MUV total ever seen, in rupees). The script reads it
from the environment and, when it should change, writes `new_low=<int>` to
$GITHUB_OUTPUT; the workflow then saves it back with `gh variable set`.

How the site works:
  1. GET /search?...          stores the search (hub, dates) in the server-side session
  2. GET /filter_vehicles?... returns the car cards for that session as HTML
Both requests share cookies through requests.Session.

Environment:
  ALL_TIME_LOW         previous low; empty/missing = first run (sets the baseline, no alert)
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_IDS    comma-separated chat ids, e.g. "5612988574,7239632899"

Flags:
  --daily        always send a summary (run once a day from a separate cron)
  --dry-run      print alerts instead of sending them
  --from-file F  parse a saved response instead of fetching
  --test-notify  send a test Telegram message and exit

Local test:  python evm_watch.py --from-file saved_response.txt --dry-run --daily
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config ---
PICKUP = dt.datetime(2026, 12, 20, 9, 0)   # pickup date and time (IST)
DROP = dt.datetime(2026, 12, 22, 12, 0)    # drop date and time (IST)
CITY_ID = 5
HUB_ID = 41
VEHICLE_TYPE = 1                           # 1 = cars

# Body-type codes from the site's filter: 1 Hatchback, 2 Sedan, 3 Compact SUV, 4 SUV,
# 5 MUV, 6 Luxury, 7 Compact EV. Empty list = all categories.
WATCH_CATEGORIES = [5]                     # MUVs only

BASE = "https://evmwheels.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
# ---------------------------------------------------------------------------


def fetch_cards_html() -> str:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})

    # Homepage first: picks up the session cookie and the CSRF token the search form sends.
    home = s.get(BASE + "/", timeout=30)
    home.raise_for_status()
    m = re.search(r'name="csrf-token" content="([^"]+)"', home.text)
    token = m.group(1) if m else ""

    search_params = {
        "_token": token,
        "v_type": VEHICLE_TYPE,
        "pickup_city": CITY_ID, "pickup_hub": HUB_ID,
        "drop_city": CITY_ID, "drop_hub": HUB_ID,
        "pickup_date_hidden": PICKUP.strftime("%d-%m-%Y"),
        "pickup_date": PICKUP.strftime("%Y-%m-%dT%H:%M:%S"),
        "drop_date": DROP.strftime("%Y-%m-%dT%H:%M:%S"),
        "return_same_location": "on",
    }
    r = s.get(BASE + "/search", params=search_params, timeout=30)
    r.raise_for_status()

    hours = int((DROP - PICKUP).total_seconds() // 3600)
    r = s.get(
        BASE + "/filter_vehicles",
        params={"totalhours": hours, "list_type": "unique", "search_value": "",
                "chauffeur": 0, "pagetype": ""},
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Accept": "application/json, text/javascript, */*; q=0.01",
                 "Referer": r.url},
        timeout=120,   # the response is ~50 MB, mostly inline SVG icons
    )
    r.raise_for_status()
    return response_to_html(r.text)


def response_to_html(text: str) -> str:
    """The endpoint returns a JSON-encoded string of HTML; fall back to raw text."""
    text = text.strip()
    try:
        data = json.loads(text, strict=False)
    except ValueError:
        return text
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and "mainDivBorder" in v:
                return v
    return text


def _num(t):
    digits = re.sub(r"[^\d]", "", t or "")
    return int(digits) if digits else None


def rs(n) -> str:
    """Format rupees; safe for None."""
    return "-" if n is None else f"₹{n:,}"


def parse_cars(page: str) -> list:
    soup = BeautifulSoup(page, "lxml")
    cars = []
    for c in soup.select("div.mainDivBorder"):
        name_el = c.select_one(".featuredTextDiv h4")
        if not name_el:
            continue
        brand_el = c.select_one(".featuredTextDiv label")
        price_el = c.select_one("strong.price")
        lis = c.select("ul.priceUlNav li")
        cars.append({
            "model": re.sub(r"\s+", " ", name_el.get_text(strip=True)).strip("` "),
            "brand": brand_el.get_text(strip=True) if brand_el else "",
            "category": int(c["data-body"]) if (c.get("data-body") or "").isdigit() else None,
            "total": _num(price_el.get_text()) if price_el else None,
            "per_day": _num(lis[1].get_text()) if len(lis) > 1 else None,
        })
    return cars


def notify(title: str, body: str, emoji: str = "🚗", dry_run: bool = False):
    if dry_run:
        print(f"  (dry run, not sent) {title} | {body}")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
    if not token or not chat_ids:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS not set; skipping Telegram.", file=sys.stderr)
        return
    text = (f"{emoji} <b>{html.escape(title)}</b>\n{html.escape(body)}\n\n"
            f'<a href="{BASE}">Open EVM Wheels</a>')
    for chat_id in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=15,
            )
            if not (r.status_code == 200 and r.json().get("ok")):
                print(f"Telegram error for {chat_id} ({r.status_code}): {r.text[:200]}",
                      file=sys.stderr)
        except requests.RequestException as e:
            print(f"Telegram request failed for {chat_id}: {e}", file=sys.stderr)


def save_low(value: int):
    """Hand the new low to the workflow, which stores it in the ALL_TIME_LOW variable."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"new_low={value}\n")
    print(f"new_low={value}")


def run(a):
    trip = f"{PICKUP:%d %b %H:%M} to {DROP:%d %b %H:%M}"

    if dt.datetime.now(IST).replace(tzinfo=None) >= PICKUP:
        msg = "Pickup time has passed; nothing to watch. Disable the workflow."
        print(msg)
        if a.daily:
            notify("EVM watcher: trip has started", msg, "🛑", a.dry_run)
        return

    raw_prev = os.environ.get("ALL_TIME_LOW", "").strip()
    prev = int(raw_prev) if raw_prev.isdigit() else None

    page = (response_to_html(Path(a.from_file).read_text(encoding="utf-8"))
            if a.from_file else fetch_cards_html())
    cars = parse_cars(page)
    if not cars:
        raise RuntimeError("0 cars parsed; the site's layout or flow may have changed")

    watched = [c for c in cars if not WATCH_CATEGORIES or c["category"] in WATCH_CATEGORIES]
    priced = sorted((c for c in watched if c["total"] is not None), key=lambda c: c["total"])
    cheapest = priced[0] if priced else None

    now_str = "-" if not cheapest else f"{rs(cheapest['total'])} ({cheapest['model']})"
    print(f"{len(cars)} cars, {len(watched)} watched, {len(priced)} priced; "
          f"cheapest now {now_str}; all-time low {rs(prev)}")

    if cheapest and prev is None:
        save_low(cheapest["total"])
        notify("EVM watcher started",
               f"Watching MUVs for {trip}.\nStarting low: {rs(cheapest['total'])} "
               f"({cheapest['brand']} {cheapest['model']}). I'll ping you when it goes lower, "
               f"plus a daily summary.",
               "✅", a.dry_run)

    elif cheapest and cheapest["total"] < prev:
        save_low(cheapest["total"])
        per_day = f", {rs(cheapest['per_day'])}/day" if cheapest["per_day"] else ""
        body = (f"{cheapest['brand']} {cheapest['model']}\n"
                f"{rs(cheapest['total'])} total{per_day}\n"
                f"Previous low {rs(prev)}, down {rs(prev - cheapest['total'])}\n{trip}")
        print("NEW LOW:", body.replace("\n", " | "))
        notify(f"New all-time low: {rs(cheapest['total'])}", body, "📉", a.dry_run)

    elif a.daily:
        if cheapest:
            body = (f"Cheapest MUV now: {rs(cheapest['total'])} "
                    f"({cheapest['brand']} {cheapest['model']})\n"
                    f"All-time low: {rs(prev)}\n"
                    f"{len(priced)} MUVs available\n{trip}")
        else:
            body = (f"No MUV available right now (all sold out).\n"
                    f"All-time low: {rs(prev)}\n{trip}")
        notify("Daily check: watcher is running", body, "🟢", a.dry_run)

    elif not cheapest:
        print("No watched car has a price right now (all sold out).")


def main():
    for stream in (sys.stdout, sys.stderr):   # don't crash printing ₹ on non-UTF-8 consoles
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file", help="parse a saved response instead of fetching (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    ap.add_argument("--test-notify", action="store_true", help="send a test Telegram message and exit")
    ap.add_argument("--daily", action="store_true", help="always send a summary message")
    a = ap.parse_args()

    if a.test_notify:
        notify("EVM watcher test", "If you can read this, alerts work.")
        return

    try:
        run(a)
    except Exception as e:   # anything at all: network, parsing, formatting bugs
        print(f"Check failed: {type(e).__name__}: {e}", file=sys.stderr)
        notify("EVM watcher failed", f"{type(e).__name__}: {str(e)[:300]}", "⚠️", a.dry_run)
        sys.exit(1)   # marks the Actions run as failed, so it's visible there too


if __name__ == "__main__":
    main()
