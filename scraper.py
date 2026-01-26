import logging
import os
import re
import ssl
import urllib.request
from pathlib import Path

import feedparser
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

BASE_URL = "https://www.toppreise.ch"


async def scrape_best_prices(
    url: str,
    rules: list[dict] | None = None,
    brand_filter: str = "",
    min_discount_percent: float = 20,
) -> list[dict]:
    """
    Scrape toppreise.ch/new-best-prices for discounted products.

    Supports two modes:
      1. Multi-rule mode: pass `rules` (list of dicts with keys:
         id, rule_type, value, min_discount_percent).
      2. Legacy mode: pass brand_filter + min_discount_percent.

    Returns a list of dicts with keys:
        name, old_price, new_price, discount, shop, url, matched_rule_id
    """
    products = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="de-CH",
        )
        page = await context.new_page()

        logger.info("Loading page: %s", url)
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.error("Failed to load page: %s", e)
            await browser.close()
            return products

        await page.wait_for_timeout(2000)

        # Accept cookie consent if present
        try:
            cookie_btn = page.locator(
                "button:has-text('Akzeptieren'), "
                "button:has-text('Accept'), "
                "button:has-text('OK'), "
                "#onetrust-accept-btn-handler"
            )
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # --- Scrape product cards ---
        cards = await page.query_selector_all("a.small-box2")
        logger.info("Found %d product cards", len(cards))

        for card in cards:
            try:
                # Manufacturer
                mfg_el = await card.query_selector(".product-manufacturer")
                manufacturer = (await mfg_el.inner_text()).strip() if mfg_el else ""

                # Product name
                name_el = await card.query_selector(".product-name")
                name = (await name_el.inner_text()).strip() if name_el else ""
                full_name = f"{manufacturer} {name}".strip() if name else manufacturer

                if not full_name:
                    continue

                # Product URL
                href = await card.get_attribute("href")
                product_url = f"{BASE_URL}{href}" if href and href.startswith("/") else (href or "")

                # Old price (crossed out)
                old_price_val = None
                old_price_el = await card.query_selector(".priceContainer.crossed .Plugin_Price")
                if old_price_el:
                    old_price_text = (await old_price_el.inner_text()).strip()
                    old_price_val = _parse_price(old_price_text)

                # Current price
                new_price_val = None
                new_price_el = await card.query_selector(".priceContainer.productPrice .Plugin_Price")
                if new_price_el:
                    new_price_text = (await new_price_el.inner_text()).strip()
                    new_price_val = _parse_price(new_price_text)

                if old_price_val is None or new_price_val is None:
                    continue
                if old_price_val <= 0:
                    continue

                # Calculate discount
                discount_pct = ((old_price_val - new_price_val) / old_price_val) * 100

                # Match against rules
                if rules:
                    matched = _match_rules(manufacturer, full_name, discount_pct, rules)
                    if not matched:
                        continue
                    for rule_id, rule_min in matched:
                        products.append({
                            "name": full_name,
                            "old_price": f"CHF {old_price_val:,.2f}",
                            "new_price": f"CHF {new_price_val:,.2f}",
                            "discount": f"-{discount_pct:.0f}%",
                            "shop": "",
                            "url": product_url,
                            "matched_rule_id": rule_id,
                        })
                else:
                    # Legacy single-filter mode
                    if brand_filter and brand_filter.lower() not in manufacturer.lower():
                        continue
                    if discount_pct < min_discount_percent:
                        continue
                    products.append({
                        "name": full_name,
                        "old_price": f"CHF {old_price_val:,.2f}",
                        "new_price": f"CHF {new_price_val:,.2f}",
                        "discount": f"-{discount_pct:.0f}%",
                        "shop": "",
                        "url": product_url,
                        "matched_rule_id": None,
                    })

                logger.info(
                    "Found: %s | CHF %.2f -> CHF %.2f (-%d%%)",
                    full_name, old_price_val, new_price_val, int(discount_pct),
                )

            except Exception as e:
                logger.debug("Error parsing card: %s", e)
                continue

        # Save debug HTML if no results found
        if not products:
            try:
                data_dir = os.environ.get("DATA_DIR", "")
                debug_path = str(Path(data_dir) / "debug_page.html") if data_dir else "debug_page.html"
                html = await page.content()
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info("Saved %s for selector tuning", debug_path)
            except Exception:
                pass

        await browser.close()

    logger.info("Scraping complete: %d product(s) found", len(products))
    return products


