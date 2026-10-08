#!/usr/bin/env python3
# enrich_item_meta.py — получает метаданные предметов из Blizzard API
# Извлекает: класс, подкласс, слот, качество, ilvl, уровень, BoE/BoP
# Сохраняет в таблицу item_meta (НЕ трогает существующие таблицы)
# Можно запускать многократно — дозаполнит только отсутствующее

import os
import sys
import sqlite3
import requests
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
if not CLIENT_ID or not CLIENT_SECRET:
    raise ValueError("CLIENT_ID and CLIENT_SECRET must be in .env")

DB_FILE = "auction_data.db"
TOKEN_URL = "https://oauth.battle.net/token"
REGION = "eu"
NAMESPACE_STATIC = "static-eu"
BASE_URL = f"https://{REGION}.api.blizzard.com"

# Сколько потоков для загрузки
MAX_WORKERS = 8
# Пауза между запросами (на поток) — чтобы не упереться в лимит API
REQUEST_DELAY = 0.05


def get_db():
    """Открывает соединение с БД (для потоков — каждое соединение отдельно)."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def get_access_token():
    """Получает OAuth токен Blizzard API."""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    auth = (CLIENT_ID, CLIENT_SECRET)
    resp = session.post(TOKEN_URL, auth=auth,
                         data={"grant_type": "client_credentials"},
                         verify=False, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def init_meta_table():
    """Создаёт таблицу item_meta, если её ещё нет."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS item_meta (
            item_id         INTEGER PRIMARY KEY,
            class_id        INTEGER,
            class_name      TEXT,       -- например: "Оружие", "Броня"
            subclass_id     INTEGER,
            subclass_name   TEXT,       -- например: "Мечи", "Латные доспехи"
            slot_type       TEXT,       -- например: "HEAD", "SHOULDER", "TWOHAND"
            slot_name       TEXT,       -- например: "Голова", "Двуручное"
            quality_type    TEXT,       -- например: "EPIC", "RARE"
            quality_name    TEXT,       -- например: "Эпическое"
            item_level      INTEGER,
            required_level  INTEGER,
            bind_type       INTEGER,    -- 0=none, 1=BoP, 2=BoE, 3=BoA
            is_equippable   INTEGER,    -- 0/1
            max_stack       INTEGER     -- максимальный стак (для расходников)
        )
    """)
    conn.commit()
    conn.close()


def get_items_to_enrich():
    """
    Возвращает список item_id, которых ещё нет в item_meta
    ИЛИ которые были записаны с ошибкой (нет class_id).
    """
    conn = get_db()
    cur = conn.cursor()

    # Все ID предметов, которые у нас есть
    cur.execute("""
        SELECT DISTINCT id FROM items
        UNION
        SELECT DISTINCT item_id FROM auction_snapshots
    """)
    all_ids = {row[0] for row in cur.fetchall()}

    # Те, которые уже нормально обогащены (class_id не NULL)
    cur.execute("SELECT item_id FROM item_meta WHERE class_id IS NOT NULL")
    enriched = {row[0] for row in cur.fetchall()}

    conn.close()

    missing = sorted(all_ids - enriched)
    return missing


def _safe_str(value, default=None):
    """Безопасно извлекает строку из поля API (может быть dict с локалями)."""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get("ru_RU") or value.get("en_US") or str(value)
    return str(value)


def _safe_int(value, default=None):
    """Безопасно извлекает целое число."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def fetch_item_meta(item_id, token, headers):
    """
    Загружает метаданные одного предмета из Blizzard API.
    Возвращает словарь с полями для item_meta или None при ошибке.
    """
    url = f"{BASE_URL}/data/wow/item/{item_id}?namespace={NAMESPACE_STATIC}&locale=ru_RU"
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=15, verify=False)

            if resp.status_code == 404:
                return {
                    'item_id': item_id,
                    'class_id': None, 'class_name': 'Not Found',
                    'subclass_id': None, 'subclass_name': None,
                    'slot_type': None, 'slot_name': None,
                    'quality_type': None, 'quality_name': 'Not Found',
                    'item_level': None, 'required_level': None,
                    'bind_type': 0, 'is_equippable': 0, 'max_stack': None,
                }

            if resp.status_code == 429:
                time.sleep(2)
                continue

            resp.raise_for_status()
            data = resp.json()
            item_class = data.get('item_class', {}) or {}
            subclass = data.get('item_subclass', {}) or {}
            inv_type = data.get('inventory_type', {}) or {}
            quality = data.get('quality', {}) or {}
            preview = data.get('preview_item', {}) or {}

            bind_type = 0
            preview_bind = preview.get('binding', {}) or {}
            if isinstance(preview_bind, dict):
                btype = preview_bind.get('type', '')
                if 'bop' in str(btype).lower(): bind_type = 1
                elif 'boe' in str(btype).lower(): bind_type = 2
                elif 'boa' in str(btype).lower(): bind_type = 3
            if bind_type == 0:
                for spell in data.get('spells', []) or []:
                    desc = str(spell.get('description', '')).lower()
                    if 'становится персональным' in desc or 'soulbound' in desc:
                        bind_type = 1; break

            is_equippable = 1 if preview.get('item_level') else 0

            return {
                'item_id': item_id,
                'class_id': _safe_int(item_class.get('id')),
                'class_name': _safe_str(item_class.get('name')),
                'subclass_id': _safe_int(subclass.get('id')),
                'subclass_name': _safe_str(subclass.get('name')),
                'slot_type': _safe_str(inv_type.get('type')),
                'slot_name': _safe_str(inv_type.get('name')),
                'quality_type': _safe_str(quality.get('type')),
                'quality_name': _safe_str(quality.get('name')),
                'item_level': _safe_int(data.get('level')),
                'required_level': _safe_int(data.get('required_level')),
                'bind_type': bind_type,
                'is_equippable': is_equippable,
                'max_stack': _safe_int(data.get('max_count')),
            }

        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            print(f"  [ERROR] item {item_id}: {e}")
            return None
        except Exception as e:
            print(f"  [PARSE ERROR] item {item_id}: {e}")
            return None
    return None


