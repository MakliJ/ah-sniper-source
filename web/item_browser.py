#!/usr/bin/env python3
# item_browser.py — WoW Auction Item Browser (аналог undermine.exchange)
# Standalone PyWebView + Flask приложение для просмотра предметов по категориям
# Порты: Flask на 8766, PyWebView GUI

import os, sys, json, time, threading, sqlite3, logging
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone
from collections import defaultdict

# ── Path to root (paths.py is one level up from web/) ──
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data, static_dir

HOST, PORT = "127.0.0.1", 8766

from dotenv import load_dotenv; load_dotenv()

# ── Config ──
SETTINGS_FILE = data("settings.json")
PRESETS_FILE = data("presets.json")
REGION = os.getenv("AHGEN_REGION", "eu")

# ── App ──
from io import BytesIO
from flask import Flask, jsonify, request, send_from_directory, send_file, abort
app = Flask(__name__, static_folder=static_dir(), static_url_path="")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("browser")

_HTML = None  # будет загружен при старте

# ═══════════════ RAM Cache ═══════════════

_CACHE_LOCK = threading.Lock()

# auction data: {(item_id, ilvl): [(realm_id, min_buyout, quantity, avg_price), ...]}
_cache = defaultdict(list)

# top10avg: {(item_id, ilvl): float}
_avg = {}

# ── Midnight 12.1: только предметы патча 12.1.0+ (id >= 276571, проверено по wowhead) ──
# Плюс ручные id из midnight_12_1_items.txt (через запятую).
MIN_MIDNIGHT_121_ID = 276571
_MIDNIGHT_121 = None

def _midnight_121_set():
    global _MIDNIGHT_121
    if _MIDNIGHT_121 is None:
        s = set()
        try:
            p = data("midnight_12_1_items.txt")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    content = "\n".join(
                        line.split("#", 1)[0] for line in f
                    )
                    for tok in content.replace("\n", ",").split(","):
                        tok = tok.strip()
                        if tok.isdigit():
                            s.add(int(tok))
        except Exception:
            pass
        _MIDNIGHT_121 = s
    return _MIDNIGHT_121

# item_meta: {item_id: {class_id, class_name, subclass_id, subclass_name, ...}}
_meta = {}

# item names: {item_id: str}
_inames = {}

# realm info: {realm_id: {name_en, name_ru, slug}}
_realms = {}

# preset info
_presets = []          # list of presets
_active_preset = None  # active preset dict
_snipe_ids = set()     # all item_ids from active preset's item_ids + boe_filters

# last snapshot time
_last_collected = ""
_region = REGION

# log buffer
_log_buf = []
_MAX_LOG = 200

def _db_path(region=None):
    r = region or _region
    return data("auction_data.db" if r == "eu" else "auction_data_us.db")

def _avg_cache_path(region=None):
    r = region or _region
    return data(f"topxavg_cache_{r}.json" if r != "eu" else "topxavg_cache.json")

def _g(v):
    """Format copper to gold string"""
    if not v: return "—"
    return f"{v//10000}.{(v%10000)//100:02d}"

def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    _log_buf.append(f"{ts} {msg}")
    if len(_log_buf) > _MAX_LOG:
        _log_buf.pop(0)
    log.info(msg)

# ═══════════════ DB Helpers ═══════════════

def _get_db(region=None):
    r = region or _region
    db = _db_path(r)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn

def _load_meta():
    """Load item_meta into RAM"""
    global _meta
    _meta = {}
    try:
        conn = _get_db()
        # Check if expansion_id column exists
        has_exp = False
        try:
            conn.execute("SELECT expansion_id FROM item_meta LIMIT 1")
            has_exp = True
        except:
            pass
        if has_exp:
            rows = conn.execute("""
                SELECT item_id, class_id, class_name, subclass_id, subclass_name,
                       slot_type, slot_name, quality_type, quality_name,
                       item_level, required_level, bind_type, is_equippable, max_stack,
                       expansion_id
                FROM item_meta
                WHERE class_id IS NOT NULL
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT item_id, class_id, class_name, subclass_id, subclass_name,
                       slot_type, slot_name, quality_type, quality_name,
                       item_level, required_level, bind_type, is_equippable, max_stack,
                       -1 as expansion_id
                FROM item_meta
                WHERE class_id IS NOT NULL
            """).fetchall()
        for r in rows:
            _meta[r["item_id"]] = dict(r)
        conn.close()
        _log(f"Loaded {len(_meta)} item meta entries")
    except Exception as e:
        _log(f"Error loading item_meta: {e}")

def _load_realms():
    """Load realms into RAM"""
    global _realms
    _realms = {}
    try:
        conn = _get_db()
        rows = conn.execute("SELECT id, name_en, name_ru, slug FROM realms").fetchall()
        for r in rows:
            _realms[r["id"]] = {
                "name_en": r["name_en"] or f"R{r['id']}",
                "name_ru": r["name_ru"] or r["name_en"] or f"R{r['id']}",
                "slug": r["slug"] or ""
            }
        conn.close()
        _log(f"Loaded {len(_realms)} realms")
    except Exception as e:
        _log(f"Error loading realms: {e}")

def _load_item_names():
    """Load item names from DB or JSON"""
    global _inames
    _inames = {}
    # Try JSON first (faster)
    for jf in ["item_names.json", "item_names_ru.json"]:
        jp = data(jf)
        if os.path.exists(jp):
            try:
                with open(jp, encoding="utf-8") as f:
                    raw = json.load(f)
                _inames.update({int(k) if isinstance(k, str) and k.isdigit() else k: v for k, v in raw.items()})
                break
            except:
                pass
    if not _inames:
        try:
            conn = _get_db()
            for r in conn.execute("SELECT id, name_en FROM items").fetchall():
                _inames[r["id"]] = r["name_en"]
            conn.close()
        except:
            pass
    _log(f"Loaded {len(_inames)} item names")

def _load_auction_data():
    """Load latest auction data into RAM cache"""
    global _cache, _avg, _last_collected
    try:
        conn = _get_db()
        r = conn.execute("SELECT value FROM meta WHERE key='last_snapshot'").fetchone()
        new_cache = defaultdict(list)
        new_avg = {}
        new_last = ""

        if r:
            new_last = r[0]
            rows = conn.execute("""
                SELECT item_id, ilvl, realm_id, min_buyout, quantity, avg_price
                FROM auction_snapshots WHERE collected_at=?
            """, (new_last,)).fetchall()
            if rows:
                for row in rows:
                    k = (row["item_id"], row["ilvl"] or 0)
                    new_cache[k].append((row["realm_id"], row["min_buyout"], row["quantity"], row["avg_price"] or 0))
        else:
            rows = conn.execute("""
                SELECT item_id, ilvl, realm_id, min_buyout, quantity, avg_price
                FROM auction_latest
            """).fetchall()
            if rows:
                new_last = datetime.now().isoformat()
                for row in rows:
                    k = (row["item_id"], row["ilvl"] or 0)
                    new_cache[k].append((row["realm_id"], row["min_buyout"], row["quantity"], row["avg_price"] or 0))

        conn.close()

        # Sort by price
        for k in new_cache:
            new_cache[k].sort(key=lambda x: x[1])

        # Load avg cache from JSON
        try:
            jp = _avg_cache_path()
            if os.path.exists(jp):
                with open(jp, encoding="utf-8") as f:
                    raw = json.load(f)
                for ks, v in raw.items():
                    parts = ks.split("_")
                    if len(parts) >= 2:
                        new_avg[(int(parts[0]), int(parts[1]) if parts[1] else 0)] = v.get("top10avg", 0)
        except:
            pass

        # Fallback: compute avg from cache
        if not new_avg:
            for k, prices in new_cache.items():
                top = prices[:10]
                new_avg[k] = sum(p[1] for p in top) // len(top) if top else 0

        # Atomic swap
        with _CACHE_LOCK:
            _cache.clear()
            _cache.update(new_cache)
            _avg.clear()
            _avg.update(new_avg)
            _last_collected = new_last

        _log(f"Cache: {len(_cache)} keys, {len(_avg)} avgs")
    except Exception as e:
        _log(f"Error loading auction data: {e}")

def _load_presets():
    """Load presets from presets.json"""
    global _presets, _active_preset, _snipe_ids
    _presets = []
    _active_preset = None
    _snipe_ids = set()
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, encoding="utf-8") as f:
                _presets = json.load(f)
            # Find active/default preset
            for p in _presets:
                if p.get("is_default") or not _active_preset:
                    _active_preset = p
            if _active_preset:
                # Parse item_ids
                ids_str = _active_preset.get("item_ids", "")
                if ids_str:
                    for pid in ids_str.split(","):
                        pid = pid.strip()
                        if pid.isdigit():
                            _snipe_ids.add(int(pid))
                # Parse boe_filters
                boe_json = _active_preset.get("boe_filters", "")
                if boe_json and isinstance(boe_json, str):
                    try:
                        boe_filters = json.loads(boe_json)
                        for bf in boe_filters:
                            ids_str = bf.get("ids", "")
                            for pid in ids_str.split(","):
                                pid = pid.strip()
                                if pid.isdigit():
                                    _snipe_ids.add(int(pid))
                    except:
                        pass
                _log(f"Active preset: {_active_preset.get('name', '?')} ({len(_snipe_ids)} items)")
        else:
            _log("No presets.json found")
    except Exception as e:
        _log(f"Error loading presets: {e}")

def _save_presets():
    """Save presets back to presets.json"""
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump(_presets, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"Error saving presets: {e}")

def _toggle_snipe(item_id):
    """Add or remove item_id from active preset's item_ids list"""
    global _snipe_ids, _active_preset
    if not _active_preset:
        return False, "No active preset"

    # Get current item_ids string
    ids_str = _active_preset.get("item_ids", "")
    ids_list = [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]

    if item_id in _snipe_ids:
        # Remove
        ids_list = [x for x in ids_list if x != item_id]
        _snipe_ids.discard(item_id)
        action = "removed"
    else:
        # Add
        if item_id not in ids_list:
            ids_list.append(item_id)
        _snipe_ids.add(item_id)
        action = "added"

    _active_preset["item_ids"] = ",".join(str(x) for x in ids_list)
    _save_presets()
    return True, action

# ═══════════════ Category Helpers ═══════════════

