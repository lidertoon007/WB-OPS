"""Перенос карточек между кабинетами WB, Ozon и Яндекс Маркета: подготовка, предпросмотр, выгрузка, история."""
from __future__ import annotations

import copy
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import TransferError, clean_offer_id, norm
from .mp_ozon import Ozon
from .mp_wb import WB
from .mp_ym import YM

MP_TITLES = {"wildberries": "Wildberries", "ozon": "Ozon", "yandex": "Яндекс Маркет"}

DEFAULT_TARGET = {"price_multiplier": 1, "old_price_fixed": 0, "skip_size_words": "акц", "categories": {}, "extras": {}}
DEFAULTS = {
    "targets": {
        "ozon": {**DEFAULT_TARGET, "vat": "0"},
        "wildberries": {**DEFAULT_TARGET, "new_barcodes": True},
        "yandex": {**DEFAULT_TARGET},
    },
    "offer_id_rename": {},
    "certificate": {},
}
# Настройки из прежнего мини-сервиса wb2ozon (переносятся один раз при первом запуске)
SEED = {
    "targets": {"ozon": {"price_multiplier": 1.5, "old_price_fixed": 15000, "skip_size_words": "акц", "vat": "0",
                         "categories": {"wildberries:Джинсы": {"id": "200000933:93080", "name": "Джинсы", "path": "Одежда / Одежда",
                                                                "description_category_id": 200000933, "type_id": 93080}},
                         "extras": {"Джинсы": {"Бренд в одежде и обуви": "Deemivis", "Бренд": "Deemivis",
                                               "ТН ВЭД коды ЕАЭС": "6203423100 - Брюки и бриджи мужские или для мальчиков из денима, или джинсовой ткани",
                                               "Нужен код маркировки": "Да", "Размер джинсов": None, "Таблица размеров JSON": None}}}},
    "offer_id_rename": {"флис-9018": "9018"},
    "certificate": {"name": "Декларация соответствия", "number": "ЕАЭС N RU Д-CN.РА05.В.93021/24", "issue_date": "2024-07-11", "expire_date": "2029-07-10"},
}


def ready_reason(c) -> str:
    """Пустая строка — кабинет годится для переноса; иначе — чего не хватает."""
    if not getattr(c, "enabled", True):
        return "кабинет выключен"
    if not c.token:
        return "нет ключа API"
    if c.marketplace == "ozon" and not c.client_id:
        return "нет Client ID"
    return ""


