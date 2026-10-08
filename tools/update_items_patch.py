#!/usr/bin/env python3
# update_items_patch.py — обновление имён (en/ru) и иконок предметов после патча
# 1) Дозагружает недостающие названия из Blizzard API (item endpoint, en_US + ru_RU)
# 2) Полностью пересобирает item_icons.json (media endpoint -> чистые имена иконок)
# 3) Заполняет таблицу items в auction_data.db (id, name_en, name_ru)
# Идемпотентен: можно перезапускать, незавершённое продолжит докачиваться.

import os, sys, json, time, re, shutil, sqlite3, threading
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv()

CID = os.getenv("CLIENT_ID")
CSEC = os.getenv("CLIENT_SECRET")
if not CID or not CSEC:
    raise SystemExit("CLIENT_ID/CLIENT_SECRET не найдены в .env")

REGION = "eu"
NS = "static-eu"
BASE = f"https://{REGION}.api.blizzard.com"

NAMES_FILE = "item_names.json"
NAMES_RU_FILE = "item_names_ru.json"
ICONS_FILE = "item_icons.json"
DB_FILE = "auction_data.db"

# ── Rate limiter (Blizzard: 100 req/s burst; держим ~55 req/s) ──
class Rate:
    def __init__(self, rps=55.0):
        self.min_interval = 1.0 / rps
        self.lock = threading.Lock()
        self.next_t = 0.0
    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_t:
                time.sleep(self.next_t - now)
            self.next_t = time.monotonic() + self.min_interval

rate = Rate(55.0)
_token = None
_token_lock = threading.Lock()

def get_token(force=False):
    global _token
    with _token_lock:
        if _token and not force:
            return _token
        r = requests.post("https://oauth.battle.net/token",
                          auth=(CID, CSEC),
                          data={"grant_type": "client_credentials"},
                          timeout=30, verify=False)
        r.raise_for_status()
        _token = r.json()["access_token"]
        return _token

def api_get(url, retries=4):
    for i in range(retries):
        rate.wait()
        try:
            tok = get_token()
            r = requests.get(url, headers={"Authorization": f"Bearer {tok}"},
                             timeout=15, verify=False)
        except Exception:
            time.sleep(1.0 + i)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            get_token(force=True)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            time.sleep(max(1, int(r.headers.get("Retry-After", "2"))))
            continue
        if r.status_code >= 500:
            time.sleep(1.5 * (i + 1))
            continue
        return None
    return None

def fetch_name(iid, locale):
    d = api_get(f"{BASE}/data/wow/item/{iid}?namespace={NS}&locale={locale}")
    if not d:
        return None
    n = d.get("name")
    if isinstance(n, dict):
        n = n.get(locale) or n.get("en_US")
    if isinstance(n, str) and n.strip():
        return n.strip()
    return None

def norm_icon(val):
    if not val:
        return None
    v = str(val)
    m = re.search(r'https?://render\.worldofwarcraft\.com[^\s"\']+', v)
    if m:
        v = m.group(0)
    if '/' in v:
        v = v.split('/')[-1]
    for ext in ('.jpg', '.png'):
        if v.lower().endswith(ext):
            v = v[:-len(ext)]
            break
    v = v.strip()
    return v if v else None

def fetch_icon(iid):
    d = api_get(f"{BASE}/data/wow/media/item/{iid}?namespace={NS}&locale=en_US")
    if not d:
        return None
    for a in (d.get("assets") or []):
        if a.get("key") == "icon":
            return norm_icon(a.get("value"))
    return None

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)

def backup(path):
    bak = path + ".prebak"
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f"  backup: {path} -> {bak}")

