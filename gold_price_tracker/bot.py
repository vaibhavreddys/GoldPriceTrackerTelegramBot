"""
Gold/Silver Price Telegram Bot
Features:
  - /gold [city]    — Gold prices for a city (default: bangalore)
  - /silver [city]  — Silver prices for a city (default: bangalore)
  - /cities         — List supported cities
  - /subscribe      — Daily 9 AM IST price push
  - /unsubscribe    — Cancel daily subscription
  - /alert <price>  — Alert when 22K gold (per gram) drops below price
  - /myalert        — Show your current alert
  - /cancelalert    — Remove your alert
  - /status         — Cache age + bot uptime
  - /help           — Full help text
"""

from flask import Flask, jsonify
import threading
import os
import time
import logging
import sqlite3
import datetime
from contextlib import contextmanager
from bs4 import BeautifulSoup
import cloudscraper
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config & constants
# ---------------------------------------------------------------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("TOKEN environment variable not set.")

DB_PATH = os.getenv("DB_PATH", "bot_data.db")
CACHE_TTL = 30 * 60        # 30 minutes in seconds
ALERT_CHECK_INTERVAL = 3600  # check alerts every 60 minutes
BOT_START_TIME = time.time()

# Supported metals with their URL slug and display metadata
METALS = {
    "gold": {
        "url_slug": "gold-rates",
        "label": "Gold",
        "emoji": "🥇",
        "section_keyword": "Gold Price",
    },
    "silver": {
        "url_slug": "silver-rates",
        "label": "Silver",
        "emoji": "🥈",
        "section_keyword": "Silver Price",
    },
}

# Supported cities: key = what the user types, value = display name
CITIES = {
    "bangalore":  "Bangalore",
    "mumbai":     "Mumbai",
    "delhi":      "Delhi",
    "chennai":    "Chennai",
    "hyderabad":  "Hyderabad",
    "kolkata":    "Kolkata",
    "pune":       "Pune",
    "ahmedabad":  "Ahmedabad",
    "jaipur":     "Jaipur",
    "surat":      "Surat",
}

DEFAULT_CITY  = "bangalore"
DEFAULT_METAL = "gold"

# ---------------------------------------------------------------------------
# Flask health-check server
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "ok", "message": "Bot server is running"})


def start_flask_server():
    flask_app.run(host="0.0.0.0", port=10000, use_reloader=False)

# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    """Context manager that auto-commits or rolls back."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id   INTEGER PRIMARY KEY,
                city      TEXT    NOT NULL DEFAULT 'bangalore',
                metal     TEXT    NOT NULL DEFAULT 'gold',
                created_at TEXT   NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alerts (
                chat_id        INTEGER PRIMARY KEY,
                metal          TEXT    NOT NULL DEFAULT 'gold',
                city           TEXT    NOT NULL DEFAULT 'bangalore',
                threshold      REAL    NOT NULL,
                created_at     TEXT    NOT NULL
            );
        """)
    logger.info("Database initialised at %s", DB_PATH)


# --- Subscriptions ---

def add_subscription(chat_id: int, city: str, metal: str):
    with db() as conn:
        conn.execute("""
            INSERT INTO subscriptions (chat_id, city, metal, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET city=excluded.city,
                                               metal=excluded.metal,
                                               created_at=excluded.created_at
        """, (chat_id, city, metal, datetime.datetime.utcnow().isoformat()))


def remove_subscription(chat_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
        return cur.rowcount > 0


def get_all_subscriptions() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT chat_id, city, metal FROM subscriptions").fetchall()
        return [dict(r) for r in rows]


def get_subscription(chat_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT chat_id, city, metal FROM subscriptions WHERE chat_id = ?",
            (chat_id,)
        ).fetchone()
        return dict(row) if row else None


# --- Alerts ---

def set_alert(chat_id: int, metal: str, city: str, threshold: float):
    with db() as conn:
        conn.execute("""
            INSERT INTO alerts (chat_id, metal, city, threshold, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET metal=excluded.metal,
                                               city=excluded.city,
                                               threshold=excluded.threshold,
                                               created_at=excluded.created_at
        """, (chat_id, metal, city, threshold, datetime.datetime.utcnow().isoformat()))


def remove_alert(chat_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM alerts WHERE chat_id = ?", (chat_id,))
        return cur.rowcount > 0


def get_alert(chat_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT chat_id, metal, city, threshold FROM alerts WHERE chat_id = ?",
            (chat_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_alerts() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, metal, city, threshold FROM alerts"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Price cache  {(metal, city): {"data": str, "timestamp": float, "price": float}}
# ---------------------------------------------------------------------------
_price_cache: dict[tuple, dict] = {}


def _cache_key(metal: str, city: str) -> tuple:
    return (metal.lower(), city.lower())


def _get_cached(metal: str, city: str) -> dict | None:
    entry = _price_cache.get(_cache_key(metal, city))
    if entry and (time.time() - entry["timestamp"]) < CACHE_TTL:
        return entry
    return None


def _set_cache(metal: str, city: str, message: str, price: float):
    _price_cache[_cache_key(metal, city)] = {
        "data":      message,
        "timestamp": time.time(),
        "price":     price,
    }

# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def _parse_price_from_cell(text: str) -> float | None:
    """
    Extract a numeric price from a cell like '₹6,123' or '6,123.50'.
    Returns None if parsing fails.
    """
    cleaned = text.replace("₹", "").replace(",", "").strip()
    # Take first token in case there's trailing text
    token = cleaned.split()[0] if cleaned.split() else ""
    try:
        return float(token)
    except ValueError:
        return None


def _display_len(s: str) -> int:
    """Visual display width of a string — emoji count as 2, ASCII as 1."""
    width = 0
    for ch in s:
        cp = ord(ch)
        if (
            0x1100 <= cp <= 0x115F
            or 0x2E80 <= cp <= 0x303E
            or 0x3040 <= cp <= 0x33FF
            or 0x3400 <= cp <= 0x4DBF
            or 0x4E00 <= cp <= 0xA4CF
            or 0xAC00 <= cp <= 0xD7AF
            or 0xF900 <= cp <= 0xFAFF
            or 0xFE10 <= cp <= 0xFE1F
            or 0xFE30 <= cp <= 0xFE4F
            or 0xFF00 <= cp <= 0xFF60
            or 0xFFE0 <= cp <= 0xFFE6
            or 0x1F300 <= cp <= 0x1FAFF   # Emoji (covers 🔴 🟢 and most others)
            or 0x20000 <= cp <= 0x2A6DF
        ):
            width += 2
        else:
            width += 1
    return width


def _pad_right(s: str, width: int) -> str:
    return s + " " * max(0, width - _display_len(s))


def _pad_center(s: str, width: int) -> str:
    padding = max(0, width - _display_len(s))
    left = padding // 2
    return " " * left + s + " " * (padding - left)


def _build_table_str(headers: list[str], rows: list[list[str]]) -> str:
    """
    Build a monospace-aligned table string.
    Emojis are added to the Change column (index 3) BEFORE width calculation.
    """
    COL_CHANGE = 3
    for row in rows:
        if len(row) > COL_CHANGE:
            change = row[COL_CHANGE]
            is_negative = change.startswith("−") or change.startswith("-")
            row[COL_CHANGE] = f"{'🔴' if is_negative else '🟢'} {change}"

    col_count = len(headers)
    column_widths = [
        max(_display_len(headers[i]), max(_display_len(row[i]) for row in rows))
        for i in range(col_count)
    ]

    separator  = "-+-".join("-" * w for w in column_widths)
    header_row = " | ".join(_pad_center(headers[i], column_widths[i]) for i in range(col_count))

    lines = [header_row, separator]
    for row in rows:
        lines.append(" | ".join(_pad_right(row[i], column_widths[i]) for i in range(col_count)))

    return "\n".join(lines)


def get_metal_prices(metal: str, city: str, force_refresh: bool = False) -> str:
    """
    Main scraping function. Returns a formatted HTML string for Telegram.
    Raises ValueError for unsupported metal/city.
    Raises RuntimeError on network or parse failures.
    """
    metal = metal.lower()
    city  = city.lower()

    if metal not in METALS:
        raise ValueError(f"Unsupported metal '{metal}'. Use: {', '.join(METALS)}")
    if city not in CITIES:
        raise ValueError(
            f"Unsupported city '{city}'.\nUse /cities to see the full list."
        )

    # Return cache if fresh
    if not force_refresh:
        cached = _get_cached(metal, city)
        if cached:
            logger.info("Cache hit for %s/%s", metal, city)
            return cached["data"]

    metal_info = METALS[metal]
    city_name  = CITIES[city]
    url = f"https://www.goodreturns.in/{metal_info['url_slug']}/{city}.html"

    # --- Network fetch ---
    # IMPORTANT: use plain cloudscraper.create_scraper() with NO extra arguments.
    # Passing browser config or custom headers overrides cloudscraper's own
    # carefully crafted browser emulation and causes 403 errors.
    try:
        scraper  = cloudscraper.create_scraper()
        response = scraper.get(url, timeout=15)
    except Exception as exc:
        logger.error("Network error fetching %s: %s", url, exc)
        return (
            f"⚠️ <b>Network error fetching {metal_info['label']} prices.</b>\n\n"
            f"🔗 Check manually: <a href='{url}'>{city_name} {metal_info['label']} Prices</a>\n"
            f"<i>Please try again in a few minutes.</i>"
        )

    if response.status_code != 200:
        logger.warning("HTTP %d for %s", response.status_code, url)
        return (
            f"⚠️ <b>Source website returned HTTP {response.status_code}.</b>\n\n"
            f"🔗 Check manually: <a href='{url}'>{city_name} {metal_info['label']} Prices</a>\n"
            f"<i>Please try again in a few minutes.</i>"
        )

    # --- Parse ---
    try:
        soup = BeautifulSoup(response.text, "html.parser")

        # Find the relevant section by checking data-gr-title contains the
        # metal keyword — more resilient than an exact string match
        section = None
        for sec in soup.find_all("section", attrs={"data-gr-title": True}):
            title = sec.get("data-gr-title", "")
            if metal_info["section_keyword"].lower() in title.lower():
                section = sec
                break

        if not section:
            raise RuntimeError(
                f"Could not locate the {metal_info['label']} price section. "
                "The website layout may have changed."
            )

        # 'table-conatiner' is a typo on the source website itself
        table = section.find("table", {"class": "table-conatiner"})
        if not table:
            raise RuntimeError(
                f"Could not find the price table for {metal_info['label']}. "
                "The website layout may have changed."
            )

        thead = table.find("thead")
        tbody = table.find("tbody")
        if not thead or not tbody:
            raise RuntimeError("Malformed table structure on the source website.")

        headers = [th.get_text(strip=True) for th in thead.find_all("th")]
        if not headers:
            raise RuntimeError("Could not read table headers.")

        raw_rows: list[list[str]] = []
        for tr in tbody.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) == len(headers):  # skip malformed / ad rows
                raw_rows.append(cells)

        if not raw_rows:
            raise RuntimeError("No data rows found in the table.")

        # Extract the current price from the first data row, second column (index 1)
        current_price: float | None = None
        if len(raw_rows[0]) > 1:
            current_price = _parse_price_from_cell(raw_rows[0][1])

        table_str = _build_table_str(headers, raw_rows)

        fetched_at = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p IST")
        emoji      = metal_info["emoji"]
        label      = metal_info["label"]

        message = (
            f"{emoji} <b>Today's {label} Prices in {city_name}</b> {emoji}\n\n"
            f"<pre><code>{table_str}</code></pre>\n\n"
            f"<i>🕐 Fetched at: {fetched_at}</i>\n"
            f"<i>📊 Source: <a href=\"{url}\">GoodReturns.in</a></i>"
        )

        _set_cache(metal, city, message, current_price or 0.0)
        return message

    except RuntimeError as exc:
        # Parse failures return a fallback URL message instead of crashing
        logger.error("Parse error for %s/%s: %s", metal, city, exc)
        return (
            f"⚠️ <b>Could not parse {metal_info['label']} prices.</b>\n"
            f"<i>{exc}</i>\n\n"
            f"🔗 Check manually: <a href='{url}'>{city_name} {metal_info['label']} Prices</a>"
        )
    except Exception as exc:
        logger.exception("Unexpected parse error for %s/%s: %s", metal, city, exc)
        return (
            f"⚠️ <b>Unexpected error processing {metal_info['label']} prices.</b>\n\n"
            f"🔗 Check manually: <a href='{url}'>{city_name} {metal_info['label']} Prices</a>"
        )

# ---------------------------------------------------------------------------
# Helpers for command handlers
# ---------------------------------------------------------------------------

def _parse_metal_city_args(args: list[str], default_metal: str) -> tuple[str, str]:
    """
    Parse optional [city] argument from a command.
    Example: /gold mumbai  →  ("gold", "mumbai")
    """
    city  = args[0].lower() if args else DEFAULT_CITY
    return default_metal, city


async def _fetch_and_reply(
    update: Update,
    metal: str,
    city: str,
    force: bool = False,
) -> None:
    """Send a loading message, fetch prices, edit with result."""
    loading = await update.message.reply_text("⏳ Fetching prices, please wait…")
    try:
        text = get_metal_prices(metal, city, force_refresh=force)
        await loading.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except ValueError as exc:
        # Only ValueError is raised now (bad metal/city args)
        await loading.edit_text(f"❌ {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in _fetch_and_reply: %s", exc)
        await loading.edit_text("⚠️ An unexpected error occurred. Please try again.")

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        "👋 <b>Welcome to the Metal Price Bot!</b>\n\n"
        "I fetch live gold &amp; silver prices across major Indian cities.\n\n"
        "Use /help to see all available commands.",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        "📖 <b>Available Commands</b>\n\n"
        "<b>Prices</b>\n"
        "/gold [city]     — Gold prices (default: Bangalore)\n"
        "/silver [city]   — Silver prices (default: Bangalore)\n"
        "/cities          — List all supported cities\n\n"
        "<b>Daily Subscription</b>\n"
        "/subscribe [metal] [city]  — Get prices every day at 9 AM IST\n"
        "/unsubscribe               — Cancel your daily subscription\n\n"
        "<b>Price Alerts</b>\n"
        "/alert &lt;price&gt; [metal] [city]\n"
        "  — Notify when price drops below threshold\n"
        "  — Example: /alert 6500 gold bangalore\n"
        "/myalert      — Show your current alert\n"
        "/cancelalert  — Remove your alert\n\n"
        "<b>Other</b>\n"
        "/status  — Cache age &amp; uptime info\n"
        "/help    — This message",
        parse_mode="HTML",
    )


async def cmd_gold(update: Update, context: CallbackContext) -> None:
    _, city = _parse_metal_city_args(context.args or [], DEFAULT_METAL)
    force   = "refresh" in (context.args or [])
    await _fetch_and_reply(update, "gold", city if city != "refresh" else DEFAULT_CITY, force)


async def cmd_silver(update: Update, context: CallbackContext) -> None:
    _, city = _parse_metal_city_args(context.args or [], "silver")
    force   = "refresh" in (context.args or [])
    await _fetch_and_reply(update, "silver", city if city != "refresh" else DEFAULT_CITY, force)


async def cmd_cities(update: Update, context: CallbackContext) -> None:
    city_list = "\n".join(f"• {key}  →  {name}" for key, name in CITIES.items())
    await update.message.reply_text(
        "🏙️ <b>Supported Cities</b>\n\n"
        f"<code>{city_list}</code>\n\n"
        "Usage example: <code>/gold mumbai</code>",
        parse_mode="HTML",
    )


# --- Subscribe ---

async def cmd_subscribe(update: Update, context: CallbackContext) -> None:
    """
    /subscribe [metal] [city]
    Registers the user for daily 9 AM IST price updates.
    """
    args  = [a.lower() for a in (context.args or [])]
    metal = args[0] if args and args[0] in METALS   else DEFAULT_METAL
    city  = args[1] if len(args) > 1 and args[1] in CITIES else DEFAULT_CITY
    # Handle case where user only passed city (not metal)
    if args and args[0] in CITIES and args[0] not in METALS:
        city  = args[0]
        metal = DEFAULT_METAL

    chat_id = update.effective_chat.id
    add_subscription(chat_id, city, metal)
    metal_label = METALS[metal]["label"]
    city_label  = CITIES[city]

    await update.message.reply_text(
        f"✅ <b>Subscribed!</b>\n\n"
        f"You'll receive <b>{metal_label}</b> prices for <b>{city_label}</b> "
        f"every day at <b>9:00 AM IST</b>.\n\n"
        f"Use /unsubscribe to cancel.",
        parse_mode="HTML",
    )


async def cmd_unsubscribe(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    removed = remove_subscription(chat_id)
    if removed:
        await update.message.reply_text("✅ You've been unsubscribed from daily price updates.")
    else:
        await update.message.reply_text(
            "ℹ️ You don't have an active subscription.\nUse /subscribe to sign up."
        )


# --- Alert ---

async def cmd_alert(update: Update, context: CallbackContext) -> None:
    """
    /alert <price> [metal] [city]
    Set a threshold alert. Fires when current price < threshold.
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "❌ Usage: <code>/alert &lt;price&gt; [metal] [city]</code>\n"
            "Example: <code>/alert 6500 gold bangalore</code>",
            parse_mode="HTML",
        )
        return

    # Validate threshold
    try:
        threshold = float(args[0].replace(",", ""))
        if threshold <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid price. Please enter a positive number.\n"
            "Example: <code>/alert 6500</code>",
            parse_mode="HTML",
        )
        return

    lower_args = [a.lower() for a in args[1:]]
    metal = DEFAULT_METAL
    city  = DEFAULT_CITY

    for arg in lower_args:
        if arg in METALS:
            metal = arg
        elif arg in CITIES:
            city = arg

    chat_id = update.effective_chat.id
    set_alert(chat_id, metal, city, threshold)
    metal_label = METALS[metal]["label"]
    city_label  = CITIES[city]

    await update.message.reply_text(
        f"🔔 <b>Alert Set!</b>\n\n"
        f"I'll notify you when <b>{metal_label}</b> price in <b>{city_label}</b> "
        f"drops below <b>₹{threshold:,.2f}</b>.\n\n"
        f"Use /cancelalert to remove it.",
        parse_mode="HTML",
    )