class TransferService:
    def __init__(self, store, cabinets, data_dir: Path = Path("data/transfer")):
        self.store = store
        self.cabinets = cabinets             # callable -> [Cabinet]
        self.data_dir = Path(data_dir)
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        with store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS transfer_runs (id TEXT PRIMARY KEY, created_at TEXT, source_id TEXT, target_id TEXT, "
                      "source_title TEXT, target_title TEXT, articles TEXT, status TEXT, items TEXT, log TEXT, author TEXT)")
        if not store.get_setting("transfer_settings", ""):
            self.save_settings(_deep_merge(copy.deepcopy(DEFAULTS), SEED))

    # ------------------------------------------------------------------ настройки
    def settings(self) -> dict:
        try:
            saved = json.loads(self.store.get_setting("transfer_settings", "") or "{}")
        except ValueError:
            saved = {}
        return _deep_merge(copy.deepcopy(DEFAULTS), saved)

    def save_settings(self, value: dict) -> dict:
        merged = _deep_merge(copy.deepcopy(DEFAULTS), value)
        self.store.set_setting("transfer_settings", json.dumps(merged, ensure_ascii=False))
        return merged

    def update_settings(self, patch: dict) -> dict:
        cur = self.settings()
        for mp, t in (patch.get("targets") or {}).items():
            if mp in cur["targets"]:
                for k in ("price_multiplier", "old_price_fixed", "skip_size_words", "vat", "new_barcodes"):
                    if k in t:
                        cur["targets"][mp][k] = t[k]
        if "offer_id_rename" in patch:
            cur["offer_id_rename"] = {str(k).strip(): str(v).strip() for k, v in (patch["offer_id_rename"] or {}).items() if str(k).strip() and str(v).strip()}
        if "certificate" in patch:
            cur["certificate"] = {**cur.get("certificate", {}), **(patch["certificate"] or {})}
        return self.save_settings(cur)

    def set_category(self, target_mp: str, source_key: str, category: dict | None):
        cur = self.settings()
        cats = cur["targets"][target_mp]["categories"]
        if category:
            cats[source_key] = category
        else:
            cats.pop(source_key, None)
        self.save_settings(cur)

    def set_extra(self, target_mp: str, category_name: str, attr: str, value):
        cur = self.settings()
        ex = cur["targets"][target_mp]["extras"].setdefault(category_name, {})
        if value == "":
            ex.pop(attr, None)
        else:
            ex[attr] = value          # None — не заполнять характеристику
        self.save_settings(cur)

    def cert_files(self) -> list:
        folder = self.data_dir / "certificate"
        return sorted(str(p) for p in folder.glob("*") if p.is_file()) if folder.exists() else []

    # ------------------------------------------------------------------ кабинеты и клиенты
    def cabinet_list(self) -> list:
        return [{"id": c.id, "name": c.name, "marketplace": c.marketplace, "marketplace_title": MP_TITLES.get(c.marketplace, c.marketplace),
                 "ready": not ready_reason(c), "reason": ready_reason(c)} for c in self.cabinets()]

    def _cabinet(self, cabinet_id):
        c = next((c for c in self.cabinets() if c.id == cabinet_id), None)
        if c is None:
            raise TransferError("Кабинет не найден")
        reason = ready_reason(c)
        if reason:
            raise TransferError(f"Кабинет «{c.name}»: {reason}")
        return c

    def client(self, cabinet, log=None):
        if cabinet.marketplace == "wildberries":
            return WB(cabinet, log)
        if cabinet.marketplace == "ozon":
            return Ozon(cabinet, self.data_dir, log)
        if cabinet.marketplace == "yandex":
            return YM(cabinet, self.data_dir, log)
        raise TransferError("Площадка не поддерживается")

    def search_categories(self, cabinet_id: str, q: str) -> list:
        cl = self.client(self._cabinet(cabinet_id))
        try:
            return cl.search_categories(q)[:30]
        finally:
            cl.close()

    # ------------------------------------------------------------------ задания
    def _new_job(self, kind, source, target, articles, author="") -> dict:
        with self._lock:
            if any(j["status"] == "running" for j in self.jobs.values()):
                raise TransferError("Уже идёт перенос — дождитесь окончания")
            job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
                   "source_id": source.id, "target_id": target.id, "source_title": f"{MP_TITLES[source.marketplace]} · {source.name}",
                   "target_title": f"{MP_TITLES[target.marketplace]} · {target.name}", "target_mp": target.marketplace,
                   "articles": articles, "items": [], "log": [], "progress": [0, max(1, len(articles))], "author": author}
            self.jobs[job["id"]] = job
            return job

    def _log(self, job, text):
        job["log"].append(f"{time.strftime('%H:%M:%S')} {text}")
        job["log"] = job["log"][-400:]

    def start(self, source_id: str, target_id: str, articles: list, auto: bool = False, author: str = "") -> dict:
        articles = list(dict.fromkeys(a.strip() for a in articles if a and a.strip()))[:200]
        if not articles:
            raise TransferError("Впишите хотя бы один артикул")
        if source_id == target_id:
            raise TransferError("Кабинет-источник и кабинет-приёмник совпадают")
        source, target = self._cabinet(source_id), self._cabinet(target_id)
        job = self._new_job("prepare", source, target, articles, author)
        threading.Thread(target=self._run_prepare, args=(job, source, target, auto), daemon=True).start()
        return self.public(job)

    def _run_prepare(self, job, source, target, auto):
        log = lambda t: self._log(job, t)          # noqa: E731
        src = tgt = None
        try:
            src, tgt = self.client(source, log), self.client(target, log)
            cfg = self.settings()
            tcfg = cfg["targets"][target.marketplace]
            for n, art in enumerate(job["articles"], 1):
                job["progress"] = [n - 1, len(job["articles"])]
                log(f"{art}: читаю карточку в {job['source_title']}…")
                try:
                    card = src.fetch(art)
                except TransferError as exc:
                    job["items"].append(_row(art, status="error", message=f"источник: {exc}"))
                    continue
                if not card:
                    job["items"].append(_row(art, status="error", message="артикул не найден в кабинете-источнике"))
                    continue
                try:
                    job["items"] += self._prepare_card(card, art, tgt, tcfg, cfg, log)
                except TransferError as exc:
                    job["items"].append(_row(art, status="error", message=f"приёмник: {exc}"))
            job["progress"] = [len(job["articles"]), len(job["articles"])]
            ready = [i for i in job["items"] if i["status"] == "ready"]
            log(f"Готово к выгрузке: {len(ready)} из {len([i for i in job['items'] if i['status'] != 'skipped'])}")
            if auto and ready and all(i["status"] in ("ready", "skipped") for i in job["items"]):
                log("Все данные на месте — выгружаю…")
                self._upload(job, tgt, tcfg, ready, log)
            final = "done"
        except Exception as exc:          # noqa: BLE001 — показываем ошибку в интерфейсе
            final = "error"
            log(f"Ошибка: {exc}")
            if not isinstance(exc, TransferError):
                log(traceback.format_exc(limit=3))
        finally:
            for cl in (src, tgt):
                if cl:
                    cl.close()
        self._finish(job, final)

    def _prepare_card(self, card, art, tgt, tcfg, cfg, log) -> list:
        key = f"{card['source_mp']}:{card['category']}"
        chosen = tcfg["categories"].get(key)
        alternatives = []
        if not chosen and card["source_mp"] == tgt.mp:
            n = card["native"]
            chosen = {"wildberries": {"id": n.get("subject_id"), "name": n.get("subject") or card["category"], "path": ""},
                      "ozon": {"id": f'{n.get("description_category_id")}:{n.get("type_id")}', "name": card["category"], "path": "",
                               "description_category_id": n.get("description_category_id"), "type_id": n.get("type_id")},
                      "yandex": {"id": n.get("category_id"), "name": card["category"], "path": ""}}[tgt.mp]
        if not chosen:
            log(f"  подбираю категорию для «{card['category']}»…")
            alternatives = tgt.match_category(card["category"] or card["title"])
            if not alternatives:
                raise TransferError(f"не нашлось категории для «{card['category']}»")
            chosen = alternatives[0]
        extras = {**tcfg["extras"].get("*", {}), **tcfg["extras"].get(chosen["name"], {})}
        vendor = clean_offer_id(cfg["offer_id_rename"].get(card["key"], card["key"]))
        meta = {"article": art, "group": card["key"], "source_category": card["category"], "category_key": key, "category": chosen,
                "alternatives": [a for a in alternatives if a["id"] != chosen["id"]][:5], "auto_category": not tcfg["categories"].get(key),
                "photo": card["photos"][0] if card["photos"] else ""}
        if tgt.mp == "wildberries":
            it = tgt.prepare(card, tcfg, chosen, extras, vendor)
            return [{**_row(art), **meta, **it, "status": "ready" if not it["missing"] else "missing"}]
        rows = []
        skip = [w.strip().lower() for w in str(tcfg.get("skip_size_words") or "").split(",") if w.strip()]
        multi = len(card["variants"]) > 1
        for v in card["variants"]:
            if any(w in (v.get("size") or "").lower() for w in skip):
                rows.append({**_row(art), **meta, "offer_id": "", "sizes": [v.get("size")], "status": "skipped", "message": f"размер «{v['size']}» пропущен"})
                continue
            if card["source_mp"] == tgt.mp and isinstance(v.get("id"), str):
                offer_id = v["id"]
            else:
                offer_id = clean_offer_id(f"{vendor}_{v['size']}" if multi and v.get("size") else vendor)
            it = tgt.prepare_variant(card, v, tcfg, chosen, extras, offer_id, vendor)
            rows.append({**_row(art), **meta, **it, "status": "ready" if not it["missing"] else "missing"})
        return rows

    def upload(self, run_id: str, offer_ids: list | None, force: bool = False, author: str = "") -> dict:
        job = self.get(run_id)
        if job is None:
            raise TransferError("Перенос не найден")
        if job["status"] == "running":
            raise TransferError("Подготовка ещё идёт")
        allowed = ("ready", "missing", "error") if force else ("ready",)
        chosen = [i for i in job["items"] if i.get("payload") and i["status"] in allowed and (offer_ids is None or i["offer_id"] in offer_ids)]
        if not chosen:
            raise TransferError("Нет товаров, готовых к выгрузке" + ("" if force else " — дополните данные или выгрузите принудительно"))
        target = self._cabinet(job["target_id"])
        self.jobs[job["id"]] = job
        job["status"] = "running"
        job["progress"] = [0, len(chosen)]

        def run():
            tgt, final = None, "done"
            try:
                tgt = self.client(target, lambda t: self._log(job, t))
                self._upload(job, tgt, self.settings()["targets"][target.marketplace], chosen, lambda t: self._log(job, t))
            except Exception as exc:      # noqa: BLE001
                final = "error"
                self._log(job, f"Ошибка выгрузки: {exc}")
            finally:
                if tgt:
                    tgt.close()
            self._finish(job, final)
        threading.Thread(target=run, daemon=True).start()
        return self.public(job)

    def _upload(self, job, tgt, tcfg, items, log):
        log(f"Выгружаю {len(items)} шт. в {job['target_title']}…")
        results = tgt.upload(items, tcfg)
        self._apply(job, results)
        ok = sum(1 for r in results if r["status"] == "ok")
        log(f"Выгрузка завершена: создано {ok}, ошибок {sum(1 for r in results if r['status'] == 'error')}, "
            f"в обработке {sum(1 for r in results if r['status'] == 'pending')}")

    def _apply(self, job, results):
        by = {r["offer_id"]: r for r in results}
        for it in job["items"]:
            r = by.get(it.get("offer_id"))
            if r and it["status"] != "skipped":
                it["status"] = {"ok": "uploaded", "pending": "pending", "error": "failed", "sent": "pending"}[r["status"]]
                it["message"] = r["message"]
                if r.get("target_id"):
                    it["target_id"] = r["target_id"]
        job["progress"] = [len(results), len(results)]

    def finalize(self, run_id: str) -> dict:
        """WB: догрузить фото и цены у карточек, которые WB создал позже."""
        job = self.get(run_id)
        if not job or job["target_mp"] != "wildberries":
            raise TransferError("Догрузка нужна только для переноса в Wildberries")
        items = [i for i in job["items"] if i["status"] == "pending"]
        if not items:
            raise TransferError("Нет карточек в обработке")
        tgt = self.client(self._cabinet(job["target_id"]), lambda t: self._log(job, t))
        try:
            self._apply(job, tgt.finalize(items, self.settings()["targets"]["wildberries"], wait=0))
        finally:
            tgt.close()
        self._save(job)
        return self.public(job)

    def bind_certificate(self, run_id: str) -> dict:
        job = self.get(run_id)
        if not job or job["target_mp"] != "ozon":
            raise TransferError("Декларация привязывается только к товарам Ozon")
        offers = [i["offer_id"] for i in job["items"] if i["status"] == "uploaded"]
        if not offers:
            raise TransferError("Нет созданных товаров")
        cfg = self.settings()
        cert = cfg.get("certificate") or {}
        if not cert.get("certificate_id") and not (cert.get("number") and cert.get("issue_date") and cert.get("expire_date")):
            raise TransferError("Заполните номер и даты декларации в настройках переноса")
        tgt = self.client(self._cabinet(job["target_id"]))
        try:
            cert_id = tgt.bind_certificate(cert, offers, self.cert_files())
        finally:
            tgt.close()
        cfg["certificate"]["certificate_id"] = cert_id
        self.save_settings(cfg)
        self._log(job, f"Декларация {cert_id} привязана к {len(offers)} товарам (Ozon проверяет до 3 рабочих дней)")
        self._save(job)
        return self.public(job)

    # ------------------------------------------------------------------ история
    def _finish(self, job, status):
        """Сохранить в историю и только потом снять отметку «идёт»."""
        try:
            self._save(job, status)
        except Exception as exc:          # noqa: BLE001
            self._log(job, f"История не сохранилась: {exc}")
        job["status"] = status

    def _save(self, job, status=None):
        with self.store._connect() as c:
            c.execute("INSERT OR REPLACE INTO transfer_runs(id, created_at, source_id, target_id, source_title, target_title, articles, status, items, log, author) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (job["id"], job["created_at"], job["source_id"], job["target_id"], job["source_title"], job["target_title"],
                       json.dumps(job["articles"], ensure_ascii=False), status or job["status"], json.dumps(job["items"], ensure_ascii=False),
                       json.dumps(job["log"][-200:], ensure_ascii=False), job.get("author", "")))
            c.execute("DELETE FROM transfer_runs WHERE id NOT IN (SELECT id FROM transfer_runs ORDER BY created_at DESC LIMIT 50)")

    def get(self, run_id: str) -> dict | None:
        if run_id in self.jobs:
            return self.jobs[run_id]
        with self.store._connect() as c:
            row = c.execute("SELECT id, created_at, source_id, target_id, source_title, target_title, articles, status, items, log, author "
                            "FROM transfer_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        job = {"id": row[0], "created_at": row[1], "source_id": row[2], "target_id": row[3], "source_title": row[4], "target_title": row[5],
               "articles": json.loads(row[6] or "[]"), "status": row[7] if row[7] != "running" else "error", "items": json.loads(row[8] or "[]"),
               "log": json.loads(row[9] or "[]"), "author": row[10] or "", "kind": "prepare"}
        target = next((c for c in self.cabinets() if c.id == job["target_id"]), None)
        job["target_mp"] = target.marketplace if target else ""
        job["progress"] = [len(job["articles"]), len(job["articles"])]
        self.jobs[run_id] = job
        return job

    def history(self) -> list:
        with self.store._connect() as c:
            rows = c.execute("SELECT id, created_at, source_title, target_title, articles, status, items FROM transfer_runs ORDER BY created_at DESC LIMIT 30").fetchall()
        out = []
        for rid, created, src, tgt, arts, status, items in rows:
            live = self.jobs.get(rid)
            its = live["items"] if live else json.loads(items or "[]")
            out.append({"id": rid, "created_at": created, "source_title": src, "target_title": tgt, "articles": json.loads(arts or "[]"),
                        "status": live["status"] if live else status,
                        "counts": {s: sum(1 for i in its if i["status"] == s) for s in ("ready", "missing", "uploaded", "failed", "pending", "error", "skipped")}})
        return out

    @staticmethod
    def public(job) -> dict:
        items = [{k: v for k, v in i.items() if k != "payload"} | {"has_payload": bool(i.get("payload"))} for i in job["items"]]
        return {k: v for k, v in job.items() if k != "items"} | {"items": items}


def _row(art, status="ready", message="") -> dict:
    return {"article": art, "group": art, "offer_id": "", "title": "", "status": status, "message": message, "missing": [], "filled": {},
            "notes": [], "sizes": [], "category": None, "alternatives": [], "price": None, "old_price": None}


def _deep_merge(base: dict, extra: dict) -> dict:
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict) and k not in ("categories", "extras", "offer_id_rename"):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
