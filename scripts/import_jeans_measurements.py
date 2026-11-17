"""Merge the approved D1 and X6 jeans measurements into the product models."""
import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


KNOWLEDGE = Path(__file__).resolve().parents[1] / "app" / "knowledge"
EXCLUDED = re.compile(r"багги|банан|джоггер|резинк|\bbaggy\b|\bbanana\b|\bjogger", re.I)
WASHED_OR_NARROW = re.compile(r"хулиган|зауж|\bslim\b", re.I)
X6 = re.compile(r"(?<!\d)5049(?!\d)|(?<!\w)[xх]6(?!\d)", re.I)
D1 = re.compile(r"(?<!\w)d1(?!\w)|прям\w*|флис", re.I)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def key(value):
    return " ".join(value.casefold().split())


def prepare_changes(document, cards, grids):
    models = document["models"]
    by_id = {str(nm): name for name, model in models.items() for nm in model.get("nm_ids", [])}
    by_key = {key(name): name for name in models}
    groups = defaultdict(list)
    for card in cards:
        if card.get("vendor") and card.get("subject", "").casefold() == "джинсы":
            groups[key(card["vendor"])].append(card)
    changes = {}
    skipped = defaultdict(list)
    candidates = defaultdict(list)
    for article, records in sorted(groups.items()):
        facts = " ".join([article, *(card.get("title", "") for card in records)])
        if EXCLUDED.search(facts):
            skipped["excluded"].append(article)
            continue
        if X6.search(article):
            group = "X6"
        elif D1.search(facts) and not WASHED_OR_NARROW.search(facts):
            group = "D1"
        else:
            skipped["unconfirmed"].append(article)
            continue
        names = {by_id[str(card["nm"])] for card in records if str(card["nm"]) in by_id}
        if article in by_key:
            names.add(by_key[article])
        if len(names) > 1:
            skipped["conflicting_links"].append(article)
            continue
        target = next(iter(names), records[0]["vendor"].strip())
        candidates[target].append((group, records))

    # The article is explicitly confirmed even when it is absent from the WB cache.
    if "5049" not in groups:
        candidates[by_key.get("5049", "5049")].append(("X6", []))

    applied = defaultdict(list)
    for name, entries in sorted(candidates.items()):
        families = {group for group, _ in entries}
        records = [record for _, cards in entries for record in cards]
        current = models.get(name, {})
        other_cards = [card for article, cards in groups.items() for card in cards
                       if by_id.get(str(card["nm"])) == name and card not in records]
        model_description = " ".join(str(current.get(field, "")) for field in ("name", "fit", "fastener"))
        if len(families) != 1 or other_cards or EXCLUDED.search(model_description):
            skipped["conflicting_links"].append(name)
            continue
        family = families.pop()
        if family == "D1" and WASHED_OR_NARROW.search(model_description):
            skipped["unconfirmed"].append(name)
            continue
        grid = grids[family]
        sizes = {match.group(1) for card in records for value in card.get("sizes", [])
                 if (match := re.fullmatch(r"(?:W\s*)?(\d{2})(?:\s+акц)?", str(value).strip(), re.I))}
        if records and not (sizes & grid.keys()):
            skipped["no_matching_sizes"].append(name)
            continue
        model = dict(current)
        model.setdefault("name", records[0].get("title", "") if records else "Джинсы приуженные 5049")
        model["nm_ids"] = sorted({int(nm) for nm in model.get("nm_ids", [])} | {int(card["nm"]) for card in records})
        if not model.get("sizes"):
            model["sizes"] = [
                {"size": size, **dict(zip(grids["fields"], map(str, values), strict=True))}
                for size, values in grid.items() if not records or size in sizes
            ]
        if model != current:
            changes[name] = model
        applied[family].append(name)
    return changes, dict(applied), dict(skipped)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=KNOWLEDGE / "models.json")
    parser.add_argument("--cards", type=Path, default=KNOWLEDGE / "wb_cards.json")
    parser.add_argument("--apply", action="store_true", help="Save the changes and back up the current file")
    args = parser.parse_args()
    original = args.models.read_bytes()
    document = json.loads(original.decode("utf-8-sig"))
    changes, applied, skipped = prepare_changes(document, read(args.cards)["cards"], read(KNOWLEDGE / "jeans_measurements.json"))
    print(json.dumps({"changed": list(changes), "families": applied, "skipped": skipped}, ensure_ascii=False))
    if not args.apply or not changes:
        return
    document["models"].update(changes)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = args.models.with_suffix(f".{timestamp}.bak")
    pending = args.models.with_suffix(f".{timestamp}.tmp")
    try:
        pending.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.chmod(args.models.stat().st_mode & 0o777)
        if args.models.read_bytes() != original:
            raise SystemExit("The models file changed during import. Run the import again.")
        backup.write_bytes(original)
        pending.replace(args.models)
    finally:
        pending.unlink(missing_ok=True)
    print(f"Backup saved to {backup}")


if __name__ == "__main__":
    main()
