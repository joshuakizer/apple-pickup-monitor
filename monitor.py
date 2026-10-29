#!/usr/bin/env python3
"""Watch in-store pickup availability of iPhone 18 Pro Max at NYC Apple Stores.

Polls Apple's public pickup endpoint every INTERVAL seconds (optionally through a
proxy) and sends a push notification (ntfy and/or Telegram) when any model becomes
available. Standard library only.
"""

import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ENDPOINT = "https://www.apple.com/shop/retail/pickup-message"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

 # iPhone Duo - US configurations
PARTS = {
    "MK2E4LL/A": ("256gb", "night sky"),
    "MK2F4LL/A": ("256gb", "star white"),
    "MK2J4LL/A": ("512gb", "night sky"),
    "MK2K4LL/A": ("512gb", "star white"),
    "MK2N4LL/A": ("1tb", "night sky"),
    "MK2P4LL/A": ("1tb", "star white"),
    "MK2T4LL/A": ("2tb", "night sky"),
    "MK2U4LL/A": ("2tb", "star white"),
}

# Apple Stores in the five NYC boroughs.
NYC_STORES = {
    "R095": "Fifth Avenue",
    "R415": "Grand Central",
    "R250": "West 14th Street",
    "R032": "SoHo",
    "R654": "World Trade Center",
    "R251": "Upper West Side",
    "R582": "Upper East Side",
    "R594": "Williamsburg",
    "R715": "Downtown Brooklyn",
    "R612": "Queens Center",
    "R753": "The Mall at Bay Plaza",
    "R152": "Staten Island",
}

INTERVAL = int(os.getenv("INTERVAL", "60"))
# Comma-separated capacities to watch (e.g. "256gb,512gb"), or "ALL".
CAPACITIES = os.getenv("CAPACITIES", "ALL")
if CAPACITIES.strip().upper() != "ALL":
    wanted = {c.strip().lower() for c in CAPACITIES.split(",")}
    PARTS = {part: spec for part, spec in PARTS.items() if spec[0] in wanted}
    if not PARTS:
        sys.exit(f"CAPACITIES={CAPACITIES!r} matches no models")
# Each location returns its ~12 nearest stores; 10314 is needed to reach Staten Island.
LOCATIONS = [l.strip() for l in os.getenv("LOCATIONS", "10001,10314").split(",") if l.strip()]
# Comma-separated store numbers to watch, or "ALL" for every store returned.
STORES = os.getenv("STORES", ",".join(NYC_STORES))
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Alert once if there has been no successful check for this long (transient errors stay silent).
FAIL_ALERT_MINUTES = int(os.getenv("FAIL_ALERT_MINUTES", "10"))
# Minimum gap between two failure alerts, so a flapping outage doesn't spam.
FAIL_ALERT_COOLDOWN_MINUTES = int(os.getenv("FAIL_ALERT_COOLDOWN_MINUTES", "60"))
# A model counts as gone only after missing from this many consecutive successful checks,
# so a one-off blip in Apple's data doesn't produce a "gone" + duplicate "available" pair.
GONE_AFTER_CHECKS = int(os.getenv("GONE_AFTER_CHECKS", "2"))
# "Wave" mode for big restocks: one alert when it starts, one when it winds down, silence in between.
# Starts when this many (store, model) pairs are available at once...
WAVE_START_ITEMS = int(os.getenv("WAVE_START_ITEMS", "6"))
# ...or when this many stock alerts fire within WAVE_WINDOW_MINUTES (stock trickling in).
WAVE_START_ALERTS = int(os.getenv("WAVE_START_ALERTS", "3"))
WAVE_WINDOW_MINUTES = int(os.getenv("WAVE_WINDOW_MINUTES", "10"))
# Ends once availability drops to this many pairs or fewer.
WAVE_END_ITEMS = int(os.getenv("WAVE_END_ITEMS", "2"))
MAX_STORES_PER_MODEL = 3
# Poll slower outside store hours to save proxy bandwidth. ACTIVE_HOURS is "start-end" in local store time.
NIGHT_INTERVAL = int(os.getenv("NIGHT_INTERVAL", str(INTERVAL)))
ACTIVE_START, ACTIVE_END = (int(h) for h in os.getenv("ACTIVE_HOURS", "0-24").split("-"))
STORE_TZ = ZoneInfo(os.getenv("STORE_TZ", "America/New_York"))
# Optional proxy for Apple requests only (e.g. a residential rotating proxy), http://user:pass@host:port.
PROXY_URL = os.getenv("PROXY_URL", "")
APPLE_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else {})
)


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def product_url(part):
    return "https://www.apple.com/shop/buy-iphone/iphone-duo"
    
