#!/usr/bin/env python3
# browser.py — логика браузера предметов (без Flask-роутов)
# Используется main.py для роутов /browser/*

import os, sys, json, time, threading, sqlite3
from collections import defaultdict
from datetime import datetime
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data

# ── State (shared via module-level, accessed by main.py) ──
_meta = {}             # {item_id: {class_id, class_name, subclass_id, ...}}
_icon_cache = {}       # {item_id: image_bytes}
_icon_name_map = {}    # {item_id: icon_name}
_snipe_list = set()    # {item_id} — item_ids in browser snipe list

# ── Category constants (from Undermine Exchange structure) ──

CAT_CLASSES = {
    "Weapons": [2],
    "Armor": [4],
    "Container": [1],
    "Consumable": [0],
    "Glyph": [11],
    "Trade Goods": [7],
    "Recipe": [9],
    "Gem": [3],
    "Miscellaneous": [15],
    "Quest": [12],
    "Battle Pet": [17],
    "Mount": [15],
    "Toy": [15],
    "Reagent": [5],
    "Projectile": [6],
    "Key": [13],
    "Quiver": [11],
    "Housing": [20],
    "Profession": [19],
}

CAT_ORDER = ["Weapons", "Armor", "Container", "Consumable", "Glyph", "Trade Goods",
             "Recipe", "Gem", "Miscellaneous", "Quest", "Battle Pet", "Mount",
             "Toy", "Reagent", "Projectile", "Key", "Quiver", "Housing", "Profession"]

WEAPON_GROUPS = [
    {"name": "One-Handed", "subclass_ids": list(range(0, 20)),
     "children": [
         {"name": "Axes", "subclass_ids": [0]},
         {"name": "Maces", "subclass_ids": [4]},
         {"name": "Swords", "subclass_ids": [7]},
         {"name": "Fist Weapons", "subclass_ids": [13]},
         {"name": "Warglaives", "subclass_ids": [20]},
         {"name": "Daggers", "subclass_ids": [15]},
     ]},
    {"name": "Two-Handed", "subclass_ids": list(range(1, 18)),
     "children": [
         {"name": "Axes", "subclass_ids": [1]},
         {"name": "Maces", "subclass_ids": [5]},
         {"name": "Swords", "subclass_ids": [8]},
         {"name": "Polearms", "subclass_ids": [6]},
         {"name": "Staves", "subclass_ids": [10]},
     ]},
    {"name": "Ranged", "subclass_ids": [2, 3, 16, 18, 19],
     "children": [
         {"name": "Bows", "subclass_ids": [2]},
         {"name": "Guns", "subclass_ids": [3]},
         {"name": "Crossbows", "subclass_ids": [18]},
         {"name": "Wands", "subclass_ids": [19]},
     ]},
    {"name": "Other", "subclass_ids": list(range(20, 50)),
     "children": [
         {"name": "Fishing Pole", "subclass_ids": [20]},
         {"name": "Miscellaneous", "subclass_ids": list(range(21, 50))},
     ]},
]

ARMOR_GROUPS = [
    {"name": "Miscellaneous", "subclass_ids": [0]},
    {"name": "Cloth", "subclass_ids": [1]},
    {"name": "Leather", "subclass_ids": [2]},
    {"name": "Mail", "subclass_ids": [3]},
    {"name": "Plate", "subclass_ids": [4]},
    {"name": "Cosmetic", "subclass_ids": [5]},
    {"name": "Shields", "subclass_ids": [6]},
    {"name": "Librams", "subclass_ids": [7]},
    {"name": "Idols", "subclass_ids": [8]},
    {"name": "Totems", "subclass_ids": [9]},
    {"name": "Sigils", "subclass_ids": [10]},
    {"name": "Relics", "subclass_ids": [11]},
]

# ── Helpers ──

def _g(v):
    if not v: return "—"
    return f"{v//10000}.{(v%10000)//100:02d}"

