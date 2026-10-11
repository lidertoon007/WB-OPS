from __future__ import annotations

import httpx


async def send_telegram_message(settings: dict[str, object], text: str) -> bool:
    token = str(settings.get("bot_token") or "").strip()
    chat_id = str(settings.get("chat_id") or "").strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})
    return response.is_success
