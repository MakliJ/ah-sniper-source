#!/usr/bin/env python3
"""Однократный скрипт: дополняет английские названия предметов в SQLite."""

import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
if not CLIENT_ID or not CLIENT_SECRET:
    raise ValueError("CLIENT_ID и CLIENT_SECRET должны быть в .env")

DB_FILE = "auction_data.db"
REGION = "eu"
NAMESPACE_STATIC = "static-eu"
BASE_URL = f"https://{REGION}.api.blizzard.com"

def get_token():
    resp = requests.post("https://oauth.battle.net/token",
                         auth=(CLIENT_ID, CLIENT_SECRET),
                         data={"grant_type": "client_credentials"})
    resp.raise_for_status()
    return resp.json()["access_token"]

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def fetch_and_store(item_id, token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{BASE_URL}/data/wow/item/{item_id}?namespace={NAMESPACE_STATIC}&locale=en_US"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            name_field = data.get("name")
            if isinstance(name_field, dict):
                name_en = name_field.get("en_US")
            else:
                name_en = name_field
            if name_en:
                conn = get_db()
                conn.execute("UPDATE items SET name_en = ? WHERE id = ?", (name_en, item_id))
                conn.commit()
                conn.close()
                return True
    except Exception as e:
        print(f"Error item {item_id}: {e}")
    return False

def main():
    token = get_token()
    conn = get_db()
    cur = conn.cursor()
    # Выбираем предметы, у которых нет английского названия
    cur.execute("SELECT id FROM items WHERE name_en IS NULL OR name_en = ''")
    missing = [row[0] for row in cur.fetchall()]
    conn.close()

    if not missing:
        print("Все предметы уже имеют английское название.")
        return

    print(f"Дозагружаем английские названия для {len(missing)} предметов...")
    completed = 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_and_store, item_id, token): item_id for item_id in missing}
        for future in futures:
            if future.result():
                completed += 1
                if completed % 100 == 0:
                    print(f"   {completed}/{len(missing)}", end='\r')
    print(f"\nГотово! Загружено {completed} названий.")

if __name__ == "__main__":
    main()
