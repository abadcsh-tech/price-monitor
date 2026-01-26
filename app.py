import asyncio
import logging
import os
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

from db import AlertDB
from scraper import scrape_best_prices
from notifier import send_telegram_alert

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("app")

# --- App setup ---
BASE_DIR = Path(__file__).parent
app = Flask(__name__)
db = AlertDB(os.environ.get("DB_PATH", str(BASE_DIR / "price_alerts.db")))
db.seed_from_config()

scheduler = BackgroundScheduler(daemon=True)
SCAN_JOB_ID = "price_scan"

# --- Scan logic ---

def scan_job():
    """Run one scrape-filter-notify cycle using DB rules and settings."""
    logger.info("=== Starting scan ===")

    rules = db.get_enabled_rules()
    if not rules:
        logger.info("No enabled rules. Skipping scan.")
        return

    url = db.get_setting("monitoring_url",
                         "https://www.toppreise.ch/new-best-prices")
    bot_token = db.get_setting("telegram_bot_token")
    chat_id = db.get_setting("telegram_chat_id")

    # Run async scraper in a new event loop
    try:
        products = asyncio.run(scrape_best_prices(url=url, rules=rules))
    except Exception as e:
        logger.error("Scraping failed: %s", e)
        return

    if not products:
        logger.info("No qualifying products found.")
        return

    logger.info("Found %d qualifying product(s)", len(products))

    # Deduplicate by (url, price) — keep first occurrence only
    seen = set()
    unique_products = []
    for p in products:
        key = (p.get("url") or p["name"], p["new_price"])
        if key not in seen:
            seen.add(key)
            unique_products.append(p)
    products = unique_products

    # Filter out already-alerted products
    new_products = []
    for p in products:
        key_url = p.get("url") or p["name"]
        key_price = p["new_price"]
        if not db.already_alerted(key_url, key_price):
            new_products.append(p)
        else:
            logger.debug("Already alerted: %s @ %s", p["name"], key_price)

    if not new_products:
        logger.info("All products already alerted. Nothing to send.")
        return

    logger.info("%d new product(s) to alert", len(new_products))

    # Send Telegram notification
    if bot_token and chat_id:
        try:
            asyncio.run(send_telegram_alert(bot_token, chat_id, new_products))
        except Exception as e:
            logger.error("Failed to send Telegram alert: %s", e)
    else:
        logger.warning("Telegram not configured. Skipping notification.")

    # Record alerts
    for p in new_products:
        key_url = p.get("url") or p["name"]
        db.record_alert(
            product_url=key_url,
            price=p["new_price"],
            product_name=p["name"],
            discount=p["discount"],
            rule_id=p.get("matched_rule_id"),
        )

    # Cleanup old records
    db.cleanup_old(hours=24)
    logger.info("=== Scan complete ===")


def _start_scheduler():
    interval = int(db.get_setting("monitoring_interval_minutes", "30"))
    if scheduler.get_job(SCAN_JOB_ID):
        scheduler.remove_job(SCAN_JOB_ID)
    scheduler.add_job(
        scan_job,
        "interval",
        minutes=interval,
        id=SCAN_JOB_ID,
        max_instances=1,
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()
    logger.info("Scheduler started (every %d min)", interval)


def _stop_scheduler():
    if scheduler.get_job(SCAN_JOB_ID):
        scheduler.remove_job(SCAN_JOB_ID)
    logger.info("Scheduler stopped")


def _is_monitoring():
    return scheduler.get_job(SCAN_JOB_ID) is not None


# --- Routes ---

@app.route("/")
def index():
    rules = db.get_all_rules()
    history = db.get_alert_history(limit=50)
    monitoring = _is_monitoring()
    interval = db.get_setting("monitoring_interval_minutes", "30")
    return render_template("index.html",
                           rules=rules,
                           history=history,
                           monitoring=monitoring,
                           interval=interval)


@app.route("/rules", methods=["POST"])
def add_rule():
    rule_type = request.form.get("rule_type", "brand")
    value = request.form.get("value", "").strip()
    try:
        min_discount = float(request.form.get("min_discount_percent", "20"))
    except ValueError:
        min_discount = 20.0

    if value:
        db.add_rule(rule_type, value, min_discount)
    return redirect(url_for("index"))


@app.route("/rules/<int:rule_id>/toggle", methods=["POST"])
def toggle_rule(rule_id):
    db.toggle_rule(rule_id)
    return redirect(url_for("index"))


@app.route("/rules/<int:rule_id>/delete", methods=["POST"])
def delete_rule(rule_id):
    db.delete_rule(rule_id)
    return redirect(url_for("index"))


@app.route("/rules/<int:rule_id>/edit", methods=["POST"])
def edit_rule(rule_id):
    value = request.form.get("value")
    min_discount = request.form.get("min_discount_percent")
    try:
        min_discount = float(min_discount) if min_discount else None
    except ValueError:
        min_discount = None
    db.update_rule(rule_id, value=value, min_discount_percent=min_discount)
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        for key in ("telegram_bot_token", "telegram_chat_id",
                     "monitoring_interval_minutes", "monitoring_url"):
            val = request.form.get(key, "").strip()
            if val:
                db.set_setting(key, val)
        # Restart scheduler with new interval if monitoring is active
        if _is_monitoring():
            _start_scheduler()
        return redirect(url_for("index"))

    settings = db.get_all_settings()
    return jsonify(settings)


@app.route("/settings/test", methods=["POST"])
def test_telegram():
    bot_token = db.get_setting("telegram_bot_token")
    chat_id = db.get_setting("telegram_chat_id")
    if not bot_token or not chat_id:
        return jsonify({"ok": False, "error": "텔레그램 설정이 없습니다."}), 400

    from telegram import Bot
    try:
        asyncio.run(
            Bot(token=bot_token).send_message(
                chat_id=chat_id, text="테스트 메시지입니다. 텔레그램 연동이 정상 작동합니다!"
            )
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/monitor/start", methods=["POST"])
def monitor_start():
    _start_scheduler()
    return redirect(url_for("index"))


@app.route("/monitor/stop", methods=["POST"])
def monitor_stop():
    _stop_scheduler()
    return redirect(url_for("index"))


@app.route("/monitor/run-now", methods=["POST"])
def monitor_run_now():
    import threading
    t = threading.Thread(target=scan_job, daemon=True)
    t.start()
    return redirect(url_for("index"))


@app.route("/history")
def history_json():
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_alert_history(limit=limit))


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=debug)
