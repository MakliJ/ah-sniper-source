#!/usr/bin/env python3
# fix_names_via_wowhead.py — добивает имена/иконки для id, которых нет в Blizzard static (404):
#   - tooltip/item/{id} → name/quality/icon
#   - fallback: tooltip/spell/{id} (это ID заклинаний, попавшие в данные аукциона)
import os, sys, json, time, sqlite3, re
import requests, urllib3
urllib3.disable_warnings()

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, 'tools')

NAMES_FILE = "item_names.json"
ICONS_FILE = "item_icons.json"
DB_FILE = "auction_data.db"

names = json.load(open(NAMES_FILE, encoding='utf-8'))
icons = json.load(open(ICONS_FILE, encoding='utf-8'))

c = sqlite3.connect(DB_FILE)
ids = set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM auction_snapshots"))
ids |= set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM item_meta"))
try:
    ids |= set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM auction_latest"))
except Exception:
    pass
c.close()
for line in open('.env', encoding='utf-8'):
    line = line.strip()
    if line.startswith("DEFAULT_ITEM_IDS=") or line.startswith("ITEM_IDS="):
        for tok in line.split("=", 1)[1].split(","):
            tok = tok.strip()
            if tok.isdigit():
                ids.add(int(tok))

def is_bad_name(iid):
    v = names.get(str(iid))
    if v is None:
        return True
    return bool(re.fullmatch(r'item\s+' + str(iid), str(v).strip(), re.I))

todo = sorted({i for i in ids if is_bad_name(i) or str(i) not in icons})
print(f"Нужно обработать (имя и/или иконка): {len(todo)}")

def wowhead(kind, iid):
    r = requests.get(f"https://nether.wowhead.com/tooltip/{kind}/{iid}",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    if r.status_code != 200:
        return None
    try:
        d = json.loads(r.text)
        return d
    except Exception:
        return None

def norm_icon(v):
    if not v:
        return None
    v = str(v)
    if '/' in v:
        v = v.split('/')[-1]
    for ext in ('.jpg', '.png'):
        if v.lower().endswith(ext):
            v = v[:-len(ext)]
            break
    return v.strip() or None

c = sqlite3.connect(DB_FILE)
c.execute("PRAGMA busy_timeout=10000")
fixed_names = fixed_icons = 0
spell_sourced = []
for n, iid in enumerate(todo, 1):
    d = wowhead("item", iid)
    source = "item"
    if not d:
        d = wowhead("spell", iid)
        source = "spell"
    if d and d.get("name"):
        nm = str(d["name"]).strip()
        if is_bad_name(iid):
            names[str(iid)] = nm
            fixed_names += 1
            c.execute("INSERT OR REPLACE INTO items(id, name_en, name_ru) VALUES(?,?,COALESCE((SELECT name_ru FROM items WHERE id=?), NULL))",
                      (iid, nm, iid))
            if source == "spell":
                spell_sourced.append(iid)
        ic = norm_icon(d.get("icon"))
        if ic and str(iid) not in icons:
            icons[str(iid)] = ic
            fixed_icons += 1
        print(f"  {n}/{len(todo)} {iid} [{source}]: {nm!r} icon={ic!r}")
    else:
        print(f"  {n}/{len(todo)} {iid}: ничего не найдено (оставляю как есть)")
    time.sleep(0.25)

c.commit()
c.close()

with open(NAMES_FILE, "w", encoding="utf-8") as f:
    json.dump(names, f, ensure_ascii=False)
with open(ICONS_FILE, "w", encoding="utf-8") as f:
    json.dump(icons, f, ensure_ascii=False)

print(f"\nГотово: имён добавлено={fixed_names}, иконок добавлено={fixed_icons}")
if spell_sourced:
    print("Взяты имена у заклинаний (не предметы в данных):", spell_sourced)
