import os
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class AlertDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_url TEXT NOT NULL,
                    price TEXT NOT NULL,
                    product_name TEXT DEFAULT '',
                    discount TEXT DEFAULT '',
                    rule_id INTEGER,
                    alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_url_price
                ON alert_history (product_url, price)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_type TEXT NOT NULL CHECK(rule_type IN ('brand', 'keyword')),
                    value TEXT NOT NULL,
                    min_discount_percent REAL NOT NULL DEFAULT 20,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            # Migrate: add columns to alert_history if missing
            self._migrate_alert_history(conn)
            conn.commit()
        logger.info("Database initialized: %s", self.db_path)

    def _migrate_alert_history(self, conn):
        cursor = conn.execute("PRAGMA table_info(alert_history)")
        columns = {row[1] for row in cursor.fetchall()}
        if "product_name" not in columns:
            conn.execute("ALTER TABLE alert_history ADD COLUMN product_name TEXT DEFAULT ''")
        if "discount" not in columns:
            conn.execute("ALTER TABLE alert_history ADD COLUMN discount TEXT DEFAULT ''")
        if "rule_id" not in columns:
            conn.execute("ALTER TABLE alert_history ADD COLUMN rule_id INTEGER")

    # --- Alert History ---

    def already_alerted(self, product_url: str, price: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM alert_history WHERE product_url = ? AND price = ?",
                (product_url, price),
            ).fetchone()
        return row is not None

    def record_alert(self, product_url: str, price: str,
                     product_name: str = "", discount: str = "",
                     rule_id: int | None = None):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO alert_history
                   (product_url, price, product_name, discount, rule_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (product_url, price, product_name, discount, rule_id),
            )
            conn.commit()
        logger.debug("Alert recorded: %s @ %s", product_url, price)

    def cleanup_old(self, hours: int = 24):
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        with self._conn() as conn:
            result = conn.execute(
                "DELETE FROM alert_history WHERE alerted_at < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
        deleted = result.rowcount
        if deleted > 0:
            logger.info("Cleaned up %d old alert records", deleted)

    def get_alert_history(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, product_url, price, product_name, discount,
                          rule_id, alerted_at
                   FROM alert_history
                   ORDER BY alerted_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Watch Rules ---

    def get_all_rules(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM watch_rules ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_enabled_rules(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM watch_rules WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_rule(self, rule_type: str, value: str,
                 min_discount_percent: float = 20) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO watch_rules (rule_type, value, min_discount_percent)
                   VALUES (?, ?, ?)""",
                (rule_type, value, min_discount_percent),
            )
            conn.commit()
            return cursor.lastrowid

    def delete_rule(self, rule_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM watch_rules WHERE id = ?", (rule_id,))
            conn.commit()

    def toggle_rule(self, rule_id: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE watch_rules SET enabled = 1 - enabled WHERE id = ?",
                (rule_id,),
            )
            conn.commit()

    def update_rule(self, rule_id: int, value: str | None = None,
                    min_discount_percent: float | None = None):
        updates = []
        params = []
        if value is not None:
            updates.append("value = ?")
            params.append(value)
        if min_discount_percent is not None:
            updates.append("min_discount_percent = ?")
            params.append(min_discount_percent)
        if not updates:
            return
        params.append(rule_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE watch_rules SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()

    # --- Settings ---

    def get_setting(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def get_all_settings(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- Seed from config.yaml ---

    def seed_from_config(self, config_path: str | None = None):
        """Import initial data from config.yaml if DB is empty."""
        if config_path is None:
            config_path = str(Path(__file__).parent / "config.yaml")

        # Only seed if no rules exist yet
        if self.get_all_rules():
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except FileNotFoundError:
            logger.warning("config.yaml not found, skipping seed")
            return

        # Seed settings (environment variables take priority over config.yaml)
        tg = cfg.get("telegram", {})
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id")
        if bot_token:
            self.set_setting("telegram_bot_token", bot_token)
        if chat_id:
            self.set_setting("telegram_chat_id", str(chat_id))

        mon = cfg.get("monitoring", {})
        interval = os.environ.get("MONITORING_INTERVAL") or mon.get("interval_minutes")
        url = os.environ.get("MONITORING_URL") or mon.get("url")
        if interval:
            self.set_setting("monitoring_interval_minutes", str(interval))
        if url:
            self.set_setting("monitoring_url", url)

        # Seed brand filter as a watch rule
        brand = mon.get("brand_filter")
        if brand:
            discount = mon.get("min_discount_percent", 20)
            self.add_rule("brand", brand, discount)
            logger.info("Seeded brand rule: %s (>=%s%%)", brand, discount)

        logger.info("Config seed complete")