def product_name(part):
    capacity, color = PARTS[part]
    return f"{capacity.upper()} {color.capitalize()}"


def fetch_location(location):
    params = [("pl", "true"), ("location", location)]
            raise RuntimeError(f"unexpected response for {location}: {str(data)[:1500]}")
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with APPLE_OPENER.open(req, timeout=30) as resp:
        data = json.load(resp)
    stores = data.get("body", {}).get("stores")
    if stores is None:
        raise RuntimeError(f"unexpected response for {location}: {str(data)[:200]}")
    return stores


def check():
    """Return {(store_number, part): "Store Name (zip)"} for every available combination."""
    watched = None if STORES.strip().upper() == "ALL" else {s.strip() for s in STORES.split(",")}
    available = {}
    seen_stores = set()
    for location in LOCATIONS:
        for store in fetch_location(location):
            number = store["storeNumber"]
            if watched is not None and number not in watched:
                continue
            seen_stores.add(number)
            postal_code = (store.get("address") or {}).get("postalCode")
            label = f"{store['storeName']} ({postal_code})" if postal_code else store["storeName"]
            for part, info in store.get("partsAvailability", {}).items():
                if info.get("pickupDisplay") == "available":
                    available[(number, part)] = label
    # A 200 response with missing stores would silently hide stock, so treat it as a failure.
    if watched is not None:
        missing = watched - seen_stores
        if len(seen_stores) < len(watched) / 2:
            raise RuntimeError(f"only {len(seen_stores)}/{len(watched)} stores in response")
        if missing:
            log(f"WARNING: stores missing from response: {sorted(missing)}")
    elif not seen_stores:
        raise RuntimeError("no stores in response")
    return available, seen_stores


def notify(title, message, click=None, priority="high", links=None):
    """links: list of (label, url) shown as buttons in ntfy (max 3) and as lines in Telegram."""
    links = links or []
    log(f"NOTIFY {title}: {message}")
    if NTFY_TOPIC:
        headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "iphone"}
        if click:
            headers["Click"] = click
        if links:
            headers["Actions"] = "; ".join(f"view, {label}, {url}" for label, url in links[:3])
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=headers, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            log(f"ntfy failed: {exc}")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        text = f"{title}\n{message}"
        text += "".join(f"\n{label}: {url}" for label, url in links) if links else (f"\n{click}" if click else "")
        body = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=body, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            log(f"telegram failed: {exc}")


def format_items(items):
    by_store = {}
    for (number, part), store_name in sorted(items.items(), key=lambda kv: (kv[1], kv[0][1])):
        by_store.setdefault(store_name, []).append(product_name(part))
    return "\n".join(f"{store}: {', '.join(models)}" for store, models in by_store.items())


def format_by_model(items):
    """Compact view for many items: one line per model, a few stores each."""
    by_part = {}
    for (number, part), label in sorted(items.items(), key=lambda kv: kv[1]):
        by_part.setdefault(part, []).append(label)
    lines = []
    for part, stores in sorted(by_part.items(), key=lambda kv: (-len(kv[1]), product_name(kv[0]))):
        shown = ", ".join(stores[:MAX_STORES_PER_MODEL])
        extra = f" and {len(stores) - MAX_STORES_PER_MODEL} more" if len(stores) > MAX_STORES_PER_MODEL else ""
        count = f"{len(stores)} store" + ("s" if len(stores) != 1 else "")
        lines.append(f"{product_name(part)} ({count}): {shown}{extra}")
    return "\n".join(lines)


def format_any(items):
    return format_items(items) if len(items) < WAVE_START_ITEMS else format_by_model(items)


def buy_links(items):
    """Buy buttons for the models with the most stores first."""
    counts = {}
    for _, part in items:
        counts[part] = counts.get(part, 0) + 1
    parts = sorted(counts, key=lambda p: -counts[p])
    return [(f"Buy {product_name(p)}", product_url(p)) for p in parts]


