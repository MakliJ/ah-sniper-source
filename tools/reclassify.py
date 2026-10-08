#!/usr/bin/env python3
"""
reclassify.py — Инструмент переклассификации предметов.
Меняет class_id/subclass_id в item_meta для перегруппировки категорий.

Использование:
  python tools/reclassify.py                    # интерактивный режим
  python tools/reclassify.py --list             # список всех предметов с meta
  python tools/reclassify.py --search "меч"     # поиск по имени
  python tools/reclassify.py --set 12345 --class 4 --subclass 1  # переназначить ID

Изменения применяются в БД и подхватываются EXE после перезапуска.
"""

import os
import sys
import sqlite3
import json
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data

DB = data("auction_data.db")

def _connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

# ── Справочник категорий ──
CLASSES = {
    0: "Consumable",
    1: "Container",
    2: "Weapon",
    3: "Gem",
    4: "Armor",
    5: "Reagent",
    6: "Projectile",
    7: "Trade Goods",
    9: "Recipe",
    11: "Glyph",
    12: "Quest",
    13: "Key",
    15: "Miscellaneous",
    17: "Battle Pet",
    19: "Profession",
    20: "Housing",
}

SUBCLASSES = {
    0: {
        0: "Potion", 1: "Elixir", 2: "Flask", 3: "Scroll", 4: "Food & Drink",
        5: "Item Enhancement", 6: "Bandage", 7: "Other", 8: "Vantus Rune",
        9: "Cooldown", 10: "Tome", 11: "Weapon Enhancement",
    },
    2: {
        0: "One-Handed Axe", 1: "Two-Handed Axe", 2: "Bow", 3: "Gun",
        4: "One-Handed Mace", 5: "Two-Handed Mace", 6: "Polearm",
        7: "One-Handed Sword", 8: "Two-Handed Sword", 9: "Warglaive",
        10: "Staff", 11: "Fishing Pole", 13: "Fist Weapon",
        15: "Dagger", 18: "Crossbow", 19: "Wand", 20: "Fishing Pole",
    },
    4: {
        0: "Miscellaneous", 1: "Cloth", 2: "Leather", 3: "Mail",
        4: "Plate", 5: "Cosmetic", 6: "Shield",
    },
    7: {
        0: "Parts", 1: "Jewelcrafting", 2: "Leatherworking", 3: "Tailoring",
        4: "Engineering", 5: "Blacksmithing", 6: "Cooking", 7: "Alchemy",
        8: "First Aid", 9: "Enchanting", 10: "Fishing", 11: "Herbalism",
        12: "Mining", 13: "Skinning", 14: "Inscription",
    },
}

def list_items(search=None, class_id=None):
    conn = _connect()
    try:
        conn.execute("SELECT expansion_id FROM item_meta LIMIT 1")
        query = "SELECT * FROM item_meta WHERE class_id IS NOT NULL"
    except:
        query = "SELECT * FROM item_meta WHERE class_id IS NOT NULL"
    params = []
    if search:
        query += " AND (item_id LIKE ? OR subclass_name LIKE ? OR class_name LIKE ?)"
        params.extend([f"%{search}%"] * 3)
    if class_id is not None:
        query += " AND class_id = ?"
        params.append(class_id)
    query += " ORDER BY class_id, subclass_id, item_id LIMIT 200"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows

def update_item(item_id, new_class_id, new_subclass_id):
    conn = _connect()
    meta = conn.execute("SELECT * FROM item_meta WHERE item_id = ?", (item_id,)).fetchone()
    if not meta:
        print(f"❌ Item {item_id} not found in item_meta")
        return False

    old_class = meta["class_id"]
    old_subclass = meta["subclass_id"]
    new_class_name = CLASSES.get(new_class_id, f"Class {new_class_id}")
    new_subclass_name = SUBCLASSES.get(new_class_id, {}).get(new_subclass_id, f"Subclass {new_subclass_id}")

    print(f"\nИзменение предмета {item_id}:")
    print(f"  Старое: class={old_class} ({meta['class_name']}), subclass={old_subclass} ({meta['subclass_name']})")
    print(f"  Новое:  class={new_class_id} ({new_class_name}), subclass={new_subclass_id} ({new_subclass_name})")

    if input("Применить? (y/N): ").lower() != 'y':
        print("  Отменено")
        return False

    conn.execute("""
        UPDATE item_meta SET class_id=?, class_name=?, subclass_id=?, subclass_name=?
        WHERE item_id=?
    """, (new_class_id, new_class_name, new_subclass_id, new_subclass_name, item_id))
    conn.commit()
    conn.close()
    print(f"  ✅ Item {item_id} обновлён!")
    return True

