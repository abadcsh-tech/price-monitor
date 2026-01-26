import logging
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


def format_message(products: list[dict]) -> str:
    lines = ["🔥 <b>최저가 할인 알림!</b>\n"]

    for p in products:
        lines.append(f"📱 <b>{p['name']}</b>")
        lines.append(f"💰 {p['old_price']} → {p['new_price']} ({p['discount']})")
        if p.get("shop"):
            lines.append(f"🏪 {p['shop']}")
        if p.get("url"):
            lines.append(f"🔗 <a href=\"{p['url']}\">제품 링크</a>")
        lines.append("")

    return "\n".join(lines)


async def send_telegram_alert(bot_token: str, chat_id: str, products: list[dict]):
    if not products:
        return

    bot = Bot(token=bot_token)
    message = format_message(products)

    # Telegram 메시지 최대 길이 4096자 — 넘으면 분할 발송
    if len(message) <= 4096:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info("Telegram alert sent: %d product(s)", len(products))
    else:
        # 제품별로 개별 발송
        for p in products:
            single_msg = format_message([p])
            await bot.send_message(
                chat_id=chat_id,
                text=single_msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        logger.info("Telegram alerts sent individually: %d message(s)", len(products))