async def cmd_myalert(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    alert   = get_alert(chat_id)
    if not alert:
        await update.message.reply_text(
            "ℹ️ You have no active alert.\n"
            "Use <code>/alert &lt;price&gt;</code> to set one.",
            parse_mode="HTML",
        )
        return

    metal_label = METALS[alert["metal"]]["label"]
    city_label  = CITIES.get(alert["city"], alert["city"].title())
    await update.message.reply_text(
        f"🔔 <b>Your Active Alert</b>\n\n"
        f"Metal: <b>{metal_label}</b>\n"
        f"City:  <b>{city_label}</b>\n"
        f"Threshold: <b>₹{alert['threshold']:,.2f}</b>\n\n"
        f"Alert fires when price drops <i>below</i> this value.",
        parse_mode="HTML",
    )


async def cmd_cancelalert(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    removed = remove_alert(chat_id)
    if removed:
        await update.message.reply_text("✅ Your price alert has been removed.")
    else:
        await update.message.reply_text(
            "ℹ️ You have no active alert to cancel.\n"
            "Use <code>/alert &lt;price&gt;</code> to create one.",
            parse_mode="HTML",
        )


async def cmd_status(update: Update, context: CallbackContext) -> None:
    uptime_secs = int(time.time() - BOT_START_TIME)
    hours, rem  = divmod(uptime_secs, 3600)
    mins, secs  = divmod(rem, 60)

    lines = [f"🤖 <b>Bot Status</b>\n", f"⏱ Uptime: {hours}h {mins}m {secs}s\n"]

    if _price_cache:
        lines.append("\n📦 <b>Cache Entries</b>")
        for (metal, city), entry in _price_cache.items():
            age_secs = int(time.time() - entry["timestamp"])
            age_str  = f"{age_secs // 60}m {age_secs % 60}s ago"
            lines.append(f"• {metal}/{city} — fetched {age_str}")
    else:
        lines.append("📦 Cache: empty")

    sub_count   = len(get_all_subscriptions())
    alert_count = len(get_all_alerts())
    lines.append(f"\n👥 Subscriptions: {sub_count}")
    lines.append(f"🔔 Active alerts: {alert_count}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

async def job_daily_prices(context: CallbackContext) -> None:
    """Runs daily at 9 AM IST. Sends prices to all subscribers."""
    subs = get_all_subscriptions()
    logger.info("Daily job: sending to %d subscribers", len(subs))
    for sub in subs:
        chat_id = sub["chat_id"]
        metal   = sub["metal"]
        city    = sub["city"]
        try:
            msg = get_metal_prices(metal, city)
            header = (
                f"🌅 <b>Good morning! Your daily {METALS[metal]['label']} update:</b>\n\n"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=header + msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Failed to send daily update to %s: %s", chat_id, exc)


async def job_check_alerts(context: CallbackContext) -> None:
    """Runs every hour. Fires alerts when price drops below threshold."""
    alerts = get_all_alerts()
    if not alerts:
        return

    logger.info("Alert check: %d active alerts", len(alerts))

    # Group by (metal, city) to minimise scrape calls
    groups: dict[tuple, list[dict]] = {}
    for alert in alerts:
        key = (alert["metal"], alert["city"])
        groups.setdefault(key, []).append(alert)

    for (metal, city), group_alerts in groups.items():
        # Use cached price if available; otherwise scrape fresh
        cached = _get_cached(metal, city)
        current_price: float | None = cached["price"] if cached else None

        if current_price is None:
            try:
                get_metal_prices(metal, city)  # populates cache as side-effect
                cached = _get_cached(metal, city)
                current_price = cached["price"] if cached else None
            except Exception as exc:
                logger.warning("Alert check scrape failed for %s/%s: %s", metal, city, exc)
                continue

        if current_price is None or current_price <= 0:
            continue

        metal_label = METALS[metal]["label"]
        city_label  = CITIES.get(city, city.title())

        for alert in group_alerts:
            if current_price < alert["threshold"]:
                chat_id   = alert["chat_id"]
                threshold = alert["threshold"]
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🔔 <b>Price Alert Triggered!</b>\n\n"
                            f"{METALS[metal]['emoji']} <b>{metal_label}</b> in <b>{city_label}</b> "
                            f"is now <b>₹{current_price:,.2f}</b>, which is below your "
                            f"threshold of <b>₹{threshold:,.2f}</b>.\n\n"
                            f"Use /cancelalert if you no longer need this alert."
                        ),
                        parse_mode="HTML",
                    )
                    logger.info(
                        "Alert fired for chat_id=%s (%s/%s < %.2f)",
                        chat_id, metal, city, threshold,
                    )
                except Exception as exc:
                    logger.warning("Failed to send alert to %s: %s", chat_id, exc)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()

    # Flask health-check in daemon thread
    server_thread = threading.Thread(target=start_flask_server, daemon=True)
    server_thread.start()

    application = Application.builder().token(TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start",        cmd_start))
    application.add_handler(CommandHandler("help",         cmd_help))
    application.add_handler(CommandHandler("gold",         cmd_gold))
    application.add_handler(CommandHandler("silver",       cmd_silver))
    application.add_handler(CommandHandler("cities",       cmd_cities))
    application.add_handler(CommandHandler("subscribe",    cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe",  cmd_unsubscribe))
    application.add_handler(CommandHandler("alert",        cmd_alert))
    application.add_handler(CommandHandler("myalert",      cmd_myalert))
    application.add_handler(CommandHandler("cancelalert",  cmd_cancelalert))
    application.add_handler(CommandHandler("status",       cmd_status))

    # Schedule background jobs -- requires python-telegram-bot[job-queue]
    if application.job_queue is None:
        logger.critical(
            "JobQueue is not available. Background jobs (daily prices, alerts) are DISABLED.\n"
            "Fix: pip install \"python-telegram-bot[job-queue]\"\n"
            "     Ensure your requirements.txt has:  python-telegram-bot[job-queue]"
        )
    else:
        # 9:00 AM IST = 03:30 UTC
        application.job_queue.run_daily(
            job_daily_prices,
            time=datetime.time(3, 30, 0, tzinfo=datetime.timezone.utc),
            name="daily_prices",
        )
        # Alert check every hour
        application.job_queue.run_repeating(
            job_check_alerts,
            interval=ALERT_CHECK_INTERVAL,
            first=60,
            name="alert_check",
        )
        logger.info("JobQueue active: daily prices at 09:00 IST, alerts every %ds.", ALERT_CHECK_INTERVAL)

    logger.info("Bot started. Polling for updates…")
    application.run_polling()


if __name__ == "__main__":
    main()
