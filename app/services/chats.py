"""Local chat inbox used by the unified marketplace chat workspace.

The store keeps a normalized conversation model so marketplace adapters can be
added without changing the UI. The first adapter can populate this store from
WB/Ozon/Yandex APIs; manual replies and templates already use the same model.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4


class ChatStore:
    def __init__(self, path: str = "app/data/chats.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        if not self.path.exists():
            self.path.write_text(json.dumps({"chats": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def all(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [x for x in data.get("chats", []) if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, chats: list[dict]) -> None:
        self.path.write_text(json.dumps({"chats": chats}, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self, marketplace: str | None = None, status: str | None = None, search: str | None = None) -> list[dict]:
        values = self.all()
        term = (search or "").strip().lower()
        result = []
        for chat in values:
            if marketplace and chat.get("marketplace") != marketplace:
                continue
            if status and chat.get("status") != status:
                continue
            haystack = " ".join([str(chat.get("product_name", "")), str(chat.get("product_id", "")), *[str(m.get("text", "")) for m in chat.get("messages", [])]]).lower()
            if term and term not in haystack:
                continue
            result.append(chat)
        return sorted(result, key=lambda x: x.get("updated_at", ""), reverse=True)

    def get(self, chat_id: str) -> dict | None:
        return next((x for x in self.all() if x.get("id") == chat_id), None)

    def add_message(self, chat_id: str, text: str, sender: str = "seller", status: str | None = None) -> dict:
        with self.lock:
            chats = self.all()
            chat = next((x for x in chats if x.get("id") == chat_id), None)
            if chat is None:
                raise KeyError(chat_id)
            now = datetime.now(timezone.utc).isoformat()
            chat.setdefault("messages", []).append({"id": uuid4().hex, "sender": sender, "text": text.strip(), "created_at": now})
            chat["updated_at"] = now
            chat["status"] = status or ("waiting" if sender == "buyer" else "answered")
            self._save(chats)
            return chat

    def upsert_many(self, incoming: list[dict]) -> int:
        with self.lock:
            chats = self.all()
            existing = {x.get("id"): x for x in chats}
            for value in incoming:
                item = {"id": value.get("id") or uuid4().hex, "marketplace": value.get("marketplace", "wildberries"), "cabinet_id": value.get("cabinet_id", "default"), "product_id": value.get("product_id"), "product_name": value.get("product_name", ""), "status": value.get("status", "waiting"), "updated_at": value.get("updated_at") or datetime.now(timezone.utc).isoformat(), "messages": value.get("messages", [])}
                existing[item["id"]] = item
            self._save(list(existing.values()))
            return len(incoming)
