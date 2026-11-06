from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

MSK = timezone(timedelta(hours=3))
PRIMARY_METRICS = ("ctr", "atb_rate", "cart_rate", "order_rate", "revenue_rate", "revenue_per_1000", "profit_rate", "profit_per_1000")
METRIC_ALIASES = {"cart_rate": "atb_rate", "revenue_per_1000": "revenue_rate", "profit_per_1000": "profit_rate"}
STATUS_LABELS = {"draft": "Черновик", "ready": "Готов к запуску", "running": "Идёт",
                 "paused": "На паузе", "finished": "Завершён", "no_decision": "Нет решения",
                 "rolled_back": "Откат выполнен"}


def now_iso() -> str:
    return datetime.now(MSK).isoformat(timespec="seconds")


class ABHypothesisError(Exception):
    pass


class ABHypotheses:
    """Изолированный контур гипотез с preflight, жизненным циклом и откатом."""

    def __init__(self, store, tokens: Callable, card_loader=None, runner=None):
        self.store, self.tokens, self.card_loader, self.runner = store, tokens, card_loader, runner
        with store._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS ab_hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, nm_id INTEGER NOT NULL,
                article TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', advert_id INTEGER NOT NULL,
                status TEXT NOT NULL, hypothesis TEXT NOT NULL, owner TEXT NOT NULL, primary_metric TEXT NOT NULL,
                guardrails TEXT NOT NULL DEFAULT '{}', variants TEXT NOT NULL DEFAULT '[]', mde REAL NOT NULL DEFAULT 10,
                power REAL NOT NULL DEFAULT 80, alpha REAL NOT NULL DEFAULT 5, min_days REAL NOT NULL DEFAULT 7,
                min_views REAL NOT NULL DEFAULT 5000, max_days REAL NOT NULL DEFAULT 30, original_photos TEXT NOT NULL DEFAULT '[]',
                original_video TEXT NOT NULL DEFAULT '', preflight TEXT NOT NULL DEFAULT '{}', runner_id INTEGER,
                created_at TEXT NOT NULL, started_at TEXT, paused_at TEXT, finished_at TEXT, decision_note TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '', error_count INTEGER NOT NULL DEFAULT 0, last_stats_at TEXT,
                freshness_seconds REAL
            );
            CREATE TABLE IF NOT EXISTS ab_hypothesis_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id INTEGER NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ab_hypotheses_status ON ab_hypotheses(status);
            """)

    def _row(self, c, hid):
        row = c.execute("SELECT * FROM ab_hypotheses WHERE id=?", (hid,)).fetchone()
        if not row:
            raise ABHypothesisError("Гипотеза не найдена")
        cols = [d[0] for d in c.execute("SELECT * FROM ab_hypotheses LIMIT 0").description]
        d = dict(zip(cols, row))
        for key in ("guardrails", "variants", "original_photos", "preflight"):
            try: d[key] = json.loads(d[key] or ("[]" if key in ("variants", "original_photos") else "{}"))
            except (TypeError, ValueError, json.JSONDecodeError): d[key] = [] if key in ("variants", "original_photos") else {}
        d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
        d["freshness_status"] = self._freshness(d)
        return d

    def _log(self, c, hid, kind, text):
        c.execute("INSERT INTO ab_hypothesis_log(hypothesis_id,at,kind,text) VALUES(?,?,?,?)", (hid, now_iso(), kind, str(text)[:1000]))

    @staticmethod
    def _freshness(d):
        if not d.get("last_stats_at"): return "нет данных"
        try: age = (datetime.now(MSK) - datetime.fromisoformat(d["last_stats_at"])).total_seconds()
        except (TypeError, ValueError): return "неизвестно"
        return "свежие" if age <= 7200 else "устарели"

    def _campaign(self, advert_id):
        with self.store._connect() as c:
            try: row = c.execute("SELECT account,id,name,status,payment_type,nms,budget FROM ads_campaigns WHERE id=?", (advert_id,)).fetchone()
            except sqlite3.OperationalError: row = None
        if not row: raise ABHypothesisError("Рекламная кампания не найдена в синхронизированных данных")
        account, cid, name, status, payment, nms, budget = row
        try: nms_list = [int(x.get("nm_id")) for x in json.loads(nms or "[]") if str(x.get("nm_id") or "").isdigit()]
        except (TypeError, ValueError, json.JSONDecodeError): nms_list = []
        return {"account": account, "id": cid, "name": name, "status": status, "payment_type": payment, "nms": nms_list, "budget": budget}

    def _card(self, nm_id, account=None):
        if not self.card_loader: raise ABHypothesisError("Провайдер карточек не настроен")
        try:
            try: card = self.card_loader(nm_id, account)
            except TypeError: card = self.card_loader(nm_id)
        except Exception as exc: raise ABHypothesisError(str(exc)) from exc
        if not card or not card.get("photos"): raise ABHypothesisError("В карточке нет опубликованных фото")
        return card

    def preflight(self, nm_id, advert_id, account=None):
        campaign = self._campaign(int(advert_id)); checks = []
        checks.append({"key": "campaign_active", "ok": campaign["status"] in (9, 11), "text": "Кампания активна или на паузе" if campaign["status"] in (9, 11) else "Кампания не активна"})
        checks.append({"key": "nm_in_campaign", "ok": int(nm_id) in campaign["nms"], "text": "Артикул входит в кампанию" if int(nm_id) in campaign["nms"] else "Артикул не входит в кампанию"})
        checks.append({"key": "account_match", "ok": not account or account == campaign["account"], "text": "Кабинет кампании подтверждён" if not account or account == campaign["account"] else "Кампания относится к другому кабинету"})
        card = self._card(int(nm_id), campaign["account"])
        same = not card.get("account") or card["account"] == campaign["account"]
        checks.append({"key": "card_account_match", "ok": same, "text": "Кабинет карточки подтверждён" if same else "Карточка относится к другому кабинету"})
        checks.append({"key": "photos", "ok": len(card["photos"]) >= 2, "text": f"Опубликовано фото: {len(card['photos'])}"})
        checks.append({"key": "token", "ok": any(n == campaign["account"] and t for n, t, *_ in self.tokens()), "text": "Ключ кабинета доступен"})
        return {"ok": all(x["ok"] for x in checks), "checks": checks, "campaign": campaign,
                "card": {"account": card.get("account", campaign["account"]), "nm_id": int(nm_id), "article": card.get("article", ""), "title": card.get("title", ""), "photos": list(card["photos"]), "video": card.get("video", "")}, "checked_at": now_iso()}

    def create(self, *, nm_id, advert_id, hypothesis, owner, primary_metric="ctr", guardrails=None, variants=None, mde=10, power=80, alpha=5, min_days=7, min_views=5000, max_days=30, account=None):
        if primary_metric not in PRIMARY_METRICS: raise ABHypothesisError("Неизвестная основная метрика")
        primary_metric = METRIC_ALIASES.get(primary_metric, primary_metric)
        if not str(hypothesis).strip() or not str(owner).strip(): raise ABHypothesisError("Укажите гипотезу и владельца")
        if not 0 < float(mde) <= 100 or not 50 <= float(power) <= 99.9 or not 0.1 <= float(alpha) <= 20: raise ABHypothesisError("Проверьте MDE, мощность и alpha")
        if float(min_days) < 1 or float(max_days) < float(min_days) or float(min_views) < 100: raise ABHypothesisError("Проверьте срок и объём показов")
        pf = self.preflight(nm_id, advert_id, account)
        if not pf["ok"]: raise ABHypothesisError("Preflight не пройден: " + "; ".join(x["text"] for x in pf["checks"] if not x["ok"]))
        selected = []
        for item in variants or []:
            try: index = int(item.get("index") if isinstance(item, dict) else item)
            except (TypeError, ValueError): continue
            if 0 <= index < len(pf["card"]["photos"]): selected.append(index)
        if not variants:
            selected = list(range(min(2, len(pf["card"]["photos"]))))
        selected = list(dict.fromkeys([0, *selected]))
        if len(selected) < 2: raise ABHypothesisError("Выберите минимум два фото, включая текущее главное")
        with self.store._connect() as c:
            busy = c.execute("SELECT id FROM ab_hypotheses WHERE nm_id=? AND status IN ('ready','running','paused')", (int(nm_id),)).fetchone()
            if busy: raise ABHypothesisError(f"По этому артикулу уже есть активная гипотеза №{busy[0]}")
            card, camp = pf["card"], pf["campaign"]
            cur = c.execute("INSERT INTO ab_hypotheses(account,nm_id,article,title,advert_id,status,hypothesis,owner,primary_metric,guardrails,variants,mde,power,alpha,min_days,min_views,max_days,original_photos,original_video,preflight,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (camp["account"], int(nm_id), card["article"], card["title"], int(advert_id), "ready", str(hypothesis).strip(), str(owner).strip(), primary_metric, json.dumps(guardrails or {}, ensure_ascii=False), json.dumps([{"index": i} for i in selected], ensure_ascii=False), float(mde), float(power), float(alpha), float(min_days), float(min_views), float(max_days), json.dumps(card["photos"], ensure_ascii=False), card["video"], json.dumps(pf, ensure_ascii=False), now_iso()))
            hid = cur.lastrowid; self._log(c, hid, "create", "Гипотеза создана после успешного preflight")
        return self.detail(hid)

    def _ensure(self, hid, statuses):
        with self.store._connect() as c: d = self._row(c, hid)
        if d["status"] not in statuses: raise ABHypothesisError(f"Действие недоступно для статуса «{d['status_label']}»")
        return d

    def _record_error(self, hid, message):
        with self.store._connect() as c:
            c.execute("UPDATE ab_hypotheses SET last_error=?,error_count=error_count+1 WHERE id=?", (str(message)[:1000], hid)); self._log(c, hid, "error", message)

    def start(self, hid):
        d = self._ensure(hid, ("ready", "paused")); pf = self.preflight(d["nm_id"], d["advert_id"], d["account"])
        if not pf["ok"]: raise ABHypothesisError("Повторный preflight не пройден")
        if list(pf["card"]["photos"]) != list(d["original_photos"]): raise ABHypothesisError("Карточка изменилась — создайте новую гипотезу")
        runner_id = d.get("runner_id")
        if self.runner:
            try:
                if not runner_id:
                    legacy_metric = {"atb_rate": "cart", "cart_rate": "cart"}.get(d["primary_metric"], d["primary_metric"])
                    indexes = [int(item.get("index")) for item in (d.get("variants") or []) if isinstance(item, dict) and str(item.get("index")).isdigit()]
                    legacy = self.runner.create(d["nm_id"], d["advert_id"], indexes or list(range(min(4, len(d["original_photos"])))), mode="views", views_per_step=max(100, d["min_views"] / 4), metric=legacy_metric, max_days=d["max_days"]); runner_id = legacy["id"]
                self.runner.start(runner_id)
            except Exception as exc: self._record_error(hid, exc); raise ABHypothesisError(f"Не удалось запустить тест: {exc}") from exc
        with self.store._connect() as c:
            now = now_iso(); c.execute("UPDATE ab_hypotheses SET status='running',runner_id=?,started_at=COALESCE(started_at,?),paused_at=NULL,last_error='' WHERE id=?", (runner_id, now, hid)); self._log(c, hid, "start", "Гипотеза запущена")
        return self.detail(hid)

    def pause(self, hid, reason="Пауза вручную"):
        d = self._ensure(hid, ("running",))
        if self.runner and d.get("runner_id"):
            try: self.runner.pause(d["runner_id"], reason)
            except Exception as exc:
                if (self._runner_detail(d) or {}).get("status") != "finished":
                    self._record_error(hid, exc); raise ABHypothesisError(str(exc)) from exc
        with self.store._connect() as c: c.execute("UPDATE ab_hypotheses SET status='paused',paused_at=? WHERE id=?", (now_iso(), hid)); self._log(c, hid, "pause", reason)
        return self.detail(hid)

    def _runner_detail(self, d):
        if self.runner and d.get("runner_id"):
            try:
                metrics = self.runner.detail(d["runner_id"])
                if metrics:
                    self._decorate_metrics(metrics, d)
                return metrics
            except Exception as exc: self._record_error(d["id"], exc)
        return None

    @staticmethod
    def _metric_value(v, metric):
        views = float(v.get("views") or 0)
        if not views:
            return None
        if metric == "ctr":
            return float(v.get("clicks") or 0) / views * 100
        if metric == "atb_rate":
            return float(v.get("atbs") or 0) / views * 1000
        if metric == "order_rate":
            return float(v.get("orders") or 0) / views * 1000
        if metric == "revenue_rate":
            return float(v.get("sum_price") or 0) / views * 1000
        if metric == "profit_rate":
            return (float(v.get("sum_price") or 0) - float(v.get("spend") or 0)) / views * 1000
        return None

    def _decorate_metrics(self, metrics, d):
        variants = metrics.get("variants") or []
        metric = METRIC_ALIASES.get(d.get("primary_metric"), d.get("primary_metric"))
        for variant in variants:
            variant["primary_value"] = self._metric_value(variant, metric)
        alive = [v for v in variants if not v.get("excluded")]
        control = next((v for v in alive if v.get("position") == 0), None)
        best = max(alive, key=lambda v: v.get("primary_value") if v.get("primary_value") is not None else float("-inf"), default=None)
        summary = metrics.setdefault("summary", {})
        summary["primary_metric"] = metric
        summary["control_value"] = control.get("primary_value") if control else None
        summary["best_value"] = best.get("primary_value") if best else None
        summary["primary_value"] = summary["best_value"]
        if control and best and control.get("primary_value") not in (None, 0) and best.get("primary_value") is not None:
            summary["lift"] = round((best["primary_value"] / control["primary_value"] - 1) * 100, 2)
        summary["best_id"] = best.get("id") if best else None
        metrics["stats_at"] = metrics.get("switched_at") or d.get("started_at")
        return metrics

    def _guardrail_report(self, d, metrics):
        rules = d.get("guardrails") or {}
        if not isinstance(rules, dict):
            return {}
        summary = (metrics or {}).get("summary") or {}
        actual = summary.get("lift")
        metric_tokens = {
            "ctr": ("ctr", "клик"),
            "atb_rate": ("atb", "корз", "добавлен"),
            "order_rate": ("order", "заказ"),
            "revenue_rate": ("revenue", "выруч"),
            "profit_rate": ("profit", "прибыл"),
        }.get(METRIC_ALIASES.get(d.get("primary_metric"), d.get("primary_metric")), ())
        report = {}
        for key, rule in rules.items():
            text = f"{key} {rule}".strip()
            match = re.search(r"([+-]?\d+(?:[.,]\d+)?)\s*%", text)
            if actual is None or not match or not any(token in text.casefold() for token in metric_tokens):
                report[str(key)] = {"status": "pending", "rule": str(rule)}
                continue
            threshold = float(match.group(1).replace(",", "."))
            lowered = text.casefold()
            is_max = any(token in lowered for token in ("не выше", "не более", "max", "cpo"))
            ok = actual <= threshold if is_max else actual >= threshold
            report[str(key)] = {"status": "ok" if ok else "fail", "rule": str(rule), "actual": round(actual, 2), "threshold": threshold}
        return report

    def _enough_data(self, d, metrics):
        if not d.get("started_at"): return False
        age = (datetime.now(MSK) - datetime.fromisoformat(d["started_at"])).total_seconds() / 86400
        views = float((metrics or {}).get("summary", {}).get("views") or 0)
        return age >= float(d["min_days"]) and views >= float(d["min_views"])

    def finish(self, hid, decision_note="", winner_variant=None, force=False):
        d = self._ensure(hid, ("running", "paused")); metrics = self._runner_detail(d)
        if not str(decision_note).strip(): raise ABHypothesisError("Опишите решение и причину выбора победителя")
        if not force and not self._enough_data(d, metrics): raise ABHypothesisError("Недостаточно данных — используйте «Нет решения» или дождитесь критерия")
        if self.runner and d.get("runner_id") and (metrics or {}).get("status") != "finished":
            try: self.runner.finish(d["runner_id"], winner_id=winner_variant, remove_uploaded=False)
            except Exception as exc: self._record_error(hid, exc); raise ABHypothesisError(str(exc)) from exc
        with self.store._connect() as c: c.execute("UPDATE ab_hypotheses SET status='finished',finished_at=?,decision_note=? WHERE id=?", (now_iso(), str(decision_note).strip(), hid)); self._log(c, hid, "finish", decision_note or "Гипотеза завершена")
        return self.detail(hid)

    def no_decision(self, hid, reason):
        if not str(reason).strip(): raise ABHypothesisError("Укажите причину решения «Нет решения»")
        d = self._ensure(hid, ("ready", "running", "paused"))
        if self.runner and d.get("runner_id") and d["status"] == "running":
            try: self.runner.pause(d["runner_id"], "Нет решения: " + reason)
            except Exception as exc: self._record_error(hid, exc)
        with self.store._connect() as c: c.execute("UPDATE ab_hypotheses SET status='no_decision',finished_at=?,decision_note=? WHERE id=?", (now_iso(), reason.strip(), hid)); self._log(c, hid, "no_decision", reason)
        return self.detail(hid)

    def rollback(self, hid, reason="Откат к исходной карточке"):
        d = self._ensure(hid, ("ready", "running", "paused", "finished", "no_decision"))
        if not self.runner: raise ABHypothesisError("Откат требует провайдера карточек")
        if d.get("runner_id") and d["status"] == "running":
            try: self.runner.pause(d["runner_id"], "Откат")
            except Exception: pass
        try:
            import httpx
            token, _ = self.runner._token(d["account"])
            with httpx.Client(timeout=60) as client: self.runner._save_media(client, token, d["nm_id"], list(d["original_photos"]), d.get("original_video", ""))
            if list(self._card(d["nm_id"], d["account"])["photos"]) != list(d["original_photos"]): raise ABHypothesisError("WB не подтвердил восстановление фото")
        except ABHypothesisError: raise
        except Exception as exc: self._record_error(hid, exc); raise ABHypothesisError(f"Не удалось выполнить откат: {exc}") from exc
        with self.store._connect() as c: c.execute("UPDATE ab_hypotheses SET status='rolled_back',finished_at=?,decision_note=? WHERE id=?", (now_iso(), reason.strip(), hid)); self._log(c, hid, "rollback", reason)
        return self.detail(hid)

    def detail(self, hid):
        with self.store._connect() as c:
            d = self._row(c, hid); d["log"] = [{"at": a, "kind": k, "text": x} for a, k, x in c.execute("SELECT at,kind,text FROM ab_hypothesis_log WHERE hypothesis_id=? ORDER BY id DESC LIMIT 200", (hid,))]
        d["metrics"] = self._runner_detail(d)
        if d["metrics"]:
            d["last_stats_at"] = d["metrics"].get("stats_at") or d.get("last_stats_at")
            d["freshness_status"] = self._freshness(d)
        d["guardrails_report"] = self._guardrail_report(d, d["metrics"])
        d["can_decide"] = d["status"] in ("running", "paused") and self._enough_data(d, d["metrics"]); return d

    def list(self):
        with self.store._connect() as c: ids = [r[0] for r in c.execute("SELECT id FROM ab_hypotheses ORDER BY id DESC")]
        return [self.detail(i) for i in ids]
