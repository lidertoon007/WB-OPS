"""Run in the user's project AFTER replacing sources. No publication calls."""
import asyncio
import json

from app.config import get_settings
from app.services.cabinets import load_cabinets
from app.services.replies import ReplyService
from app.services.wb_client import WBFeedbackClient
from app.storage import DraftStore
from pathlib import Path

async def main():
    settings = get_settings()
    store = DraftStore(settings.database_url)
    classification = json.loads(Path("data/order_status_upgrade.json").read_text(encoding="utf-8"))
    with store._connect() as c:
        c.executemany("UPDATE reply_drafts SET user_edited=0 WHERE item_id=? AND confirmed=0 AND published=0",
                      [(item_id,) for item_id in classification["automatic"]])
    service = ReplyService()
    totals = {}
    for cabinet in load_cabinets(settings.cabinets_json):
        items = await WBFeedbackClient(cabinet.token, settings.feedbacks_api_base_url).fetch_unanswered()
        for item in items:
            item.cabinet_id = cabinet.id
        store.save_inbox(items)
        for item in items:
            store.save_generated(service.draft(item))
            if item.item_type.value == "review":
                totals[item.order_status.value] = totals.get(item.order_status.value, 0) + 1
    # Refresh automatically generated stale inbox rows with conservative unknown status.
    for item in store.list_inbox():
        store.save_generated(service.draft(item))
    store.audit("order_status_upgrade", details=json.dumps(totals))
    classification["completed"] = True
    Path("data/order_status_upgrade.json").write_text(json.dumps(classification), encoding="utf-8")
    print(json.dumps({"statuses": totals, "preserved_drafts": len(classification["preserved"]), "published": 0}))

if __name__ == "__main__":
    asyncio.run(main())
