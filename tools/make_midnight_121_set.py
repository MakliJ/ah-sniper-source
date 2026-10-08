#!/usr/bin/env python3
# make_midnight_121_set.py — собирает множество "Midnight 12.1" предметов:
#   1) все отслеживаемые item_id >= 262000 (контент 12.0.7/12.1, см. Lively Songwriter's Quill 262616 и выше)
#   2) все имена, добавленные последним обновлением с Blizzard (diff против .prebak)
# Пишет midnight_12_1_items.txt — айдишки через запятую (для фильтра "Midnight 12.1" и для юзера).

import os, sys, json, sqlite3

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
os.chdir(ROOT)

MIN_ID = 262000  # граница 12.0.7/12.1

# ── отслеживаемые id ──
c = sqlite3.connect('auction_data.db')
ids = set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM auction_snapshots"))
ids |= set(r[0] for r in c.execute("SELECT DISTINCT item_id FROM item_meta"))
c.close()

for line in open('.env', encoding='utf-8'):
    line = line.strip()
    if line.startswith("DEFAULT_ITEM_IDS=") or line.startswith("ITEM_IDS="):
        for tok in line.split("=", 1)[1].split(","):
            tok = tok.strip()
            if tok.isdigit():
                ids.add(int(tok))

# ── свежие имена (diff против prebak) ──
def load(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)

new_en = load('item_names.json')
old_en = load('item_names.json.prebak')
new_ru = load('item_names_ru.json')
old_ru = load('item_names_ru.json.prebak')
diff = {int(k) for k in new_en if str(k).isdigit() and k not in old_en}
diff |= {int(k) for k in new_ru if str(k).isdigit() and k not in old_ru}

# ── итоговое множество ──
final = {i for i in ids if i >= MIN_ID} | diff
final = sorted(final)

with open('midnight_12_1_items.txt', 'w', encoding='utf-8') as f:
    f.write(",".join(str(i) for i in final))

print(f"отслеживаемых id >= {MIN_ID}: {sum(1 for i in ids if i >= MIN_ID)}")
print(f"новых имён (diff): {len(diff)}")
print(f"ИТОГО в midnight_12_1_items.txt: {len(final)}")
print("min:", final[0], "max:", final[-1])
print("первый десяток:", final[:10])
print("последний десяток:", final[-10:])
