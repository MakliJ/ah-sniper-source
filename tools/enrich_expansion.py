#!/usr/bin/env python3
"""enrich_expansion.py — Добавляет expansion_id в item_meta на основе ID предмета
Использует item ID ranges (т.к. Blizzard API не предоставляет expansion напрямую)
"""
import os, json, time, sqlite3, sys
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data
DB_FILE = data("auction_data.db")

# Item ID ranges per expansion (approximate, based on WoW data)
# Format: (min_id, max_id, expansion_id, expansion_name)
EXPANSIONS = [
    (1,       25817,   0, "Classic"),
    (25818,   35573,   1, "The Burning Crusade"),
    (35574,   52251,   2, "Wrath of the Lich King"),
    (52252,   79012,   3, "Cataclysm"),
    (79013,   106000,  4, "Mists of Pandaria"),
    (106001,  129392,  5, "Warlords of Draenor"),
    (129393,  154881,  6, "Legion"),
    (154882,  175354,  7, "Battle for Azeroth"),
    (175355,  190955,  8, "Shadowlands"),
    (190956,  210725,  9, "Dragonflight"),
    (210726,  229000,  10, "The War Within"),
    (229001,  9999999, 11, "Midnight"),
]

def get_expansion(item_id):
    for min_id, max_id, exp_id, exp_name in EXPANSIONS:
        if min_id <= item_id <= max_id:
            return exp_id, exp_name
    return -1, "Unknown"

def main():
    force = "--force" in sys.argv

    print("=" * 55)
    print("  Item Expansion Enricher")
    print("=" * 55)
    if force:
        print("  FORCE MODE: перезаписываю все expansion_id")

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Check if expansion_id column exists
    try:
        c.execute("SELECT expansion_id FROM item_meta LIMIT 1")
        has_column = True
    except:
        has_column = False

    if not has_column:
        print("Добавляю колонку expansion_id в item_meta...")
        c.execute("ALTER TABLE item_meta ADD COLUMN expansion_id INTEGER DEFAULT -1")
        conn.commit()
        print("✅ Колонка добавлена")

    if force:
        c.execute("SELECT item_id FROM item_meta")
    else:
        c.execute("SELECT item_id FROM item_meta WHERE expansion_id IS NULL OR expansion_id = -1")
    rows = c.fetchall()
    total = len(rows)
    print(f"Нужно обработать: {total} предметов")

    if total == 0:
        c.execute("SELECT COUNT(*) FROM item_meta")
        all_count = c.fetchone()[0]
        print(f"Все {all_count} предметов уже имеют expansion_id")
        conn.close()
        return

    # Update in batches
    batch_size = 1000
    updated = 0
    start = time.time()

    # Group by expansion for stats
    exp_counts = {}

    for i in range(0, total, batch_size):
        batch = rows[i:i+batch_size]
        for row in batch:
            item_id = row["item_id"]
            exp_id, exp_name = get_expansion(item_id)
            c.execute("UPDATE item_meta SET expansion_id = ? WHERE item_id = ?", (exp_id, item_id))
            exp_counts[exp_name] = exp_counts.get(exp_name, 0) + 1
            updated += 1

        conn.commit()

        if updated % 2000 == 0 or updated == total:
            elapsed = time.time() - start
            rate = updated / elapsed if elapsed > 0 else 0
            print(f"  {updated}/{total} | {rate:.0f}/с")

    elapsed = time.time() - start
    conn.close()

    print(f"\n✅ Готово за {elapsed:.1f}с")
    print(f"  Обновлено: {updated}")
    print(f"\nРаспределение по дополнениям:")
    for exp_name in ["Classic", "The Burning Crusade", "Wrath of the Lich King",
                     "Cataclysm", "Mists of Pandaria", "Warlords of Draenor",
                     "Legion", "Battle for Azeroth", "Shadowlands",
                     "Dragonflight", "The War Within", "Midnight", "Unknown"]:
        if exp_name in exp_counts:
            print(f"  {exp_name}: {exp_counts[exp_name]} предметов")

if __name__ == "__main__":
    main()
