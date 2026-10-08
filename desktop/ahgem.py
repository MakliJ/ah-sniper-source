#!/usr/bin/env python3
# AHGEM.py — Auction House Gatherer & Monitor (SQLite)
# v3.0 – EU + US регионы в параллельных потоках, умное окно для US

import os
import sys
import requests
import json
import re
import time
import sqlite3
import random
import threading
import logging
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# ================== OAuth ==================

# Свои модули (опционально — работают и без них)
try:
    import ws_server
except ImportError:
    ws_server = None
try:
    import topx_cache
except ImportError:
    topx_cache = None

load_dotenv()

# Если True — auction_latest пишется (нужно для сайта). В standalone EXE можно False.
WRITE_AUCTION_LATEST = os.getenv("WRITE_AUCTION_LATEST", "1") == "1"

# ── Настройки стриминга ──
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("WS_PORT", "8766"))
STREAM_ENABLED = os.getenv("STREAM_ENABLED", "1") == "1"

# Размер мониторинга (сколько реалмов проверять на обновление)
MONITOR_COUNT = int(os.getenv("MONITOR_COUNT", "15"))

# Логгер
log = logging.getLogger("ahgem")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ================== КОНФИГУРАЦИЯ РЕГИОНОВ ==================
REGION_CONFIGS = {
    "eu": {
        "label": "EU",
        "db_file": "auction_data.db",
        "region": "eu",
        "namespace_dynamic": "dynamic-eu",
        "namespace_static": "static-eu",
        "locale": "ru_RU",
        "locale_alias": "ru_RU",
        "base_url": "https://eu.api.blizzard.com",
        "token_url": "https://oauth.battle.net/token",
        "client_id": os.getenv("CLIENT_ID", ""),
        "client_secret": os.getenv("CLIENT_SECRET", ""),
        "check_realm_ids_raw": os.getenv("CHECK_REALM_IDS", ""),
        "pinned_realms": [1615, 1602],
        "ws_host": WS_HOST,
        "ws_port": WS_PORT,
    },
    "us": {
        "label": "US",
        "db_file": "auction_data_us.db",
        "region": "us",
        "namespace_dynamic": "dynamic-us",
        "namespace_static": "static-us",
        "locale": "en_US",
        "locale_alias": "en_US",
        "base_url": "https://us.api.blizzard.com",
        "token_url": "https://oauth.battle.net/token",
        "client_id": os.getenv("CLIENT_ID_US", ""),
        "client_secret": os.getenv("CLIENT_SECRET_US", ""),
        "check_realm_ids_raw": os.getenv("CHECK_REALM_IDS_US", ""),
        "pinned_realms": [],
        "ws_host": WS_HOST,
        "ws_port": WS_PORT,
    },
}

# Глобальные константы (не зависят от региона)
TOKEN_URL = "https://oauth.battle.net/token"

# Маппинг bonus_lists → ilvl для BoE-доспехов
# Загружается из ilvl_map.json (можно обновлять без пересборки EXE)
_FALLBACK_ILVL = {
    (13332, 12780): 243,
    (13332, 12779): 240,
    (13333, 12788): 256,
    (13333, 12787): 253,
    (13334, 12796): 269,
    (13334, 12795): 266,
    (13335, 12803): 279,
    (13335, 12804): 282,
    (13332, 12825): 279,
    (13333, 12833): 292,
    (13333, 12834): 295,
    (13333, 12835): 298,
    (13334, 12841): 305,
    (13334, 12842): 308,
    (13334, 12843): 311,
    (13334, 12844): 315,
    (13335, 12849): 318,
    (13335, 12850): 321,
    (13335, 12851): 324,
}

