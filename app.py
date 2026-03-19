import os
import re
import sys
import statistics
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from supabase import create_client, Client


def resource_path(relative_path: str) -> str:
    """Resolve template/static paths for normal execution and PyInstaller builds."""
    base_path = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base_path, relative_path)


app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static")
)
app.config["JSON_SORT_KEYS"] = False

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
PORT = int(os.environ.get("PORT", 5000))
DEBUG = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
SCRAPE_INTERVAL_MINUTES = int(os.environ.get("SCRAPE_INTERVAL_MINUTES", 60))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", 20))

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Missing SUPABASE_URL or SUPABASE_KEY environment variables. "
        "Use your Supabase project URL and key before starting the app."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
scheduler = BackgroundScheduler(daemon=True, timezone="UTC")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str) -> datetime:
    if not value:
        return utc_now()
    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def normalize_amazon_url(raw_url: str):
    """Normalize Amazon URLs and extract ASIN when available."""
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise ValueError("Amazon product URL is required.")

    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)
    if "amazon." not in parsed.netloc.lower():
        raise ValueError("Please enter a valid Amazon product URL.")

    asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path, re.IGNORECASE)
    asin = asin_match.group(1).upper() if asin_match else None

    normalized_path = f"/dp/{asin}" if asin else parsed.path
    cleaned = urlunparse((parsed.scheme or "https", parsed.netloc, normalized_path, "", "", ""))
    return cleaned, asin


def extract_numeric_price(text: str):
    """Extract float price from raw Amazon price text."""
    if not text:
        return None
    cleaned = text.replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", cleaned)
    return round(float(match.group(1)), 2) if match else None


def detect_currency_symbol(text: str) -> str:
    for symbol in ["₹", "$", "£", "€"]:
        if symbol in text:
            return symbol
    return "₹"


