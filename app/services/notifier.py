"""Уведомления в Telegram и на email о новых отзывах, вопросах, ответах и чатах.

Настройки — в app_settings["notifications"]. Секреты (токен бота, пароль почты) в интерфейс не отдаются.
Новые отзывы и вопросы сообщаются сразу после синхронизации; отправленные ответы, вопросы на проверке
и чаты — фоновой проверкой журнала раз в минуту (так ловятся все способы отправки: по одному, пачкой, автоответ).
"""
from __future__ import annotations

import asyncio
import copy
import json
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx

from app.services.telegram import send_telegram_message

DEFAULTS = {
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "email": {"enabled": False, "host": "", "port": 465, "security": "ssl", "username": "", "password": "", "sender": "", "to": ""},
    "events": {"reviews_new": True, "reviews_answered": False, "reviews_negative_only": False,
               "questions_new": True, "questions_status": False, "chats_new": True, "sync_errors": True, "stocks_low": True},
    "cabinets": [],               # пусто — все кабинеты
}
SECRETS = {("telegram", "bot_token"), ("email", "password")}
STARS = {1: "★☆☆☆☆", 2: "★★☆☆☆", 3: "★★★☆☆", 4: "★★★★☆", 5: "★★★★★"}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = {**out[key], **{k: v for k, v in value.items() if k in out[key]}}
        elif key in out:
            out[key] = value
    return out