def _load_ilvl_map():
    """Загрузить маппинг из ilvl_map.json (рядом с EXE или в корне проекта)."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        from paths import data
        jp = data("ilvl_map.json")
        if os.path.exists(jp):
            with open(jp, encoding="utf-8") as f:
                raw = json.load(f)
            mappings = raw.get("mappings", {})
            result = {}
            for key, ilvl in mappings.items():
                parts = key.split("_")
                if len(parts) == 2:
                    result[tuple(sorted(map(int, parts)))] = int(ilvl)
            if result:
                log.info(f"Loaded ilvl_map.json: {len(result)} mappings")
                return result
    except Exception as e:
        log.warning(f"ilvl_map.json load failed: {e}")
    return {tuple(sorted(k)): v for k, v in _FALLBACK_ILVL.items()}

BONUS_TO_ILVL = _load_ilvl_map()

def compute_ilvl(bonus_lists):
    if not bonus_lists:
        return 0
    sig = tuple(sorted(b for b in bonus_lists if 12700 <= b <= 12900 or 13300 <= b <= 13400))
    return BONUS_TO_ILVL.get(sig, 0)

# Blizzard auction ``item.modifiers`` carries the two randomly rolled
# secondary stats.  The values are the same stat ids used by item preview.
BOE_STAT_MODIFIERS = {
    32: ("critical_strike", "Critical Strike"),
    36: ("haste", "Haste"),
    40: ("versatility", "Versatility"),
    49: ("mastery", "Mastery"),
}
BOE_TERTIARY_BONUSES = {
    40: ("avoidance", "Avoidance"),
    41: ("leech", "Leech"),
    42: ("speed", "Speed"),
    43: ("indestructible", "Indestructible"),
}

def decode_boe_variant(item_id, bonus_lists=None, modifiers=None):
    """Decode exact BoE stats/effects from one Blizzard auction item object."""
    bonus_lists = sorted({int(x) for x in (bonus_lists or [])})
    modifiers = modifiers or []
    stat_ids = []
    for modifier in modifiers:
        try:
            if int(modifier.get("type")) in (29, 30):
                value = int(modifier.get("value"))
                if value in BOE_STAT_MODIFIERS and value not in stat_ids:
                    stat_ids.append(value)
        except (TypeError, ValueError, AttributeError):
            continue
    # Keep UI/filter output stable regardless of modifier ordering.
    stat_ids.sort(key=lambda x: {32: 0, 36: 1, 40: 2, 49: 3}.get(x, 99))
    stats = [BOE_STAT_MODIFIERS[x][0] for x in stat_ids]
    labels = [BOE_STAT_MODIFIERS[x][1] for x in stat_ids]
    effects = []
    effect_labels = []
    for bonus in bonus_lists:
        if bonus in BOE_TERTIARY_BONUSES:
            effect, label = BOE_TERTIARY_BONUSES[bonus]
            if effect not in effects:
                effects.append(effect)
                effect_labels.append(label)
    # 13695 is the socket-bearing random-stat variant; 13668 is the neck
    # variant that has a prismatic socket on these Midnight BoEs.
    # Verified on live 12.1 lots (including necks). Do not claim "no socket"
    # for an unrecognised socket family from another patch.
    if 13695 in bonus_lists or 13668 in bonus_lists:
        sockets = 1
    elif 13696 in bonus_lists or 13662 in bonus_lists:
        sockets = 0
    else:
        sockets = None
    return {
        "stats": stats,
        "stats_label": " + ".join(labels + effect_labels),
        "effects": effects,
        "effects_label": " + ".join(effect_labels),
        "sockets": sockets,
        "bonus_lists": bonus_lists,
        "modifier_stat_ids": stat_ids,
    }

# ================== БД (регион-зависимые) ==================
def get_db(cfg):
    conn = sqlite3.connect(cfg["db_file"])
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 120000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(cfg):
    conn = get_db(cfg)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS realms (
            id INTEGER PRIMARY KEY,
            name_en TEXT,
            name_ru TEXT,
            slug TEXT
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            name_en TEXT,
            name_ru TEXT
        );
        CREATE TABLE IF NOT EXISTS auction_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at TEXT,
            realm_id INTEGER,
            item_id INTEGER,
            ilvl INTEGER DEFAULT 0,
            min_buyout INTEGER,
            quantity INTEGER,
            avg_price INTEGER,
            median_price INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON auction_snapshots(collected_at);
        CREATE INDEX IF NOT EXISTS idx_snapshots_realm_item ON auction_snapshots(realm_id, item_id);

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS auction_latest (
            realm_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            ilvl INTEGER NOT NULL DEFAULT 0,
            min_buyout INTEGER,
            quantity INTEGER,
            avg_price INTEGER,
            PRIMARY KEY (realm_id, item_id, ilvl)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            realm_id INTEGER,
            ilvl INTEGER DEFAULT 0,
            min_buyout INTEGER,
            recorded_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_price_hist_item_realm ON price_history(item_id, realm_id);

        CREATE TABLE IF NOT EXISTS user_deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            item_id INTEGER,
            realm_id INTEGER,
            price_at_add INTEGER,
            added_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_ignored (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            item_id INTEGER,
            realm_id INTEGER,
            price INTEGER,
            ignored_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT DEFAULT 'user'
        );
    """)
    # Миграция: добавляем ilvl в существующие таблицы (если старые)
    for tbl in ['auction_snapshots', 'auction_latest', 'price_history']:
        try: c.execute(f"ALTER TABLE {tbl} ADD COLUMN ilvl INTEGER DEFAULT 0")
        except: pass
    try: c.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_item_ilvl ON auction_snapshots(item_id, ilvl)")
    except: pass
    try: c.execute("CREATE INDEX IF NOT EXISTS idx_price_hist_latest ON price_history(item_id, ilvl, realm_id, recorded_at DESC)")
    except: pass
    # Миграция: quantity в price_history
    try: c.execute("ALTER TABLE price_history ADD COLUMN quantity INTEGER DEFAULT 0")
    except: pass
    conn.commit()
    conn.close()

def get_access_token(cfg):
    auth = (cfg["client_id"], cfg["client_secret"])
    data = {"grant_type": "client_credentials"}
    for attempt in range(3):
        try:
            resp = requests.post(TOKEN_URL, auth=auth, data=data, timeout=15)
            resp.raise_for_status()
            js = resp.json()
            # Запоминаем срок жизни (−5 мин запас); caller проверяет token_expired(cfg)
            try:
                cfg["_token_expires_at"] = time.time() + int(js.get("expires_in", 3600)) - 300
            except Exception:
                cfg["_token_expires_at"] = time.time() + 3300
            return js["access_token"]
        except Exception as e:
            if attempt == 2:
                raise
            print(f"   Token error (attempt {attempt+1}): {e}, retrying...")
            time.sleep(2)

