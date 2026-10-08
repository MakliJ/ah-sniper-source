import os
import secrets
import json
import glob
import re
import requests
from io import BytesIO
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET") or secrets.token_urlsafe(64)

DATA_DIR = "auction_prices"
REALMS_FILE = "realms.json"
ITEM_NAMES_FILE = "item_names.json"
ITEM_ICONS_FILE = "item_icons.json"
HISTORY_FILE = "history.json"
USERS_FILE = "users.json"
USER_DEALS_FILE = "user_deals.json"
USER_IGNORED_FILE = "user_ignored.json"

# Кеш для иконок (в памяти)
icon_cache = {}

def load_json(filename, default=None):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_undermine_url(realm_id, item_id, realm_names):
    realm_name = realm_names.get(str(realm_id), "")
    if not realm_name:
        return "#"
    if ',' in realm_name:
        realm_name = realm_name.split(',')[0].strip()
    clean = re.sub(r'[^a-zA-Z0-9 ]', '', realm_name)
    clean = clean.lower().replace(' ', '-')
    return f"https://undermine.exchange/#eu-{clean}/{item_id}"

def format_price(copper):
    gold = copper // 10000
    silver = (copper % 10000) // 100
    return f"{gold}.{silver:02d}"

realm_names = load_json(REALMS_FILE)
item_names = load_json(ITEM_NAMES_FILE)
item_icons = load_json(ITEM_ICONS_FILE, {})
users = load_json(USERS_FILE, [])
user_deals = load_json(USER_DEALS_FILE, [])
user_ignored = load_json(USER_IGNORED_FILE, [])

if not users:
    admin_hash = generate_password_hash("admin")
    users.append({"id": 1, "username": "admin", "password": admin_hash, "role": "admin"})
    save_json(USERS_FILE, users)
    print("✓ Создан пользователь admin с паролем admin")

ACTIVE_REALMS = [3391, 1403, 1084, 1305, 3691, 3674, 1390, 580, 1615, 1602, 581]
SALE_REALMS = [3391, 1403, 1084, 1305, 3691, 3674, 1390, 3702, 1329, 1615, 1602, 581]

def get_item_icon(item_id, token):
    # Для прокси-эндпоинта не нужно хранить URL, просто возвращаем путь
    return f"/icon/{item_id}"

