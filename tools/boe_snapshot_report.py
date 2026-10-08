"""Build a compact report from Blizzard connected-realm auction JSON.

Usage: python tools/boe_snapshot_report.py snapshot.json > boe_snapshot_report.json
The input may be the raw API object or a plain auctions list. No credentials/network.
"""
import json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
from ahgem import compute_ilvl, decode_boe_variant

def main(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    auctions = raw.get("auctions", raw) if isinstance(raw, dict) else raw
    out = defaultdict(lambda: {"prices": [], "quantity": 0, "bonus_lists": []})
    for a in auctions:
        item = a.get("item") or {"id": a.get("item_id"), "bonus_lists": a.get("bonus_lists"), "modifiers": a.get("modifiers")}
        iid = int(item.get("id") or 0)
        ilvl = compute_ilvl(item.get("bonus_lists") or [])
        if not iid or ilvl < 292:
            continue
        price = a.get("buyout") or a.get("unit_price") or a.get("price") or 0
        if not price:
            continue
        decoded = decode_boe_variant(iid, item.get("bonus_lists"), item.get("modifiers"))
        sig = tuple(decoded["bonus_lists"])
        key = (iid, ilvl, sig, tuple(decoded["modifier_stat_ids"]))
        row = out[key]
        row.update(decoded)
        row["item_id"], row["ilvl"] = iid, ilvl
        row["prices"].append(price)
        row["quantity"] += int(a.get("quantity") or 1)
        row["bonus_lists"] = list(sig)
    rows = []
    for row in out.values():
        row["min_price"] = min(row["prices"])
        row["lot_count"] = len(row["prices"])
        del row["prices"]
        rows.append(row)
    rows.sort(key=lambda x: (x["item_id"], x["ilvl"], x["min_price"]))
    print(json.dumps({"schema": 4, "min_ilvl": 292, "items": rows}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tools/boe_snapshot_report.py snapshot.json")
    main(sys.argv[1])
