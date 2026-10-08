import sqlite3
import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
DB_NAME = "auction_data.db"

def get_access_token():
    url = "https://oauth.battle.net/token"
    data = {"grant_type": "client_credentials"}
    auth = (CLIENT_ID, CLIENT_SECRET)
    response = requests.post(url, data=data, auth=auth)
    return response.json().get("access_token")

def enrich_items():
    token = get_access_token()
    if not token:
        print("Ошибка: Не удалось получить токен доступа.")
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Проверяем наличие колонки
    try:
        cursor.execute("ALTER TABLE items_info ADD COLUMN item_class TEXT")
        print("Колонка item_class добавлена.")
    except sqlite3.OperationalError:
        print("Колонка item_class уже существует, продолжаем...")

    # 2. Выбираем только те ID, у которых еще нет категории
    cursor.execute("SELECT item_id FROM items_info WHERE item_class IS NULL")
    items = cursor.fetchall()

    if not items:
        print("Все предметы уже обновлены!")
        return

    print(f"Найдено {len(items)} предметов для обновления...")

    headers = {"Authorization": f"Bearer {token}"}
    updated_count = 0

    for (item_id,) in items:
        try:
            # Запрос к API Blizzard
            url = f"https://eu.api.blizzard.com/data/wow/item/{item_id}?namespace=static-eu&locale=ru_RU"
            response = requests.get(url, headers=headers)

            if response.status_code == 200:
                data = response.json()

                # Безопасно достаем название класса (категории)
                # Бывает в data['item_class']['name'] (в зависимости от локали)
                item_class_data = data.get('item_class', {})
                item_class_name = "Unknown"

                if isinstance(item_class_data.get('name'), dict):
                    item_class_name = item_class_data['name'].get('ru_RU', item_class_data['name'].get('en_US', 'Unknown'))
                elif isinstance(item_class_data.get('name'), str):
                    item_class_name = item_class_data['name']

                cursor.execute("UPDATE items_info SET item_class = ? WHERE item_id = ?", (item_class_name, item_id))
                conn.commit()
                updated_count += 1
                print(f"ID {item_id}: {item_class_name}")

            elif response.status_code == 404:
                print(f"ID {item_id}: Не найден в API (404)")
                cursor.execute("UPDATE items_info SET item_class = 'Not Found' WHERE item_id = ?", (item_id,))
                conn.commit()

            elif response.status_code == 401: # Токен протух
                token = get_access_token()
                headers = {"Authorization": f"Bearer {token}"}
                print("Обновили токен доступа...")

            time.sleep(0.05) # Небольшая пауза, чтобы не спамить API

        except Exception as e:
            print(f"Ошибка на ID {item_id}: {e}")
            continue

    conn.close()
    print(f"Готово! Обновлено предметов: {updated_count}")

if __name__ == "__main__":
    enrich_items()