def get_latest_data_file():
    files = glob.glob(os.path.join(DATA_DIR, "auction_prices_*.json"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def prepare_items_info(data, token):
    items_info = []
    for item_id, realm_prices in data.items():
        if not realm_prices:
            continue
        sorted_realms = sorted(realm_prices.items(), key=lambda x: x[1])
        cheapest_realm, cheapest_price = sorted_realms[0]
        top10 = sorted_realms[:10]
        top10_avg = sum(price for _, price in top10) / len(top10) if top10 else None
        top10_avg_formatted = format_price(round(top10_avg)) if top10_avg is not None else "—"

        if top10_avg and top10_avg > 0:
            discount_percent = (1 - cheapest_price / top10_avg) * 100
            discount_str = f"{discount_percent:.1f}%"
        else:
            discount_str = "—"

        sale_realm_id = None
        sale_price = None
        for cr_id, price in realm_prices.items():
            if int(cr_id) in SALE_REALMS:
                if sale_price is None or price > sale_price:
                    sale_price = price
                    sale_realm_id = cr_id

        item_name = item_names.get(str(item_id), str(item_id))
        cheapest_name = realm_names.get(str(cheapest_realm), f"Unknown({cheapest_realm})")
        top3 = sorted_realms[:3]
        top3_str = ", ".join([f"{realm_names.get(str(r), r)} ({format_price(p)})" for r, p in top3])

        sale_name = realm_names.get(str(sale_realm_id), f"Unknown({sale_realm_id})") if sale_realm_id else "—"
        undermine_url = get_undermine_url(cheapest_realm, item_id, realm_names)
        icon_url = get_item_icon(item_id, token)

        items_info.append({
            'item_id': item_id,
            'item_name': item_name,
            'cheapest_realm': cheapest_name,
            'cheapest_realm_id': cheapest_realm,
            'cheapest_price': format_price(cheapest_price),
            'cheapest_price_raw': cheapest_price,
            'sale_realm': sale_name,
            'sale_price': format_price(sale_price) if sale_price else "—",
            'discount': discount_str,
            'top3': top3_str,
            'top10_avg': top10_avg_formatted,
            'top10_avg_raw': top10_avg,
            'undermine_url': undermine_url,
            'icon_url': icon_url
        })

    items_info.sort(key=lambda x: float(x['discount'].replace('%', '')) if x['discount'] != '—' else -1, reverse=True)
    return items_info

def get_current_items(token):
    latest = get_latest_data_file()
    if not latest:
        return None, None
    with open(latest, "r", encoding="utf-8") as f:
        full_data = json.load(f)
    last_modified = full_data.get("last_modified", "неизвестно")
    data = full_data.get("data")
    if data is None:
        data = full_data
        if "last_modified" in data:
            data.pop("last_modified", None)
    items = prepare_items_info(data, token)
    return items, last_modified

def get_raw_data():
    latest = get_latest_data_file()
    if not latest:
        return None
    with open(latest, "r", encoding="utf-8") as f:
        full_data = json.load(f)
    data = full_data.get("data")
    if data is None:
        data = full_data
        if "last_modified" in data:
            data.pop("last_modified", None)
    return data

def compute_stats_for_item(item_id, realm_prices):
    if not realm_prices:
        return "—", "—"
    sorted_realms = sorted(realm_prices.items(), key=lambda x: x[1])
    cheapest_price = sorted_realms[0][1]
    top10 = sorted_realms[:10]
    top10_avg = sum(price for _, price in top10) / len(top10) if top10 else None
    top10_avg_formatted = format_price(round(top10_avg)) if top10_avg is not None else "—"
    if top10_avg and top10_avg > 0:
        discount_percent = (1 - cheapest_price / top10_avg) * 100
        discount_str = f"{discount_percent:.1f}%"
    else:
        discount_str = "—"
    return discount_str, top10_avg_formatted

def get_last_modified():
    latest = get_latest_data_file()
    if not latest:
        return None
    with open(latest, "r", encoding="utf-8") as f:
        full_data = json.load(f)
    return full_data.get("last_modified", "неизвестно")

def get_user_deals(user_id):
    deals = load_json(USER_DEALS_FILE, [])
    return [d for d in deals if d.get('user_id') == user_id and
            'item_id' in d and 'realm_id' in d and 'price' in d]

def get_user_ignored(user_id):
    ignored = load_json(USER_IGNORED_FILE, [])
    return [d for d in ignored if d.get('user_id') == user_id and
            'item_id' in d and 'realm_id' in d and 'price' in d]

def toggle_deal(user_id, item_id, realm_id, price):
    deals = load_json(USER_DEALS_FILE, [])
    for deal in deals:
        if (deal.get('user_id') == user_id and
            deal.get('item_id') == item_id and
            deal.get('realm_id') == realm_id and
            deal.get('price') == price):
            deals.remove(deal)
            save_json(USER_DEALS_FILE, deals)
            return False
    deals.append({
        'user_id': user_id,
        'item_id': item_id,
        'realm_id': realm_id,
        'price': price,
        'added_at': datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    save_json(USER_DEALS_FILE, deals)
    return True

def toggle_ignore(user_id, item_id, realm_id, price):
    ignored = load_json(USER_IGNORED_FILE, [])
    for entry in ignored:
        if (entry.get('user_id') == user_id and
            entry.get('item_id') == item_id and
            entry.get('realm_id') == realm_id and
            entry.get('price') == price):
            ignored.remove(entry)
            save_json(USER_IGNORED_FILE, ignored)
            return False
    ignored.append({
        'user_id': user_id,
        'item_id': item_id,
        'realm_id': realm_id,
        'price': price,
        'ignored_at': datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    save_json(USER_IGNORED_FILE, ignored)
    return True

@app.route('/')
def index():
    return render_template_string(HTML_MAIN, session=session)

@app.route('/my_deals')
def my_deals():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template_string(HTML_MY_DEALS, session=session)

@app.route('/old_deals')
def old_deals():
    return render_template_string(HTML_OLD_DEALS, session=session)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = next((u for u in users if u['username'] == username), None)
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('index'))
        else:
            return render_template_string(HTML_LOGIN, error="Неверный логин или пароль", session=session)
    return render_template_string(HTML_LOGIN, error=None, session=session)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if any(u['username'] == username for u in users):
            return render_template_string(HTML_REGISTER, error="Пользователь уже существует", session=session)
        new_id = max([u['id'] for u in users]) + 1 if users else 1
        users.append({
            'id': new_id,
            'username': username,
            'password': generate_password_hash(password),
            'role': 'user'
        })
        save_json(USERS_FILE, users)
        return redirect(url_for('login'))
    return render_template_string(HTML_REGISTER, error=None, session=session)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/icon/<int:item_id>')
def get_item_icon_route(item_id):
    """Прокси-эндпоинт для иконок. Скачивает изображение с CDN и отдаёт его."""
    item_id_str = str(item_id)
    if item_id_str in icon_cache:
        return send_file(BytesIO(icon_cache[item_id_str]), mimetype='image/jpeg')

    token = getattr(app, 'blizzard_token', None)
    if not token:
        return abort(404)

    # Получаем имя иконки через API
    url = f"https://eu.api.blizzard.com/data/wow/media/item/{item_id}?namespace=static-eu&locale=ru_RU"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        for asset in data.get("assets", []):
            if asset.get("key") == "icon":
                icon_value = asset.get("value")
                if icon_value:
                    if icon_value.startswith("http"):
                        icon_name = icon_value.split('/')[-1]
                        if icon_name.endswith('.jpg'):
                            icon_name = icon_name[:-4]
                    else:
                        icon_name = icon_value
                    cdn_url = f"https://render.worldofwarcraft.com/eu/icons/56/{icon_name}.jpg"
                    img_resp = requests.get(cdn_url, timeout=10)
                    img_resp.raise_for_status()
                    icon_cache[item_id_str] = img_resp.content
                    return send_file(BytesIO(img_resp.content), mimetype='image/jpeg')
    except Exception as e:
        print(f"Ошибка при получении иконки для {item_id}: {e}")
    abort(404)

@app.route('/api/data')
def api_data():
    token = getattr(app, 'blizzard_token', None)
    items, last_modified = get_current_items(token)
    if items is None:
        return jsonify({'error': 'No data'}), 404
    user_id = session.get('user_id')
    user_deals_list = get_user_deals(user_id) if user_id else []
    user_ignored_list = get_user_ignored(user_id) if user_id else []
    deals_set = {(d['item_id'], d['realm_id'], d['price']) for d in user_deals_list}
    ignored_set = {(d['item_id'], d['realm_id'], d['price']) for d in user_ignored_list}
    for item in items:
        item_id = int(item['item_id'])
        realm_id = int(item['cheapest_realm_id'])
        price = int(item['cheapest_price_raw'])
        item['is_deal'] = (item_id, realm_id, price) in deals_set
        item['is_ignored'] = (item_id, realm_id, price) in ignored_set
    return jsonify({
        'last_modified': last_modified,
        'items': items
    })

@app.route('/api/last_modified')
def api_last_modified():
    last_modified = get_last_modified()
    if last_modified is None:
        return jsonify({'error': 'No data'}), 404
    return jsonify({'last_modified': last_modified})

@app.route('/api/deals')
def api_deals():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    user_id = session['user_id']
    user_deals_list = get_user_deals(user_id)
    raw_data = get_raw_data()
    if raw_data is None:
        result = []
        for deal in user_deals_list:
            result.append({
                'item_id': deal['item_id'],
                'item_name': f"Предмет {deal['item_id']} (данные отсутствуют)",
                'cheapest_realm': deal['realm_id'],
                'cheapest_price': format_price(deal['price']),
                'discount': '—',
                'top10_avg': '—',
                'is_deal': True,
                'undermine_url': '#',
                'added_at': deal.get('added_at', 'неизвестно')
            })
        result.sort(key=lambda x: x['added_at'], reverse=True)
        return jsonify({'items': result})

    result = []
    for deal in user_deals_list:
        item_id = deal['item_id']
        realm_id = deal['realm_id']
        price_orig = deal['price']
        realm_prices = raw_data.get(str(item_id))
        if realm_prices:
            discount_str, top10_avg_formatted = compute_stats_for_item(item_id, realm_prices)
            sorted_realms = sorted(realm_prices.items(), key=lambda x: x[1])
            current_cheapest_realm = sorted_realms[0][0]
            current_cheapest_price = sorted_realms[0][1]
            item_name = item_names.get(str(item_id), str(item_id))
            cheapest_name = realm_names.get(str(current_cheapest_realm), f"Unknown({current_cheapest_realm})")
            undermine_url = get_undermine_url(current_cheapest_realm, item_id, realm_names)
            if current_cheapest_realm == realm_id:
                price_change = current_cheapest_price - price_orig
            else:
                price_change = None
            result.append({
                'item_id': item_id,
                'item_name': item_name,
                'cheapest_realm': cheapest_name,
                'cheapest_price': format_price(current_cheapest_price),
                'discount': discount_str,
                'top10_avg': top10_avg_formatted,
                'is_deal': True,
                'undermine_url': undermine_url,
                'original_price': price_orig,
                'price_change': price_change,
                'added_at': deal.get('added_at', 'неизвестно')
            })
        else:
            result.append({
                'item_id': item_id,
                'item_name': f"Предмет {item_id} (удалён)",
                'cheapest_realm': realm_id,
                'cheapest_price': format_price(price_orig),
                'discount': '—',
                'top10_avg': '—',
                'is_deal': True,
                'undermine_url': '#',
                'original_price': price_orig,
                'price_change': None,
                'added_at': deal.get('added_at', 'неизвестно')
            })
    result.sort(key=lambda x: x['added_at'], reverse=True)
    return jsonify({'items': result})

@app.route('/api/toggle_deal', methods=['POST'])
def api_toggle_deal():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    item_id = int(data.get('item_id'))
    realm_id = int(data.get('realm_id'))
    price = int(data.get('price'))
    if not item_id or not realm_id or not price:
        return jsonify({'error': 'Missing parameters'}), 400
    user_id = session['user_id']
    added = toggle_deal(user_id, item_id, realm_id, price)
    return jsonify({'added': added})

@app.route('/api/toggle_ignore', methods=['POST'])
def api_toggle_ignore():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    item_id = int(data.get('item_id'))
    realm_id = int(data.get('realm_id'))
    price = int(data.get('price'))
    if not item_id or not realm_id or not price:
        return jsonify({'error': 'Missing parameters'}), 400
    user_id = session['user_id']
    added = toggle_ignore(user_id, item_id, realm_id, price)
    return jsonify({'added': added})

@app.route('/api/history')
def api_history():
    history = load_json(HISTORY_FILE, {})
    for item_id, entries in history.items():
        for entry in entries:
            entry['price_formatted'] = format_price(entry['price'])
    return jsonify(history)

# ====== HTML-ШАБЛОНЫ (без изменений) ======
HTML_MAIN = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WoW Auction Monitor</title>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700&family=Roboto:wght@300;400;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #0d0d0d; color: #c8c8c8; font-family: 'Roboto', sans-serif; padding: 20px; min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; background: rgba(20,20,20,0.9); backdrop-filter: blur(10px); border-radius: 20px; padding: 30px; box-shadow: 0 10px 40px rgba(0,0,0,0.8); border: 1px solid rgba(245,200,66,0.15); }
        .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; border-bottom: 2px solid #f5c842; padding-bottom: 15px; margin-bottom: 25px; }
        .logo { font-family: 'Cinzel', serif; font-size: 28px; font-weight: 700; color: #f5c842; text-shadow: 0 0 20px rgba(245,200,66,0.3); letter-spacing: 2px; }
        .logo i { margin-right: 10px; }
        .update-time { font-size: 14px; color: #aaa; background: rgba(255,255,255,0.05); padding: 6px 16px; border-radius: 20px; border: 1px solid rgba(245,200,66,0.2); }
        .user-info { display: flex; gap: 15px; align-items: center; flex-wrap: wrap; margin-top: 10px; }
        .user-info a { color: #f5c842; text-decoration: none; font-size: 14px; transition: color 0.3s; font-weight: 300; letter-spacing: 0.5px; }
        .user-info a:hover { color: #fff; text-shadow: 0 0 10px rgba(245,200,66,0.5); }
        .user-info span { color: #aaa; }
        .controls { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; margin-bottom: 20px; }
        .filter-options { display: flex; gap: 20px; align-items: center; }
        .filter-options label { color: #ccc; font-size: 14px; cursor: pointer; display: flex; align-items: center; gap: 6px; }
        .filter-options input[type="checkbox"] { accent-color: #f5c842; width: 16px; height: 16px; cursor: pointer; }
        .filter-bar { flex: 1; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
        .filter-bar .filter-info { color: #f5c842; font-size: 14px; background: rgba(245,200,66,0.1); padding: 4px 14px; border-radius: 20px; border: 1px solid rgba(245,200,66,0.2); }
        .btn-reset { background: rgba(255,255,255,0.05); color: #f5c842; border: 1px solid #f5c842; padding: 4px 14px; border-radius: 20px; cursor: pointer; font-size: 13px; transition: all 0.3s; font-family: 'Roboto', sans-serif; }
        .btn-reset:hover { background: #f5c842; color: #0d0d0d; }
        .export-area { background: rgba(255,255,255,0.03); border: 1px solid rgba(245,200,66,0.2); border-radius: 12px; padding: 12px 16px; margin: 15px 0; display: flex; flex-wrap: wrap; align-items: center; gap: 15px; }
        .export-area .selected-ids { flex: 1; font-size: 14px; color: #aaa; word-break: break-all; min-height: 30px; line-height: 1.5; }
        .export-area .selected-ids span { color: #f5c842; }
        .btn-copy { background: rgba(245,200,66,0.15); color: #f5c842; border: 1px solid #f5c842; padding: 6px 18px; border-radius: 20px; cursor: pointer; font-size: 14px; transition: all 0.3s; font-family: 'Roboto', sans-serif; white-space: nowrap; }
        .btn-copy:hover { background: #f5c842; color: #0d0d0d; }
        .btn-copy i { margin-right: 6px; }
        .table-wrapper { overflow-x: auto; border-radius: 12px; border: 1px solid rgba(255,255,255,0.05); }
        table { width: 100%; border-collapse: collapse; font-size: 14px; min-width: 850px; }
        th { background: rgba(245,200,66,0.08); color: #f5c842; padding: 14px 10px; text-align: left; border-bottom: 2px solid #f5c842; cursor: pointer; user-select: none; transition: background 0.2s; font-weight: 400; letter-spacing: 0.5px; }
        th:hover { background: rgba(245,200,66,0.15); }
        th .sort-arrow { margin-left: 6px; font-size: 11px; color: #888; }
        td { padding: 12px 10px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }
        tr:hover td { background: rgba(255,255,255,0.02); }
        .item-name { display: flex; align-items: center; gap: 10px; }
        .item-name img { width: 28px; height: 28px; border-radius: 4px; flex-shrink: 0; background: #1a1a1a; padding: 2px; object-fit: contain; }
        .item-name img[src=""] { display: none; }
        .item-name a { color: #fff; text-decoration: none; border-bottom: 1px dotted rgba(245,200,66,0.3); transition: border-color 0.3s; }
        .item-name a:hover { border-bottom-color: #f5c842; }
        .price { color: #7ec850; font-weight: 500; }
        .realm { color: #88c0d0; }
        .realm-filter-link { color: #f5c842; text-decoration: none; font-size: 12px; margin-left: 4px; border-bottom: 1px dotted rgba(245,200,66,0.3); cursor: pointer; transition: border-color 0.3s; }
        .realm-filter-link:hover { border-bottom-color: #f5c842; }
        .discount-high { color: #f5c842; font-weight: bold; }
        .discount-medium { color: #e0a030; }
        .discount-low { color: #a0a0a0; }
        .top3 { font-size: 11px; color: #aaa; line-height: 1.3; }
        .deal-btn { background: none; border: none; color: #f5c842; cursor: pointer; font-size: 18px; padding: 0 6px; transition: color 0.3s, transform 0.2s; width: 32px; height: 32px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; }
        .deal-btn:hover { color: #fff; transform: scale(1.15); background: rgba(245,200,66,0.1); }
        .checkbox-col { text-align: center; width: 40px; }
        .checkbox-col input[type="checkbox"] { accent-color: #f5c842; width: 16px; height: 16px; cursor: pointer; }
        .deal-row { background: rgba(42,90,42,0.3); }
        .ignored-row { background: rgba(60,60,60,0.3); color: #888; }
        .ignored-row .item-name a { color: #888; border-bottom-color: #555; }
        .ignored-row .price { color: #888; }
        .ignored-row .realm { color: #888; }
        .no-data { color: #888; text-align: center; padding: 60px 20px; font-size: 18px; font-weight: 300; }
        .no-data i { font-size: 40px; color: #555; margin-bottom: 15px; display: block; }
        .footer { margin-top: 25px; color: #555; font-size: 12px; text-align: right; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 15px; }
        .loading { text-align: center; padding: 60px 20px; color: #888; font-size: 16px; }
        .loading i { font-size: 30px; margin-bottom: 10px; display: block; animation: spin 1s linear infinite; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        @media (max-width: 768px) {
            .container { padding: 15px; }
            .logo { font-size: 22px; }
            .header { flex-direction: column; align-items: start; gap: 10px; }
            .controls { flex-direction: column; align-items: stretch; }
            .filter-bar { justify-content: flex-start; }
            .export-area { flex-direction: column; align-items: stretch; }
            .btn-copy { align-self: flex-start; }
            table { font-size: 12px; min-width: 600px; }
            th, td { padding: 8px 6px; }
            .top3 { font-size: 10px; }
            .item-name img { width: 22px; height: 22px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo"><i class="fas fa-shield-halved"></i> Auction Monitor</div>
            <div class="update-time" id="update-time"><i class="fas fa-clock"></i> Обновление: загрузка...</div>
        </div>
        <div class="user-info">
            {% if session.user_id %}
                <span><i class="fas fa-user"></i> Привет, {{ session.username }}!</span>
                <a href="/logout"><i class="fas fa-sign-out-alt"></i> Выйти</a>
                <a href="/my_deals"><i class="fas fa-star"></i> Мои сделки</a>
                <a href="/old_deals"><i class="fas fa-history"></i> История</a>
            {% else %}
                <a href="/login"><i class="fas fa-sign-in-alt"></i> Войти</a>
                <a href="/register"><i class="fas fa-user-plus"></i> Регистрация</a>
                <a href="/old_deals"><i class="fas fa-history"></i> История</a>
            {% endif %}
        </div>

        <div class="controls">
            <div class="filter-options">
                <label><input type="checkbox" id="showDealsOnly" onchange="applyFilters()"> <i class="fas fa-star" style="color:#f5c842;"></i> Только мои сделки</label>
            </div>
            <div id="filter-bar" class="filter-bar"></div>
        </div>

        <div class="export-area" id="exportArea">
            <div class="selected-ids" id="selectedIdsDisplay">Выберите предметы, чтобы скопировать их ID</div>
            <button class="btn-copy" id="copyIdsBtn" onclick="copySelectedIds()"><i class="fas fa-copy"></i> Копировать ID</button>
        </div>

        <div id="table-container">
            <div class="loading"><i class="fas fa-spinner"></i> Загрузка данных...</div>
        </div>
        <div class="footer"><i class="fas fa-sync-alt"></i> Автообновление в интервале обновления</div>
    </div>
    <script>
        let userId = {{ session.get('user_id', 'null') }};
        let currentItems = [];
        let sortState = {};
        let currentFilter = null;
        let realmCounts = {};
        let lastKnownModified = null;
        let showDealsOnly = false;
        let selectedIds = [];

        function fetchData() {
            fetch('/api/data')
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        document.getElementById('table-container').innerHTML = '<div class="no-data"><i class="fas fa-exclamation-circle"></i>Нет данных</div>';
                        document.getElementById('update-time').textContent = 'Обновление: ошибка';
                        return;
                    }
                    document.getElementById('update-time').innerHTML = `<i class="fas fa-clock"></i> Обновление: ${data.last_modified}`;
                    currentItems = data.items || [];
                    realmCounts = {};
                    currentItems.forEach(item => {
                        const realm = item.cheapest_realm;
                        if (realm) realmCounts[realm] = (realmCounts[realm] || 0) + 1;
                    });
                    if (currentFilter && !realmCounts[currentFilter]) currentFilter = null;
                    selectedIds = [];
                    applyFilters();
                })
                .catch(err => {
                    console.error(err);
                    document.getElementById('table-container').innerHTML = '<div class="no-data"><i class="fas fa-exclamation-triangle"></i>Ошибка загрузки</div>';
                });
        }

        function applyFilters() {
            showDealsOnly = document.getElementById('showDealsOnly').checked;
            let filtered = currentItems;
            if (showDealsOnly) {
                filtered = filtered.filter(item => item.is_deal === true);
            }
            if (currentFilter) {
                filtered = filtered.filter(item => item.cheapest_realm === currentFilter);
            }
            renderTable(filtered);
            updateFilterBar();
            updateSelectedIdsDisplay();
        }

        function updateSelectedIdsDisplay() {
            const display = document.getElementById('selectedIdsDisplay');
            if (selectedIds.length === 0) {
                display.innerHTML = 'Выберите предметы, чтобы скопировать их ID';
                return;
            }
            const idsStr = selectedIds.join(', ');
            display.innerHTML = `<span>${idsStr}</span>`;
        }

        function copySelectedIds() {
            if (selectedIds.length === 0) {
                alert('Нет выбранных ID для копирования');
                return;
            }
            const idsStr = selectedIds.join(', ');
            navigator.clipboard.writeText(idsStr).then(() => {
                alert('ID скопированы в буфер обмена!');
            }).catch(err => {
                const textarea = document.createElement('textarea');
                textarea.value = idsStr;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);
                alert('ID скопированы в буфер обмена!');
            });
        }

        function toggleSelectItem(itemId) {
            const idx = selectedIds.indexOf(itemId);
            if (idx > -1) {
                selectedIds.splice(idx, 1);
            } else {
                selectedIds.push(itemId);
            }
            updateSelectedIdsDisplay();
        }

        function checkLastModified() {
            fetch('/api/last_modified')
                .then(response => response.json())
                .then(data => {
                    if (data.error) return;
                    const newModified = data.last_modified;
                    if (lastKnownModified === null) {
                        lastKnownModified = newModified;
                    } else if (newModified !== lastKnownModified) {
                        console.log('Обнаружено обновление данных, загружаем...');
                        lastKnownModified = newModified;
                        fetchData();
                    }
                })
                .catch(err => console.error('Ошибка проверки last_modified:', err));
        }

        function isInUpdateInterval() {
            const now = new Date();
            const minute = now.getMinutes();
            return (minute >= 56 && minute <= 59) || (minute >= 0 && minute <= 3);
        }

        function startUpdateCheck() {
            fetchData();
            let intervalId;
            function scheduleCheck() {
                if (isInUpdateInterval()) {
                    checkLastModified();
                    intervalId = setTimeout(scheduleCheck, 1000);
                } else {
                    checkLastModified();
                    intervalId = setTimeout(scheduleCheck, 60000);
                }
            }
            scheduleCheck();
        }

        function toggleDeal(itemId, realmId, price) {
            if (!userId) {
                alert('Пожалуйста, войдите, чтобы добавлять сделки.');
                return;
            }
            fetch('/api/toggle_deal', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({item_id: itemId, realm_id: realmId, price: price})
            })
            .then(response => response.json())
            .then(data => {
                fetchData();
            })
            .catch(err => {
                alert('Ошибка при добавлении сделки');
            });
        }

        function toggleIgnore(itemId, realmId, price) {
            if (!userId) {
                alert('Пожалуйста, войдите, чтобы игнорировать предметы.');
                return;
            }
            fetch('/api/toggle_ignore', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({item_id: itemId, realm_id: realmId, price: price})
            })
            .then(response => response.json())
            .then(data => {
                fetchData();
            })
            .catch(err => {
                alert('Ошибка при игнорировании');
            });
        }

        function renderTable(items) {
            if (!items.length) {
                document.getElementById('table-container').innerHTML = '<div class="no-data"><i class="fas fa-box-open"></i>Нет предметов</div>';
                return;
            }
            let html = `<div class="table-wrapper"><table><thead><tr>
                <th class="checkbox-col">Выбрать</th>
                <th data-sort="item_name">Предмет <span class="sort-arrow">▾</span></th>
                <th data-sort="cheapest_price">Цена (g) <span class="sort-arrow">▾</span></th>
                <th data-sort="discount">Скидка <span class="sort-arrow">▾</span></th>
                <th data-sort="cheapest_realm">Дешёвый реалм <span class="sort-arrow">▾</span></th>
                <th data-sort="sale_realm">Реалм для продажи <span class="sort-arrow">▾</span></th>
                <th data-sort="sale_price">Цена продажи (g) <span class="sort-arrow">▾</span></th>
                <th>Топ-3</th>
                <th>Действие</th>
            </tr></thead><tbody>`;
            items.forEach(item => {
                const isDeal = item.is_deal;
                const isIgnored = item.is_ignored;
                let rowClass = '';
                if (isIgnored) rowClass = 'ignored-row';
                else if (isDeal) rowClass = 'deal-row';
                const dealBtn = userId ? (isDeal ? '<i class="fas fa-times"></i>' : '<i class="fas fa-plus"></i>') : '<i class="fas fa-plus"></i>';
                const ignoreBtn = userId ? (isIgnored ? '<i class="fas fa-times"></i>' : '<i class="fas fa-minus"></i>') : '<i class="fas fa-minus"></i>';
                const count = realmCounts[item.cheapest_realm] || 0;
                const filterLink = count > 1 ? ` <span class="realm-filter-link" onclick="applyFilter('${item.cheapest_realm}')">(+${count})</span>` : '';
                let discClass = 'discount-low';
                let discVal = item.discount;
                if (discVal !== '—') {
                    let num = parseFloat(discVal);
                    if (num >= 50) discClass = 'discount-high';
                    else if (num >= 30) discClass = 'discount-medium';
                }
                const isChecked = selectedIds.includes(item.item_id) ? 'checked' : '';
                const iconHtml = item.icon_url ? `<img src="${item.icon_url}" alt="${item.item_name}" loading="lazy" onerror="this.style.display='none'">` : '';
                html += `<tr class="${rowClass}">
                    <td class="checkbox-col"><input type="checkbox" ${isChecked} onchange="toggleSelectItem(${item.item_id})"></td>
                    <td class="item-name">${iconHtml}<a href="${item.undermine_url}" target="_blank">${item.item_name}</a></td>
                    <td class="price">${item.cheapest_price}</td>
                    <td><span class="${discClass}">${discVal}</span></td>
                    <td class="realm">${item.cheapest_realm}${filterLink}</td>
                    <td class="realm">${item.sale_realm}</td>
                    <td class="price">${item.sale_price}</td>
                    <td class="top3">${item.top3}</td>
                    <td>
                        <button class="deal-btn" onclick="toggleDeal(${item.item_id}, ${item.cheapest_realm_id}, ${item.cheapest_price_raw})" title="Добавить/удалить из сделок">${dealBtn}</button>
                        <button class="deal-btn" onclick="toggleIgnore(${item.item_id}, ${item.cheapest_realm_id}, ${item.cheapest_price_raw})" title="Игнорировать" style="color:#888;">${ignoreBtn}</button>
                    </td>
                </tr>`;
            });
            html += `</tbody></table></div>`;
            document.getElementById('table-container').innerHTML = html;
            document.querySelectorAll('th[data-sort]').forEach(th => {
                th.addEventListener('click', function() {
                    const key = this.dataset.sort;
                    sortTable(key);
                });
            });
            updateSelectedIdsDisplay();
        }

        function sortTable(key) {
            if (!currentItems.length) return;
            if (!sortState[key]) sortState[key] = 'asc';
            else sortState[key] = sortState[key] === 'asc' ? 'desc' : 'asc';
            const dir = sortState[key];
            const reverse = dir === 'desc' ? -1 : 1;
            const sorted = [...currentItems].sort((a, b) => {
                let va = a[key], vb = b[key];
                if (key === 'cheapest_price' || key === 'sale_price') {
                    va = parseFloat(va) || 0; vb = parseFloat(vb) || 0;
                    return (va - vb) * reverse;
                }
                if (key === 'discount') {
                    va = va === '—' ? -1 : parseFloat(va);
                    vb = vb === '—' ? -1 : parseFloat(vb);
                    return (va - vb) * reverse;
                }
                if (typeof va === 'string') return va.localeCompare(vb) * reverse;
                return 0;
            });
            currentItems = sorted;
            applyFilters();
        }

        function applyFilter(realm) {
            if (realm === currentFilter) return;
            currentFilter = realm;
            applyFilters();
        }

        function resetFilter() {
            currentFilter = null;
            applyFilters();
        }

        function updateFilterBar() {
            const bar = document.getElementById('filter-bar');
            if (currentFilter) {
                bar.innerHTML = `<span class="filter-info"><i class="fas fa-filter"></i> Фильтр: <strong>${currentFilter}</strong> (${realmCounts[currentFilter] || 0})</span>
                                 <button class="btn-reset" onclick="resetFilter()"><i class="fas fa-times"></i> Сбросить</button>`;
            } else {
                bar.innerHTML = '';
            }
        }

        startUpdateCheck();
    </script>
</body>
</html>
"""

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Вход</title>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700&family=Roboto:wght@300;400;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { background: #0d0d0d; color: #c8c8c8; font-family: 'Roboto', sans-serif; padding: 40px; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .container { max-width: 400px; width: 100%; background: rgba(20,20,20,0.9); padding: 30px; border-radius: 16px; border: 1px solid rgba(245,200,66,0.15); box-shadow: 0 10px 40px rgba(0,0,0,0.8); }
        h2 { font-family: 'Cinzel', serif; color: #f5c842; text-align: center; margin-bottom: 30px; }
        input { display: block; width: 100%; padding: 12px; margin: 10px 0; background: #1a1a1a; border: 1px solid #3a3a3a; color: #fff; border-radius: 8px; font-family: 'Roboto', sans-serif; }
        button { width: 100%; padding: 12px; background: #f5c842; color: #0d0d0d; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; transition: background 0.3s; font-size: 16px; }
        button:hover { background: #e0b030; }
        a { color: #f5c842; text-decoration: none; }
        .error { color: #ff6b6b; text-align: center; }
        p { text-align: center; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Вход</h2>
        {% if error %}<p class="error">{{ error }}</p>{% endif %}
        <form method="post">
            <input type="text" name="username" placeholder="Имя пользователя" required>
            <input type="password" name="password" placeholder="Пароль" required>
            <button type="submit">Войти</button>
        </form>
        <p><a href="/register">Регистрация</a> | <a href="/">На главную</a></p>
    </div>
</body>
</html>
"""

HTML_REGISTER = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Регистрация</title>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700&family=Roboto:wght@300;400;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { background: #0d0d0d; color: #c8c8c8; font-family: 'Roboto', sans-serif; padding: 40px; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .container { max-width: 400px; width: 100%; background: rgba(20,20,20,0.9); padding: 30px; border-radius: 16px; border: 1px solid rgba(245,200,66,0.15); box-shadow: 0 10px 40px rgba(0,0,0,0.8); }
        h2 { font-family: 'Cinzel', serif; color: #f5c842; text-align: center; margin-bottom: 30px; }
        input { display: block; width: 100%; padding: 12px; margin: 10px 0; background: #1a1a1a; border: 1px solid #3a3a3a; color: #fff; border-radius: 8px; font-family: 'Roboto', sans-serif; }
        button { width: 100%; padding: 12px; background: #f5c842; color: #0d0d0d; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; transition: background 0.3s; font-size: 16px; }
        button:hover { background: #e0b030; }
        a { color: #f5c842; text-decoration: none; }
        .error { color: #ff6b6b; text-align: center; }
        p { text-align: center; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Регистрация</h2>
        {% if error %}<p class="error">{{ error }}</p>{% endif %}
        <form method="post">
            <input type="text" name="username" placeholder="Имя пользователя" required>
            <input type="password" name="password" placeholder="Пароль" required>
            <button type="submit">Зарегистрироваться</button>
        </form>
        <p><a href="/login">Вход</a> | <a href="/">На главную</a></p>
    </div>
</body>
</html>
"""

HTML_MY_DEALS = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Мои сделки</title>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700&family=Roboto:wght@300;400;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { background: #0d0d0d; color: #c8c8c8; font-family: 'Roboto', sans-serif; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; background: rgba(20,20,20,0.9); padding: 30px; border-radius: 20px; border: 1px solid rgba(245,200,66,0.15); box-shadow: 0 10px 40px rgba(0,0,0,0.8); }
        h2 { font-family: 'Cinzel', serif; color: #f5c842; border-bottom: 2px solid #f5c842; padding-bottom: 15px; margin-bottom: 25px; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; }
        th { background: rgba(245,200,66,0.08); color: #f5c842; padding: 12px; text-align: left; border-bottom: 2px solid #f5c842; }
        td { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.04); }
        .price { color: #7ec850; }
        .realm { color: #88c0d0; }
        .added-at { color: #aaa; font-size: 12px; }
        a { color: #f5c842; text-decoration: none; border-bottom: 1px dotted rgba(245,200,66,0.3); }
        a:hover { border-bottom-color: #f5c842; }
        .no-data { color: #888; text-align: center; padding: 60px 20px; }
        .no-data i { font-size: 40px; color: #555; display: block; margin-bottom: 15px; }
        .back-link { display: inline-block; margin: 20px 0; color: #f5c842; }
        @media (max-width: 768px) { table, th, td { font-size: 12px; } .container { padding: 15px; } }
    </style>
</head>
<body>
<div class="container">
    <h2><i class="fas fa-star"></i> Мои сделки</h2>
    <div id="table-container">Загрузка...</div>
    <p><a href="/"><i class="fas fa-home"></i> На главную</a> | <a href="/old_deals"><i class="fas fa-history"></i> История</a></p>
</div>
<script>
    fetch('/api/deals')
        .then(r => r.json())
        .then(data => {
            const items = data.items || [];
            const container = document.getElementById('table-container');
            if (!items.length) {
                container.innerHTML = '<p class="no-data"><i class="fas fa-box-open"></i>Нет отмеченных сделок</p>';
                return;
            }
            let html = `<table><thead><tr>
                <th>Предмет</th>
                <th>Цена</th>
                <th>Скидка</th>
                <th>Дешёвый реалм</th>
                <th>Средняя по 10 дешёвым</th>
                <th>Добавлено</th>
            </tr></thead><tbody>`;
            items.forEach(item => {
                let priceChange = '';
                if (item.price_change !== undefined && item.price_change !== null) {
                    const sign = item.price_change > 0 ? '+' : '';
                    priceChange = ` (${sign}${item.price_change} медяков)`;
                }
                html += `<tr>
                    <td><a href="${item.undermine_url}" target="_blank">${item.item_name}</a></td>
                    <td class="price">${item.cheapest_price} g${priceChange}</td>
                    <td>${item.discount}</td>
                    <td class="realm">${item.cheapest_realm}</td>
                    <td class="price">${item.top10_avg}</td>
                    <td class="added-at">${item.added_at}</td>
                </tr>`;
            });
            html += `</tbody></table>`;
            container.innerHTML = html;
        });
</script>
</body>
</html>
"""

HTML_OLD_DEALS = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>История цен</title>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700&family=Roboto:wght@300;400;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body { background: #0d0d0d; color: #c8c8c8; font-family: 'Roboto', sans-serif; padding: 20px; }
        .container { max-width: 1400px; margin: 0 auto; background: rgba(20,20,20,0.9); padding: 30px; border-radius: 20px; border: 1px solid rgba(245,200,66,0.15); box-shadow: 0 10px 40px rgba(0,0,0,0.8); }
        h2 { font-family: 'Cinzel', serif; color: #f5c842; border-bottom: 2px solid #f5c842; padding-bottom: 15px; margin-bottom: 25px; }
        .item-block { border-bottom: 1px solid rgba(255,255,255,0.05); padding: 15px 0; }
        .item-title { color: #f5c842; cursor: pointer; font-size: 16px; transition: color 0.3s; }
        .item-title:hover { color: #fff; }
        .history-list { display: none; margin-top: 10px; }
        .history-list table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .history-list th { background: rgba(245,200,66,0.08); color: #aaa; text-align: left; padding: 8px; }
        .history-list td { padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.03); }
        .price { color: #7ec850; }
        .realm { color: #88c0d0; }
        a { color: #f5c842; text-decoration: none; }
        .no-data { color: #888; text-align: center; padding: 60px 20px; }
        .no-data i { font-size: 40px; color: #555; display: block; margin-bottom: 15px; }
        @media (max-width: 768px) { .container { padding: 15px; } }
    </style>
</head>
<body>
<div class="container">
    <h2><i class="fas fa-history"></i> История изменений цен</h2>
    <div id="history-container">Загрузка...</div>
    <p><a href="/"><i class="fas fa-home"></i> На главную</a> | <a href="/my_deals"><i class="fas fa-star"></i> Мои сделки</a></p>
</div>
<script>
    fetch('/api/history')
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById('history-container');
            let html = '';
            for (const [itemId, entries] of Object.entries(data)) {
                if (!entries.length) continue;
                html += `<div class="item-block">
                    <div class="item-title" onclick="toggleHistory('${itemId}')"><i class="fas fa-chevron-right"></i> Предмет ${itemId} (${entries.length} записей)</div>
                    <div class="history-list" id="hist-${itemId}">
                        <table><thead><tr><th>Время</th><th>Реалм</th><th>Цена (g)</th></tr></thead><tbody>`;
                entries.slice().reverse().forEach(entry => {
                    html += `<tr><td>${entry.timestamp}</td><td class="realm">${entry.realm}</td><td class="price">${entry.price_formatted}</td></tr>`;
                });
                html += `</tbody></table></div></div>`;
            }
            container.innerHTML = html || '<p class="no-data"><i class="fas fa-box-open"></i>Нет истории</p>';
        });

    function toggleHistory(id) {
        const el = document.getElementById('hist-'+id);
        if (el.style.display === 'block') {
            el.style.display = 'none';
        } else {
            el.style.display = 'block';
        }
    }
</script>
</body>
</html>
"""

if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    CLIENT_ID = os.getenv("CLIENT_ID")
    CLIENT_SECRET = os.getenv("CLIENT_SECRET")
    if CLIENT_ID and CLIENT_SECRET:
        auth = (CLIENT_ID, CLIENT_SECRET)
        data = {"grant_type": "client_credentials"}
        resp = requests.post("https://oauth.battle.net/token", auth=auth, data=data)
        if resp.status_code == 200:
            token = resp.json()["access_token"]
            app.blizzard_token = token
            print("✓ Токен Blizzard получен для иконок")
        else:
            print("⚠️ Не удалось получить токен для иконок")
    else:
        print("⚠️ CLIENT_ID/CLIENT_SECRET не заданы в .env, иконки не будут загружены")

    app.run(debug=True, host='0.0.0.0', port=5000)