def interactive_mode():
    print("\n=== Переклассификация предметов ===")
    print("Команды:")
    print("  search <текст>  — поиск по имени")
    print("  class <id>      — фильтр по class_id")
    print("  list            — показать результаты")
    print("  set <item_id> <class_id> <subclass_id> — изменить категорию")
    print("  stats           — статистика по class_id")
    print("  help            — эта справка")
    print("  exit            — выход\n")

    last_results = []
    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not cmd:
            continue
        parts = cmd.split()
        action = parts[0].lower()

        if action == "exit":
            break
        elif action == "help":
            print("Команды: search, class, list, set, stats, exit")
        elif action == "search":
            q = " ".join(parts[1:]) if len(parts) > 1 else ""
            last_results = list_items(search=q)
            print(f"Найдено: {len(last_results)} предметов")
            for r in last_results[:20]:
                print(f"  {r['item_id']}: class={r['class_id']} ({r['class_name']}) / subclass={r['subclass_id']} ({r['subclass_name']}) — {r.get('quality_type','')}")
            if len(last_results) > 20:
                print(f"  ... и ещё {len(last_results) - 20}")
        elif action == "class":
            cid = int(parts[1]) if len(parts) > 1 else None
            last_results = list_items(class_id=cid)
            print(f"Найдено: {len(last_results)} предметов class={cid}")
            for r in last_results[:20]:
                print(f"  {r['item_id']}: subclass={r['subclass_id']} ({r['subclass_name']})")
        elif action == "list":
            for r in last_results[:50]:
                print(f"  {r['item_id']}: class={r['class_id']} ({r['class_name']}), subclass={r['subclass_id']} ({r['subclass_name']})")
            if len(last_results) > 50:
                print(f"  ... и ещё {len(last_results) - 50}")
        elif action == "set":
            if len(parts) < 4:
                print("Использование: set <item_id> <class_id> <subclass_id>")
                continue
            item_id = int(parts[1])
            new_class = int(parts[2])
            new_subclass = int(parts[3])
            update_item(item_id, new_class, new_subclass)
        elif action == "stats":
            conn = _connect()
            try:
                rows = conn.execute("""
                    SELECT class_id, class_name, COUNT(*) as cnt
                    FROM item_meta WHERE class_id IS NOT NULL
                    GROUP BY class_id ORDER BY cnt DESC
                """).fetchall()
                print("\nСтатистика по категориям:")
                for r in rows:
                    print(f"  [{r['class_id']}] {r['class_name']}: {r['cnt']} предметов")
                conn.close()
            except Exception as e:
                print(f"Ошибка: {e}")
                conn.close()
        else:
            print(f"Неизвестная команда: {action}")

def main():
    if len(sys.argv) > 1:
        # CLI mode
        if "--list" in sys.argv:
            rows = list_items()
            for r in rows:
                print(f"{r['item_id']}\t{r['class_id']}\t{r['class_name']}\t{r['subclass_id']}\t{r['subclass_name']}")
        elif "--search" in sys.argv:
            idx = sys.argv.index("--search")
            q = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
            rows = list_items(search=q)
            for r in rows:
                print(f"{r['item_id']}\t{r['class_name']}\t{r['subclass_name']}")
        elif "--set" in sys.argv:
            idx = sys.argv.index("--set")
            item_id = int(sys.argv[idx + 1])
            new_class = int(sys.argv[sys.argv.index("--class") + 1]) if "--class" in sys.argv else None
            new_subclass = int(sys.argv[sys.argv.index("--subclass") + 1]) if "--subclass" in sys.argv else None
            if new_class is not None and new_subclass is not None:
                update_item(item_id, new_class, new_subclass)
            else:
                print("Использование: --set <item_id> --class <class_id> --subclass <subclass_id>")
        else:
            print("Неизвестный аргумент")
            print("Использование: python tools/reclassify.py [--list|--search <q>|--set <id> --class <c> --subclass <s>]")
    else:
        interactive_mode()

if __name__ == "__main__":
    main()
