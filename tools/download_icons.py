#!/usr/bin/env python3
"""download_icons.py — скачивает все иконки предметов в локальную папку icons/
Запуск: python download_icons.py
После скачивания в item_browser.py будут браться локальные иконки из папки icons/
"""
import os, sys, json, time, sqlite3, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data
ICONS_DIR = data("icons")
DB_FILE = data("auction_data.db")
MAX_WORKERS = 20

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_all_item_ids():
    """Get all unique item IDs from database"""
    conn = get_db()
    ids = set()
    for table in ["items", "auction_snapshots", "auction_latest"]:
        try:
            if table == "items":
                rows = conn.execute(f"SELECT id FROM {table}").fetchall()
                ids.update(r[0] for r in rows)
            else:
                rows = conn.execute(f"SELECT DISTINCT item_id FROM {table}").fetchall()
                ids.update(r[0] for r in rows)
        except:
            pass
    conn.close()
    return sorted(ids)

def get_existing_icons():
    """Get already downloaded icons"""
    existing = set()
    if os.path.exists(ICONS_DIR):
        for f in os.listdir(ICONS_DIR):
            if f.endswith(".jpg"):
                existing.add(int(f[:-4]))
    return existing

def get_icon_name(item_id, token, headers):
    """Get icon name from Blizzard API"""
    url = f"https://eu.api.blizzard.com/data/wow/item/{item_id}?namespace=static-eu&locale=ru_RU"
    try:
        resp = requests.get(url, headers=headers, timeout=10, verify=False)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        # Try to find media asset
        media_url = f"https://eu.api.blizzard.com/data/wow/media/item/{item_id}?namespace=static-eu&locale=ru_RU"
        media_resp = requests.get(media_url, headers=headers, timeout=10, verify=False)
        if media_resp.status_code == 200:
            media_data = media_resp.json()
            for asset in media_data.get("assets", []):
                if asset.get("key") == "icon":
                    icon_value = asset.get("value", "")
                    if icon_value:
                        if icon_value.startswith("http"):
                            icon_name = icon_value.split('/')[-1]
                            if icon_name.endswith('.jpg'):
                                icon_name = icon_name[:-4]
                        else:
                            icon_name = icon_value
                        return icon_name
        return None
    except:
        return None

def download_icon(item_id, icon_name):
    """Download icon file from CDN"""
    url = f"https://render.worldofwarcraft.com/eu/icons/56/{icon_name}.jpg"
    path = os.path.join(ICONS_DIR, f"{item_id}.jpg")
    try:
        resp = requests.get(url, timeout=10, verify=False)
        if resp.status_code == 200:
            with open(path, "wb") as f:
                f.write(resp.content)
            return True
    except:
        pass
    return False

def main():
    print("=" * 50)
    print("WoW Item Icon Downloader")
    print("=" * 50)

    os.makedirs(ICONS_DIR, exist_ok=True)

    # Get all item IDs
    all_ids = get_all_item_ids()
    existing = get_existing_icons()
    missing = [iid for iid in all_ids if iid not in existing]

    print(f"Total items in DB: {len(all_ids)}")
    print(f"Already downloaded: {len(existing)}")
    print(f"Need to download: {len(missing)}")

    if not missing:
        print("All icons already downloaded!")
        return

    # Get Blizzard API token
    cid = os.getenv("CLIENT_ID")
    csec = os.getenv("CLIENT_SECRET")
    if not cid or not csec:
        print("ERROR: CLIENT_ID and CLIENT_SECRET must be in .env file")
        return

    print("Getting OAuth token...")
    resp = requests.post("https://oauth.battle.net/token",
        auth=(cid, csec), data={"grant_type": "client_credentials"}, verify=False)
    resp.raise_for_status()
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Phase 1: Get icon names
    print("\nPhase 1: Getting icon names from Blizzard API...")
    icon_map = {}
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for item_id in missing:
            future = executor.submit(get_icon_name, item_id, token, headers)
            futures[future] = item_id
            time.sleep(0.05)

        for future in as_completed(futures):
            item_id = futures[future]
            icon_name = future.result()
            if icon_name:
                icon_map[item_id] = icon_name
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(missing)} items processed ({len(icon_map)} icons found)")

    print(f"Found {len(icon_map)} icon names out of {len(missing)} items")

    # Phase 2: Download icon images
    print("\nPhase 2: Downloading icon images...")
    downloaded = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for item_id, icon_name in icon_map.items():
            future = executor.submit(download_icon, item_id, icon_name)
            futures[future] = item_id
            time.sleep(0.02)

        for future in as_completed(futures):
            if future.result():
                downloaded += 1
            else:
                errors += 1
            if (downloaded + errors) % 500 == 0:
                print(f"  {downloaded + errors}/{len(icon_map)} downloaded ({downloaded} ok, {errors} errors)")

    print(f"\nDone! Downloaded: {downloaded}, Errors: {errors}")
    print(f"Total icons in folder: {len(get_existing_icons())}")
    print(f"Icons directory: {ICONS_DIR}")

if __name__ == "__main__":
    main()