def _match_rules(manufacturer: str, full_name: str,
                 discount_pct: float, rules: list[dict]) -> list[tuple[int, float]]:
    """Return list of (rule_id, min_discount) for all matching rules."""
    matched = []
    for rule in rules:
        rule_type = rule["rule_type"]
        value = rule["value"].lower()
        min_disc = rule.get("min_discount_percent", 20)

        if discount_pct < min_disc:
            continue

        if rule_type == "brand":
            if value in manufacturer.lower():
                matched.append((rule["id"], min_disc))
        elif rule_type == "keyword":
            if value in full_name.lower():
                matched.append((rule["id"], min_disc))

    return matched


def _parse_price(text: str) -> float | None:
    """Parse a price string like '1,299.00', '3.84', or '189.-' into a float."""
    cleaned = text.replace("'", "").replace(",", "").replace(" ", "").replace("CHF", "").strip()
    cleaned = re.sub(r'\.-$', '', cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return None


# --- Preispirat RSS ---

PREISPIRAT_RSS_URL = "https://www.preispirat.ch/feed/"


async def scrape_preispirat_rss(rules: list[dict] | None = None) -> list[dict]:
    """Scrape Preispirat.ch RSS feed for deals and match against rules."""
    products = []

    # Fetch with SSL workaround (preispirat.ch certificate chain issue)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(PREISPIRAT_RSS_URL,
                                headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, context=ssl_ctx)
    feed = feedparser.parse(resp.read())
    logger.info("Preispirat RSS: %d entries", len(feed.entries))

    for entry in feed.entries:
        title = entry.get("title", "").strip()
        if not title:
            continue

        link = entry.get("link", "")
        description = entry.get("description", "")

        # Extract shop from title: "Product bei ShopName"
        shop = "Preispirat"
        bei_match = re.search(r'\bbei\s+(.+)$', title, re.IGNORECASE)
        if bei_match:
            shop = bei_match.group(1).strip()

        # Extract prices from description HTML
        new_price_val = None
        old_price_val = None

        price_match = re.search(r'Preis:\s*CHF\s*([\d\'.,]+(?:-)?)', description)
        if price_match:
            new_price_val = _parse_price(price_match.group(1))

        old_price_match = re.search(r'Zweitbester\s+Preis:\s*CHF\s*([\d\'.,]+(?:-)?)', description)
        if old_price_match:
            old_price_val = _parse_price(old_price_match.group(1))

        # Calculate discount
        discount_pct = 0.0
        if old_price_val and new_price_val and old_price_val > 0:
            discount_pct = ((old_price_val - new_price_val) / old_price_val) * 100

        # Match against rules
        if rules:
            # Use title as both manufacturer and full_name for matching
            matched = _match_rules(title, title, discount_pct, rules)
            if not matched:
                continue
            for rule_id, rule_min in matched:
                products.append({
                    "name": title,
                    "old_price": f"CHF {old_price_val:,.2f}" if old_price_val else "",
                    "new_price": f"CHF {new_price_val:,.2f}" if new_price_val else "",
                    "discount": f"-{discount_pct:.0f}%" if discount_pct > 0 else "",
                    "shop": shop,
                    "url": link,
                    "matched_rule_id": rule_id,
                })
        else:
            products.append({
                "name": title,
                "old_price": f"CHF {old_price_val:,.2f}" if old_price_val else "",
                "new_price": f"CHF {new_price_val:,.2f}" if new_price_val else "",
                "discount": f"-{discount_pct:.0f}%" if discount_pct > 0 else "",
                "shop": shop,
                "url": link,
                "matched_rule_id": None,
            })

    logger.info("Preispirat RSS: %d matching product(s)", len(products))
    return products
