"""Run in the user's project BEFORE replacing sources. No credentials read."""
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.services.replies import ReplyService
from app.storage import DraftStore

EXPECTED = {
    "app/models.py": "e217f80b22565edcb0e9edda148bd7f3ce68d368dd40a73e7e9ea91d9c13dec2",
    "app/storage.py": "61311c9c7f882af64f57ea66727468a6fdabe3e801361ee0a32bdd02c457d539",
    "app/main.py": "c6a1d356d360fcb3502efe28a8061b46702d97c760e49eb37d10c348978e10aa",
    "app/services/wb_client.py": "939317f0389f9844817865c445645fd7f5e09387d4a8383eaabd61ff38fb015a",
    "app/services/replies.py": "6605f663786d506f600b021322e093544445c2c27533b90637a135caa1be4170",
}
for name, digest in EXPECTED.items():
    if name != "app/main.py" and hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
        raise RuntimeError(f"Concurrent source change: {name}")
store = DraftStore(get_settings().database_url)
backup = Path("backups") / ("order_status_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
backup.mkdir(parents=True)
for name in EXPECTED:
    destination = backup / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(name, destination)
with sqlite3.connect(store.path) as source, sqlite3.connect(backup / "wbops.sqlite3") as target:
    source.backup(target)
service = ReplyService()
automatic, preserved = [], []
for item in store.list_inbox():
    draft = store.get(item.id)
    if not draft:
        continue
    generated = service.draft(item)
    if not draft.confirmed and not draft.published and draft.text == generated.text and draft.blocked_reason == generated.blocked_reason:
        automatic.append(item.id)
    else:
        preserved.append(item.id)
(backup / "draft_classification.json").write_text(json.dumps({"automatic": automatic, "preserved": preserved}, ensure_ascii=False), encoding="utf-8")
Path("data/order_status_upgrade.json").write_text(json.dumps({"backup": str(backup), "automatic": automatic, "preserved": preserved}), encoding="utf-8")
print(json.dumps({"backup": str(backup), "automatic": len(automatic), "preserved": len(preserved)}))