def fetch_html(url: str) -> str:
    """Download Amazon HTML with browser-like headers."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "close"
    }
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def scrape_amazon_product(url: str) -> dict:
    """Scrape title and price from an Amazon product page using BeautifulSoup."""
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(" ", strip=True) if title_el else "Amazon Product"

    selectors = [
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#price_inside_buybox",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        "#tp_price_block_total_price_ww .a-offscreen",
        ".apexPriceToPay .a-offscreen",
        ".a-price .a-offscreen"
    ]

    found = []
    for selector in selectors:
        for el in soup.select(selector):
            raw = el.get_text(" ", strip=True)
            value = extract_numeric_price(raw)
            if value:
                found.append((raw, value))

    if not found:
        html_text = str(soup)
        fallback = re.findall(r"[₹$£€]\s?[0-9,]+(?:\.[0-9]{1,2})?", html_text)
        if fallback:
            raw = fallback[0]
            value = extract_numeric_price(raw)
            if value:
                found.append((raw, value))

    if not found:
        raise ValueError("Could not extract price from this Amazon page. Amazon may have changed markup or blocked the request.")

    raw_price, price = found[0]
    currency = detect_currency_symbol(raw_price)

    return {
        "title": title,
        "price": price,
        "currency": currency
    }


def purge_old_history() -> None:
    """Delete price history older than 365 days to keep a rolling 1-year window."""
    cutoff = (utc_now() - timedelta(days=365)).isoformat()
    supabase.table("price_history").delete().lt("captured_at", cutoff).execute()


def get_product_by_url(amazon_url: str):
    result = supabase.table("products").select("*").eq("amazon_url", amazon_url).limit(1).execute()
    return result.data[0] if result.data else None


def get_product_by_id(product_id: int):
    result = supabase.table("products").select("*").eq("id", product_id).limit(1).execute()
    return result.data[0] if result.data else None


def create_or_update_product(amazon_url: str, asin: str, title: str, currency: str):
    """Create a new tracked product or update its basic metadata."""
    existing = get_product_by_url(amazon_url)

    payload = {
        "amazon_url": amazon_url,
        "asin": asin,
        "title": title,
        "currency": currency,
        "updated_at": utc_now().isoformat()
    }

    if existing:
        updated = supabase.table("products").update(payload).eq("id", existing["id"]).execute()
        return updated.data[0] if updated.data else existing

    payload["is_active"] = True
    created = supabase.table("products").insert(payload).execute()
    if not created.data:
        raise RuntimeError("Failed to create product record in Supabase.")
    return created.data[0]


def insert_price_snapshot(product_id: int, price: float):
    """Insert a point in the price history table."""
    inserted = supabase.table("price_history").insert({
        "product_id": product_id,
        "price": price,
        "captured_at": utc_now().isoformat()
    }).execute()
    return inserted.data[0] if inserted.data else None


def get_price_history(product_id: int):
    """Get 1-year price history ordered from oldest to newest."""
    cutoff = (utc_now() - timedelta(days=365)).isoformat()
    result = (
        supabase.table("price_history")
        .select("id, product_id, price, captured_at")
        .eq("product_id", product_id)
        .gte("captured_at", cutoff)
        .order("captured_at", desc=False)
        .execute()
    )
    return result.data or []


def generate_signal(current_price: float, lowest_price: float, average_price: float) -> str:
    if abs(current_price - lowest_price) < 0.01:
        return "BUY NOW"
    if current_price < average_price:
        return "GOOD DEAL"
    if current_price > (average_price * 1.15):
        return "OVERPRICED"
    return "WAIT"


def detect_anomaly(prices):
    """Basic z-score anomaly detection over available history."""
    if len(prices) < 5:
        return False

    mean_price = statistics.mean(prices)
    std_dev = statistics.pstdev(prices)
    if std_dev == 0:
        return False

    current = prices[-1]
    z_score = abs(current - mean_price) / std_dev
    return z_score >= 2.0


def predict_price_7d(history_rows):
    """Lightweight predictive engine using linear trend + moving average blend."""
    if not history_rows:
        return 0.0
    if len(history_rows) == 1:
        return round(float(history_rows[0]["price"]), 2)

    recent = history_rows[-60:] if len(history_rows) > 60 else history_rows
    first_time = parse_iso_datetime(recent[0]["captured_at"])

    xs = []
    ys = []
    for row in recent:
        row_time = parse_iso_datetime(row["captured_at"])
        delta_days = (row_time - first_time).total_seconds() / 86400.0
        xs.append(delta_days)
        ys.append(float(row["price"]))

    if len(set(xs)) <= 1:
        return round(ys[-1], 2)

    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    slope = (numerator / denominator) if denominator else 0.0
    intercept = y_mean - (slope * x_mean)

    future_x = xs[-1] + 7
    regression_prediction = intercept + slope * future_x
    moving_avg = statistics.mean(ys[-7:] if len(ys) >= 7 else ys)
    blended = (0.6 * regression_prediction) + (0.4 * moving_avg)

    current_price = ys[-1]
    lower_bound = current_price * 0.75
    upper_bound = current_price * 1.25
    blended = max(lower_bound, min(upper_bound, blended))

    return round(blended, 2)


def calculate_analytics(history_rows):
    """Compute dashboard-ready analytics for a product."""
    if not history_rows:
        return {
            "current_price": 0,
            "lowest_price": 0,
            "highest_price": 0,
            "average_price": 0,
            "predicted_price_7d": 0,
            "recommendation": "WAIT",
            "anomaly_detected": False,
            "volatility_percent": 0,
            "samples": 0
        }

    prices = [float(row["price"]) for row in history_rows]
    current_price = round(prices[-1], 2)
    lowest_price = round(min(prices), 2)
    highest_price = round(max(prices), 2)
    average_price = round(sum(prices) / len(prices), 2)
    volatility_percent = 0.0

    if len(prices) > 1 and average_price > 0:
        volatility_percent = round((statistics.pstdev(prices) / average_price) * 100, 2)

    predicted_price_7d = predict_price_7d(history_rows)
    recommendation = generate_signal(current_price, lowest_price, average_price)
    anomaly_detected = detect_anomaly(prices)

    return {
        "current_price": current_price,
        "lowest_price": lowest_price,
        "highest_price": highest_price,
        "average_price": average_price,
        "predicted_price_7d": predicted_price_7d,
        "recommendation": recommendation,
        "anomaly_detected": anomaly_detected,
        "volatility_percent": volatility_percent,
        "samples": len(prices)
    }


def update_product_summary(product_id: int, analytics: dict):
    """Store latest summary fields on the product row for quick access."""
    payload = {
        "last_known_price": analytics["current_price"],
        "recommendation": analytics["recommendation"],
        "predicted_price_7d": analytics["predicted_price_7d"],
        "anomaly_flag": analytics["anomaly_detected"],
        "last_scraped_at": utc_now().isoformat(),
        "updated_at": utc_now().isoformat()
    }
    supabase.table("products").update(payload).eq("id", product_id).execute()


def serialize_history(history_rows):
    return [
        {
            "timestamp": row["captured_at"],
            "price": float(row["price"])
        }
        for row in history_rows
    ]


def build_response(product_id: int):
    product = get_product_by_id(product_id)
    if not product:
        raise ValueError("Product not found.")

    history_rows = get_price_history(product_id)
    analytics = calculate_analytics(history_rows)

    return {
        "product": {
            "id": product["id"],
            "title": product["title"],
            "url": product["amazon_url"],
            "asin": product.get("asin"),
            "currency": product.get("currency") or "₹"
        },
        "analytics": analytics,
        "history": serialize_history(history_rows)
    }


def track_product_now(raw_url: str):
    """Immediate tracking workflow used by the UI and the scheduler."""
    clean_url, asin = normalize_amazon_url(raw_url)
    scraped = scrape_amazon_product(clean_url)

    purge_old_history()
    product = create_or_update_product(clean_url, asin, scraped["title"], scraped["currency"])
    insert_price_snapshot(product["id"], scraped["price"])

    history_rows = get_price_history(product["id"])
    analytics = calculate_analytics(history_rows)
    update_product_summary(product["id"], analytics)

    return build_response(product["id"])


def refresh_all_tracked_products():
    """APScheduler job to refresh every active tracked product periodically."""
    try:
        purge_old_history()
        result = supabase.table("products").select("id, amazon_url, is_active").eq("is_active", True).execute()
        products = result.data or []
        for product in products:
            try:
                track_product_now(product["amazon_url"])
                print(f"[Scheduler] Refreshed product {product['id']}")
            except Exception as exc:
                print(f"[Scheduler] Failed product {product['id']}: {exc}")
    except Exception as exc:
        print(f"[Scheduler] Global refresh error: {exc}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/track", methods=["POST"])
def api_track():
    payload = request.get_json(silent=True) or {}
    raw_url = (payload.get("url") or "").strip()
    if not raw_url:
        return jsonify({"error": "Amazon product URL is required."}), 400

    try:
        return jsonify(track_product_now(raw_url))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": f"Network error while contacting Amazon: {exc}"}), 502
    except Exception as exc:
        return jsonify({"error": f"Unexpected server error: {exc}"}), 500


@app.route("/api/history/<int:product_id>", methods=["GET"])
def api_history(product_id: int):
    try:
        return jsonify(build_response(product_id))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": f"Could not load history: {exc}"}), 500


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "ok",
        "service": "AI SmartCart",
        "server": f"http://127.0.0.1:{PORT}",
        "scheduler_interval_minutes": SCRAPE_INTERVAL_MINUTES
    })


def start_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(
        refresh_all_tracked_products,
        trigger="interval",
        minutes=SCRAPE_INTERVAL_MINUTES,
        id="refresh_all_tracked_products",
        replace_existing=True,
        max_instances=1
    )
    scheduler.start()
    print(f"[Scheduler] Started with {SCRAPE_INTERVAL_MINUTES}-minute interval.")


if __name__ == "__main__":
    # Avoid duplicate scheduler startup under Flask debug reloader.
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()

    print(f"AI SmartCart running on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=DEBUG)
