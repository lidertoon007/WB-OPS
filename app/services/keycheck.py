"""Проверка ключа API кабинета: действует ли он и к каким разделам маркетплейса даёт доступ.

Wildberries: у каждого раздела API свой домен с методом /ping — 200, если ключ подходит разделу, 403 «scope
is not allowed», если у ключа нет этой категории, 401 — ключ недействителен. Срок действия и признак «только
чтение» — из самого токена (JWT: exp и бит 30 поля s). Ozon — /v1/roles (роли и срок ключа), Яндекс Маркет —
/v2/campaigns.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone

import httpx

# (домен, категория токена в ЛК WB, для чего нужна в WB OPS, какой переключатель кабинета её требует)
WB_SCOPES = [
    ("feedbacks-api", "Вопросы и отзывы", "отзывы и вопросы", "replies"),
    ("finance-api", "Финансы", "ОПиУ: отчёт реализации", "stats"),
    ("statistics-api", "Статистика", "заказы, РНП", "stats"),
    ("seller-analytics-api", "Аналитика", "остатки, воронка продаж", None),
    ("content-api", "Контент", "карточки товаров", None),
    ("discounts-prices-api", "Цены и скидки", "цены, репрайсер", None),
    ("marketplace-api", "Маркетплейс", "FBS, остатки продавца", None),
    ("advert-api", "Продвижение", "реклама", None),
]
WB_READ_ONLY_BIT = 1 << 30
TIMEOUT = 15


def fingerprint(cabinet) -> str:
    """Отпечаток ключей кабинета: результат проверки относится к конкретным ключам, новые проверяются заново."""
    raw = "|".join((cabinet.token, cabinet.ads_token, cabinet.client_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if cabinet.token else ""


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def present(result: dict | None, cabinet) -> dict | None:
    """Результат проверки для интерфейса с учётом текущих переключателей кабинета.
    status: ok — всё нужное доступно, partial — ключ действует, но не хватает нужных разделов,
    error — ключ не работает, unknown — проверить не удалось (сеть, лимит запросов).
    None — ключи не проверялись или с тех пор заменены."""
    if not result or result.get("fingerprint") != fingerprint(cabinet):
        return None
    needs = {"replies": cabinet.reviews or cabinet.questions, "stats": cabinet.stats}
    out = {k: v for k, v in result.items() if k != "fingerprint"}
    out["scopes"] = [{**s, "needed": bool(needs.get(s.get("need")))} for s in result.get("scopes") or []]
    if out.get("status") not in ("ok", "partial"):
        return out
    missing = [s["title"] for s in out["scopes"] if s["needed"] and s.get("ok") is False]
    if out.get("read_only") and needs["replies"]:
        missing.append("запись")   # ключ только для чтения — ответы на отзывы и вопросы не отправятся
    out["missing"] = missing
    out["status"] = "partial" if missing else "ok"
    return out


async def _wb_ping(client: httpx.AsyncClient, domain: str, token: str) -> int | None:
    try:
        response = await client.get(f"https://{domain}.wildberries.ru/ping", headers={"Authorization": token})
        return response.status_code
    except httpx.HTTPError:
        return None


async def check_wildberries(cabinet) -> dict:
    claims = jwt_claims(cabinet.token)
    expires = datetime.fromtimestamp(claims["exp"], timezone.utc) if isinstance(claims.get("exp"), (int, float)) else None
    result = {"expires_at": expires.date().isoformat() if expires else None,
              "read_only": bool(int(claims.get("s") or 0) & WB_READ_ONLY_BIT) if "s" in claims else None}
    if expires and expires < datetime.now(timezone.utc):
        return {**result, "status": "error", "message": f"Срок действия ключа истёк {expires:%d.%m.%Y}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        tokens = [cabinet.ads_token or cabinet.token if domain == "advert-api" else cabinet.token for domain, *_ in WB_SCOPES]
        codes = await asyncio.gather(*(_wb_ping(client, d, t) for (d, *_), t in zip(WB_SCOPES, tokens)))
    scopes = []
    for (domain, title, purpose, need), code in zip(WB_SCOPES, codes):
        ok = True if code == 200 else False if code in (401, 403) else None
        scopes.append({"title": title, "purpose": purpose, "ok": ok, "need": need,
                       "note": "отдельный ключ рекламы" if domain == "advert-api" and cabinet.ads_token else ""})
    result["scopes"] = scopes
    main_codes = [c for (d, *_), c in zip(WB_SCOPES, codes) if d != "advert-api"]
    if all(c == 401 for c in main_codes):
        return {**result, "status": "error", "message": "Ключ недействителен: Wildberries его не принимает"}
    if not any(c == 200 for c in main_codes):
        if all(c in (401, 403) for c in main_codes):
            return {**result, "status": "error", "message": "У ключа нет доступа ни к одному разделу"}
        return {**result, "status": "unknown", "message": "Wildberries не ответил — проверьте позже"}
    return {**result, "status": "ok"}


async def check_ozon(cabinet) -> dict:
    if not cabinet.client_id:
        return {"status": "error", "message": "Не указан Client ID"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post("https://api-seller.ozon.ru/v1/roles", json={},
                                         headers={"Client-Id": cabinet.client_id, "Api-Key": cabinet.token})
    except httpx.HTTPError:
        return {"status": "unknown", "message": "Ozon не ответил — проверьте позже (VPN отключён?)"}
    if response.status_code in (401, 403, 404):
        return {"status": "error", "message": "Ключ или Client ID не подходят: Ozon их не принимает"}
    if response.status_code != 200:
        return {"status": "unknown", "message": f"Ozon ответил {response.status_code} — проверьте позже"}
    try:
        data = response.json()
    except ValueError:
        data = {}
    roles = data.get("roles") or (data.get("result") or {}).get("roles") or []
    expires = data.get("expires_at") or (data.get("result") or {}).get("expires_at")
    result = {"status": "ok", "expires_at": str(expires)[:10] if expires else None,
              "scopes": [{"title": str(r.get("name") or r) if isinstance(r, dict) else r, "ok": True, "need": None}
                         for r in roles if isinstance(r, (dict, str))]}
    return result


async def check_yandex(cabinet) -> dict:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get("https://api.partner.market.yandex.ru/v2/campaigns", params={"limit": 50},
                                        headers={"Api-Key": cabinet.token})
    except httpx.HTTPError:
        return {"status": "unknown", "message": "Яндекс Маркет не ответил — проверьте позже"}
    if response.status_code in (401, 403):
        return {"status": "error", "message": "Ключ недействителен: Яндекс Маркет его не принимает"}
    if response.status_code != 200:
        return {"status": "unknown", "message": f"Яндекс Маркет ответил {response.status_code} — проверьте позже"}
    campaigns = (response.json() or {}).get("campaigns") or []
    return {"status": "ok", "message": f"Магазинов: {len(campaigns)}"}


async def check(cabinet) -> dict:
    if not cabinet.token:
        result = {"status": "error", "message": "Ключ не задан"}
    elif cabinet.marketplace == "ozon":
        result = await check_ozon(cabinet)
    elif cabinet.marketplace == "yandex":
        result = await check_yandex(cabinet)
    else:
        result = await check_wildberries(cabinet)
    return {**result, "checked_at": _now(), "fingerprint": fingerprint(cabinet)}