# Weapon groups matching undermine.exchange structure
WEAPON_GROUPS = [
    {
        "name": "One-Handed",
        "subclass_ids": [0, 4, 7, 15, 13, 9],
        "children": [
            {"name": "One-Handed Swords", "subclass_ids": [7]},
            {"name": "One-Handed Axes", "subclass_ids": [0]},
            {"name": "One-Handed Maces", "subclass_ids": [4]},
            {"name": "Daggers", "subclass_ids": [15]},
            {"name": "Fist Weapons", "subclass_ids": [13]},
        ]
    },
    {
        "name": "Two-Handed",
        "subclass_ids": [1, 5, 8, 6, 10],
        "children": [
            {"name": "Two-Handed Swords", "subclass_ids": [8]},
            {"name": "Two-Handed Axes", "subclass_ids": [1]},
            {"name": "Two-Handed Maces", "subclass_ids": [5]},
            {"name": "Polearms", "subclass_ids": [6]},
            {"name": "Staves", "subclass_ids": [10]},
        ]
    },
    {
        "name": "Ranged",
        "subclass_ids": [2, 18, 3, 16, 19],
        "children": [
            {"name": "Bows", "subclass_ids": [2]},
            {"name": "Crossbows", "subclass_ids": [18]},
            {"name": "Guns", "subclass_ids": [3]},
            {"name": "Thrown", "subclass_ids": [16]},
            {"name": "Wands", "subclass_ids": [19]},
        ]
    },
    {
        "name": "Miscellaneous",
        "subclass_ids": [20],
        "children": [
            {"name": "Fishing Poles", "subclass_ids": [20]},
        ]
    },
]

ARMOR_GROUPS = [
    {"name": "Cloth", "subclass_ids": [1]},
    {"name": "Leather", "subclass_ids": [2]},
    {"name": "Mail", "subclass_ids": [3]},
    {"name": "Plate", "subclass_ids": [4]},
    {"name": "Cloaks", "subclass_ids": [0]},
    {"name": "Shields", "subclass_ids": [6]},
    {"name": "Cosmetic", "subclass_ids": [5]},
]

# ═══════════════ Category configuration ═══════════════

# Category order matching Undermine Exchange exactly
CAT_ORDER = [
    "Weapons", "Armor", "Containers", "Gems", "Item Enhancements",
    "Consumables", "Glyphs", "Reagents", "Recipes",
    "Profession Equipment", "Housing", "Battle Pets",
    "Quest Items", "Miscellaneous"
]

# Map English category names to class_ids (merged where needed)
CAT_CLASSES = {
    "Weapons": [2],
    "Armor": [4],
    "Containers": [1],
    "Gems": [3],
    "Item Enhancements": [8],
    "Consumables": [0],
    "Glyphs": [16],
    "Reagents": [5, 7],    # merged: Реагент + Ремесло
    "Recipes": [9],
    "Profession Equipment": [19],
    "Housing": [20],
    "Battle Pets": [17],
    "Quest Items": [12],
    "Miscellaneous": [15],
}

def _build_categories():
    """Build category tree matching Undermine Exchange exactly"""
    items_with_data = set()
    for (item_id, ilvl) in _cache:
        items_with_data.add(item_id)
    for (item_id, ilvl) in _avg:
        items_with_data.add(item_id)

    # Count items per (class_id, subclass_id)
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
        if total_count == 0:
            continue

        if cat_name == "Weapons":
            weapon_tree = []
            for group in WEAPON_GROUPS:
                g_count = 0
                children = []
                for child in group["children"]:
                    c_count = sum(subclass_counts[cid].get(scid, 0) for cid in cids for scid in child["subclass_ids"])
                    if c_count > 0:
                        children.append({
                            "id": child["subclass_ids"][0],
                            "name": child["name"],
                            "count": c_count,
                            "filter_class_ids": cids,
                            "filter_subclass_ids": child["subclass_ids"],
                            "is_leaf": True
                        })
                    g_count += c_count
                if g_count > 0:
                    weapon_tree.append({
                        "id": f"g_{group['name']}",
                        "name": group["name"],
                        "count": g_count,
                        "subclasses": children,
                        "is_group": True,
                        "filter_class_ids": cids,
                        "filter_subclass_ids": group["subclass_ids"]
                    })
            tree.append({"id": 2, "name": "Weapons", "count": total_count, "subclasses": weapon_tree, "filter_class_ids": [2]})

        elif cat_name == "Armor":
            armor_tree = []
            for group in ARMOR_GROUPS:
                a_count = sum(subclass_counts[cid].get(scid, 0) for cid in cids for scid in group["subclass_ids"])
                if a_count > 0:
                    armor_tree.append({
                        "id": group["subclass_ids"][0],
                        "name": group["name"],
                        "count": a_count,
                        "filter_class_ids": cids,
                        "filter_subclass_ids": group["subclass_ids"],
                        "is_leaf": True
                    })
            tree.append({"id": 4, "name": "Armor", "count": total_count, "subclasses": armor_tree, "filter_class_ids": [4]})

        else:
            # Generic category: list all subclasses, merge across class_ids
            sub_map = {}
            for cid in cids:
                for scid, cnt in subclass_counts[cid].items():
                    meta = subclass_meta.get((cid, scid))
                    scname = (meta.get("subclass_name") or f"Subclass {scid}") if meta else f"Subclass {scid}"
                    if scname in sub_map:
                        sub_map[scname]["count"] += cnt
                    else:
                        sub_map[scname] = {
                            "id": scid, "name": scname, "count": cnt,
                            "filter_class_ids": cids,
                            "filter_subclass_ids": [scid],
                            "is_leaf": True
                        }
            sub_list = sorted(sub_map.values(), key=lambda x: -x["count"])
            tree.append({"id": cids[0], "name": cat_name, "count": total_count, "subclasses": sub_list, "filter_class_ids": cids})

    return tree

def _get_item_icon(item_id):
    """Get icon name for item from item_icons.json or return placeholder"""
    return None  # Will be handled by frontend via /icon/ endpoint

# ── Icon proxy (copied from webgem.py pattern) ──
_icon_cache = {}
_icon_token = None
_icon_token_expiry = 0

def _ensure_icon_token():
    global _icon_token, _icon_token_expiry
    now = time.time()
    if _icon_token and now < _icon_token_expiry - 300:
        return _icon_token
    cid = os.getenv("CLIENT_ID", "")
    csec = os.getenv("CLIENT_SECRET", "")
    if not cid or not csec:
        return None
    try:
        resp = requests.post("https://oauth.battle.net/token",
            auth=(cid, csec), data={"grant_type": "client_credentials"}, verify=False, timeout=10)
        resp.raise_for_status()
        d = resp.json()
        _icon_token = d["access_token"]
        _icon_token_expiry = now + d["expires_in"]
        return _icon_token
    except:
        return None

@app.route('/icon/<int:item_id>')
def get_item_icon(item_id):
    """Serve item icon - first check local icons/ dir, then cache, then Blizzard CDN"""
    # 1) Check local icons/ directory
    local_path = data(os.path.join("icons", f"{item_id}.jpg"))
    if os.path.exists(local_path):
        return send_file(local_path, mimetype='image/jpeg')

    # 2) Check memory cache
    cache_key = f"{item_id}"
    if cache_key in _icon_cache:
        return send_file(BytesIO(_icon_cache[cache_key]), mimetype='image/jpeg')

    # 3) Try Blizzard CDN directly via icon name map
    icon_name = _icon_name_map.get(item_id)
    if icon_name:
        cdn_url = f"https://render.worldofwarcraft.com/{_region}/icons/56/{icon_name}.jpg"
        try:
            img_resp = requests.get(cdn_url, timeout=10, verify=False)
            if img_resp.status_code == 200:
                _icon_cache[cache_key] = img_resp.content
                return send_file(BytesIO(img_resp.content), mimetype='image/jpeg')
        except:
            pass

    # 4) Try Blizzard API to discover icon name
    token = _ensure_icon_token()
    if token:
        try:
            region = _region
            ns = f"static-{region}"
            base = f"https://{region}.api.blizzard.com"
            url = f"{base}/data/wow/media/item/{item_id}?namespace={ns}&locale=ru_RU"
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            resp.raise_for_status()
            resp_data = resp.json()
            for asset in resp_data.get("assets", []):
                if asset.get("key") == "icon":
                    icon_value = asset.get("value", "")
                    if icon_value:
                        if icon_value.startswith("http"):
                            icon_name = icon_value.split('/')[-1]
                            if icon_name.endswith('.jpg'):
                                icon_name = icon_name[:-4]
                        else:
                            icon_name = icon_value
                        cdn_url = f"https://render.worldofwarcraft.com/{region}/icons/56/{icon_name}.jpg"
                        img_resp = requests.get(cdn_url, timeout=10, verify=False)
                        if img_resp.status_code == 200:
                            _icon_cache[cache_key] = img_resp.content
                            return send_file(BytesIO(img_resp.content), mimetype='image/jpeg')
        except:
            pass

    # 5) Fallback: question mark icon
    try:
        r = requests.get("https://render.worldofwarcraft.com/eu/icons/56/inv_misc_questionmark.jpg", timeout=5, verify=False)
        if r.status_code == 200:
            return send_file(BytesIO(r.content), mimetype='image/jpeg')
    except:
        pass

    return "", 404

# ═══════════════ API Endpoints ═══════════════

@app.route("/")
def _index():
    global _HTML
    if _HTML is None:
        _HTML = _load_html()
    if _HTML:
        return _HTML
    return send_from_directory(static_dir(), "index.html")

@app.route("/api/categories")
def api_categories():
    """Return category tree"""
    tree = _build_categories()
    return jsonify({"categories": tree, "region": _region})