def token_expired(cfg):
    """True если токен истёк или вот-вот истечёт."""
    return time.time() >= cfg.get("_token_expires_at", 0)

# ================== ЗАПОЛНЕНИЕ РЕАЛМОВ ==================
def rebuild_realms(token, cfg):
    print(f"[{cfg['label']}] Rebuilding realms table...")
    url_index = f"{cfg['base_url']}/data/wow/connected-realm/index?namespace={cfg['namespace_dynamic']}&locale={cfg['locale']}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url_index, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    realm_map = {}   # cr_id -> {name_en_list, name_ru_list, slug}

    # Сначала получим все connected realm ids
    cr_ids = []
    for entry in data.get("connected_realms", []):
        href = entry.get("href", "")
        match = re.search(r"/connected-realm/(\d+)", href)
        if match:
            cr_ids.append(int(match.group(1)))

    print(f"Found {len(cr_ids)} connected realms. Fetching details...")

    def fetch_realm_details(cr_id):
        url_en = f"{cfg['base_url']}/data/wow/connected-realm/{cr_id}?namespace={cfg['namespace_dynamic']}&locale=en_US"
        en_resp = requests.get(url_en, headers=headers, timeout=10)
        names_en = []
        slug = ""
        if en_resp.status_code == 200:
            en_data = en_resp.json()
            for realm in en_data.get("realms", []):
                name_field = realm.get("name")
                if isinstance(name_field, dict):
                    n = name_field.get("en_US")
                else:
                    n = name_field
                if n:
                    names_en.append(n)
            if names_en:
                slug = re.sub(r'[^a-zA-Z0-9 ]', '', names_en[0]).lower().replace(' ', '-')

        url_ru = f"{cfg['base_url']}/data/wow/connected-realm/{cr_id}?namespace={cfg['namespace_dynamic']}&locale={cfg['locale_alias']}"
        ru_resp = requests.get(url_ru, headers=headers, timeout=10)
        names_ru = []
        if ru_resp.status_code == 200:
            ru_data = ru_resp.json()
            for realm in ru_data.get("realms", []):
                name_field = realm.get("name")
                if isinstance(name_field, dict):
                    n = name_field.get(cfg['locale_alias'])
                else:
                    n = name_field
                if n:
                    names_ru.append(n)

        return cr_id, ", ".join(names_en) if names_en else "Unknown", ", ".join(names_ru) if names_ru else "", slug

    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_realm_details, cid): cid for cid in cr_ids}
        for future in as_completed(futures):
            cid, name_en, name_ru, slug = future.result()
            realm_map[cid] = (name_en, name_ru, slug)

    conn = get_db(cfg)
    cur = conn.cursor()
    cur.execute("DELETE FROM realms")
    for cr_id, (name_en, name_ru, slug) in realm_map.items():
        cur.execute("INSERT INTO realms (id, name_en, name_ru, slug) VALUES (?,?,?,?)",
                    (cr_id, name_en, name_ru, slug))
    conn.commit()
    conn.close()
    print(f"[{cfg['label']}] Saved {len(realm_map)} connected realms.")

def get_realm_ids(cfg):
    conn = get_db(cfg)
    cur = conn.cursor()
    cur.execute("SELECT id FROM realms")
    ids = [row[0] for row in cur.fetchall()]
    conn.close()
    return ids

# ================== ЗАГРУЗКА АУКЦИОНОВ ==================

