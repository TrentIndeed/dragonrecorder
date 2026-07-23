"""Telegram notifications — same pattern and env vars as operatorDashboard."""

import logging

import httpx

import config

log = logging.getLogger("dragonrecorder.notify")


def send_telegram(text: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4096]},
            timeout=15,
        )
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)