@app.route("/api/items")
def api_items():
    """Return items with filtering - class_id alone does NOT filter, only subclass_ids does"""
    with _CACHE_LOCK:
        class_id = request.args.get("class_id", type=int)
        filter_scids_str = request.args.get("filter_subclass_ids", "")
        search = request.args.get("search", "").strip().lower()
        min_discount = request.args.get("min_discount", type=float)
        max_discount = request.args.get("max_discount", type=float)
        min_avg = request.args.get("min_avg", type=float)
        quality = request.args.get("quality", "").upper()
        ilvl_min = request.args.get("ilvl_min", type=int)
        ilvl_max = request.args.get("ilvl_max", type=int)
        snipe_only = request.args.get("snipe_only", "0") == "1"
        sort_by = request.args.get("sort", "discount_desc")
        realm_ids_str = request.args.get("realm_ids", "")
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 200, type=int)

        filter_scids = set()
        if filter_scids_str:
            filter_scids = {int(x.strip()) for x in filter_scids_str.split(",") if x.strip().isdigit()}

        filter_cids_str = request.args.get("filter_class_ids", "")
        filter_cids = set()
        if filter_cids_str:
            filter_cids = {int(x.strip()) for x in filter_cids_str.split(",") if x.strip().isdigit()}

        realm_ids = set()
        if realm_ids_str:
            realm_ids = {int(x.strip()) for x in realm_ids_str.split(",") if x.strip().isdigit()}

        # Use module-level icon map
        icon_map = _icon_name_map

        results = []
        seen_keys = set()

        for (item_id, ilvl), prices in _cache.items():
            if not prices:
                continue

            meta = _meta.get(item_id)

            # Category filter: filter by subclass_ids AND class_ids if provided
            if filter_scids and (not meta or meta.get("subclass_id") not in filter_scids):
                continue
            if filter_cids and (not meta or meta.get("class_id") not in filter_cids):
                continue

            # Quality filter
            if quality and (not meta or (meta.get("quality_type") or "").upper() != quality):
                continue

            # Expansion filter
            exp_filter = request.args.get("expansion", "").strip()
            if exp_filter:
                if not meta:
                    continue
                exp_id = meta.get("expansion_id")
                if exp_id is None:
                    continue
                if exp_filter == "tww":
                    if exp_id != 10:
                        continue
                elif exp_filter == "df":
                    if exp_id < 9 or exp_id > 10:
                        continue
                elif exp_filter == "sl":
                    if exp_id < 8 or exp_id > 10:
                        continue
                elif exp_filter == "current":
                    if exp_id < 10:
                        continue
                elif exp_filter == "midnight":
                    if exp_id < 11:
                        continue
                elif exp_filter == "midnight121":
                    if item_id < MIN_MIDNIGHT_121_ID and item_id not in _midnight_121_set():
                        continue

            # ilvl range filter
            if (ilvl_min is not None or ilvl_max is not None) and meta:
                item_ilvl = meta.get("item_level") or 0
                if ilvl_min is not None and item_ilvl < ilvl_min:
                    continue
                if ilvl_max is not None and item_ilvl > ilvl_max:
                    continue

            # Snipe-only filter
            if snipe_only and item_id not in _snipe_ids:
                continue

            # Cheapest price
            rid0, cp, qty, avgp = prices[0]
            ta = _avg.get((item_id, ilvl), 0)
            disc = round((1 - cp / ta) * 100, 1) if ta and cp else 0

            # Discount filters
            if min_discount is not None and disc < min_discount:
                continue
            if max_discount is not None and disc > max_discount:
                continue
            if min_avg is not None and ta < min_avg * 10000:
                continue

            # Realm filter
            if realm_ids:
                has_realm = any(r[0] in realm_ids for r in prices)
                if not has_realm:
                    continue

            # Item name for search
            nm = _inames.get(item_id, f"Item {item_id}")
            if ilvl:
                nm_display = f"{nm} [{ilvl}]"
            else:
                nm_display = nm

            if search:
                if search not in nm.lower() and search not in str(item_id):
                    continue

            # Skip duplicate (item_id, ilvl) - shouldn't happen but just in case
            key = (item_id, ilvl)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # Get realm name for cheapest realm
            realm_info = _realms.get(rid0, {})
            realm_name = realm_info.get("name_ru") or realm_info.get("name_en") or f"R{rid0}"

            # Quality display
            quality_name = None
            quality_type = None
            item_ilvl = None
            class_name = None
            subclass_name = None
            if meta:
                quality_type = meta.get("quality_type")
                quality_name = meta.get("quality_name")
                item_ilvl = meta.get("item_level")
                class_name = meta.get("class_name")
                subclass_name = meta.get("subclass_name")

            # Is snipe?
            in_snipe = item_id in _snipe_ids

            # Icon URL
            icon_name = icon_map.get(item_id)
            icon_url = None
            if icon_name:
                icon_url = f"https://render.worldofwarcraft.com/{_region}/icons/56/{icon_name}.jpg"

            results.append({
                "item_id": item_id,
                "ilvl": ilvl,
                "name": nm_display,
                "name_raw": nm,
                "price": cp,
                "price_g": _g(cp),
                "discount": disc,
                "quantity": qty,
                "cheapest_realm_id": rid0,
                "cheapest_realm": realm_name,
                "top10avg": ta,
                "top10avg_g": _g(ta),
                "quality_type": quality_type,
                "quality_name": quality_name,
                "item_level": item_ilvl,
                "class_name": class_name,
                "subclass_name": subclass_name,
                "in_snipe": in_snipe,
                "realms_count": len(prices),
                "icon_url": icon_url,
            })

        # Sort
        reverse = True
        if sort_by == "discount_desc":
            results.sort(key=lambda x: x["discount"], reverse=True)
        elif sort_by == "discount_asc":
            results.sort(key=lambda x: x["discount"])
        elif sort_by == "price_asc":
            results.sort(key=lambda x: x["price"])
        elif sort_by == "price_desc":
            results.sort(key=lambda x: x["price"], reverse=True)
        elif sort_by == "name":
            results.sort(key=lambda x: x["name_raw"])
        elif sort_by == "ilvl_desc":
            results.sort(key=lambda x: (x["item_level"] or 0), reverse=True)
        elif sort_by == "ilvl_asc":
            results.sort(key=lambda x: (x["item_level"] or 0))
        elif sort_by == "snipe":
            results.sort(key=lambda x: (not x["in_snipe"], x["discount"]), reverse=True)

        total = len(results)
        start = (page - 1) * per_page
        end = start + per_page
        page_items = results[start:end]

        return jsonify({
            "items": page_items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "region": _region,
            "last_collected": _last_collected
        })

@app.route("/api/item")
def api_item():
    """Return detailed info for a specific item"""
    item_id = request.args.get("id", type=int)
    ilvl = request.args.get("ilvl", 0, type=int)

    if not item_id:
        return jsonify({"error": "item_id required"}), 400

    with _CACHE_LOCK:
        key = (item_id, ilvl)
        prices = _cache.get(key, [])

        if not prices:
            return jsonify({"error": "Item not found"}), 404

        meta = _meta.get(item_id)
        nm = _inames.get(item_id, f"Item {item_id}")
        ta = _avg.get(key, 0)

        # Cheapest price info
        rid0, cp, qty0, avgp0 = prices[0]
        disc = round((1 - cp / ta) * 100, 1) if ta and cp else 0

        # Build per-realm price list
        realm_prices = []
        for rid, price, qty, avg_price in prices:
            realm_info = _realms.get(rid, {})
            realm_prices.append({
                "realm_id": rid,
                "realm_name": realm_info.get("name_ru") or realm_info.get("name_en") or f"R{rid}",
                "price": price,
                "price_g": _g(price),
                "quantity": qty,
                "avg_price": avg_price,
            })

        # Top3
        top3 = [f"{_realms.get(r[0],{}).get('name_ru') or _realms.get(r[0],{}).get('name_en') or f'R{r[0]}'} ({_g(r[1])})" for r in prices[:3]]

        # Sale realm price (highest price among sale realms)
        sale_realm_ids_str = ""
        if _active_preset:
            sale_realm_ids_str = _active_preset.get("sale_realm_ids", "")
        sale_realm_ids = set()
        if sale_realm_ids_str:
            sale_realm_ids = {int(x.strip()) for x in sale_realm_ids_str.split(",") if x.strip().isdigit()}

        sale_price_info = None
        if sale_realm_ids:
            sale_prices = [(r, p) for r, p, q, a in prices if r in sale_realm_ids]
            if sale_prices:
                sale_prices.sort(key=lambda x: x[1], reverse=True)
                sr_id, sr_price = sale_prices[0]
                sale_price_info = {
                    "realm_id": sr_id,
                    "realm_name": _realms.get(sr_id, {}).get("name_ru") or _realms.get(sr_id, {}).get("name_en") or f"R{sr_id}",
                    "price": sr_price,
                    "price_g": _g(sr_price),
                }

        result = {
            "item_id": item_id,
            "ilvl": ilvl,
            "name": nm if not ilvl else f"{nm} [{ilvl}]",
            "name_raw": nm,
            "meta": {
                "class_name": meta.get("class_name") if meta else None,
                "subclass_name": meta.get("subclass_name") if meta else None,
                "quality_type": meta.get("quality_type") if meta else None,
                "quality_name": meta.get("quality_name") if meta else None,
                "item_level": meta.get("item_level") if meta else None,
                "required_level": meta.get("required_level") if meta else None,
                "slot_name": meta.get("slot_name") if meta else None,
                "bind_type": meta.get("bind_type") if meta else None,
                "is_equippable": meta.get("is_equippable") if meta else None,
            } if meta else None,
            "cheapest_price": cp,
            "cheapest_price_g": _g(cp),
            "cheapest_realm": _realms.get(rid0, {}).get("name_ru") or _realms.get(rid0, {}).get("name_en") or f"R{rid0}",
            "cheapest_realm_id": rid0,
            "discount": disc,
            "top10avg": ta,
            "top10avg_g": _g(ta),
            "quantity": qty0,
            "realms_count": len(prices),
            "in_snipe": item_id in _snipe_ids,
            "sale_price": sale_price_info,
            "top3": top3,
            "realm_prices": realm_prices,
            "icon_url": _get_icon_url(item_id),
        }

        return jsonify(result)

@app.route("/api/snipe/list")
def api_snipe_list():
    """Return list of item IDs in snipe list"""
    # Also return full item info for each snipe item
    items = []
    for item_id in sorted(_snipe_ids):
        nm = _inames.get(item_id, f"Item {item_id}")
        meta = _meta.get(item_id)
        # Check if item has data
        with _CACHE_LOCK:
            has_data = any(k[0] == item_id for k in list(_cache.keys()))
        items.append({
            "item_id": item_id,
            "name": nm,
            "class_name": meta.get("class_name") if meta else None,
            "quality_type": meta.get("quality_type") if meta else None,
            "has_data": has_data,
        })

    return jsonify({
        "items": items,
        "count": len(items),
        "preset_name": _active_preset.get("name", "") if _active_preset else None
    })

# ═══════════════ Preset Management API ═══════════════

@app.route("/api/presets/list")
def api_presets_list():
    """Return all presets"""
    return jsonify({
        "presets": _presets,
        "active": _active_preset.get("name") if _active_preset else None,
        "snipe_count": len(_snipe_ids)
    })