def get_auctions_for_realm_with_retry(token, realm_id, cfg, session=None, max_retries=3, last_modified=None):
    """
    Загрузить аукционы реалма. Если last_modified передан, использует If-Modified-Since.
    Возвращает (data, new_last_modified) или (None, None) если 304 (не изменилось).
    """
    url = f"{cfg['base_url']}/data/wow/connected-realm/{realm_id}/auctions?namespace={cfg['namespace_dynamic']}&locale={cfg['locale']}"
    headers = {"Authorization": f"Bearer {token}"}
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = session.get if session else requests.get
    delay = 1
    for attempt in range(max_retries):
        try:
            resp = req(url, headers=headers, timeout=20)
            new_lm = resp.headers.get("Last-Modified", "")
            if resp.status_code == 200:
                return resp.json(), new_lm
            elif resp.status_code == 304:
                return None, last_modified  # не изменилось
            elif resp.status_code in (429, 500, 502, 503, 504):
                print(f"   Error {resp.status_code} on realm {realm_id}, attempt {attempt+1}/{max_retries}, sleeping {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            else:
                resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                print(f"   FAILED realm {realm_id}: {e}")
                return {}, ""
            print(f"   Connection error on realm {realm_id}, retry in {delay}s...")
            time.sleep(delay)
            delay *= 2
    return {}, ""

def process_auctions(auctions_data):
    auctions = auctions_data.get("auctions", [])
    agg = {}
    variant_acc = {}
    variants_by_item = {}
    _b2i = BONUS_TO_ILVL
    _agg_get = agg.get
    for auc in auctions:
        item = auc.get("item")
        if not item:
            continue
        item_id = item.get("id")
        price = auc.get("buyout") or auc.get("unit_price")
        if item_id is None or not price or price < 0:
            continue
        bl = item.get("bonus_lists")
        if bl:
            sig = tuple(sorted(b for b in bl if 12700 <= b <= 12900 or 13300 <= b <= 13400))
            ilvl = _b2i.get(sig, 0)
        else:
            ilvl = 0
        key = (item_id, ilvl)
        entry = _agg_get(key)
        if entry is None:
            agg[key] = [price, price, 1]
        else:
            if price < entry[0]:
                entry[0] = price
            entry[1] += price
            entry[2] += 1
        if ilvl > 0:
            decoded = decode_boe_variant(item_id, bl, item.get("modifiers"))
            vkey = (item_id, ilvl, tuple(decoded["bonus_lists"]), tuple(decoded["modifier_stat_ids"]), tuple(decoded["effects"]), decoded["sockets"])
            ventry = variant_acc.get(vkey)
            qty = int(auc.get("quantity") or 1)
            if ventry is None:
                variant_acc[vkey] = {"min_buyout": price, "avg_price": price, "count": 1, "quantity": qty, **decoded}
            else:
                ventry["min_buyout"] = min(ventry["min_buyout"], price)
                ventry["avg_price"] += price
                ventry["count"] += 1
                ventry["quantity"] += qty
    result = {k: {'min_buyout': v[0], 'avg_price': v[1] // v[2], 'count': v[2]} for k, v in agg.items()}
    for v in variant_acc.values():
        v["avg_price"] //= max(v["count"], 1)
        v.pop("count", None)
    for (item_id, ilvl, *_), variant in variant_acc.items():
        variants_by_item.setdefault((item_id, ilvl), []).append(variant)
    for (item_id, ilvl), value in result.items():
        variants = variants_by_item.get((item_id, ilvl))
        if variants:
            value["variants"] = variants
    return result

def parse_and_process(content_bytes):
    """Parse raw JSON bytes + aggregate auctions. Worker for ProcessPoolExecutor."""
    try:
        import orjson
        data = orjson.loads(content_bytes)
    except ImportError:
        data = json.loads(content_bytes)
    return process_auctions(data)

KEEP_SNAPSHOT_RUNS = 2  # только текущий + предыдущий снепшот

def _write_latest_fast(conn, collected_at, latest_rows):
    """Полная замена auction_latest — пакетная вставка по 100K строк."""
    conn.execute("DROP TABLE IF EXISTS auction_latest_new")
    conn.execute("""
        CREATE TABLE auction_latest_new (
            realm_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            ilvl INTEGER NOT NULL DEFAULT 0,
            min_buyout INTEGER,
            quantity INTEGER,
            avg_price INTEGER,
            PRIMARY KEY (realm_id, item_id, ilvl)
        ) WITHOUT ROWID
    """)
    BATCH = 100000
    for i in range(0, len(latest_rows), BATCH):
        conn.executemany("""
            INSERT INTO auction_latest_new (realm_id, item_id, ilvl, min_buyout, quantity, avg_price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, latest_rows[i:i + BATCH])
    conn.execute("DROP TABLE IF EXISTS auction_latest")
    conn.execute("ALTER TABLE auction_latest_new RENAME TO auction_latest")
    conn.execute("""
        INSERT INTO meta (key, value) VALUES ('last_snapshot', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (collected_at,))

def _archive_snapshots(collected_at, snapshot_rows, cfg):
    """Фон: история для sparkline + price_history + очистка старых снимков."""
    conn = get_db(cfg)
    try:
        t0 = time.time()
        conn.execute("PRAGMA synchronous=OFF")

        # Вставляем пачками по 10K — не держим лок надолго
        BATCH = 10000
        for i in range(0, len(snapshot_rows), BATCH):
            batch = snapshot_rows[i:i + BATCH]
            conn.execute("BEGIN")
            conn.executemany("""
                INSERT INTO auction_snapshots (collected_at, realm_id, item_id, ilvl, min_buyout, quantity, avg_price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, batch)
            conn.commit()

        # Очистка старых снимков
        conn.execute("BEGIN")
        conn.execute("""
            DELETE FROM auction_snapshots
            WHERE collected_at < (
                SELECT MIN(ts) FROM (
                    SELECT DISTINCT collected_at AS ts
                    FROM auction_snapshots
                    ORDER BY ts DESC
                    LIMIT ?
                )
            )
        """, (KEEP_SNAPSHOT_RUNS,))
        conn.commit()

        # Очистка price_history старше 30 дней
        conn.execute("BEGIN")
        conn.execute("""
            DELETE FROM price_history
            WHERE recorded_at < datetime('now', '-30 days')
        """)
        conn.commit()

        # price_history — тоже отдельной транзакцией
        conn.execute("BEGIN")
        conn.execute("""
            INSERT INTO price_history (item_id, ilvl, realm_id, min_buyout, recorded_at)
            SELECT s.item_id, s.ilvl, s.realm_id, s.min_buyout, ?
            FROM auction_snapshots s
            LEFT JOIN (
                SELECT ph.item_id, ph.ilvl, ph.realm_id, ph.min_buyout AS last_min
                FROM price_history ph
                INNER JOIN (
                    SELECT item_id, ilvl, realm_id, MAX(recorded_at) AS max_at
                    FROM price_history
                    GROUP BY item_id, ilvl, realm_id
                ) latest ON ph.item_id = latest.item_id
                        AND ph.ilvl = latest.ilvl
                        AND ph.realm_id = latest.realm_id
                        AND ph.recorded_at = latest.max_at
            ) last_ph ON last_ph.item_id = s.item_id AND last_ph.ilvl = s.ilvl AND last_ph.realm_id = s.realm_id
            WHERE s.collected_at = ?
              AND s.min_buyout <> COALESCE(last_ph.last_min, -1)
        """, (collected_at, collected_at))
        conn.commit()

        print(f"   Archive done ({time.time() - t0:.1f}s).", flush=True)
    except Exception as e:
        print(f"   Archive error: {e}", flush=True)
    finally:
        conn.close()

def collect_and_store(token, cfg):
    t_total = time.time()
    conn = get_db(cfg)
    realm_ids = get_realm_ids(cfg)
    label = cfg['label']
    print(f"[{label}] Starting collection from {len(realm_ids)} realms (If-Modified-Since)...")

    # ── Загружаем кеш topXavg + имена реалмов для стриминга ──
    topx_cache_data = topx_cache.load_topx_cache(cfg["db_file"]) if topx_cache else {}
    realm_names = topx_cache.load_realm_names(cfg["db_file"]) if topx_cache else {}
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # ── Загружаем last_modified из БД (для If-Modified-Since) ──
    last_modified_map = {}
    try:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'lm_%'").fetchall()
        for r in rows:
            rid = int(r["key"].replace("lm_", ""))
            last_modified_map[rid] = r["value"]
    except:
        pass

    # ── Запускаем WebSocket-сервер ──
    ws = None
    if STREAM_ENABLED:
        try:
            ws = ws_server.ensure_ws_server(cfg.get("ws_host", "0.0.0.0"), cfg.get("ws_port", 8765))
            ws.broadcast(ws_server.make_status_msg(label, 0, 0, len(realm_ids),
                                                   f"Starting collection of {len(realm_ids)} realms"))
        except Exception as e:
            log.warning(f"WS server not available (streaming disabled): {e}")
            ws = None

    # ── Session с пулом соединений ──
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=40, pool_maxsize=40)
    session.mount('https://', adapter)

    all_agg = {}
    failed = []
    skipped = []
    realm_times = {}
    new_lm_map = {}
    t_dl = time.time()
    with ThreadPoolExecutor(max_workers=40) as executor:
        future_to_realm = {
            executor.submit(get_auctions_for_realm_with_retry, token, rid, cfg, session, 3,
                          last_modified_map.get(rid)): rid
            for rid in realm_ids
        }
        for i, future in enumerate(as_completed(future_to_realm), 1):
            rid = future_to_realm[future]
            t_realm = time.time()
            try:
                result = future.result(timeout=60)
                data, new_lm = result if isinstance(result, tuple) else (result, "")
                realm_times[rid] = time.time() - t_realm

                if data is None:
                    # 304 — реалм не изменился
                    skipped.append(rid)
                else:
                    agg = process_auctions(data)
                    all_agg[rid] = agg
                    if new_lm:
                        new_lm_map[rid] = new_lm

                    # ── СТРИМИНГ: отправляем реалм в GUI сразу ──
                    if ws and agg:
                        realm_name = realm_names.get(rid, f"Realm {rid}")
                        ws.broadcast(ws_server.make_realm_msg(
                            label, rid, realm_name, agg, collected_at
                        ))

            except Exception as e:
                realm_times[rid] = time.time() - t_realm
                print(f"\n   ERROR realm {rid}: {e}")
                failed.append(rid)

            # Статус каждые 5 реалмов
            if i % 5 == 0 or i == len(realm_ids):
                pct = i / len(realm_ids)
                print(f"   checked {i}/{len(realm_ids)} ({pct*100:.0f}%)  |  changed={len(all_agg)} skipped={len(skipped)} failed={len(failed)}", end='\r')
                if ws:
                    ws.broadcast(ws_server.make_status_msg(
                        label, pct, i, len(realm_ids),
                        f"{i}/{len(realm_ids)} | {len(all_agg)} changed, {len(skipped)} same"
                    ))

    dl_time = time.time() - t_dl
    changed = len(all_agg)
    skipped_count = len(skipped)
    print(f"\n   Check {len(realm_ids)} realms: {dl_time:.1f}s ({changed} changed, {skipped_count} same, {len(failed)} failures).")
    if realm_times:
        slowest = sorted(realm_times.items(), key=lambda x: x[1], reverse=True)[:5]
        print(f"   Slowest: " + ", ".join(f"#{rid}={t:.1f}s" for rid, t in slowest))
    session.close()

    # ── Сохраняем last_modified для следующего сбора ──
    if new_lm_map:
        try:
            c = conn.cursor()
            for rid, lm in new_lm_map.items():
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"lm_{rid}", lm))
            conn.commit()
        except Exception as e:
            print(f"   LM save error: {e}")

    # ── Сборка полных данных для auction_latest + snapshot ──
    t_build = time.time()
    latest_rows = []
    snapshot_rows = []
    for realm_id, agg in all_agg.items():
        for (item_id, ilvl), v in agg.items():
            latest_rows.append((realm_id, item_id, ilvl, v['min_buyout'], v['count'], v['avg_price']))
            snapshot_rows.append((collected_at, realm_id, item_id, ilvl, v['min_buyout'], v['count'], v['avg_price']))
    build_time = time.time() - t_build

    # ── Уведомление GUI о завершении сбора ДО записи в БД ──
    total_time = time.time() - t_total
    total_streamed = sum(len(agg) for agg in all_agg.values())
    if ws:
        ws.broadcast(ws_server.make_collection_done(
            label, collected_at, total_streamed, len(realm_ids), total_time
        ))

    # ── Фоновый архив (price_history) ──
    def _compact_archive():
        t0 = time.time()
        conn2 = get_db(cfg)
        conn2.execute("PRAGMA synchronous=OFF")
        conn2.execute("BEGIN")
        rows = [(r[2], r[3] or 0, r[1], r[4], r[5] or 0, r[0]) for r in snapshot_rows]
        conn2.executemany(
            "INSERT INTO price_history (item_id, ilvl, realm_id, min_buyout, quantity, recorded_at) VALUES (?,?,?,?,?,?)",
            rows
        )
        conn2.commit()
        conn2.close()
        print(f"   Archive: {len(rows)} rows in {time.time()-t0:.1f}s.", flush=True)

    conn.close()
    # ── Пишем auction_latest (своё подключение, не блокирует) ──
    if WRITE_AUCTION_LATEST and latest_rows:
        threading.Thread(target=_write_latest_fast, args=(get_db(cfg), collected_at, latest_rows), daemon=True).start()
    # ── Фоновый архив (price_history) ──
    threading.Thread(target=_compact_archive, daemon=True).start()

    print(f">>> TOTAL collection: {total_time:.1f}s (dl={dl_time:.1f}s build={build_time:.1f}s write=async)", flush=True)

def _save_blizzard_time(last_modified_dict, cfg):
    if not last_modified_dict:
        return
    blizz_time = next((v for v in last_modified_dict.values() if v), None)
    if blizz_time:
        conn = get_db(cfg)
        conn.execute("PRAGMA busy_timeout = 120000")
        try:
            conn.execute("""
                INSERT INTO meta (key, value) VALUES ('blizzard_last_modified', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (blizz_time,))
            conn.commit()
        finally:
            conn.close()

def check_update_for_realm(token, realm_id, last_modified, cfg, session=None):
    url = f"{cfg['base_url']}/data/wow/connected-realm/{realm_id}/auctions?namespace={cfg['namespace_dynamic']}&locale={cfg['locale']}"
    headers = {"Authorization": f"Bearer {token}"}
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = session.get if session else requests.get
    try:
        resp = req(url, headers=headers, timeout=10)
        blizz_mod = resp.headers.get("Last-Modified")
        if resp.status_code == 304:
            # Не изменилось — возвращаем сохранённое
            return False, last_modified, last_modified
        elif resp.status_code == 200:
            # 200 = либо изменилось, либо первый запрос (last_modified=None)
            if blizz_mod and blizz_mod != last_modified:
                return True, blizz_mod, blizz_mod
            else:
                return False, blizz_mod or last_modified, blizz_mod
        elif resp.status_code == 429:
            # Лимит Blizzard: уважаем Retry-After, не считаем это обновлением
            ra = resp.headers.get("retry-after", "")
            try: wait_s = min(30.0, float(ra))
            except ValueError: wait_s = 5.0
            print(f"   429 rate limit on realm {realm_id}, backoff {wait_s:.0f}s")
            time.sleep(wait_s)
            return False, last_modified, None
        else:
            resp.raise_for_status()
    except Exception as e:
        print(f"\n   Error checking realm {realm_id}: {e}")
    return False, last_modified, None

# ================== УМНОЕ ОКНО ДЛЯ US ==================

def smart_window_status(last_blizz_str):
    """Динамический расчёт окна US. Возвращает (в_окне, секунд_спать)."""
    if not last_blizz_str:
        return True, 2  # Нет данных — мониторим
    try:
        last_dt = parsedate_to_datetime(last_blizz_str)
    except:
        return True, 2
    now = datetime.now(timezone.utc)
    expected_next = last_dt + timedelta(minutes=60)
    diff = (expected_next - now).total_seconds()
    WINDOW_HALF = 150  # 2.5 минуты
    if abs(diff) <= WINDOW_HALF:
        return True, 2      # В окне
    elif diff > WINDOW_HALF:
        return False, int(diff - WINDOW_HALF)  # Слишком рано
    else:
        return True, 5  # Опаздываем — форсируем

# ---------- основной цикл (регион-зависимый) ----------

def main_loop(region="eu"):
    cfg = REGION_CONFIGS[region]
    label = cfg["label"]

    if not cfg["client_id"] or not cfg["client_secret"]:
        print(f"[{label}] CLIENT_ID/CLIENT_SECRET not set -- skipping.")
        return

    init_db(cfg)
    token = get_access_token(cfg)
    print(f"[{label}] Token obtained.")

    if not get_realm_ids(cfg):
        rebuild_realms(token, cfg)

    check_ids_raw = cfg["check_realm_ids_raw"]
    if check_ids_raw.strip():
        check_ids = [int(x.strip()) for x in check_ids_raw.split(",") if x.strip()]
        # Даже если заданы вручную, можно расширить до MONITOR_COUNT
        if len(check_ids) < MONITOR_COUNT:
            all_realms = get_realm_ids(cfg)
            extra = [r for r in all_realms if r not in check_ids]
            random.shuffle(extra)
            check_ids.extend(extra[:MONITOR_COUNT - len(check_ids)])
            print(f"[{label}] Extended check_ids to {len(check_ids)} realms")
    else:
        all_realms = get_realm_ids(cfg)
        pinned = cfg["pinned_realms"]
        others = [r for r in all_realms if r not in pinned]
        random.shuffle(others)
        take = max(MONITOR_COUNT - len(pinned), 5)
        check_ids = pinned + others[:take]
        print(f"[{label}] No CHECK_REALM_IDS, using pinned {pinned} + random: {check_ids}")
    print(f"[{label}] Monitoring updates on {len(check_ids)} realms: {check_ids[:10]}...")

    last_modified = {rid: None for rid in check_ids}

    # ── SmartMonitor: отслеживаем, какие реалмы быстрее обнаруживают обновление ──
    smart_realm_scores = {}  # {realm_id: {"detections": int, "avg_speed": float}}
    first_run = True

    # EU: хардкод-окна
    def eu_in_window():
        minute = datetime.now(timezone.utc).minute
        return (22 <= minute <= 30) or (53 <= minute <= 56)

    def eu_sec_until_next():
        now = datetime.now(timezone.utc)
        m = now.minute
        if m < 22: t = now.replace(minute=22, second=0, microsecond=0)
        elif m <= 30: return 0
        elif m < 53: t = now.replace(minute=53, second=0, microsecond=0)
        elif m <= 56: return 0
        else: t = (now + timedelta(hours=1)).replace(minute=22, second=0, microsecond=0)
        return max(0, (t - now).total_seconds())

    def eu_sec_until_after():
        now = datetime.now(timezone.utc)
        m = now.minute
        if m < 22: t = now.replace(minute=22, second=0, microsecond=0)
        elif m <= 30: t = now.replace(minute=53, second=0, microsecond=0)
        elif m < 53: t = now.replace(minute=53, second=0, microsecond=0)
        elif m <= 56: t = (now + timedelta(hours=1)).replace(minute=22, second=0, microsecond=0)
        else: t = (now + timedelta(hours=1)).replace(minute=22, second=0, microsecond=0)
        return max(0, (t - now).total_seconds())

    while True:
        if first_run:
            print(f"[{label}] First run: full collection.")
            collect_and_store(token, cfg)
            for rid in check_ids:
                try:
                    url = f"{cfg['base_url']}/data/wow/connected-realm/{rid}/auctions?namespace={cfg['namespace_dynamic']}&locale={cfg['locale']}"
                    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if resp.status_code == 200:
                        last_modified[rid] = resp.headers.get("Last-Modified")
                        print(f"[{label}]   Realm {rid}: Blizz={last_modified[rid]}")
                except:
                    pass
            _save_blizzard_time(last_modified, cfg)
            first_run = False
            # US: первый пост в Discord
            if region == "us":
                print(f"[{label}] Calling Discord post (first run)...", flush=True)
                try:
                    import subprocess
                    result = subprocess.run([sys.executable, "discord_bot.py", "--post"], timeout=30, capture_output=True, text=True)
                    if result.stdout: print(result.stdout.strip())
                    if result.stderr: print(result.stderr.strip())
                    print(f"[{label}] Discord post done (exit={result.returncode}).", flush=True)
                except Exception as e:
                    print(f"[{label}] Discord post error: {e}", flush=True)
            if region == "eu":
                sleep_time = eu_sec_until_after()
            else:
                _, sleep_time = smart_window_status(last_modified.get(check_ids[0]))
            print(f"[{label}] Done. Sleeping {sleep_time/60:.0f} min...")
            time.sleep(sleep_time)
            token = get_access_token(cfg)
            continue

        # Проверка окна
        if region == "eu":
            if not eu_in_window():
                s = eu_sec_until_next()
                print(f"[{label}] Outside window. Sleeping {s/60:.0f} min...")
                time.sleep(s)
                token = get_access_token(cfg)
                continue
        else:  # US — умное окно
            sample = last_modified.get(check_ids[0])
            in_win, sleep_sec = smart_window_status(sample)
            if not in_win:
                print(f"[{label}] Outside window. Sleeping {sleep_sec/60:.0f} min...")
                time.sleep(sleep_sec)
                token = get_access_token(cfg)
                continue

        # --- Мониторинг ---
        update_detected = False
        detect_realm_id = None
        monitor_start = datetime.now(timezone.utc)
        print(f"\n[{monitor_start.strftime('%H:%M:%S UTC')}] [{label}] Update window open. "
              f"Monitoring {len(check_ids)} realms")

        # SmartMonitor: периодически пересортировываем check_ids по скорости
        monitor_round = 0

        check_session = requests.Session()
        while not update_detected:
            if region == "eu" and not eu_in_window():
                break

            # ── SmartMonitor: раз в 3 цикла, если есть данные, пересортировать ──
            monitor_round += 1
            if monitor_round % 3 == 0 and smart_realm_scores:
                sorted_realms = sorted(
                    check_ids,
                    key=lambda r: (
                        -smart_realm_scores.get(r, {}).get("detections", 0),
                        smart_realm_scores.get(r, {}).get("avg_speed", 999)
                    )
                )
                check_ids = sorted_realms

            with ThreadPoolExecutor(max_workers=len(check_ids)) as executor:
                futures = {
                    executor.submit(check_update_for_realm, token, rid, last_modified[rid], cfg, check_session): rid
                    for rid in check_ids
                }
                for future in as_completed(futures):
                    rid = futures[future]
                    try:
                        updated, new_mod, blizz_mod = future.result()
                    except:
                        continue
                    if updated:
                        update_detected = True
                        detect_realm_id = rid
                        # SmartMonitor: обновляем статистику для этого реалма
                        elapsed = (datetime.now(timezone.utc) - monitor_start).total_seconds()
                        score = smart_realm_scores.setdefault(rid, {"detections": 0, "avg_speed": 0.0})
                        score["detections"] += 1
                        score["avg_speed"] = (
                            (score["avg_speed"] * (score["detections"] - 1) + elapsed) / score["detections"]
                        )
                        print(f"\n[{label}]   [OK] Realm {rid}: UPDATE! (detected in {elapsed:.1f}s) Blizz={blizz_mod}")
                        # Отправляем статус в WS
                        if STREAM_ENABLED:
                            try:
                                ws_srv = ws_server.get_ws_server()
                                if ws_srv.is_running:
                                    ws_srv.broadcast(ws_server.make_monitor_status(
                                        label, "update_detected", blizz_mod or "?",
                                        check_ids[:5], elapsed
                                    ))
                            except:
                                pass
                        break
                    elif blizz_mod and blizz_mod != last_modified.get(rid):
                        print(f"\n[{label}]   [!] Realm {rid}: changed ({blizz_mod}), forcing.")
                        update_detected = True
                        detect_realm_id = rid
                        break
            if not update_detected:
                elapsed = (datetime.now(timezone.utc) - monitor_start).total_seconds()
                sm = last_modified.get(check_ids[0], '?') if check_ids else '?'
                print(f"   [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [{label}] "
                      f"No update | Blizz[{check_ids[0] if check_ids else '?'}]={sm} | {elapsed:.0f}s", end='\r')
                time.sleep(0.3)
                if elapsed > 600:
                    print(f"\n[{label}]   [!] 10+ min -- forcing collection.")
                    update_detected = True
        check_session.close()

        if update_detected:
            print(f"\n[{label}] >>> Full collection...")
            collect_and_store(token, cfg)
            print(f"[{label}]   Updating Last-Modified:")
            for rid in check_ids:
                try:
                    url = f"{cfg['base_url']}/data/wow/connected-realm/{rid}/auctions?namespace={cfg['namespace_dynamic']}&locale={cfg['locale']}"
                    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if resp.status_code == 200:
                        last_modified[rid] = resp.headers.get("Last-Modified")
                except:
                    pass
            _save_blizzard_time(last_modified, cfg)
            if region == "eu":
                sleep_time = eu_sec_until_after()
            else:
                # US: дёргаем Discord-бота для поста топ-20
                print(f"[{label}] Calling Discord post...", flush=True)
                try:
                    import subprocess
                    result = subprocess.run([sys.executable, "discord_bot.py", "--post"], timeout=30, capture_output=True, text=True)
                    if result.stdout:
                        print(result.stdout.strip())
                    if result.stderr:
                        print(result.stderr.strip())
                    print(f"[{label}] Discord post done (exit={result.returncode}).", flush=True)
                except Exception as e:
                    print(f"[{label}] Discord post error: {e}", flush=True)
                _, sleep_time = smart_window_status(last_modified.get(check_ids[0]))
            if sleep_time > 0:
                print(f"[{label}] Sleeping {sleep_time/60:.0f} min until next window...")
                time.sleep(sleep_time)
            token = get_access_token(cfg)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", choices=["eu", "us", "both"], default="both")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--rebuild-realms", action="store_true")
    args = parser.parse_args()

    if args.migrate:
        print("Migration deprecated.")
    elif args.rebuild_realms:
        for r in ["eu", "us"]:
            cfg = REGION_CONFIGS[r]
            if cfg["client_id"] and cfg["client_secret"]:
                init_db(cfg)
                token = get_access_token(cfg)
                rebuild_realms(token, cfg)
    else:
        if args.region == "both":
            print(">>> Starting both EU + US in parallel...")
            t1 = threading.Thread(target=main_loop, args=("eu",), daemon=True, name="EU")
            t2 = threading.Thread(target=main_loop, args=("us",), daemon=True, name="US")
            t1.start(); t2.start()
            t1.join(); t2.join()
        else:
            main_loop(args.region)