def format_duration(seconds):
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def current_interval():
    hour = datetime.now(STORE_TZ).hour
    return INTERVAL if ACTIVE_START <= hour < ACTIVE_END else NIGHT_INTERVAL


def run(once=False):
    if not (NTFY_TOPIC or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)):
        log("WARNING: no NTFY_TOPIC or TELEGRAM_* configured, alerts only go to the log")
    if PROXY_URL:
        log(f"using proxy {urllib.parse.urlsplit(PROXY_URL).hostname} for Apple requests")

    # (store_number, part) -> {"label": str, "since": epoch seconds, "misses": int}
    tracked = {}
    failures = 0
    first = True
    last_success = time.monotonic()
    last_fail_alert = None
    outage_alerted = False
    in_wave = False
    stock_alert_times = []
    while True:
        try:
            current, seen_stores = check()
            if outage_alerted:
                minutes = int((time.monotonic() - last_success) / 60)
                notify("iPhone monitor: back to normal",
                       f"Apple is responding again after {minutes} min.", priority="low")
            failures = 0
            last_success = time.monotonic()
            outage_alerted = False

            now = time.time()
            new = {}
            for key, label in current.items():
                if key in tracked:
                    tracked[key]["misses"] = 0
                else:
                    tracked[key] = {"label": label, "since": now, "misses": 0}
                    new[key] = label
            gone = {}
            for key in [k for k in tracked if k not in current]:
                tracked[key]["misses"] += 1
                if tracked[key]["misses"] >= GONE_AFTER_CHECKS:
                    gone[key] = tracked.pop(key)

            log(f"ok stores={len(seen_stores)} available={len(current)} new={len(new)} "
                f"gone={len(gone)} wave={in_wave}")
            window = WAVE_WINDOW_MINUTES * 60
            stock_alert_times = [t for t in stock_alert_times if now - t < window]

            if first:
                summary = format_any(current) if current else "Nothing available right now."
                notify("iPhone 18 Pro Max monitor started", summary, priority="low", links=buy_links(current))
                in_wave = len(current) >= WAVE_START_ITEMS
            elif in_wave:
                if len(current) <= WAVE_END_ITEMS:
                    in_wave = False
                    stock_alert_times = []
                    # Whatever sold during the wave is covered by this message; don't report it later.
                    tracked = {k: v for k, v in tracked.items() if k in current}
                    body = "Still available:\n" + format_items(current) if current else "Nothing left."
                    notify("Restock wave is over", body, priority="default", links=buy_links(current))
            elif new and (len(current) >= WAVE_START_ITEMS or len(stock_alert_times) + 1 >= WAVE_START_ALERTS):
                in_wave = True
                notify("Big iPhone 18 Pro Max restock",
                       format_by_model(current) + "\n\nNo more alerts until it winds down.",
                       click=buy_links(current)[0][1], priority="urgent", links=buy_links(current))
            else:
                if new:
                    stock_alert_times.append(now)
                    links = buy_links(new)
                    notify("iPhone 18 Pro Max available for pickup", format_any(new),
                           click=links[0][1], priority="urgent", links=links)
                if gone:
                    lines = [
                        f"{item['label']}: {product_name(part)} (lasted {format_duration(now - item['since'])})"
                        for (_, part), item in sorted(gone.items(), key=lambda kv: kv[1]["label"])
                    ]
                    notify("No longer available", "\n".join(lines), priority="default")
            first = False
        except Exception as exc:
            failures += 1
            log(f"error #{failures}: {exc}")
            down_minutes = (time.monotonic() - last_success) / 60
            cooled_down = last_fail_alert is None or (
                time.monotonic() - last_fail_alert >= FAIL_ALERT_COOLDOWN_MINUTES * 60)
            # With slow night polling, wait for at least two failed checks before alerting.
            alert_after = max(FAIL_ALERT_MINUTES, 2.5 * current_interval() / 60)
            if not outage_alerted and down_minutes >= alert_after and cooled_down:
                notify("iPhone monitor: not working",
                       f"No successful check for {int(down_minutes)} min ({failures} failed attempts). "
                       f"You won't get stock alerts until it recovers. Last error: {exc}",
                       priority="high")
                outage_alerted = True
                last_fail_alert = time.monotonic()

        if once:
            return
        interval = current_interval()
        time.sleep(max(1, interval + random.uniform(-0.15, 0.15) * interval))


if __name__ == "__main__":
    run(once="--once" in sys.argv)
