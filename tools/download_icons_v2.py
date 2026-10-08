#!/usr/bin/env python3
"""download_icons_v2.py — Быстрая загрузка иконок предметов
Использует item_icons.json для получения имён иконок и качает с CDN.
"""
import os, json, time, sys, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data
ICONS_DIR = data("icons")
ICON_MAP_FILE = data("item_icons.json")
MAX_WORKERS = 10
os.makedirs(ICONS_DIR, exist_ok=True)

def extract_icon_name(val):
    if not val:
        return None
    val = str(val)
    if '/' in val:
        part = val.rstrip('.jpg').split('/')[-1]
        if part and len(part) > 5:
            return part
    return val if len(val) > 5 else None

def load_icon_map():
    if not os.path.exists(ICON_MAP_FILE):
        print(f"Файл {ICON_MAP_FILE} не найден")
        return {}
    with open(ICON_MAP_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    result = {}
    for k, v in raw.items():
        try:
            item_id = int(k)
            icon_name = extract_icon_name(v)
            if icon_name:
                result[item_id] = icon_name
        except:
            pass
    return result

def get_existing():
    existing = set()
    if os.path.exists(ICONS_DIR):
        for f in os.listdir(ICONS_DIR):
            if f.endswith(".jpg"):
                try:
                    existing.add(int(f[:-4]))
                except:
                    pass
    return existing

def download_one(item_id, icon_name):
    url = f"https://render.worldofwarcraft.com/eu/icons/56/{icon_name}.jpg"
    path = os.path.join(ICONS_DIR, f"{item_id}.jpg")
    try:
        r = requests.get(url, timeout=15, verify=False)
        if r.status_code == 200:
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except:
        pass
    return False

def main():
    print("=" * 50)
    print("WoW Item Icon Downloader v2")
    print("=" * 50)

    icon_map = load_icon_map()
    print(f"Загружено {len(icon_map)} имён иконок из item_icons.json")

    existing = get_existing()
    print(f"Уже скачано: {len(existing)} иконок в папке icons/")

    to_download = [(iid, name) for iid, name in icon_map.items() if iid not in existing]
    print(f"Нужно скачать: {len(to_download)}")

    if not to_download:
        print("✅ Всё уже скачано!")
        return

    print(f"\nСкачиваю {len(to_download)} иконок ({MAX_WORKERS} потоков)...")
    ok = fail = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut_map = {pool.submit(download_one, iid, name): iid for iid, name in to_download}
        done = 0
        for f in as_completed(fut_map):
            done += 1
            if f.result():
                ok += 1
            else:
                fail += 1
            if done % 100 == 0 or done == len(to_download):
                elapsed = time.time() - start
                print(f"  {done}/{len(to_download)} | OK:{ok} ERR:{fail} | {done/elapsed:.1f} иконок/с")

    elapsed = time.time() - start
    print(f"\n✅ Готово за {elapsed:.1f}с")
    print(f"  Скачано: {ok}")
    print(f"  Ошибок: {fail}")
    print(f"  Всего в папке: {len(get_existing())}")

if __name__ == "__main__":
    main()