def store_meta_batch(meta_list):
    """Сохраняет пачку метаданных в БД (INSERT OR REPLACE — безопасно)."""
    if not meta_list:
        return
    conn = get_db()
    try:
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("BEGIN")
        conn.executemany("""
            INSERT OR REPLACE INTO item_meta
                (item_id, class_id, class_name, subclass_id, subclass_name,
                 slot_type, slot_name, quality_type, quality_name,
                 item_level, required_level, bind_type, is_equippable, max_stack)
            VALUES
                (:item_id, :class_id, :class_name, :subclass_id, :subclass_name,
                 :slot_type, :slot_name, :quality_type, :quality_name,
                 :item_level, :required_level, :bind_type, :is_equippable, :max_stack)
        """, meta_list)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\n  [DB ERROR] {e}")
        raise
    finally:
        conn.close()


def main():
    print("=" * 60)
    print("enrich_item_meta.py — обогащение метаданных предметов")
    print("=" * 60)

    # 1. Инициализация
    init_meta_table()

    # 2. Какие предметы нужно обогатить
    missing = get_items_to_enrich()
    if not missing:
        print("\n✓ Все предметы уже обогащены. Нечего делать.")
        return

    print(f"\nПредметов для загрузки: {len(missing)}")
    print(f"Потоков: {MAX_WORKERS} | Пауза: {REQUEST_DELAY}с")
    print()

    # 3. Токен
    print("Получение токена...")
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    # 4. Многопоточная загрузка (пишем в БД только из главного потока)
    completed = 0
    errors = 0
    batch = []
    BATCH_SIZE = 100  # сохраняем пачками

    def worker(item_id):
        time.sleep(REQUEST_DELAY)
        return fetch_item_meta(item_id, token, headers)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker, iid): iid for iid in missing}

        for future in as_completed(futures):
            item_id = futures[future]
            try:
                meta = future.result()
                if meta:
                    batch.append(meta)
                    completed += 1
                    if len(batch) >= BATCH_SIZE:
                        try:
                            store_meta_batch(batch)
                        except Exception as dbe:
                            print(f"\n  [DB SAVE ERROR] losing {len(batch)} items: {dbe}")
                        batch.clear()
                else:
                    errors += 1
            except Exception as e:
                errors += 1
                print(f"\n  [WORKER ERROR] item {item_id}: {e}")

            # Прогресс
            done = completed + errors
            if done % 100 == 0 or done == len(missing):
                pct = done / len(missing) * 100
                print(f"  [{done}/{len(missing)}] {pct:.0f}% — OK: {completed}, ERR: {errors}",
                      end='\r' if done < len(missing) else '\n')

    # 5. Сохраняем остатки
    if batch:
        store_meta_batch(batch)

    # 6. Итог
    print(f"\n{'=' * 60}")
    print(f"Готово! Загружено: {completed} | Ошибок: {errors}")
    print(f"Таблица: item_meta (в БД {DB_FILE})")
    print(f"{'=' * 60}")

    # 7. Покажем статистику по классам
    if completed > 0:
        print("\nРаспределение по классам:")
        conn = get_db()
        rows = conn.execute("""
            SELECT class_name, COUNT(*) AS cnt
            FROM item_meta
            WHERE class_name IS NOT NULL
            GROUP BY class_name
            ORDER BY cnt DESC
            LIMIT 15
        """).fetchall()
        conn.close()
        for row in rows:
            print(f"  {row['class_name']:<30s} {row['cnt']:>6d}")


if __name__ == "__main__":
    main()
