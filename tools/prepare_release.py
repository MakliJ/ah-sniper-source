#!/usr/bin/env python3
# prepare_release.py — сборка релизной папки USER-версии AH Sniper
# Запуск: python tools/prepare_release.py   (после: python build_exe.py --user)
#
# Собирает release/ — готовую папку пользователя:
#   AuctionMonitor.exe   — user EXE (PyArmor, без push/web-auth/.env)
#   ilvl_map.json        — BoE ilvl маппинг
#   item_names.json      — имена предметов
#   item_icons.json      — маппинг иконок
#   auction_data.db      — STRIPPED БД (realms/items/item_meta; без снапшотов и админских таблиц)
#   icons/               — иконки предметов (готово к запуску)
#   icons.zip            — тот же набор для апдейта/раздачи
#   README_USER.md       — инструкция
#   INFO.txt             — версия/коммит/дата сборки
#
# В релиз НЕ попадает: .env, supabase_config.json, settings.json, license.json, снапшоты.

import os
import json
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")
RELEASE_VERSION = "v1.1.0"

USER_EXE = os.path.join(ROOT, "dist", "AuctionMonitor.exe")

# Таблицы, которые нужны пользователю (с данными / пустые, но со схемой)
KEEP_WITH_DATA = ("realms", "items", "item_meta")
KEEP_EMPTY = ("meta", "auction_snapshots", "auction_latest", "price_history")
# НЕ копируем: user_deals, user_ignored, users, sqlite_sequence и всё остальное

PLAIN_FILES = (
    "ilvl_map.json",
    "midnight_12_1_items.txt",
    "item_names.json",
    "item_names_ru.json",
    "item_icons.json",
    "README_USER.md",
)


def stripped_db(src_path, dst_path):
    """Копия БД только с таблицами realms/items/item_meta (+ пустые рабочие таблицы).
    Без auction_snapshots (история админа), users и пр."""
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src = sqlite3.connect(src_path)  # только чтение схемы/метаданных
    dst = sqlite3.connect(dst_path)
    try:
        dst.execute("ATTACH DATABASE ? AS src", (src_path,))  # только SELECT из src.*
        keep = set(KEEP_WITH_DATA) | set(KEEP_EMPTY)
        # Таблицы: схема из источника, данные — только для KEEP_WITH_DATA
        for name, sql in src.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
            if name not in keep:
                continue
            dst.execute(sql)
            if name in KEEP_WITH_DATA:
                n = dst.execute(f"INSERT INTO main.{name} SELECT * FROM src.{name}").rowcount
                print(f"     {name}: {n if n >= 0 else dst.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]} строк")
        # Индексы только для разрешённых таблиц
        for (sql,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND tbl_name IN (%s)"
                % ",".join("?" * len(keep)), tuple(keep)):
            dst.execute(sql)
        dst.commit()
        dst.execute("DETACH DATABASE src")
        dst.execute("VACUUM")
        dst.commit()
    finally:
        src.close()
        dst.close()


def zip_icons(icons_dir, zip_path):
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(icons_dir):
            for f in files:
                full = os.path.join(root, f)
                arc = os.path.relpath(full, os.path.dirname(icons_dir))  # icons/xxx внутри zip
                zf.write(full, arc)
                n += 1
    return n


def main():
    os.chdir(ROOT)
    if not os.path.exists(USER_EXE):
        print(f"❌ Нет {USER_EXE} — сначала: python build_exe.py --user")
        sys.exit(1)

    # Validate before replacing release/. Export only explicitly public fields.
    sys.path.insert(0, ROOT)
    from license_manager import _load_public_config
    public_url, public_key = _load_public_config()
    if not public_url or not public_key:
        print("❌ Настройте public_config.json: нужны публичный URL и anon key для USER-лицензии")
        sys.exit(1)

    print("═" * 60)
    print("  prepare_release.py → release/")
    print("═" * 60)

    if os.path.exists(RELEASE):
        shutil.rmtree(RELEASE)
    os.makedirs(RELEASE)

    with open(os.path.join(RELEASE, "public_config.json"), "w", encoding="utf-8") as f:
        json.dump({"supabase_url": public_url, "anon_key": public_key}, f, indent=2)

    # 1. EXE
    shutil.copy2(USER_EXE, os.path.join(RELEASE, "AuctionMonitor.exe"))
    print(f"  ✅ AuctionMonitor.exe ({os.path.getsize(USER_EXE)/1048576:.1f} MB)")

    # 2. Плоские файлы
    missing = [f for f in PLAIN_FILES if not os.path.exists(f)]
    if missing:
        print(f"  ❌ Не хватает обязательных файлов: {', '.join(missing)}")
        sys.exit(1)
    for f in PLAIN_FILES:
        shutil.copy2(f, os.path.join(RELEASE, f))
        print(f"  ✅ {f}")

    # 3. Stripped БД
    src_db = "auction_data.db"
    if os.path.exists(src_db):
        dst_db = os.path.join(RELEASE, "auction_data.db")
        print(f"  … strip {src_db} → auction_data.db (без снапшотов)")
        stripped_db(src_db, dst_db)
        print(f"  ✅ auction_data.db ({os.path.getsize(dst_db)/1048576:.1f} MB)")
    else:
        print(f"  ⚠ {src_db} не найдена — пользователь стартует с пустой БД")

    # 4. Иконки: папка (готово к запуску) + zip (для раздачи)
    icons_dir = "icons"
    if os.path.isdir(icons_dir):
        print("  … копирую icons/ (это долго, ~17k файлов)")
        shutil.copytree(icons_dir, os.path.join(RELEASE, "icons"))
        n = zip_icons(icons_dir, os.path.join(RELEASE, "icons.zip"))
        print(f"  ✅ icons/ + icons.zip ({n} файлов, zip {os.path.getsize(os.path.join(RELEASE,'icons.zip'))/1048576:.1f} MB)")
    else:
        print("  ⚠ icons/ не найдена")

    # 5. INFO.txt (версия для поддержки)
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = "?"
    with open(os.path.join(RELEASE, "INFO.txt"), "w", encoding="utf-8") as f:
        f.write(f"AH Sniper {RELEASE_VERSION} — user build\n")
        f.write(f"Дата сборки: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
        f.write(f"Коммит: {commit}\n")
        f.write("Поддержка: контакты предоставляет владелец сервиса\n")
    print("  ✅ INFO.txt")

    # 6. Проверка: в release/ не должно быть секретов
    forbidden = {".env", "supabase_config.json", "settings.json", "license.json",
                 "tunnel_token.txt", ".flask_secret"}
    bad = [os.path.relpath(os.path.join(root, filename), RELEASE)
           for root, _, files in os.walk(RELEASE) for filename in files
           if filename.lower() in forbidden or filename.lower().startswith("keys_")]
    if bad:
        print(f"  ❌ В release/ найдены секреты: {bad}")
        sys.exit(1)

    total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(RELEASE) for f in fs)
    print(f"\n  📦 release/ готов: {total/1048576:.1f} MB")
    print("  📋 Папку можно запускать как есть или выложить на GitHub Releases:")
    print(f"     gh release create {RELEASE_VERSION} release/AuctionMonitor.exe release/ilvl_map.json "
          "release/public_config.json release/item_names.json release/item_names_ru.json "
          "release/item_icons.json release/auction_data.db "
          "release/icons.zip release/README_USER.md")


if __name__ == "__main__":
    main()