def _get_db(region="eu"):
    db_path = data("auction_data.db" if region == "eu" else "auction_data_us.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

# ── Loaders ──

def load_meta(region="eu"):
    global _meta
    _meta = {}
    try:
        conn = _get_db(region)
        try:
            conn.execute("SELECT expansion_id FROM item_meta LIMIT 1")
            rows = conn.execute("SELECT * FROM item_meta WHERE class_id IS NOT NULL").fetchall()
        except:
            rows = conn.execute("""
                SELECT item_id, class_id, class_name, subclass_id, subclass_name,
                       slot_type, slot_name, quality_type, quality_name,
                       item_level, required_level, bind_type, is_equippable, max_stack,
                       -1 as expansion_id
                FROM item_meta WHERE class_id IS NOT NULL
            """).fetchall()
        for r in rows:
            _meta[r["item_id"]] = dict(r)
        conn.close()
    except:
        pass
    return len(_meta)

def load_item_icons():
    global _icon_name_map
    _icon_name_map = {}
    p = data("item_icons.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.items():
                try:
                    _icon_name_map[int(k)] = v if isinstance(v, str) else str(v)
                except:
                    pass
        except:
            pass

# ── Categories ──

def build_categories(cache_dict, avg_dict):
    """Build category tree from auction cache and meta.
    cache_dict: {(item_id, ilvl): [(realm_id, price, qty, avg), ...]}
    avg_dict: {(item_id, ilvl): float}
    """
    items_with_data = set()
    for (item_id, ilvl) in cache_dict:
        items_with_data.add(item_id)
    for (item_id, ilvl) in avg_dict:
        items_with_data.add(item_id)

    class_counts = defaultdict(int)
    subclass_counts = defaultdict(lambda: defaultdict(int))
    subclass_meta = {}

    for item_id in items_with_data:
        meta = _meta.get(item_id)
        if not meta: continue
        cid = meta.get("class_id")
        if cid is None: continue
        scid = meta.get("subclass_id")
        class_counts[cid] += 1
        if scid is not None:
            subclass_counts[cid][scid] = subclass_counts[cid].get(scid, 0) + 1
            if (cid, scid) not in subclass_meta:
                subclass_meta[(cid, scid)] = meta

    tree = []
    for cat_name in CAT_ORDER:
        cids = CAT_CLASSES.get(cat_name, [])
        total_count = sum(class_counts.get(cid, 0) for cid in cids)
        if total_count == 0: continue

        if cat_name == "Weapons":
            weapon_tree = []
            for group in WEAPON_GROUPS:
                g_count = 0; children = []
                for child in group["children"]:
                    c_count = sum(subclass_counts[cid].get(scid, 0) for cid in cids for scid in child["subclass_ids"])
                    if c_count > 0:
                        children.append({"id": child["subclass_ids"][0], "name": child["name"],
                                         "count": c_count, "filter_class_ids": cids,
                                         "filter_subclass_ids": child["subclass_ids"], "is_leaf": True})
                    g_count += c_count
                if g_count > 0:
                    weapon_tree.append({"id": f"g_{group['name']}", "name": group["name"],
                                        "count": g_count, "subclasses": children, "is_group": True,
                                        "filter_class_ids": cids, "filter_subclass_ids": group["subclass_ids"]})
            tree.append({"id": 2, "name": "Weapons", "count": total_count, "subclasses": weapon_tree, "filter_class_ids": [2]})

        elif cat_name == "Armor":
            armor_tree = []
            for group in ARMOR_GROUPS:
                a_count = sum(subclass_counts[cid].get(scid, 0) for cid in cids for scid in group["subclass_ids"])
                if a_count > 0:
                    armor_tree.append({"id": group["subclass_ids"][0], "name": group["name"],
                                       "count": a_count, "filter_class_ids": cids,
                                       "filter_subclass_ids": group["subclass_ids"], "is_leaf": True})
            tree.append({"id": 4, "name": "Armor", "count": total_count, "subclasses": armor_tree, "filter_class_ids": [4]})

        else:
            sub_map = {}
            for cid in cids:
                for scid, cnt in subclass_counts[cid].items():
                    meta = subclass_meta.get((cid, scid))
                    scname = (meta.get("subclass_name") or f"Subclass {scid}") if meta else f"Subclass {scid}"
                    if scname in sub_map:
                        sub_map[scname]["count"] += cnt
                    else:
                        sub_map[scname] = {"id": scid, "name": scname, "count": cnt,
                                           "filter_class_ids": cids, "filter_subclass_ids": [scid], "is_leaf": True}
            sub_list = sorted(sub_map.values(), key=lambda x: -x["count"])
            tree.append({"id": cids[0], "name": cat_name, "count": total_count, "subclasses": sub_list, "filter_class_ids": cids})

    return tree

# ── Icons ──

def get_item_icon(item_id, region="eu"):
    """Return (bytes, content_type) or (None, None)"""
    local_path = data(os.path.join("icons", f"{item_id}.jpg"))
    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            return f.read(), "image/jpeg"

    cache_key = str(item_id)
    if cache_key in _icon_cache:
        return _icon_cache[cache_key], "image/jpeg"

    icon_name = _icon_name_map.get(item_id)
    if icon_name:
        cdn_url = f"https://render.worldofwarcraft.com/{region}/icons/56/{icon_name}.jpg"
        try:
            r = requests.get(cdn_url, timeout=10)
            if r.status_code == 200:
                _icon_cache[cache_key] = r.content
                return r.content, "image/jpeg"
        except:
            pass
    return None, None

# ── Snipe management ──

def get_snipe_list():
    return sorted(_snipe_list)

def toggle_snipe(item_id):
    if item_id in _snipe_list:
        _snipe_list.discard(item_id)
        return False
    else:
        _snipe_list.add(item_id)
        return True

def add_snipe_bulk(item_ids):
    for iid in item_ids:
        _snipe_list.add(iid)
    return len(_snipe_list)

def reset_snipe_list(item_ids):
    _snipe_list.clear()
    for iid in item_ids:
        _snipe_list.add(iid)
    return len(_snipe_list)