@app.route("/api/presets/select", methods=["POST"])
def api_presets_select():
    """Select active preset"""
    data = request.get_json(force=True)
    preset_name = data.get("preset_name", "").strip()
    for p in _presets:
        if p.get("name") == preset_name:
            # Mark all others as non-default
            for pp in _presets:
                pp["is_default"] = (pp["name"] == preset_name)
            _save_presets()
            # Reload
            _load_presets()
            return jsonify({"success": True, "preset": _active_preset})
    return jsonify({"error": f"Preset '{preset_name}' not found"}), 404

@app.route("/api/presets/save", methods=["POST"])
def api_presets_save():
    """Create or update a preset"""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    old_name = data.get("old_name", name)
    # Find existing or create new
    existing = None
    for p in _presets:
        if p.get("name") == old_name:
            existing = p
            break

    if existing:
        existing["name"] = name
        existing["item_ids"] = data.get("item_ids", existing.get("item_ids", ""))
        existing["sale_realm_ids"] = data.get("sale_realm_ids", existing.get("sale_realm_ids", ""))
        existing["discount"] = data.get("discount", existing.get("discount", 0))
        existing["min_top10avg"] = data.get("min_top10avg", existing.get("min_top10avg", 0))
        existing["boe_filters"] = data.get("boe_filters", existing.get("boe_filters", ""))
        existing["is_default"] = data.get("is_default", existing.get("is_default", False))
    else:
        new_preset = {
            "id": max([p.get("id", 0) for p in _presets], default=0) + 1,
            "name": name,
            "item_ids": data.get("item_ids", ""),
            "sale_realm_ids": data.get("sale_realm_ids", ""),
            "discount": data.get("discount", 0),
            "min_top10avg": data.get("min_top10avg", 0),
            "boe_filters": data.get("boe_filters", ""),
            "editable": True,
            "is_default": data.get("is_default", False),
        }
        _presets.append(new_preset)

    _save_presets()
    _load_presets()
    return jsonify({"success": True, "presets": _presets, "active": _active_preset})

@app.route("/api/presets/delete", methods=["POST"])
def api_presets_delete():
    """Delete a preset"""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    global _presets, _active_preset, _snipe_ids
    _presets = [p for p in _presets if p.get("name") != name]
    if _active_preset and _active_preset.get("name") == name:
        _active_preset = _presets[0] if _presets else None
        if _active_preset:
            _active_preset["is_default"] = True
    _save_presets()
    _load_presets()
    return jsonify({"success": True, "presets": _presets, "active": _active_preset})

@app.route("/api/snipe/toggle", methods=["POST"])
def api_snipe_toggle():
    """Toggle item in snipe list"""
    data = request.get_json(force=True)
    item_id = data.get("item_id")
    if not item_id:
        return jsonify({"error": "item_id required"}), 400
    item_id = int(item_id)

    success, action = _toggle_snipe(item_id)
    return jsonify({
        "success": success,
        "action": action,
        "item_id": item_id,
        "in_snipe": item_id in _snipe_ids
    })

@app.route("/api/snipe/add_bulk", methods=["POST"])
def api_snipe_add_bulk():
    """Add multiple items to snipe list at once"""
    data = request.get_json(force=True)
    item_ids = data.get("item_ids", [])
    if not item_ids:
        return jsonify({"error": "item_ids required"}), 400

    added = 0
    for item_id in item_ids:
        if item_id not in _snipe_ids:
            success, action = _toggle_snipe(item_id)
            if success:
                added += 1

    return jsonify({
        "success": True,
        "added": added
    })

@app.route("/api/realms")
def api_realms():
    """Return all realms"""
    realm_list = []
    for rid, info in sorted(_realms.items()):
        realm_list.append({
            "id": rid,
            "name_en": info["name_en"],
            "name_ru": info["name_ru"],
            "slug": info["slug"]
        })
    return jsonify({"realms": realm_list, "count": len(realm_list), "region": _region})

@app.route("/api/presets")
def api_presets():
    """Return all presets"""
    return jsonify({
        "presets": _presets,
        "active": _active_preset,
        "snipe_count": len(_snipe_ids)
    })

@app.route("/api/status")
def api_status():
    """Return current status"""
    with _CACHE_LOCK:
        return jsonify({
            "region": _region,
            "last_collected": _last_collected,
            "items_cached": len(_cache),
            "item_meta_count": len(_meta),
            "realms_count": len(_realms),
            "snipe_count": len(_snipe_ids),
            "preset_name": _active_preset.get("name", "") if _active_preset else None
        })

@app.route("/api/quality_types")
def api_quality_types():
    """Return available quality types from meta"""
    qualities = set()
    for meta in _meta.values():
        if meta.get("quality_type"):
            qualities.add(meta["quality_type"])
    return jsonify({
        "qualities": sorted(qualities, key=lambda q: ["POOR", "COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "ARTIFACT", "HEIRLOOM"].index(q) if q in ["POOR", "COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "ARTIFACT", "HEIRLOOM"] else 99)
    })

# ═══════════════ HTML Frontend ═══════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AH Sniper Browser</title>
<style>
:root{--bg:#0f1117;--bg2:#161922;--bg3:#1a1d25;--bg4:#242833;--text:#d0d0d0;--text2:#9098a5;--text3:#666;--gold:#f5c842;--green:#4ade80;--red:#f87171;--blue:#60a5fa;--border:rgba(255,255,255,0.06);--hover:rgba(255,255,255,0.04);--sidebar-w:260px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}
input,select,button{font-family:inherit}
/* ── Top bar ── */
#topbar{display:flex;align-items:center;gap:10px;padding:6px 14px;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;z-index:10}
#topbar .logo{font-size:16px;font-weight:700;color:var(--gold);white-space:nowrap;cursor:pointer}
#topbar .logo span{font-size:11px;font-weight:400;color:var(--text2);margin-left:6px}
#topbar input[type=text]{padding:5px 10px;border-radius:5px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;outline:none;transition:border .2s}
#topbar input[type=text]:focus{border-color:var(--gold)}
#topbar #searchInput{width:200px}
#topbar .region-btn{padding:3px 10px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text2);cursor:pointer;font-size:11px;font-weight:600}
#topbar .region-btn.active{background:var(--gold);color:#0d0d0d;border-color:var(--gold)}
#status{font-size:11px;color:var(--text2);margin-left:auto;white-space:nowrap}
#status .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px}
#status .dot.green{background:var(--green)}#status .dot.yellow{background:var(--gold)}#status .dot.red{background:var(--red)}
/* ── Main layout ── */
#main{display:flex;flex:1;overflow:hidden}
/* ── Sidebar ── */
#sidebar{width:var(--sidebar-w);background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;overflow:hidden}
#sidebar-header{padding:8px 12px;border-bottom:1px solid var(--border);flex-shrink:0}
#sidebar-header h3{font-size:11px;color:var(--gold);text-transform:uppercase;letter-spacing:.5px}
#sidebar-header .count{font-size:10px;color:var(--text2)}
#catSearch{width:100%;padding:4px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px;outline:none;margin-top:4px}
#catSearch:focus{border-color:var(--gold)}
#categoryTree{flex:1;overflow-y:auto;padding:4px 0}
#categoryTree::-webkit-scrollbar{width:4px}
#categoryTree::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:2px}
.cat-item{padding:3px 12px 3px 16px;cursor:pointer;font-size:12px;display:flex;align-items:center;gap:6px;transition:background .15s;user-select:none}
.cat-item:hover{background:var(--hover)}
.cat-item.active{background:rgba(245,200,66,0.1);color:var(--gold)}
.cat-item .arrow{font-size:8px;color:var(--text3);transition:transform .2s;width:10px;text-align:center;flex-shrink:0}
.cat-item .arrow.open{transform:rotate(90deg)}
.cat-item .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cat-item .count-badge{font-size:10px;color:var(--text3);background:var(--bg3);padding:0 5px;border-radius:3px;flex-shrink:0}
.cat-sub{padding-left:20px;display:none}
.cat-sub.open{display:block}
.cat-sub .cat-item{padding-left:8px;font-size:11px}
/* ── Content area ── */
#content{flex:1;display:flex;flex-direction:column;overflow:hidden}
/* ── Tabs ── */
#tabs{display:flex;gap:0;background:var(--bg2);border-bottom:1px solid var(--border);padding:0 14px;flex-shrink:0}
.tab-btn{padding:7px 16px;border:none;background:none;color:var(--text2);cursor:pointer;font-size:12px;border-bottom:2px solid transparent;transition:all .15s}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--gold);border-bottom-color:var(--gold)}
/* ── Filter bar ── */
#filterBar{display:flex;align-items:center;gap:8px;padding:5px 14px;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;font-size:11px}
#filterBar label{color:var(--text2);font-size:10px;white-space:nowrap}
#filterBar input,#filterBar select{padding:3px 6px;border-radius:3px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px;outline:none;max-width:80px}
#filterBar input:focus{border-color:var(--gold)}
#filterBar .filter-group{display:flex;align-items:center;gap:4px}
/* ── Table ── */
#tableWrap{flex:1;overflow:auto;position:relative}
#tableWrap::-webkit-scrollbar{width:6px;height:6px}
#tableWrap::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
#itemTable{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}
#itemTable th{position:sticky;top:0;background:var(--bg2);padding:5px 8px;text-align:left;font-weight:600;font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.3px;border-bottom:1px solid var(--border);cursor:pointer;user-select:none;white-space:nowrap;z-index:2}

