"""Ставка налога кабинета: хранится в настройках кабинета WB OPS и записывается в конфиг расчёта ОПиУ
(opiu/<площадка>/<...>_accounts.json, поле tax_percent у кабинета), чтобы «Финансы», «Дашборд» и «РНП»
считали налог по одной ставке.

Кабинет панели сопоставляется с кабинетом ОПиУ по токену (WB), Client ID (Ozon), ID кабинета (Я.Маркет),
затем по названию. Если у площадки ровно один кабинет в панели и один включённый в ОПиУ — они считаются одним.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

CONFIGS = {"wildberries": "Wildberries/wb_accounts.json", "ozon": "Ozon/ozon_accounts.json", "yandex": "YandexMarket/ym_accounts.json"}
_PREFIX = re.compile(r"^(м?ип|ооо|оао|зао|пао|ао)\s+")


def _norm(name) -> str:
    s = re.sub(r"[«»\"'.,()]+", " ", str(name or "").lower()).strip()
    s = _PREFIX.sub("", s)
    return re.sub(r"\s+", "", s)


def _read(path: Path):
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return json.loads(raw.decode("utf-8-sig")), bom


def _match(cabinet, accounts: list, same_mp: int):
    for i, a in enumerate(accounts):
        if cabinet.marketplace == "wildberries" and cabinet.token and a.get("token") == cabinet.token:
            return i
        if cabinet.marketplace == "ozon" and cabinet.client_id and str(a.get("client_id") or "") == cabinet.client_id:
            return i
        if cabinet.marketplace == "yandex" and cabinet.client_id and cabinet.client_id in {str(a.get("business_id") or "")} | {str(x) for x in a.get("campaign_ids") or []}:
            return i
    key = _norm(cabinet.name)
    for i, a in enumerate(accounts):
        if key and _norm(a.get("name")) == key:
            return i
    enabled = [i for i, a in enumerate(accounts) if a.get("enabled", True)]
    if same_mp == 1 and len(enabled) == 1:
        return enabled[0]
    return None


def opiu_rate(project: Path | None, cabinet, same_mp: int) -> dict:
    """{'rate': ставка в ОПиУ или None, 'account': имя кабинета в ОПиУ или None}."""
    if project is None or cabinet.marketplace not in CONFIGS:
        return {"rate": None, "account": None}
    path = Path(project) / CONFIGS[cabinet.marketplace]
    try:
        cfg, _ = _read(path)
    except (OSError, ValueError):
        return {"rate": None, "account": None}
    accounts = cfg.get("accounts") or []
    i = _match(cabinet, accounts, same_mp)
    default = cfg.get("tax_percent")
    if i is None:
        return {"rate": float(default) if default is not None else None, "account": None}
    rate = accounts[i].get("tax_percent", default)
    return {"rate": float(rate) if rate is not None else None, "account": accounts[i].get("name") or ""}


def write_opiu_rate(project: Path | None, cabinet, same_mp: int, rate: float | None) -> str | None:
    """Записать ставку кабинету в конфиге ОПиУ. Возвращает имя кабинета ОПиУ или None, если не нашли.
    rate=None — убрать ставку кабинета (будет общая ставка файла)."""
    if project is None or cabinet.marketplace not in CONFIGS:
        return None
    path = Path(project) / CONFIGS[cabinet.marketplace]
    try:
        cfg, bom = _read(path)
    except (OSError, ValueError):
        return None
    accounts = cfg.get("accounts") or []
    i = _match(cabinet, accounts, same_mp)
    if i is None:
        return None
    current = accounts[i].get("tax_percent")
    if rate is None:
        if "tax_percent" not in accounts[i]:
            return accounts[i].get("name") or ""
        accounts[i].pop("tax_percent")
    else:
        value = int(rate) if float(rate).is_integer() else float(rate)
        if current == value:
            return accounts[i].get("name") or ""
        accounts[i]["tax_percent"] = value
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    os.replace(tmp, path)
    return accounts[i].get("name") or ""