def parse_env_ids():
    ids = set()
    if not os.path.exists(".env"):
        return ids
    with open(".env", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("DEFAULT_ITEM_IDS=") or line.startswith("ITEM_IDS="):
                for tok in line.split("=", 1)[1].split(","):
                    tok = tok.strip()
                    if tok.isdigit():
                        ids.add(int(tok))
    return ids

def collect_ids():
    ids = parse_env_ids()
    if os.path.exists(DB_FILE):
        with closing(sqlite3.connect(DB_FILE)) as c:
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("auction_snapshots", "item_meta"):
                if table in tables:
                    ids.update(row[0] for row in c.execute(f"SELECT DISTINCT item_id FROM {table}") if row[0])
    return sorted(ids)

def load_names(path):
    """Missing catalogs are bootstrapped; damaged existing files remain errors."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value

def run_phase_names(names, names_ru, all_ids):
    """Дозагрузка недостающих названий en/ru."""
    name_keys = {int(k) for k in names if str(k).isdigit()}
    name_ru_keys = {int(k) for k in names_ru if str(k).isdigit()}
    miss_en = sorted(set(all_ids) - name_keys)
    miss_ru = sorted(set(all_ids) - name_ru_keys)
    print(f"Недостаёт en-имён: {len(miss_en)}, ru-имён: {len(miss_ru)}")

    if miss_en:
        print("  Загружаю en-имена...")
        done = 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = {pool.submit(fetch_name, iid, "en_US"): iid for iid in miss_en}
            for f in as_completed(futs):
                iid = futs[f]
                n = f.result()
                done += 1
                if n:
                    names[str(iid)] = n
                if done % 20 == 0:
                    print(f"    en {done}/{len(miss_en)}")
        save_json(NAMES_FILE, names)
        print(f"  en готово: +{sum(1 for i in miss_en if str(i) in names)}")
    else:
        save_json(NAMES_FILE, names)

    if miss_ru:
        print("  Загружаю ru-имена...")
        done = 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            futs = {pool.submit(fetch_name, iid, "ru_RU"): iid for iid in miss_ru}
            for f in as_completed(futs):
                iid = futs[f]
                n = f.result()
                done += 1
                if n:
                    names_ru[str(iid)] = n
                if done % 20 == 0:
                    print(f"    ru {done}/{len(miss_ru)}")
        save_json(NAMES_RU_FILE, names_ru)
        print(f"  ru готово: +{sum(1 for i in miss_ru if str(i) in names_ru)}")
    else:
        save_json(NAMES_RU_FILE, names_ru)
    return names, names_ru

def run_phase_icons(all_ids):
    """Полная пересборка item_icons.json (чистые имена иконок)."""
    icons = {}
    if os.path.exists(ICONS_FILE):
        with open(ICONS_FILE, encoding="utf-8") as f:
            old = json.load(f)
        # сохраняем только валидные (без URL) значения — их не перекачиваем
        for k, v in old.items():
            v = str(v)
            if '://' not in v and '/' not in v and v:
                icons[k] = v
    have = {int(k) for k in icons if str(k).isdigit()}
    todo = [iid for iid in all_ids if iid not in have]
    print(f"Иконок уже валидных: {len(have)}, нужно загрузить: {len(todo)}")

    if not todo:
        save_json(ICONS_FILE, icons)
        return icons

    lock = threading.Lock()
    done = 0
    ok = 0
    notfound = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = {pool.submit(fetch_icon, iid): iid for iid in todo}
        for f in as_completed(futs):
            iid = futs[f]
            res = f.result()
            with lock:
                done += 1
                if res:
                    icons[str(iid)] = res
                    ok += 1
                else:
                    notfound += 1
                if done % 500 == 0:
                    save_json(ICONS_FILE, icons)
                    el = time.time() - t0
                    print(f"    {done}/{len(todo)} ok={ok} miss={notfound} | {done/el:.1f}/s | {el:.0f}s")
    save_json(ICONS_FILE, icons)
    print(f"Иконки готово: всего {len(icons)}, ok={ok}, не найдено={notfound}")
    return icons

def run_phase_db(names, names_ru):
    """Заполняем items в SQLite из обновлённых JSON."""
    merged = {}
    for k, v in names.items():
        if str(k).isdigit():
            merged[int(k)] = [v, None]
    for k, v in names_ru.items():
        if str(k).isdigit():
            merged.setdefault(int(k), [None, None])[1] = v
    c = sqlite3.connect(DB_FILE)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    n = 0
    for iid, (en, ru) in merged.items():
        c.execute("INSERT OR REPLACE INTO items(id, name_en, name_ru) VALUES(?,?,?)",
                  (iid, en, ru))
        n += 1
    c.commit()
    c.close()
    print(f"DB items: {n} строк upsert-нуто")

def main():
    print("=" * 60)
    print("Обновление имён и иконок после патча (Blizzard API)")
    print("=" * 60)
    all_ids = collect_ids()
    print(f"Предметов в наборе: {len(all_ids)}")

    if not all_ids:
        raise SystemExit("Нет ID предметов: сначала выполните сбор аукциона или задайте ITEM_IDS в .env")
    names = load_names(NAMES_FILE)
    names_ru = load_names(NAMES_RU_FILE)
    for p in (NAMES_FILE, NAMES_RU_FILE, ICONS_FILE):
        backup(p)

    print("\n[1/3] Имена en/ru...")
    names, names_ru = run_phase_names(names, names_ru, all_ids)

    print("\n[2/3] Иконки...")
    icons = run_phase_icons(all_ids)

    print("\n[3/3] SQLite items...")
    run_phase_db(names, names_ru)

    print("\nГотово!")
    print(f"  item_names.json: {len(names)}")
    print(f"  item_names_ru.json: {len(names_ru)}")
    print(f"  item_icons.json: {len(icons)}")

if __name__ == "__main__":
    main()