#itemTable th:hover{color:var(--text)}
#itemTable th .sort-arrow{margin-left:3px;font-size:9px;color:var(--gold)}
/* Col resize — grab area on right edge of each th */
#itemTable td{padding:3px 8px;border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#itemTable tr{transition:background .1s;cursor:pointer}
#itemTable tr:hover{background:var(--hover)}
#itemTable tr.selected{background:rgba(96,165,250,0.08)}
#itemTable tr.detail-open td{background:rgba(245,200,66,0.05)}
#itemTable .snipe-star{cursor:pointer;font-size:14px;transition:transform .15s;display:inline-block;width:18px;text-align:center}
#itemTable .snipe-star:hover{transform:scale(1.3)}
#itemTable .snipe-star.on{color:var(--gold)}
#itemTable .snipe-star.off{color:var(--text3)}
#itemTable .item-cell{display:flex;align-items:center;gap:6px;overflow:hidden}
#itemTable .item-cell .icon{width:22px;height:22px;border-radius:3px;flex-shrink:0;background:var(--bg3);display:flex;align-items:center;justify-content:center;overflow:hidden;padding:0}
#itemTable .item-cell .icon img{width:22px;height:22px;object-fit:contain;display:block}
#itemTable .item-cell .name{overflow:hidden;text-overflow:ellipsis;font-weight:500}
#itemTable .price{font-variant-numeric:tabular-nums;font-weight:600}
#itemTable .discount{font-weight:700}
#itemTable .discount.disc-high{color:var(--green)}
#itemTable .discount.disc-mid{color:var(--gold)}
#itemTable .discount.disc-low{color:var(--text2)}
#itemTable .quality-EPIC{color:#c455e0}
#itemTable .quality-RARE{color:#0070dd}
#itemTable .quality-UNCOMMON{color:#1eff00}
#itemTable .quality-COMMON{color:#fff}
#itemTable .quality-POOR{color:#9d9d9d}
#itemTable .realm{font-size:11px;color:var(--text2)}
#itemTable .col-id{width:30px;text-align:center}
#itemTable .col-snipe{width:30px;text-align:center}
#itemTable .col-icon{width:38px}
#itemTable .col-name{min-width:160px}
#itemTable .col-price{width:90px}
#itemTable .col-discount{width:70px}
#itemTable .col-realm{width:100px}
#itemTable .col-qty{width:50px;text-align:center}
#itemTable .col-avg{width:90px}
#itemTable .col-ilvl{width:45px;text-align:center}
#itemTable .col-quality{width:70px}
/* ── Loading skeleton ── */
.skeleton td{height:28px;background:linear-gradient(90deg,var(--bg3) 25%,var(--bg4) 50%,var(--bg3) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
/* ── Detail view (full-width, replaces table) ── */
#detailView{display:none;flex-direction:column;flex:1;overflow:hidden;background:var(--bg1)}
#detailView.open{display:flex}
#dvBack{padding:8px 16px;background:var(--bg2);border-bottom:1px solid var(--border);font-size:12px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;gap:8px;color:var(--text2);font-weight:500}
#dvBack:hover{color:var(--text);background:var(--hover)}
#dvBack::before{content:'←';font-size:14px}
#dvHeader{display:flex;align-items:center;gap:14px;padding:16px 20px;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0}
#dvHeader .dicon{width:56px;height:56px;border-radius:8px;background:var(--bg3);flex-shrink:0;display:flex;align-items:center;justify-content:center;overflow:hidden;border:2px solid var(--border)}
#dvHeader .dicon img{max-width:52px;max-height:52px;object-fit:contain}
#dvHeader .dinfo{flex:1;min-width:0}
#dvHeader .dname{font-size:16px;font-weight:700;line-height:1.3}
#dvHeader .dmeta{font-size:11px;color:var(--text2);margin-top:3px}
#dvHeader .dprice{font-size:20px;font-weight:700;color:var(--gold);white-space:nowrap;font-variant-numeric:tabular-nums}
/* Quality colors for item name */
.q-EPIC{color:#c455e0}
.q-RARE{color:#0070dd}
.q-UNCOMMON{color:#1eff00}
.q-COMMON{color:var(--text)}
.q-POOR{color:#9d9d9d}
/* Stats grid */
#dvStats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:1px;background:var(--border);border-bottom:1px solid var(--border);flex-shrink:0}
#dvStats .stat{background:var(--bg2);padding:10px 14px;text-align:center}
#dvStats .stat .val{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums}
#dvStats .stat .lbl{font-size:9px;color:var(--text2);text-transform:uppercase;letter-spacing:.4px;margin-top:2px}
#dvStats .stat .sale{color:var(--green)}
/* Price table */
#dvPrices{flex:1;overflow:auto;padding:12px 20px}
#dvPrices table{width:100%;border-collapse:collapse;font-size:12px}
#dvPrices th{padding:5px 10px;text-align:left;font-size:10px;color:var(--text2);text-transform:uppercase;letter-spacing:.3px;border-bottom:2px solid var(--border);background:var(--bg1);position:sticky;top:0;z-index:1;font-weight:600}
#dvPrices td{padding:4px 10px;border-bottom:1px solid var(--border)}
#dvPrices tr:nth-child(even){background:var(--bg2)}
#dvPrices tr:hover{background:var(--hover)}
#dvPrices .p-price{font-weight:600;font-variant-numeric:tabular-nums;color:var(--gold)}
#dvPrices .p-qty{text-align:center;color:var(--text2)}
.detail-header{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg2);z-index:3}
.detail-header .dicon{width:48px;height:48px;border-radius:6px;background:var(--bg3);flex-shrink:0;display:flex;align-items:center;justify-content:center;overflow:hidden}
.detail-header .dicon img{max-width:48px;max-height:48px;object-fit:contain;image-rendering:auto}
.detail-header .dname{font-size:13px;font-weight:700;flex:1;line-height:1.3}
.detail-header .dprice{font-size:15px;font-weight:700;color:var(--gold);white-space:nowrap}
.detail-header .dclose{cursor:pointer;color:var(--text3);font-size:16px;padding:2px 6px;border-radius:4px;flex-shrink:0}
.detail-header .dclose:hover{background:var(--bg4);color:var(--text)}
.detail-section{padding:8px 12px;border-bottom:1px solid var(--border)}
.detail-body{display:flex;gap:16px;padding:10px 14px}
.detail-meta{flex:0 0 220px;font-size:11px;line-height:1.8}
.detail-meta .meta-row{display:flex;justify-content:space-between;color:var(--text2)}
.detail-meta .meta-row .label{color:var(--text3)}
.detail-meta .meta-row .value{font-weight:500}
.detail-prices{flex:1;overflow-x:auto}
.detail-prices table{width:100%;border-collapse:collapse;font-size:11px}
.detail-prices th{padding:4px 8px;text-align:left;font-size:10px;color:var(--text2);text-transform:uppercase;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg2)}
.detail-prices td{padding:3px 8px;border-bottom:1px solid var(--border)}
.detail-prices tr:hover{background:var(--hover)}
.detail-summary{display:flex;gap:20px;padding:8px 14px;border-top:1px solid var(--border);font-size:12px;flex-shrink:0}
.detail-summary .stat{text-align:center}
.detail-summary .stat .val{font-weight:700;font-size:14px}
.detail-summary .stat .lbl{font-size:10px;color:var(--text2)}
/* ── Empty state ── */
#emptyState{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--text3);gap:10px}
#emptyState .emoji{font-size:40px}
#emptyState .msg{font-size:14px;color:var(--text2)}
/* ── Info bar ── */
#infoBar{display:flex;align-items:center;gap:12px;padding:3px 14px;background:var(--bg2);border-top:1px solid var(--border);flex-shrink:0;font-size:10px;color:var(--text3)}
#infoBar .stat{display:flex;align-items:center;gap:4px}
#infoBar .stat b{color:var(--text2);font-weight:600}
/* ── Responsive ── */
@media(max-width:900px){
  #sidebar{width:200px}
  #filterBar input{max-width:60px}
  .filter-group label{display:none}
}
/* ── Modal ── */
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:100;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal{background:var(--bg2);border:1px solid var(--border);border-radius:8px;min-width:500px;max-width:700px;max-height:80vh;overflow:auto;padding:16px}
.modal h3{color:var(--gold);margin-bottom:12px;font-size:14px}
.modal label{display:block;font-size:11px;color:var(--text2);margin-top:8px;margin-bottom:2px}
.modal input,.modal textarea,.modal select{width:100%;padding:5px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:12px;outline:none;font-family:inherit}
.modal input:focus,.modal textarea:focus{border-color:var(--gold)}
.modal textarea{resize:vertical;min-height:40px}
.modal .btn-row{display:flex;gap:8px;margin-top:12px}
.modal .btn-row button{padding:6px 16px;border-radius:4px;border:none;cursor:pointer;font-size:12px;font-weight:600}
.modal .btn-primary{background:var(--gold);color:#0d0d0d}
.modal .btn-danger{background:var(--red);color:#fff}
.modal .btn-cancel{background:var(--bg4);color:var(--text2)}
</style>
</head>
<body>

<!-- Top Bar -->
<div id="topbar">
  <div class="logo">🏆 Sniper Browser <span>v1.0</span></div>
  <input id="searchInput" type="text" placeholder="🔍 Search items..." oninput="debounceSearch()">
  <button class="region-btn active" id="btnEu" onclick="setRegion('eu')">🇪🇺 EU</button>
  <button class="region-btn" id="btnUs" onclick="setRegion('us')">🇺🇸 US</button>
  <select id="presetSelect" onchange="selectPreset(this.value)" style="padding:3px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text);font-size:11px;max-width:160px;cursor:pointer" title="Active preset">
    <option value="">— Preset —</option>
  </select>
  <button onclick="openPresetEditor()" style="padding:3px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg3);color:var(--text2);cursor:pointer;font-size:12px" title="Manage presets">⚙</button>
  <div id="status"><span class="dot green"></span><span id="statusText">Loading...</span></div>
</div>

<!-- Main Layout -->
<div id="main">

  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sidebar-header">
      <h3>Categories</h3>
      <div class="count" id="catCount"></div>
      <input id="catSearch" type="text" placeholder="Filter categories..." oninput="filterCategories()">
    </div>
    <div id="categoryTree"></div>
  </div>

  <!-- Content -->
  <div id="content">

    <!-- Tabs -->
    <div id="tabs">
      <button class="tab-btn active" data-tab="all" onclick="switchTab('all')">📦 All Items</button>
      <button class="tab-btn" data-tab="snipe" onclick="switchTab('snipe')">🎯 Snipe List <span id="snipeBadge" style="font-size:10px;background:var(--gold);color:#0d0d0d;padding:0 5px;border-radius:3px;margin-left:4px"></span></button>
    </div>

    <!-- Filter Bar -->
    <div id="filterBar">
      <div class="filter-group">
        <label>Quality</label>
        <select id="fQuality" onchange="applyFilters()">
          <option value="">All</option>
          <option value="EPIC">Epic</option>
          <option value="RARE">Rare</option>
          <option value="UNCOMMON">Uncommon</option>
          <option value="COMMON">Common</option>
          <option value="POOR">Poor</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Min Ilvl</label>
        <input type="number" id="fIlvlMin" placeholder="0" onchange="applyFilters()">
      </div>
      <div class="filter-group">
        <label>Max Ilvl</label>
        <input type="number" id="fIlvlMax" placeholder="999" onchange="applyFilters()">
      </div>
      <div class="filter-group">
        <label>Min Discount %</label>
        <input type="number" id="fMinDisc" placeholder="0" onchange="applyFilters()">
      </div>
      <div class="filter-group">
        <label>Max Discount %</label>
        <input type="number" id="fMaxDisc" placeholder="100" onchange="applyFilters()">
      </div>
      <div class="filter-group">
        <label>Min Avg (g)</label>
        <input type="number" id="fMinAvg" placeholder="0" onchange="applyFilters()">
      </div>
      <div class="filter-group">
        <label>Exp</label>
        <select id="fExpansion" onchange="applyFilters()" style="max-width:110px">
          <option value="">All</option>
          <option value="midnight">Midnight</option>
          <option value="midnight121">Midnight 12.1 ✨</option>
          <option value="tww">The War Within</option>
          <option value="df">Dragonflight+</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Sort</label>
        <select id="fSort" onchange="applyFilters()">
          <option value="discount_desc">Discount ▼</option>
          <option value="discount_asc">Discount ▲</option>
          <option value="price_asc">Price ▲</option>
          <option value="price_desc">Price ▼</option>
          <option value="ilvl_desc">Ilvl ▼</option>
          <option value="ilvl_asc">Ilvl ▲</option>
          <option value="name">Name</option>
          <option value="snipe">Snipe First</option>
        </select>
      </div>
    </div>

    <!-- Table -->
    <div id="tableWrap">
      <table id="itemTable">
        <thead>
          <tr>
            <th class="col-snipe" onclick="switchTab(STATE.activeTab==='snipe'?'all':'snipe')" title="Toggle snipe list" style="cursor:pointer">★</th>
            <th class="col-icon"></th>
            <th class="col-name" onclick="sortBy('name')">Name</th>
            <th class="col-price" onclick="sortBy('price_asc')">Price</th>
            <th class="col-discount" onclick="sortBy('discount_desc')">-%</th>
            <th class="col-realm">Realm</th>
            <th class="col-qty">Qty</th>
            <th class="col-avg" onclick="sortBy('price_asc')">Avg</th>
            <th class="col-ilvl" onclick="sortBy('ilvl_desc')">Ilvl</th>
            <th class="col-quality">Quality</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
      <div id="emptyState" style="display:none">
        <div class="emoji">🔍</div>
        <div class="msg">No items found</div>
      </div>
    </div>

    <!-- Info Bar -->
    <div id="infoBar">
      <div class="stat">📦 Items: <b id="iCount">—</b></div>
      <div class="stat">🎯 Snipe: <b id="iSnipe">—</b></div>
      <div class="stat" id="iUpdated">🕐 Updated: —</div>
    </div>

    <!-- Detail View (full-width, replaces table) -->
    <div id="detailView">
      <div id="dvBack" onclick="closeDetail()">← Back to list</div>
      <div id="dvHeader">
        <div class="dicon"><img id="dvIcon" src="" alt=""></div>
        <div class="dinfo">
          <div class="dname" id="dvName"></div>
          <div class="dmeta" id="dvMeta"></div>
        </div>
        <div class="dprice" id="dvPrice"></div>
      </div>
      <div id="dvStats"></div>
      <div id="dvPrices">
        <table>
          <thead><tr><th>Realm</th><th>Price</th><th>Qty</th></tr></thead>
          <tbody id="dvPriceBody"></tbody>
        </table>
      </div>
    </div>

  </div><!-- end #content -->

</div><!-- end #main -->

<script>
// ═══════════════ State ═══════════════
var STATE = {
  region: 'eu',
  items: [],
  categories: [],
  activeCategory: null,    // currently expanded parent category id
  activeSubclass: null,    // currently filtering leaf node id
  filterSubclassIds: null, // set of subclass_ids to filter by (from leaf node)
  activeTab: 'all',
  sortBy: 'discount_desc',
  selectedItem: null,
  detailOpen: false,
  filter: {
    search: '',
    quality: '',
    ilvlMin: null,
    ilvlMax: null,
    minDisc: null,
    maxDisc: null,
    minAvg: null,
    expansion: '',
  },
  snipeIds: new Set(),
  presets: [],
  activePreset: null,
  timer: null,
};

// ═══════════════ Init ═══════════════
document.addEventListener('DOMContentLoaded', function() {
  loadCategories();
  loadItems();
  loadSnipeList();
  loadPresets();
  loadStatus();
  // Auto-refresh
  STATE.timer = setInterval(function() {
    loadItems(true);
    loadSnipeList();
    loadStatus();
  }, 15000);
  // Init column resize
  setTimeout(initColResize, 500);
});

function initColResize() {
  var ths = document.querySelectorAll('#itemTable th');
  ths.forEach(function(th) {
    // Use ::after pseudo-element for resize handle via CSS
    // Detect mousedown near right edge
    var startX, startW;
    th.addEventListener('mousedown', function(e) {
      var rect = th.getBoundingClientRect();
      var nearRight = rect.right - e.clientX < 8;
      if (!nearRight) return;
      e.preventDefault();
      startX = e.clientX;
      startW = rect.width;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      function onMove(e2) {
        var diff = e2.clientX - startX;
        var w = Math.max(25, startW + diff);
        th.style.width = w + 'px';
      }
      function onUp() {
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
}

// ═══════════════ Helpers ═══════════════
function g(copper) {
  if (!copper && copper !== 0) return '—';
  var g = Math.floor(copper / 10000);
  var s = Math.floor((copper % 10000) / 100);
  return g + '.' + (s < 10 ? '0' : '') + s;
}

function qualityClass(q) {
  return 'quality-' + (q || 'COMMON');
}

function discountClass(d) {
  if (d >= 70) return 'disc-high';
  if (d >= 40) return 'disc-mid';
  return 'disc-low';
}

function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function debounce(func, wait) {
  var timeout;
  return function() {
    var context = this, args = arguments;
    clearTimeout(timeout);
    timeout = setTimeout(function(){ func.apply(context, args); }, wait);
  };
}

function showStatus(msg, type) {
  var dot = document.querySelector('#status .dot');
  var text = document.getElementById('statusText');
  text.textContent = msg;
  dot.className = 'dot ' + (type || 'green');
}

// ═══════════════ API ═══════════════
async function api(url) {
  try {
    var resp = await fetch(url);
    return await resp.json();
  } catch(e) {
    console.error('API error:', url, e);
    showStatus('Connection error', 'red');
    return null;
  }
}

async function apiPost(url, data) {
  try {
    var resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    return await resp.json();
  } catch(e) {
    console.error('API error:', url, e);
    return null;
  }
}

// ═══════════════ Categories ═══════════════
async function loadCategories() {
  var data = await api('/api/categories?region=' + STATE.region);
  if (!data) return;
  STATE.categories = data.categories || [];
  renderCategories();
  document.getElementById('catCount').textContent = STATE.categories.reduce(function(s, c) { return s + c.count; }, 0) + ' items';
}

function renderCategories() {
  var tree = document.getElementById('categoryTree');
  if (STATE.categories.length === 0) {
    tree.innerHTML = '<div style="padding:10px;color:var(--text3);font-size:11px">No categories</div>';
    return;
  }
  var html = '';
  STATE.categories.forEach(function(cat) {
    var expanded = STATE.activeCategory == cat.id;
    var catCids = JSON.stringify(cat.filter_class_ids || [cat.id]);
    html += '<div class="cat-item' + (expanded ? ' active' : '') + '" onclick="toggleCategory(' + cat.id + ',' + catCids + ')">';
    html += '<span class="arrow' + (expanded ? ' open' : '') + '">▶</span>';
    html += '<span class="name">' + escapeHtml(cat.name) + '</span>';
    html += '<span class="count-badge">' + cat.count + '</span>';
    html += '</div>';
    html += '<div class="cat-sub' + (expanded ? ' open' : '') + '" id="sub-' + cat.id + '">';
    if (cat.subclasses) {
      cat.subclasses.forEach(function(sub) {
        var subActive = STATE.activeSubclass == sub.id;
        if (sub.is_leaf) {
          var scids = JSON.stringify(sub.filter_subclass_ids || [sub.id]);
          var ccids = JSON.stringify(sub.filter_class_ids || null);
          html += '<div class="cat-item leaf' + (subActive ? ' active' : '') + '" onclick="selectLeaf(' + cat.id + ',\'' + sub.id + '\',' + scids + ',' + ccids + ')">';
          html += '<span class="arrow" style="visibility:hidden">▶</span>';
          html += '<span class="name">' + escapeHtml(sub.name) + '</span>';
          html += '<span class="count-badge">' + sub.count + '</span>';
          html += '</div>';
        } else if (sub.subclasses) {
          var subScids = JSON.stringify(sub.filter_subclass_ids || null);
          var subCids = JSON.stringify(sub.filter_class_ids || null);
          html += '<div class="cat-item' + (subActive ? ' active' : '') + '" onclick="toggleSubgroup(' + cat.id + ',' + subScids + ',' + subCids + ')">';
          html += '<span class="arrow' + (subActive ? ' open' : '') + '">▶</span>';
          html += '<span class="name">' + escapeHtml(sub.name) + '</span>';
          html += '<span class="count-badge">' + sub.count + '</span>';
          html += '</div>';
          html += '<div class="cat-sub' + (subActive ? ' open' : '') + '" id="subchild-' + sub.id + '">';
          sub.subclasses.forEach(function(child) {
            var childActive = STATE.activeSubclass == child.id;
            var childScids = JSON.stringify(child.filter_subclass_ids || [child.id]);
            var childCids = JSON.stringify(child.filter_class_ids || null);
            html += '<div class="cat-item leaf' + (childActive ? ' active' : '') + '" onclick="selectLeaf(' + cat.id + ',\'' + child.id + '\',' + childScids + ',' + childCids + ')">';
            html += '<span class="arrow" style="visibility:hidden">▶</span>';
            html += '<span class="name">' + escapeHtml(child.name) + '</span>';
            html += '<span class="count-badge">' + child.count + '</span>';
            html += '</div>';
          });
          html += '</div>';
        }
      });
    }
    html += '</div>';
  });
  tree.innerHTML = html;
  filterCategories();
}

function toggleCategory(cid, filterCids) {
  if (STATE.activeCategory == cid && !STATE.filterSubclassIds) {
    STATE.activeCategory = null;
    STATE.activeSubclass = null;
    STATE.filterSubclassIds = null;
    STATE.filterClassIds = null;
  } else if (STATE.filterSubclassIds) {
    STATE.activeCategory = cid;
    STATE.activeSubclass = null;
    STATE.filterSubclassIds = null;
    STATE.filterClassIds = filterCids || [cid];
  } else {
    STATE.activeCategory = cid;
    STATE.activeSubclass = null;
    STATE.filterSubclassIds = null;
    STATE.filterClassIds = filterCids || [cid];
  }
  renderCategories();
  applyFilters();
}

function toggleSubgroup(catId, filterScids, filterCids) {
  if (STATE.activeSubclass == catId && STATE.filterSubclassIds && arraysEqual(STATE.filterSubclassIds, filterScids)) {
    STATE.activeSubclass = null;
    STATE.filterSubclassIds = null;
    STATE.filterClassIds = null;
  } else {
    STATE.activeCategory = catId;
    STATE.activeSubclass = catId;
    STATE.filterSubclassIds = filterScids;
    STATE.filterClassIds = filterCids || null;
  }
  renderCategories();
  applyFilters();
}

function arraysEqual(a, b) {
  if (!a || !b) return false;
  if (a.length !== b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

function selectLeaf(catId, subId, filterScids, filterCids) {
  // Leaf node: filter items by these subclass_ids and class_ids
  STATE.activeCategory = catId;
  STATE.activeSubclass = subId;
  STATE.filterSubclassIds = filterScids;
  STATE.filterClassIds = filterCids || null;
  closeDetail(); // Auto-close detail panel when switching categories
  renderCategories();
  applyFilters();
}

function filterCategories() {
  var q = document.getElementById('catSearch').value.toLowerCase();
  var items = document.querySelectorAll('.cat-item');
  items.forEach(function(el) {
    var name = (el.querySelector('.name') || {}).textContent || '';
    el.style.display = name.toLowerCase().includes(q) ? '' : 'none';
  });
}

// ═══════════════ Items ═══════════════
var _debouncedSearch = debounce(function() {
  STATE.filter.search = document.getElementById('searchInput').value;
  applyFilters();
}, 300);

function debounceSearch() { _debouncedSearch(); }

function applyFilters() {
  STATE.filter.quality = document.getElementById('fQuality').value;
  STATE.filter.ilvlMin = parseInt(document.getElementById('fIlvlMin').value) || null;
  STATE.filter.ilvlMax = parseInt(document.getElementById('fIlvlMax').value) || null;
  STATE.filter.minDisc = parseFloat(document.getElementById('fMinDisc').value) || null;
  STATE.filter.maxDisc = parseFloat(document.getElementById('fMaxDisc').value) || null;
  STATE.filter.minAvg = parseFloat(document.getElementById('fMinAvg').value) || null;
  STATE.filter.expansion = document.getElementById('fExpansion').value;
  STATE.sortBy = document.getElementById('fSort').value;
  loadItems();
}

async function loadItems(silent) {
  var params = new URLSearchParams();
  params.set('region', STATE.region);
  params.set('sort', STATE.sortBy);
  // Category filter
  if (STATE.filterSubclassIds && STATE.filterSubclassIds.length > 0) {
    params.set('filter_subclass_ids', STATE.filterSubclassIds.join(','));
  }
  if (STATE.filterClassIds && STATE.filterClassIds.length > 0) {
    params.set('filter_class_ids', STATE.filterClassIds.join(','));
  }
  if (STATE.filter.search) params.set('search', STATE.filter.search);
  if (STATE.filter.quality) params.set('quality', STATE.filter.quality);
  if (STATE.filter.ilvlMin) params.set('ilvl_min', STATE.filter.ilvlMin);
  if (STATE.filter.ilvlMax) params.set('ilvl_max', STATE.filter.ilvlMax);
  if (STATE.filter.minDisc !== null) params.set('min_discount', STATE.filter.minDisc);
  if (STATE.filter.maxDisc !== null) params.set('max_discount', STATE.filter.maxDisc);
  if (STATE.filter.minAvg !== null) params.set('min_avg', STATE.filter.minAvg);
  if (STATE.filter.expansion) params.set('expansion', STATE.filter.expansion);
  if (STATE.activeTab === 'snipe') params.set('snipe_only', '1');
  params.set('per_page', '500');

  var url = '/api/items?' + params.toString();

  if (!silent) {
    document.getElementById('tableBody').innerHTML = '<tr class="skeleton"><td colspan="10"><div style="height:28px"></div></td></tr>'.repeat(8);
  }

  var data = await api(url);
  if (!data) return;

  STATE.items = data.items || [];
  renderTable();

  // Update info bar
  document.getElementById('iCount').textContent = data.total;
  if (data.last_collected) {
    document.getElementById('iUpdated').textContent = '🕐 Updated: ' + data.last_collected.slice(0, 16);
  }

  if (!silent) {
    showStatus(data.total + ' items loaded', 'green');
  }
}

function renderTable() {
  var tbody = document.getElementById('tableBody');
  var empty = document.getElementById('emptyState');

  if (STATE.items.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'flex';
    document.getElementById('tableWrap').style.overflow = 'hidden';
    return;
  }
  empty.style.display = 'none';
  document.getElementById('tableWrap').style.overflow = 'auto';

  var html = '';
  var preloadIcons = [];
  STATE.items.forEach(function(item) {
    var isSnipe = STATE.snipeIds.has(item.item_id);
    var starClass = isSnipe ? 'on' : 'off';
    var starChar = isSnipe ? '★' : '☆';
    var sel = STATE.selectedItem && STATE.selectedItem.item_id === item.item_id && STATE.selectedItem.ilvl === item.ilvl;
    var qualClass = qualityClass(item.quality_type);
    var iconSrc = item.icon_url || '/icon/' + item.item_id;

    // Preload icon
    if (item.icon_url) preloadIcons.push(item.icon_url);

    html += '<tr class="' + (sel ? 'detail-open' : '') + '" onclick="selectItem(' + item.item_id + ',' + item.ilvl + ')">';
    html += '<td class="col-snipe"><span class="snipe-star ' + starClass + '" onclick="event.stopPropagation();toggleSnipe(' + item.item_id + ',this)">' + starChar + '</span></td>';
    html += '<td class="col-icon"><div class="icon"><img src="' + iconSrc + '" onerror="this.style.display=\'none\'" loading="lazy" alt=""></div></td>';
    html += '<td class="col-name"><div class="item-cell"><span class="name ' + qualClass + '">' + escapeHtml(item.name) + '</span></div></td>';
    html += '<td class="col-price price">' + g(item.price) + '</td>';
    html += '<td class="col-discount"><span class="discount ' + discountClass(item.discount) + '">' + item.discount + '%</span></td>';
    html += '<td class="col-realm realm">' + escapeHtml(item.cheapest_realm) + '</td>';
    html += '<td class="col-qty">' + item.quantity + '</td>';
    html += '<td class="col-avg price">' + g(item.top10avg) + '</td>';
    html += '<td class="col-ilvl">' + (item.item_level || '—') + '</td>';
    html += '<td class="col-quality"><span class="' + qualClass + '">' + (item.quality_type || '') + '</span></td>';
    html += '</tr>';
  });
  tbody.innerHTML = html;

  // Preload icons in background
  if (preloadIcons.length > 0) {
    setTimeout(function() {
      for (var i = 0; i < preloadIcons.length; i++) {
        var img = new Image();
        img.src = preloadIcons[i];
      }
    }, 100);
  }
}

// ═══════════════ Snipe (Star) Toggle ═══════════════
async function loadSnipeList() {
  var data = await api('/api/snipe/list?region=' + STATE.region);
  if (!data) return;
  STATE.snipeIds = new Set(data.items.map(function(i) { return i.item_id; }));
  document.getElementById('snipeBadge').textContent = STATE.snipeIds.size || '';
  document.getElementById('iSnipe').textContent = STATE.snipeIds.size;

  // Update star display if table is rendered
  if (STATE.items.length > 0) {
    var stars = document.querySelectorAll('.snipe-star');
    // Just re-render to keep it simple
    renderTable();
  }
}

async function toggleSnipe(itemId, el) {
  var data = await apiPost('/api/snipe/toggle', {item_id: itemId});
  if (!data) return;
  if (data.in_snipe) {
    STATE.snipeIds.add(itemId);
    if (el) { el.textContent = '★'; el.className = 'snipe-star on'; }
  } else {
    STATE.snipeIds.delete(itemId);
    if (el) { el.textContent = '☆'; el.className = 'snipe-star off'; }
  }
  document.getElementById('snipeBadge').textContent = STATE.snipeIds.size || '';
  document.getElementById('iSnipe').textContent = STATE.snipeIds.size;

  // If snipe tab is active, update the list
  if (STATE.activeTab === 'snipe') {
    loadItems();
  }
}

async function toggleDetailSnipe() {
  if (!STATE.selectedItem) return;
  var el = document.getElementById('dSnipeStar');
  await toggleSnipe(STATE.selectedItem.item_id, null);
  // Update star in detail panel
  if (STATE.snipeIds.has(STATE.selectedItem.item_id)) {
    el.textContent = '★'; el.style.color = 'var(--gold)';
  } else {
    el.textContent = '☆'; el.style.color = '';
  }
}

// ═══════════════ Item Detail ═══════════════
async function selectItem(itemId, ilvl) {
  STATE.selectedItem = {item_id: itemId, ilvl: ilvl};
  loadItemDetail(itemId, ilvl);
}

function closeDetail() {
  document.getElementById('detailView').classList.remove('open');
  document.getElementById('detailView').style.display = 'none';
  document.getElementById('tableWrap').style.display = '';
  STATE.selectedItem = null;
}

function showDetail() {
  document.getElementById('detailView').classList.add('open');
  document.getElementById('detailView').style.display = 'flex';
  document.getElementById('tableWrap').style.display = 'none';
}

function qualityColor(qtype) {
  return 'q-' + (qtype || 'COMMON');
}

async function loadItemDetail(itemId, ilvl) {
  var data = await api('/api/item?id=' + itemId + '&ilvl=' + ilvl + '&region=' + STATE.region);
  if (!data) return;

  showDetail();

  // Header
  var meta = data.meta || {};
  var qtype = meta.quality_type || 'COMMON';
  var nameEl = document.getElementById('dvName');
  nameEl.textContent = data.name;
  nameEl.className = 'dname ' + qualityColor(qtype);

  var iconEl = document.getElementById('dvIcon');
  iconEl.src = '/icon/' + itemId;
  iconEl.onerror = function() { this.style.display = 'none'; };
  iconEl.style.display = '';

  // Price with gold icon
  document.getElementById('dvPrice').textContent = data.cheapest_price_g + ' 🪙';

  // Meta line
  var metaParts = [];
  if (meta.class_name) metaParts.push(meta.class_name);
  if (meta.subclass_name) metaParts.push('> ' + meta.subclass_name);
  if (meta.quality_name) metaParts.push('| ' + meta.quality_name);
  if (meta.item_level) metaParts.push('| Ilvl ' + meta.item_level);
  if (meta.required_level) metaParts.push('| Req ' + meta.required_level);
  document.getElementById('dvMeta').textContent = metaParts.join(' ');

  // Stats grid
  var statHtml = '';
  statHtml += '<div class="stat"><div class="val">' + g(data.cheapest_price) + '</div><div class="lbl">Cheapest</div></div>';
  statHtml += '<div class="stat"><div class="val" style="color:' + (data.discount >= 70 ? 'var(--green)' : data.discount >= 40 ? 'var(--gold)' : 'var(--text2)') + '">' + data.discount + '%</div><div class="lbl">Discount</div></div>';
  statHtml += '<div class="stat"><div class="val">' + g(data.top10avg) + '</div><div class="lbl">Market Avg</div></div>';
  statHtml += '<div class="stat"><div class="val">' + data.realms_count + '</div><div class="lbl">Realms</div></div>';
  if (data.sale_price) {
    statHtml += '<div class="stat"><div class="val sale">' + data.sale_price.price_g + '</div><div class="lbl">Sale (' + escapeHtml(data.sale_price.realm_name) + ')</div></div>';
  }
  document.getElementById('dvStats').innerHTML = statHtml;

  // Realm prices table - group realms by price, no Avg/star
  var priceHtml = '';
  (data.realm_prices || []).forEach(function(rp) {
    priceHtml += '<tr>';
    priceHtml += '<td>' + escapeHtml(rp.realm_name || '—') + '</td>';
    priceHtml += '<td class="p-price">' + (rp.price_g || '—') + '</td>';
    priceHtml += '<td class="p-qty">' + (rp.quantity || 0) + '</td>';
    priceHtml += '</tr>';
  });
  document.getElementById('dvPriceBody').innerHTML = priceHtml;
}

// ═══════════════ Tabs ═══════════════
function switchTab(tab) {
  STATE.activeTab = tab;
  document.querySelectorAll('.tab-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  applyFilters();
}

// ═══════════════ Region ═══════════════
function setRegion(region) {
  STATE.region = region;
  document.getElementById('btnEu').classList.toggle('active', region === 'eu');
  document.getElementById('btnUs').classList.toggle('active', region === 'us');

  STATE.activeCategory = null;
  STATE.activeSubclass = null;
  closeDetail();

  loadCategories();
  loadItems();
  loadSnipeList();
  loadStatus();
}

// ═══════════════ Sort ═══════════════
function sortBy(col) {
  var sel = document.getElementById('fSort');
  var val;
  switch(col) {
    case 'discount_desc': val = 'discount_desc'; break;
    case 'name': val = 'name'; break;
    case 'price_asc': val = 'price_asc'; break;
    case 'ilvl_desc': val = 'ilvl_desc'; break;
    case 'snipe': val = 'snipe'; break;
    default: val = 'discount_desc';
  }

  // Toggle direction if same column
  if (STATE.sortBy === val) {
    if (val === 'discount_desc') val = 'discount_asc';
    else if (val === 'price_asc') val = 'price_desc';
    else if (val === 'ilvl_desc') val = 'ilvl_asc';
  }

  sel.value = val;
  STATE.sortBy = val;
  applyFilters();
}

// ═══════════════ Status ═══════════════
async function loadStatus() {
  var data = await api('/api/status?region=' + STATE.region);
  if (!data) return;

  if (data.last_collected) {
    document.getElementById('iUpdated').textContent = '🕐 Updated: ' + data.last_collected.slice(0, 16);
  }
  document.getElementById('snipeBadge').textContent = STATE.snipeIds.size || '';
  document.getElementById('iSnipe').textContent = STATE.snipeIds.size || 0;
}

// ═══════════════ Preset Management ═══════════════

async function loadPresets() {
  var data = await api('/api/presets/list?region=' + STATE.region);
  if (!data) return;
  STATE.presets = data.presets || [];
  STATE.activePreset = data.active;

  var sel = document.getElementById('presetSelect');
  var currentVal = sel.value;
  sel.innerHTML = '<option value="">— Preset —</option>';
  STATE.presets.forEach(function(p) {
    var opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = p.name + (p.is_default ? ' ★' : '');
    sel.appendChild(opt);
  });
  if (currentVal) sel.value = currentVal;
  else if (data.active) sel.value = data.active;

  document.getElementById('snipeBadge').textContent = STATE.snipeIds.size || '';
}

async function selectPreset(name) {
  if (!name) return;
  var data = await apiPost('/api/presets/select', {preset_name: name});
  if (!data || data.error) return;
  // Reload everything
  STATE.filterSubclassIds = null;
  STATE.filterClassIds = null;
  STATE.activeCategory = null;
  STATE.activeSubclass = null;
  closeDetail();
  loadSnipeList();
  loadItems();
  loadStatus();
}

function openPresetEditor() {
  var modal = document.getElementById('presetModal');
  modal.classList.add('open');

  // Fill with active preset data
  var p = null;
  var sel = document.getElementById('presetSelect');
  var selectedName = sel.value;
  if (selectedName) {
    STATE.presets.forEach(function(pp) { if (pp.name === selectedName) p = pp; });
  }

  document.getElementById('peOldName').value = p ? p.name : '';
  document.getElementById('peName').value = p ? p.name : '';
  document.getElementById('peItems').value = p ? (p.item_ids || '') : '';
  document.getElementById('peRealms').value = p ? (p.sale_realm_ids || '') : '';
  document.getElementById('peDiscount').value = p ? (p.discount || 0) : '';
  document.getElementById('peAvg').value = p ? (p.min_top10avg || 0) : '';
  document.getElementById('peDeleteBtn').style.display = p ? '' : 'none';
}

function closePresetEditor() {
  document.getElementById('presetModal').classList.remove('open');
}

async function savePreset() {
  var name = document.getElementById('peName').value.trim();
  if (!name) { alert('Name required'); return; }
  var oldName = document.getElementById('peOldName').value || name;

  var data = await apiPost('/api/presets/save', {
    name: name,
    old_name: oldName,
    item_ids: document.getElementById('peItems').value,
    sale_realm_ids: document.getElementById('peRealms').value,
    discount: parseFloat(document.getElementById('peDiscount').value) || 0,
    min_top10avg: parseFloat(document.getElementById('peAvg').value) || 0,
    is_default: false
  });
  if (!data || data.error) return;
  closePresetEditor();
  loadPresets();
  loadSnipeList();
}

async function deletePreset() {
  var name = document.getElementById('peOldName').value;
  if (!name || !confirm('Delete preset "' + name + '"?')) return;
  var data = await apiPost('/api/presets/delete', {name: name});
  if (!data || data.error) return;
  closePresetEditor();
  loadPresets();
  loadSnipeList();
  loadItems();
}
</script>

<!-- Preset Editor Modal -->
<div class="modal-overlay" id="presetModal">
<div class="modal">
  <h3>⚙ Preset Editor</h3>
  <input type="hidden" id="peOldName">
  <label>Preset Name</label><input id="peName" placeholder="Preset name...">
  <label>Item IDs (comma separated)</label><textarea id="peItems" rows="3" placeholder="34061,41508,44413,..."></textarea>
  <label>Sale Realm IDs (comma separated)</label><input id="peRealms" placeholder="3391,1403,1084,1305,1615,1602">
  <label>Min Discount %</label><input type="number" id="peDiscount" min="0" max="100" step="0.1" value="0">
  <label>Min Avg (g)</label><input type="number" id="peAvg" min="0" step="0.01" value="0">
  <div class="btn-row">
    <button class="btn-danger" id="peDeleteBtn" onclick="deletePreset()" style="display:none">🗑 Delete</button>
    <span class="spacer"></span>
    <button class="btn-cancel" onclick="closePresetEditor()">Cancel</button>
    <button class="btn-primary" onclick="savePreset()">💾 Save</button>
  </div>
</div>
</div>

</body>
</html>"""

def _load_html():
    """Return the embedded HTML page"""
    return HTML_PAGE

# ═══════════════ Main ═══════════════

_icon_name_map = {}  # item_id -> icon_name

def _load_item_icons():
    """Load item_icons.json for fast icon CDN URLs - handles both icon names and full URLs"""
    global _icon_name_map
    _icon_name_map = {}
    icon_path = data("item_icons.json")
    if os.path.exists(icon_path):
        try:
            with open(icon_path, encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in raw.items():
                item_id = int(k) if isinstance(k, str) and k.isdigit() else k
                # Extract just the icon filename from URL if needed
                if v and '/' in v:
                    icon_name = v.rstrip('.jpg').split('/')[-1]
                else:
                    icon_name = v
                if icon_name:
                    _icon_name_map[item_id] = icon_name
            _log(f"Loaded {len(_icon_name_map)} icon mappings")
        except Exception as e:
            _log(f"Error loading item_icons.json: {e}")

def _get_icon_url(item_id):
    """Get CDN icon URL for an item"""
    icon_name = _icon_name_map.get(item_id)
    if icon_name:
        return f"https://render.worldofwarcraft.com/{_region}/icons/56/{icon_name}.jpg"
    return None

def _load_all():
    """Load all caches on startup"""
    _load_item_names()
    _load_realms()
    _load_meta()
    _load_item_icons()
    _load_auction_data()
    _load_presets()
    _log("All caches loaded")

def _refresh_cache_loop():
    """Background thread to periodically refresh caches"""
    while True:
        time.sleep(30)  # Refresh every 30 seconds
        try:
            _load_auction_data()
            _load_presets()
        except:
            pass

def main():
    _load_all()

    # Start background refresh
    t = threading.Thread(target=_refresh_cache_loop, daemon=True)
    t.start()

    log.info(f"Starting Item Browser on http://{HOST}:{PORT}")
    t2 = threading.Thread(target=lambda: app.run(host=HOST, port=PORT, debug=False, use_reloader=False), daemon=True)
    t2.start()
    time.sleep(0.5)

    # Try PyWebView, fallback to browser
    try:
        import webview
        webview.create_window(
            "AH Sniper Browser",
            f"http://{HOST}:{PORT}",
            width=int(os.getenv("BROWSER_W", "1400")),
            height=int(os.getenv("BROWSER_H", "900")),
            resizable=True,
            min_size=(1000, 600)
        )
        webview.start()
    except ImportError:
        log.info("PyWebView not available, opening in browser...")
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}")
        # Keep Flask running
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
