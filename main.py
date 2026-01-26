import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from scraper import scrape_best_prices
from notifier import send_telegram_alert
from db import AlertDB

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(__file__).parent / path
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run_scan(config: dict, db: AlertDB):
    """Run one scrape-filter-notify cycle."""
    mon = config["monitoring"]
    tg = config["telegram"]

    logger.info("=== Starting scan ===")

    # 1. Scrape
    try:
        products = await scrape_best_prices(
            url=mon["url"],
            brand_filter=mon.get("brand_filter", "Apple"),
            min_discount_percent=mon.get("min_discount_percent", 20),
        )
    except Exception as e:
        logger.error("Scraping failed: %s", e)
        return

    if not products:
        logger.info("No qualifying products found.")
        return

    logger.info("Found %d qualifying product(s)", len(products))

    # 2. Filter out already-alerted products
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

    # 3. Send Telegram notification
    try:
        await send_telegram_alert(tg["bot_token"], tg["chat_id"], new_products)
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e)
        return

    # 4. Record alerts
    for p in new_products:
        key_url = p.get("url") or p["name"]
        db.record_alert(key_url, p["new_price"])

    # 5. Cleanup old records
    db.cleanup_old(hours=24)

    logger.info("=== Scan complete ===")


async def main():
    config = load_config()
    db_path = config.get("database", {}).get("path", "price_alerts.db")
    db = AlertDB(str(Path(__file__).parent / db_path))

    interval = config["monitoring"].get("interval_minutes", 10)

    # Validate Telegram config
    tg = config["telegram"]
    if tg["bot_token"] == "YOUR_BOT_TOKEN" or tg["chat_id"] == "YOUR_CHAT_ID":
        logger.warning(
            "⚠️  Telegram bot_token/chat_id not configured in config.yaml. "
            "Notifications will fail until you set them."
        )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_scan,
        "interval",
        minutes=interval,
        args=[config, db],
        id="price_scan",
        max_instances=1,
    )
    scheduler.start()
    logger.info(
        "Scheduler started. Scanning every %d minute(s). Press Ctrl+C to stop.",
        interval,
    )

    # Run first scan immediately
    await run_scan(config, db)

    # Keep running until interrupted
    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutting down...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()
    scheduler.shutdown(wait=False)
    logger.info("Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
