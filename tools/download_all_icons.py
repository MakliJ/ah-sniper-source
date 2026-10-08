#!/usr/bin/env python3
"""download_all_icons.py — Скачивает иконки ВСЕХ предметов из БД
1. Получает OAuth токен Blizzard
2. Для каждого предмета узнаёт имя иконки через API
3. Скачивает JPG с CDN в папку icons/
4. Сохраняет прогресс, чтобы можно было прервать и продолжить
"""
import os, json, time, sqlite3, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data
ICONS_DIR = data("icons")
PROGRESS_FILE = data("icon_download_progress.json")
os.makedirs(ICONS_DIR, exist_ok=True)

# ── База данных ──
def get_db():
    conn = sqlite3.connect(data("auction_data.db"))
    conn.row_factory = sqlite3.Row
    return conn

def get_all_item_ids():
    conn = get_db()
    ids = set()
    for table in ["items", "auction_snapshots", "auction_latest"]:
        try:
            if table == "items":
                rows = conn.execute("SELECT id FROM items").fetchall()
                ids.update(r[0] for r in rows)
            else:
                rows = conn.execute(f"SELECT DISTINCT item_id FROM {table}").fetchall()
                ids.update(r[0] for r in rows)
        except:
            pass
    conn.close()
    return sorted(ids)

# ── Иконки ──
def get_existing():
    existing = {}
    if os.path.exists(ICONS_DIR):
        for f in os.listdir(ICONS_DIR):
            if f.endswith(".jpg"):
                try:
                    existing[int(f[:-4])] = True
                except:
                    pass
    return existing

def load_progress():
    """Загружаем прогресс: {item_id: icon_name} уже обработанные"""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    return {}

def save_progress(icon_map):
    """Сохраняем имена иконок, которые уже нашли"""
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(icon_map, f)
    os.replace(tmp, PROGRESS_FILE)

def get_icon_name(item_id, token, headers):
    """Получить имя иконки через Blizzard Media API"""
    url = f"https://eu.api.blizzard.com/data/wow/media/item/{item_id}?namespace=static-eu&locale=ru_RU"
    try:
        r = requests.get(url, headers=headers, timeout=10, verify=False)
        if r.status_code == 200:
            data = r.json()
            for asset in data.get("assets", []):
                if asset.get("key") == "icon":
                    val = asset.get("value", "")
                    if val:
                        name = val.rstrip('.jpg').split('/')[-1]
                        if name and len(name) > 5:
                            return name
        return None
    except:
        return None

def download_icon(item_id, icon_name):
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

# ── Основной процесс ──
def main():
    print("=" * 55)
    print("  WoW All Items Icon Downloader")
    print("=" * 55)

    # Получаем все ID предметов
    all_ids = get_all_item_ids()
    print(f"\nВсего предметов в БД: {len(all_ids)}")

    # Уже скачанные
    existing_icons = get_existing()
    print(f"Уже скачано иконок: {len(existing_icons)}")

    # Прогресс (уже известные имена иконок)
    progress = load_progress()
    print(f"Уже известно имён иконок: {len(progress)}")

    # Определяем, что нужно сделать
    need_phase1 = []  # нужно узнать имя иконки
    need_phase2 = []  # имя известно, нужно скачать файл

    for iid in all_ids:
        if iid in existing_icons:
            continue  # уже скачано
        if iid in progress and progress[iid]:
            need_phase2.append((iid, progress[iid]))
        else:
            need_phase1.append(iid)

    print(f"Нужно узнать имена иконок: {len(need_phase1)}")
    print(f"Нужно скачать файлы: {len(need_phase2)}")

    if not need_phase1 and not need_phase2:
        print("\n✅ Все иконки уже скачаны!")
        return

    # OAuth токен
    cid = os.getenv("CLIENT_ID")
    csec = os.getenv("CLIENT_SECRET")
    if not cid or not csec:
        print("❌ CLIENT_ID и CLIENT_SECRET не найдены в .env")
        return

    print("\nПолучаю OAuth токен...")
    resp = requests.post("https://oauth.battle.net/token",
        auth=(cid, csec), data={"grant_type": "client_credentials"}, verify=False)
    resp.raise_for_status()
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("✅ Токен получен")

    # ── Phase 1: узнаём имена иконок ──
    if need_phase1:
        print(f"\n{'='*55}")
        print(f"  Phase 1: Получаю имена иконок ({len(need_phase1)} шт)")
        print(f"{'='*55}")
        start = time.time()
        done = 0
        found = 0

        with ThreadPoolExecutor(max_workers=15) as pool:
            fut_map = {pool.submit(get_icon_name, iid, token, headers): iid for iid in need_phase1}
            for f in as_completed(fut_map):
                iid = fut_map[f]
                name = f.result()
                done += 1
                if name:
                    progress[iid] = name
                    found += 1
                    need_phase2.append((iid, name))
                if done % 500 == 0:
                    elapsed = time.time() - start
                    print(f"  {done}/{len(need_phase1)} | найдено: {found} | {done/elapsed:.0f}/с")
                    save_progress(progress)

        save_progress(progress)
        elapsed = time.time() - start
        print(f"\n  Phase 1 завершена за {elapsed:.0f}с")
        print(f"  Найдено имён: {found} из {len(need_phase1)}")

    # ── Phase 2: скачиваем файлы ──
    if need_phase2:
        print(f"\n{'='*55}")
        print(f"  Phase 2: Скачиваю {len(need_phase2)} иконок с CDN")
        print(f"{'='*55}")
        start = time.time()
        ok = fail = 0

        with ThreadPoolExecutor(max_workers=15) as pool:
            fut_map = {pool.submit(download_icon, iid, name): iid for iid, name in need_phase2}
            done = 0
            for f in as_completed(fut_map):
                done += 1
                if f.result():
                    ok += 1
                else:
                    fail += 1
                if done % 500 == 0 or done == len(need_phase2):
                    elapsed = time.time() - start
                    print(f"  {done}/{len(need_phase2)} | OK:{ok} ERR:{fail} | {done/elapsed:.0f}/с")

        elapsed = time.time() - start
        final_count = len(get_existing())
        print(f"\n{'='*55}")
        print(f"  ✅ Готово!")
        print(f"  Скачано файлов: {ok}")
        print(f"  Ошибок: {fail}")
        print(f"  Всего иконок в папке: {final_count}")
        print(f"  Время: {elapsed:.0f}с")
        print(f"{'='*55}")

if __name__ == "__main__":
    main()