class Notifier:
    def __init__(self, store, cabinet_names=lambda: {}):
        self.store = store
        self.cabinet_names = cabinet_names

    # ------------------------------------------------------------------ настройки
    def config(self) -> dict:
        raw = self.store.get_setting("notifications", "")
        if raw:
            try:
                return _merge(DEFAULTS, json.loads(raw))
            except ValueError:
                pass
        # Перенос со старой панели «Настройки оповещений Telegram»
        try:
            old = json.loads(self.store.get_setting("module:telegram", "{}") or "{}")
        except ValueError:
            old = {}
        cfg = copy.deepcopy(DEFAULTS)
        if isinstance(old, dict) and old:
            cfg["telegram"].update(enabled=bool(old.get("enabled")), bot_token=str(old.get("bot_token") or ""), chat_id=str(old.get("chat_id") or ""))
            cfg["events"].update(reviews_new=bool(old.get("notify_new_reviews", True)), questions_new=bool(old.get("notify_new_questions", True)),
                                 reviews_answered=bool(old.get("notify_published")), questions_status=bool(old.get("notify_question_answers")))
        return cfg

    def public(self) -> dict:
        cfg = self.config()
        out = copy.deepcopy(cfg)
        for section, key in SECRETS:
            out[section][f"{key}_set"] = bool(cfg[section][key])
            out[section][key] = ""
        out["status"] = self.status()
        return out

    def save(self, value: dict) -> dict:
        cfg = self.config()
        new = _merge(cfg, value)
        for section, key in SECRETS:      # пустое поле = оставить сохранённый секрет
            if not str((value.get(section) or {}).get(key) or "").strip() and not (value.get(section) or {}).get(f"clear_{key}"):
                new[section][key] = cfg[section][key]
        tg, em = new["telegram"], new["email"]
        tg["bot_token"], tg["chat_id"] = str(tg["bot_token"]).strip(), str(tg["chat_id"]).strip()
        try:
            em["port"] = int(em["port"] or 465)
        except (TypeError, ValueError):
            raise ValueError("Порт почтового сервера — число")
        if em["security"] not in ("ssl", "starttls", "none"):
            em["security"] = "ssl"
        if tg["enabled"] and not (tg["bot_token"] and tg["chat_id"]):
            raise ValueError("Для Telegram нужны токен бота и Chat ID")
        if em["enabled"] and not (em["host"] and em["to"]):
            raise ValueError("Для email нужны сервер SMTP и адрес получателя")
        new["cabinets"] = [str(c) for c in new.get("cabinets") or []]
        if tg["bot_token"] != cfg["telegram"]["bot_token"] or tg["chat_id"] != cfg["telegram"]["chat_id"]:
            self._set_status("telegram", None)
        self.store.set_setting("notifications", json.dumps(new, ensure_ascii=False))
        return self.public()

    def status(self) -> dict:
        try:
            return json.loads(self.store.get_setting("notifications_status", "{}") or "{}")
        except ValueError:
            return {}

    def _set_status(self, channel: str, ok: bool | None, error: str = "") -> None:
        st = self.status()
        if ok is None:
            st.pop(channel, None)
        else:
            st[channel] = {"ok": ok, "error": error[:300], "at": datetime.now(timezone.utc).isoformat()}
        self.store.set_setting("notifications_status", json.dumps(st, ensure_ascii=False))

    # ------------------------------------------------------------------ отправка
    async def _telegram(self, cfg, text) -> None:
        tg = cfg["telegram"]
        try:
            ok = await send_telegram_message(tg, text)
        except Exception as exc:
            raise RuntimeError(f"Telegram: {type(exc).__name__}") from exc
        if not ok:
            raise RuntimeError("Telegram не принял сообщение: проверьте токен бота и Chat ID и что вы написали боту /start")

    @staticmethod
    def _send_email(em: dict, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = em["sender"] or em["username"]
        msg["To"] = em["to"]
        msg.set_content(text)
        host, port = em["host"], int(em["port"])
        if em["security"] == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            if em["security"] == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if em["username"]:
                server.login(em["username"], em["password"])
            server.send_message(msg, to_addrs=[x.strip() for x in em["to"].replace(";", ",").split(",") if x.strip()])

    async def _email(self, cfg, subject, text) -> None:
        try:
            await asyncio.to_thread(self._send_email, cfg["email"], subject, text)
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError("Почта: сервер не принял логин или пароль (для Яндекса и Gmail нужен пароль приложения)") from exc
        except Exception as exc:
            raise RuntimeError(f"Почта: {type(exc).__name__}: {str(exc)[:150]}") from exc

    async def send(self, subject: str, text: str, cfg: dict | None = None) -> dict:
        cfg = cfg or self.config()
        result = {}
        for channel, enabled in (("telegram", cfg["telegram"]["enabled"]), ("email", cfg["email"]["enabled"])):
            if not enabled:
                continue
            try:
                if channel == "telegram":
                    await self._telegram(cfg, f"{subject}\n\n{text}")
                else:
                    await self._email(cfg, f"WB OPS: {subject}", text)
                result[channel] = True
                self._set_status(channel, True)
            except Exception as exc:
                result[channel] = False
                self._set_status(channel, False, str(exc))
                self.store.audit("notify_error", channel, str(exc)[:300])
        return result

    async def test(self, channel: str) -> dict:
        cfg = self.config()
        cfg["telegram"]["enabled"] = channel == "telegram"
        cfg["email"]["enabled"] = channel == "email"
        if channel == "telegram" and not (cfg["telegram"]["bot_token"] and cfg["telegram"]["chat_id"]):
            raise ValueError("Сначала сохраните токен бота и Chat ID")
        if channel == "email" and not (cfg["email"]["host"] and cfg["email"]["to"]):
            raise ValueError("Сначала сохраните сервер SMTP и адрес получателя")
        result = await self.send("Проверка уведомлений", "Это тестовое сообщение WB OPS. Если вы его видите — уведомления настроены.", cfg)
        return {"ok": bool(result.get(channel)), "error": self.status().get(channel, {}).get("error", "")}

    async def detect_chat(self, bot_token: str | None = None) -> dict:
        """Найти Chat ID по последнему сообщению боту (пользователь пишет боту /start)."""
        token = (bot_token or "").strip() or self.config()["telegram"]["bot_token"]
        if not token:
            raise ValueError("Укажите токен бота")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getUpdates")
        if r.status_code == 401 or r.status_code == 404:
            raise ValueError("Telegram не принял токен бота")
        chats = []
        for update in reversed(r.json().get("result", [])):
            message = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
            chat = message.get("chat") or {}
            if chat.get("id") and chat["id"] not in [c["id"] for c in chats]:
                chats.append({"id": chat["id"], "title": chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""})
        if not chats:
            raise ValueError("Боту ещё никто не писал: откройте бота в Telegram, нажмите «Старт» и повторите")
        return {"chats": chats[:5]}

    # ------------------------------------------------------------------ события
    def _cabinet_ok(self, cfg, cabinet_id) -> bool:
        return not cfg["cabinets"] or str(cabinet_id) in cfg["cabinets"]

    def _enabled(self, cfg) -> bool:
        return cfg["telegram"]["enabled"] or cfg["email"]["enabled"]

    async def new_items(self, cabinet, items) -> None:
        cfg = self.config()
        if not self._enabled(cfg) or not self._cabinet_ok(cfg, cabinet.id):
            return
        ev = cfg["events"]
        parts = []
        reviews = [i for i in items if i.item_type.value == "review"]
        if ev["reviews_negative_only"]:
            reviews = [i for i in reviews if i.rating is not None and i.rating <= 3]
        if ev["reviews_new"] and reviews:
            title = "негативных отзывов" if ev["reviews_negative_only"] else "отзывов"
            lines = [f"Новых {title}: {len(reviews)}"]
            for i in sorted(reviews, key=lambda x: x.rating or 6)[:5]:
                lines.append(f"{STARS.get(i.rating, '—')} {(i.product_name or '')[:40]}: {(i.text or 'без текста').strip()[:140]}")
            parts.append("\n".join(lines))
        questions = [i for i in items if i.item_type.value == "question"]
        if ev["questions_new"] and questions:
            lines = [f"Новых вопросов: {len(questions)}"] + [f"• {(i.text or '').strip()[:160]}" for i in questions[:5]]
            parts.append("\n".join(lines))
        if parts:
            await self.send(f"Кабинет {cabinet.name}", "\n\n".join(parts), cfg)

    async def sync_error(self, cabinet, error: str) -> None:
        cfg = self.config()
        if self._enabled(cfg) and cfg["events"]["sync_errors"] and self._cabinet_ok(cfg, cabinet.id):
            await self.send(f"Ошибка загрузки · {cabinet.name}", str(error)[:500], cfg)

    def _cursor(self) -> int:
        try:
            return int(self.store.get_setting("notify_audit_cursor", "0") or 0)
        except ValueError:
            return 0

    def init_cursor(self) -> None:
        """При первом запуске не присылать уведомления о старых событиях."""
        if self.store.get_setting("notify_audit_cursor", "") == "":
            with self.store._connect() as c:
                last = c.execute("SELECT COALESCE(MAX(id),0) FROM audit_log").fetchone()[0]
            self.store.set_setting("notify_audit_cursor", str(last))

    async def scan_journal(self) -> dict:
        """Отправленные ответы, вопросы на проверке, полученные чаты — из журнала с прошлого раза."""
        cursor = self._cursor()
        with self.store._connect() as c:
            rows = c.execute("SELECT a.id, a.event, a.item_id, a.details, COALESCE(i.cabinet_id,''), a.actor FROM audit_log a "
                             "LEFT JOIN inbox_items i ON i.id=a.item_id WHERE a.id>? AND a.event IN "
                             "('published','auto_question_generated','auto_reply_generated','chats_synced') ORDER BY a.id", (cursor,)).fetchall()
        if not rows:
            return {}
        self.store.set_setting("notify_audit_cursor", str(rows[-1][0]))
        cfg = self.config()
        if not self._enabled(cfg):
            return {}
        ev = cfg["events"]
        counts = {"review_answers": 0, "question_answers": 0, "moderation": 0, "chats": 0}
        for _, event, _item, details, cabinet_id, _actor in rows:
            if cabinet_id and not self._cabinet_ok(cfg, cabinet_id):
                continue
            details = details or ""
            if event == "published":
                counts["question_answers" if "type=question" in details else "review_answers"] += 1
            elif event in ("auto_question_generated", "auto_reply_generated") and "status=moderation" in details:
                counts["moderation"] += 1
            elif event == "chats_synced":
                try:
                    counts["chats"] += int(details.split("received=")[1].split(";")[0])
                except (IndexError, ValueError):
                    pass
        lines = []
        if ev["reviews_answered"] and counts["review_answers"]:
            lines.append(f"Отправлено ответов на отзывы: {counts['review_answers']}")
        if ev["questions_status"] and counts["question_answers"]:
            lines.append(f"Отправлено ответов на вопросы: {counts['question_answers']}")
        if ev["questions_status"] and counts["moderation"]:
            lines.append(f"Черновиков ждут проверки: {counts['moderation']}")
        if ev["chats_new"] and counts["chats"]:
            lines.append(f"Новых сообщений в чатах: {counts['chats']}")
        if lines:
            await self.send("Сводка", "\n".join(lines), cfg)
        return counts
