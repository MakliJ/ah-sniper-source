#!/usr/bin/env python3
# fetch_missing_once.py — одноразовая дозагрузка ИМЁН (en+ru) и ИКОНОК для отсутствующих id.
# Blizzard item/media API → fallback wowhead (PTR/удалённые). Пишет JSON + items в БД.
import os, sys, json, sqlite3, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, urllib3
urllib3.disable_warnings()

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, 'tools')
import update_items_patch as u

NAMES_FILE = "item_names.json"
NAMES_RU_FILE = "item_names_ru.json"
ICONS_FILE = "item_icons.json"
DB_FILE = "auction_data.db"

names = json.load(open(NAMES_FILE, encoding='utf-8'))
names_ru = json.load(open(NAMES_RU_FILE, encoding='utf-8'))
icons = json.load(open(ICONS_FILE, encoding='utf-8'))

c = sqlite3.connect(DB_FILE)
ids = set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM auction_snapshots"))
try:
    ids |= set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM auction_latest"))
except Exception:
    pass
ids |= set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM item_meta"))
c.close()
for line in open('.env', encoding='utf-8'):
    line = line.strip()
    if line.startswith("DEFAULT_ITEM_IDS=") or line.startswith("ITEM_IDS="):
        for tok in line.split("=", 1)[1].split(","):
            tok = tok.strip()
            if tok.isdigit():
                ids.add(int(tok))

def bad_name(iid):
    v = names.get(str(iid))
    return v is None or bool(re.fullmatch(r'item\s+' + str(iid), str(v).strip(), re.I))

todo = sorted(i for i in ids if bad_name(i) or str(i) not in icons)
print(f"Отсутствуют (имя и/или иконка): {len(todo)}")

def wowhead(iid):
    try:
        r = requests.get(f"https://nether.wowhead.com/tooltip/item/{iid}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code == 200:
            d = r.json()
            if d.get("name"):
                return str(d["name"]).strip(), u.norm_icon(d.get("icon"))
    except Exception:
        pass
    return None, None

def one(iid):
    en = u.fetch_name(iid, "en_US")
    ru = u.fetch_name(iid, "ru_RU") if str(iid) not in names_ru else None
    ic = u.fetch_icon(iid)
    if not en and not ic:
        wh_n, wh_ic = wowhead(iid)
        en, ic = wh_n, wh_ic
    return iid, en, ru, ic

res = {}
with ThreadPoolExecutor(max_workers=10) as pool:
    futs = {pool.submit(one, iid): iid for iid in todo}
    for f in as_completed(futs):
        iid, en, ru, ic = f.result()
        res[iid] = (en, ru, ic)
        print(f"  {iid}: en={en!r} ru={ru!r} icon={ic!r}")

c = sqlite3.connect(DB_FILE)
c.execute("PRAGMA busy_timeout=10000")
n_add = 0
for iid, (en, ru, ic) in res.items():
    if en:
        names[str(iid)] = en
        n_add += 1
    if ru:
        names_ru[str(iid)] = ru
    if ic:
        icons[str(iid)] = ic
    if en or ru:
        c.execute("INSERT OR REPLACE INTO items(id, name_en, name_ru) VALUES(?,?,?)", (iid, en, ru))
c.commit(); c.close()

u.save_json(NAMES_FILE, names)
u.save_json(NAMES_RU_FILE, names_ru)
u.save_json(ICONS_FILE, icons)
print(f"\nГотово: имён en добавлено={n_add}, иконок={sum(1 for v in res.values() if v[2])}")
still = [i for i, v in res.items() if not v[0]]
print("Остались без имени:", still if still else "нет")
