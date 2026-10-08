#!/usr/bin/env python3
# WEBGEM.py — v6.0: EU + US регионы, переключение на лету

import os
import sys
import json
import time
import sqlite3
import secrets
from io import BytesIO
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change_me_please")

# ================== КОНФИГУРАЦИЯ РЕГИОНОВ ==================
REGION_CONFIGS = {
    "eu": {
        "label": "EU",
        "db_file": "auction_data.db",
        "region": "eu",
        "namespace_static": "static-eu",
        "base_url": "https://eu.api.blizzard.com",
        "token_url": "https://oauth.battle.net/token",
        "undermine_prefix": "eu",
        "locale": "ru_RU",
    },
    "us": {
        "label": "US",
        "db_file": "auction_data_us.db",
        "region": "us",
        "namespace_static": "static-us",
        "base_url": "https://us.api.blizzard.com",
        "token_url": "https://oauth.battle.net/token",
        "undermine_prefix": "us",
        "locale": "en_US",
    },
}
DEFAULT_REGION = "eu"

def _get_region():
    """Получить регион из ?region=... или сессии. По умолчанию eu."""
    r = request.args.get('region', '').strip().lower()
    if r in REGION_CONFIGS:
        session['region'] = r
        return r
    r = session.get('region', '').strip().lower()
    if r in REGION_CONFIGS:
        return r
    return DEFAULT_REGION

def _cfg(region=None):
    """Вернуть конфиг региона."""
    r = region or DEFAULT_REGION
    return REGION_CONFIGS.get(r, REGION_CONFIGS[DEFAULT_REGION])

DEFAULT_ITEM_IDS = os.getenv("DEFAULT_ITEM_IDS", "").strip()
SALE_REALMS_ENV = os.getenv("SALE_REALMS", "")
if SALE_REALMS_ENV.strip():
    try:
        SALE_REALMS_STR = SALE_REALMS_ENV.strip().strip('[]')
        DEFAULT_SALE_REALM_IDS = SALE_REALMS_STR.replace(' ', '')
    except:
        DEFAULT_SALE_REALM_IDS = ""
else:
    DEFAULT_SALE_REALM_IDS = ""

# ---------- Иконки ----------
ICON_TOKEN = None
ICON_TOKEN_EXPIRY = datetime.min

# ---------- Токены иконок (по регионам) ----------
_ICON_TOKENS = {}  # {"eu": {"token": ..., "expiry": ...}, "us": {...}}

def _ensure_icon_token(region='eu'):
    global _ICON_TOKENS
    entry = _ICON_TOKENS.get(region)
    if entry and entry['token'] and datetime.now(timezone.utc) < entry['expiry'] - timedelta(minutes=5):
        return entry['token']
    cfg = _cfg(region)
    env_suffix = "_US" if region == "us" else ""
    client_id = os.getenv(f"CLIENT_ID{env_suffix}")
    client_secret = os.getenv(f"CLIENT_SECRET{env_suffix}")
    if not client_id or not client_secret:
        return None
    import requests
    try:
        resp = requests.post(cfg['token_url'],
                             auth=(client_id, client_secret),
                             data={"grant_type": "client_credentials"}, verify=False)
        resp.raise_for_status()
        data = resp.json()
        _ICON_TOKENS[region] = {
            'token': data["access_token"],
            'expiry': datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
        }
        return _ICON_TOKENS[region]['token']
    except:
        return None

icon_cache = {}
@app.route('/icon/<int:item_id>')
def get_item_icon(item_id):
    region = _get_region()
    cache_key = f"{region}_{item_id}"
    if cache_key in icon_cache:
        return send_file(BytesIO(icon_cache[cache_key]), mimetype='image/jpeg')
    token = _ensure_icon_token(region)
    if not token:
        abort(404)
    import requests
    cfg = _cfg(region)
    url = f"{cfg['base_url']}/data/wow/media/item/{item_id}?namespace={cfg['namespace_static']}&locale={cfg['locale']}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10, verify=False)
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
                    cdn_url = f"https://render.worldofwarcraft.com/{cfg['region']}/icons/56/{icon_name}.jpg"
                    img_resp = requests.get(cdn_url, timeout=10, verify=False)
                    img_resp.raise_for_status()
                    icon_cache[cache_key] = img_resp.content
                    return send_file(BytesIO(img_resp.content), mimetype='image/jpeg')
    except:
        pass
    abort(404)

# ---------- БД ----------
def get_db(region='eu'):
    cfg = _cfg(region)
    conn = sqlite3.connect(cfg['db_file'])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 120000")  # ждать до 2 мин при блокировке
    return conn

def query_db(query, args=(), one=False, region='eu'):
    conn = get_db(region)
    cur = conn.execute(query, args)
    rv = cur.fetchall()
    conn.close()
    return (rv[0] if rv else None) if one else rv

def format_price(copper):
    if copper is None:
        return "—"
    gold = copper // 10000
    silver = (copper % 10000) // 100
    return f"{gold}.{silver:02d}"

# ---------- Маршруты ----------
@app.route('/')
def index():
    if 'user_id' in session:
        return render_template_string(HTML_MAIN,
                                      session=session,
                                      default_item_ids=DEFAULT_ITEM_IDS,
                                      default_sale_realm_ids=DEFAULT_SALE_REALM_IDS)
    else:
        return render_template_string(HTML_LANDING)

@app.route('/faq')
def faq():
    return render_template_string(HTML_FAQ)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        user = query_db("SELECT * FROM users WHERE email = ?", [email], one=True)
        if not user:
            user = query_db("SELECT * FROM users WHERE username = ?", [email], one=True)
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('index'))
        return render_template_string(HTML_LOGIN, error="Invalid credentials")
    return render_template_string(HTML_LOGIN, error=None, promo='')

@app.route('/register', methods=['GET', 'POST'])
def register():
    promo_prefill = request.args.get('promo', '')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        promo = request.form.get('promo', '').strip().upper()
        if not username or not email or not password:
            return render_template_string(HTML_REGISTER, error="All fields required", promo=promo)
        if '@' not in email or '.' not in email:
            return render_template_string(HTML_REGISTER, error="Invalid email", promo=promo)
        if query_db("SELECT id FROM users WHERE email = ?", [email], one=True):
            return render_template_string(HTML_REGISTER, error="Email already registered", promo=promo)
        if query_db("SELECT id FROM users WHERE username = ?", [username], one=True):
            return render_template_string(HTML_REGISTER, error="Username taken", promo=promo)
        token = secrets.token_urlsafe(32)
        conn = get_db()
        conn.execute("INSERT INTO users (username, password_hash, role, email, email_verified) VALUES (?,?,?,?,'1')",
                     (username, generate_password_hash(password), 'user', email))
        conn.commit(); conn.close()
        # Применяем промокод
        if promo:
            redeem_for_user(email, promo)
        return redirect(url_for('login'))
    return render_template_string(HTML_REGISTER, error=None, promo=promo_prefill)

def redeem_for_user(email, code):
    """Применить промокод: безлимитный по использованиям, но с проверкой срока."""
    conn = get_db()
    user = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    promo = conn.execute("SELECT * FROM promo_codes WHERE code=?", (code.upper(),)).fetchone()
    if user and promo:
        try:
            if promo['expires_at'] and datetime.now(timezone.utc) > datetime.fromisoformat(promo['expires_at']):
                conn.close(); return
        except: pass
        days = promo['duration_days'] or 7
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        conn.execute("UPDATE users SET tier='premium', premium_until=?, max_items=999 WHERE id=?", (until, user['id']))
        conn.commit()
    conn.close()

@app.route('/verify/<token>')
def verify_email(token):
    conn = get_db()
    conn.execute("UPDATE users SET email_verified='1', email_token=NULL WHERE email_token=?", (token,))
    conn.commit(); conn.close()
    return redirect(url_for('login'))

@app.route('/account')
def account():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = query_db("SELECT username, email, tier, premium_until FROM users WHERE id=?", [session['user_id']], one=True)
    return render_template_string(HTML_ACCOUNT, user=user)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/my_deals')
def my_deals():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template_string(HTML_MY_DEALS)

@app.route('/old_deals')
def old_deals():
    return render_template_string(HTML_OLD_DEALS)

# ---------- API ----------
def get_latest_snapshot_time(region='eu'):
    row = query_db("SELECT value AS last_time FROM meta WHERE key = 'last_snapshot'", one=True, region=region)
    if row and row["last_time"]:
        return row["last_time"]
    row = query_db("SELECT MAX(collected_at) AS last_time FROM auction_snapshots", one=True, region=region)
    return row["last_time"] if row else None

@app.route('/api/data')
def api_data():
    region = _get_region()
    realm_ids_param = request.args.get('realm_ids', '')
    sale_realm_ids_param = request.args.get('sale_realm_ids', '')
    discount_threshold = request.args.get('discount', None, type=float)
    min_top10avg = request.args.get('min_top10avg', None, type=float)
    item_ids_param = request.args.get('item_ids', '')
    search = request.args.get('search', '').strip().lower()
    deals_only = request.args.get('deals_only', '0') == '1'
    boe_filters_json = request.args.get('boe_filters', '')

    # Парсим BoE-фильтры
    boe_filters = []
    if boe_filters_json:
        try:
            boe_filters = json.loads(boe_filters_json)
        except:
            pass

    last_time = get_latest_snapshot_time(region)
    if not last_time:
        return jsonify({'items': [], 'last_modified': ''})

    conn = get_db(region)
    has_latest = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auction_latest'"
    ).fetchone()
    if has_latest:
        rows = conn.execute("""
            SELECT s.item_id, s.realm_id, s.ilvl, s.min_buyout, s.quantity,
                   r.name_en AS realm_name_en,
                   r.name_ru AS realm_name_ru,
                   r.slug,
                   i.name_en AS item_name
            FROM auction_latest s
            JOIN realms r ON r.id = s.realm_id
            LEFT JOIN items i ON i.id = s.item_id
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT s.item_id, s.realm_id, s.ilvl, s.min_buyout, s.quantity,
                   r.name_en AS realm_name_en,
                   r.name_ru AS realm_name_ru,
                   r.slug,
                   i.name_en AS item_name
            FROM auction_snapshots s
            JOIN realms r ON r.id = s.realm_id
            LEFT JOIN items i ON i.id = s.item_id
            WHERE s.collected_at = ?
        """, (last_time,)).fetchall()
    conn.close()

    realm_filter = set(int(x) for x in realm_ids_param.split(',') if x.strip()) if realm_ids_param else None
    sale_filter = set(int(x) for x in sale_realm_ids_param.split(',') if x.strip()) if sale_realm_ids_param else None
    item_filter = set(int(x) for x in item_ids_param.split(',') if x.strip()) if item_ids_param else None

    # Если есть BoE-фильтры — добавляем их ID в общий фильтр (если он задан)
    if boe_filters and item_filter is not None:
        for bf in boe_filters:
            for x in str(bf.get('ids', '')).split(','):
                if x.strip():
                    item_filter.add(int(x.strip()))

    # Группируем по (item_id, ilvl) — BoE разбиваются на отдельные строки
    item_data = {}
    for row in rows:
        item_id = row["item_id"]
        realm_id = row["realm_id"]
        ilvl = row["ilvl"] or 0
        if item_filter and item_id not in item_filter:
            continue
        if realm_filter and realm_id not in realm_filter:
            continue
        key = (item_id, ilvl)
        if key not in item_data:
            base_name = row["item_name"] or f"Item {item_id}"
            item_data[key] = {
                "prices": {},
                "realm_names": {},
                "slugs": {},
                "quantities": {},
                "item_name": f"{base_name} [{ilvl}]" if ilvl > 0 else base_name,
                "item_id": item_id,
                "ilvl": ilvl,
            }
        item_data[key]["prices"][realm_id] = row["min_buyout"]
        item_data[key]["quantities"][realm_id] = row["quantity"] or 0
        realm_display = row["realm_name_ru"] if row["realm_name_ru"] else row["realm_name_en"]
        item_data[key]["realm_names"][realm_id] = realm_display
        item_data[key]["slugs"][realm_id] = row["slug"]

    items_result = []
    realm_counts = {}
    for info in item_data.values():
        for rid in info["prices"]:
            rname = info["realm_names"][rid]
            realm_counts[rname] = realm_counts.get(rname, 0) + 1

    # Сделки с точной ценой (теперь с ilvl)
    deal_triples = set()
    if 'user_id' in session:
        deals = query_db("SELECT item_id, realm_id, ilvl, price_at_add FROM user_deals WHERE user_id = ?", [session['user_id']])
        deal_triples = {(int(d['item_id']), int(d['realm_id']), int(d['ilvl'] or 0), int(d['price_at_add'])) for d in deals}

    # Free-tier ограничения
    user_tier, _, _ = get_user_tier()
    if user_tier == 'free':
        sale_filter = None          # все реалмы для продажи
        boe_filters = []            # без BoE-фильтров

    for (item_id, ilvl), info in item_data.items():
        prices = info["prices"]
        if not prices:
            continue
        sorted_prices = sorted(prices.items(), key=lambda x: x[1])
        cheapest_realm, cheapest_price = sorted_prices[0]

        # Определяем top-N и скидку: для BoE — из фильтра, иначе дефолт
        top_n = 10
        boe_disc_threshold = discount_threshold
        boe_matched = False
        if ilvl > 0 and boe_filters:
            for bf in boe_filters:
                bf_ids = set(int(x.strip()) for x in str(bf.get('ids', '')).split(',') if x.strip())
                bf_min = int(bf.get('ilvl_min', 0))
                bf_max = int(bf.get('ilvl_max', 999))
                if item_id in bf_ids and bf_min <= ilvl <= bf_max:
                    top_n = int(bf.get('topx', 10)) or 10
                    boe_disc_threshold = float(bf.get('discount', 0)) or 0
                    boe_matched = True
                    break
            # Если BoE но не подошёл ни под один фильтр — пропускаем
            if not boe_matched and boe_filters:
                continue

        top_prices = sorted_prices[:top_n]
        avg_top = sum(p for _, p in top_prices) / len(top_prices) if top_prices else None
        discount = None
        if avg_top and avg_top > 0:
            discount = (1 - cheapest_price / avg_top) * 100
            eff_threshold = boe_disc_threshold if boe_matched else discount_threshold
            if eff_threshold is not None and discount < eff_threshold:
                continue

        # Фильтр по минимальному avg (G -> медь) — только для не-BoE
        if not boe_matched and min_top10avg is not None and avg_top is not None:
            if avg_top < min_top10avg * 10000:
                continue

        if sale_filter:
            sale_prices = [(r, p) for r, p in prices.items() if r in sale_filter]
        else:
            sale_prices = sorted_prices
        sale_realm, sale_price = max(sale_prices, key=lambda x: x[1]) if sale_prices else (None, None)

        top3_str = ", ".join([
            f"{info['realm_names'].get(r, r)} ({format_price(p)})"
            for r, p in sorted_prices[:3]
        ])

        item_name = info["item_name"]
        slug = info["slugs"].get(cheapest_realm, "")
        um_prefix = _cfg(region)['undermine_prefix']
        if ilvl > 0:
            undermine_url = f"https://undermine.exchange/#{um_prefix}-{slug}/{item_id}-{ilvl}" if slug else "#"
        else:
            undermine_url = f"https://undermine.exchange/#{um_prefix}-{slug}/{item_id}" if slug else "#"

        is_deal = (item_id, cheapest_realm, ilvl, cheapest_price) in deal_triples

        items_result.append({
            'item_id': item_id,
            'ilvl': ilvl,
            'item_name': item_name,
            'cheapest_realm_id': cheapest_realm,
            'cheapest_realm': info['realm_names'].get(cheapest_realm, "Unknown"),
            'cheapest_price_raw': cheapest_price,
            'cheapest_price': format_price(cheapest_price),
            'quantity': info['quantities'].get(cheapest_realm, 0),
            'sale_realm': info['realm_names'].get(sale_realm, "—") if sale_realm else "—",
            'sale_price': format_price(sale_price) if sale_price else "—",
            'discount': f"{discount:.1f}%" if discount is not None else "—",
            'discount_raw': round(discount, 1) if discount is not None else None,
            'top10avg': format_price(round(avg_top)) if avg_top else "—",
            'top10avg_raw': round(avg_top) if avg_top else None,
            'boe_topx': top_n if boe_matched else 0,
            'top3': top3_str,
            'undermine_url': undermine_url,
            'icon_url': f"/icon/{item_id}?region={region}",
            'is_deal': is_deal
        })

    if search:
        items_result = [it for it in items_result if search in it['item_name'].lower()]

    if deals_only and 'user_id' in session:
        items_result = [it for it in items_result if it['is_deal']]

    items_result.sort(key=lambda x: float(x['discount'].replace('%','')) if x['discount'] != '—' else -1, reverse=True)

    blizz_row = query_db("SELECT value FROM meta WHERE key = 'blizzard_last_modified'", one=True, region=region)
    return jsonify({
        'items': items_result,
        'last_modified': last_time,
        'blizzard_time': blizz_row['value'] if blizz_row else None,
        'realm_counts': realm_counts,
        'region': region
    })

@app.route('/api/sparkline')
def api_sparkline():
    region = _get_region()
    item_id = request.args.get('item_id', type=int)
    ilvl = request.args.get('ilvl', 0, type=int)
    if not item_id:
        return jsonify({'error': 'item_id required'}), 400

    conn = get_db(region)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT collected_at, realm_id, min_buyout
        FROM auction_snapshots
        WHERE item_id = ? AND ilvl = ? AND collected_at >= ?
        ORDER BY collected_at, min_buyout
    """, (item_id, ilvl, since)).fetchall()
    conn.close()

    # Группируем по collected_at, берём 10 самых дешёвых, считаем среднее
    groups = defaultdict(list)
    for r in rows:
        groups[r['collected_at']].append(r['min_buyout'])

    points = []
    for ts in sorted(groups.keys()):
        prices = sorted(groups[ts])[:10]
        if prices:
            avg = sum(prices) / len(prices)
            points.append({
                'time': ts[:16],
                'price': round(avg),
                'gold': round(avg / 10000, 1)
            })

    return jsonify({'points': points})

@app.route('/api/update_times')
def api_update_times():
    """Вернуть время последнего обновления EU и US."""
    result = {}
    for r in ['eu', 'us']:
        try:
            last = get_latest_snapshot_time(r)
            blizz = query_db("SELECT value FROM meta WHERE key = 'blizzard_last_modified'", one=True, region=r)
            result[r] = {
                'last_modified': last,
                'blizzard_time': blizz['value'] if blizz else None
            }
        except:
            result[r] = {'last_modified': None, 'blizzard_time': None}
    return jsonify(result)

@app.route('/api/toggle_deal', methods=['POST'])
def toggle_deal():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json()
    item_id = int(data.get('item_id', 0) or 0)
    realm_id = int(data.get('realm_id', 0) or 0)
    ilvl = int(data.get('ilvl', 0) or 0)
    price = int(data.get('price', 0) or 0)
    region = data.get('region', 'eu').strip().lower()
    if region not in REGION_CONFIGS:
        region = 'eu'
    if not item_id or not realm_id or not price:
        return jsonify({'error': 'missing parameters'}), 400
    user_id = session['user_id']
    conn = get_db()
    existing = conn.execute("SELECT id FROM user_deals WHERE user_id=? AND item_id=? AND realm_id=? AND ilvl=? AND price_at_add=?",
                            (user_id, item_id, realm_id, ilvl, price)).fetchone()
    if existing:
        conn.execute("DELETE FROM user_deals WHERE id=?", (existing[0],))
        conn.commit()
        conn.close()
        return jsonify({'added': False})
    else:
        conn.execute("""
            INSERT INTO user_deals (user_id, item_id, realm_id, ilvl, price_at_add,
                item_name, discount, cheapest_realm, sale_realm, sale_price,
                top10avg, top3, undermine_url, icon_url, region)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, item_id, realm_id, ilvl, price,
            data.get('item_name', ''),
            data.get('discount', ''),
            data.get('cheapest_realm', ''),
            data.get('sale_realm', ''),
            data.get('sale_price', ''),
            data.get('top10avg', ''),
            data.get('top3', ''),
            data.get('undermine_url', ''),
            data.get('icon_url', ''),
            region
        ))
        conn.commit()
        conn.close()
        return jsonify({'added': True})

@app.route('/api/toggle_ignore', methods=['POST'])
def toggle_ignore():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json()
    item_id = data.get('item_id')
    realm_id = data.get('realm_id')
    price = data.get('price')
    if not item_id or not realm_id or price is None:
        return jsonify({'error': 'missing parameters'}), 400
    user_id = session['user_id']
    conn = get_db()
    existing = conn.execute("SELECT id FROM user_ignored WHERE user_id=? AND item_id=? AND realm_id=? AND price=?",
                            (user_id, item_id, realm_id, price)).fetchone()
    if existing:
        conn.execute("DELETE FROM user_ignored WHERE id=?", (existing[0],))
        conn.commit()
        conn.close()
        return jsonify({'added': False})
    else:
        conn.execute("INSERT INTO user_ignored (user_id, item_id, realm_id, price) VALUES (?,?,?,?)",
                     (user_id, item_id, realm_id, price))
        conn.commit()
        conn.close()
        return jsonify({'added': True})

# ---------- API пресетов ----------
@app.route('/api/presets')
def api_presets():
    region = _get_region()
    presets = []
    default = query_db("SELECT * FROM user_presets WHERE user_id = 0 AND region = ?", (region,), one=True)
    if default:
        presets.append({
            'id': default['id'],
            'name': default['name'],
            'item_ids': default['item_ids'],
            'sale_realm_ids': default['sale_realm_ids'],
            'discount': default['discount'],
            'min_top10avg': default['min_top10avg'],
            'deals_only': bool(default['deals_only']),
            'boe_filters': default['boe_filters'] if 'boe_filters' in default.keys() else '',
            'is_default': False,
            'editable': False
        })
    if 'user_id' in session:
        rows = query_db("SELECT * FROM user_presets WHERE user_id = ? AND region = ? ORDER BY name", [session['user_id'], region])
        for r in rows:
            presets.append({
                'id': r['id'],
                'name': r['name'],
                'item_ids': r['item_ids'],
                'sale_realm_ids': r['sale_realm_ids'],
                'discount': r['discount'],
                'min_top10avg': r['min_top10avg'],
                'deals_only': bool(r['deals_only']),
                'boe_filters': r['boe_filters'] if 'boe_filters' in r.keys() else '',
                'is_default': bool(r['is_default']),
                'editable': True
            })
    return jsonify({'presets': presets, 'region': region})

@app.route('/api/presets', methods=['POST'])
def save_preset():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    region = data.get('region', 'eu').strip().lower()
    if region not in REGION_CONFIGS:
        region = 'eu'
    # Free-tier: лимит айтемов
    tier, _, max_items = get_user_tier()
    item_ids_str = data.get('item_ids', '')
    if tier == 'free':
        cnt = len([x for x in item_ids_str.split(',') if x.strip()])
        if cnt > max_items:
            return jsonify({'error': f'Free tier: max {max_items} items. Get Premium for unlimited.'}), 403
    preset_id = data.get('id')
    conn = get_db()
    if preset_id:
        existing = conn.execute("SELECT * FROM user_presets WHERE id = ? AND user_id = ?", (preset_id, session['user_id'])).fetchone()
        if not existing:
            conn.close()
            return jsonify({'error': 'not found or not yours'}), 403
        conn.execute("""
            UPDATE user_presets
            SET name = ?, item_ids = ?, sale_realm_ids = ?, discount = ?, min_top10avg = ?, deals_only = ?, boe_filters = ?
            WHERE id = ?
        """, (
            name,
            data.get('item_ids', ''),
            data.get('sale_realm_ids', ''),
            data.get('discount'),
            data.get('min_top10avg'),
            int(data.get('deals_only', False)),
            str(data.get('boe_filters', '')),
            preset_id
        ))
        conn.commit()
        conn.close()
        return jsonify({'id': preset_id, 'name': name})
    else:
        cur = conn.execute("""
            INSERT INTO user_presets (user_id, name, item_ids, sale_realm_ids, discount, min_top10avg, deals_only, boe_filters, region)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session['user_id'],
            name,
            data.get('item_ids', ''),
            data.get('sale_realm_ids', ''),
            data.get('discount'),
            data.get('min_top10avg'),
            int(data.get('deals_only', False)),
            str(data.get('boe_filters', '')),
            region,
        ))
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        return jsonify({'id': new_id, 'name': name})

@app.route('/api/presets/<int:preset_id>/set_default', methods=['POST'])
def set_default_preset(preset_id):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    region = _get_region()
    conn = get_db()
    row = conn.execute("SELECT * FROM user_presets WHERE id = ? AND user_id = ? AND region = ?", (preset_id, session['user_id'], region)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    conn.execute("UPDATE user_presets SET is_default = 0 WHERE user_id = ? AND region = ?", (session['user_id'], region))
    conn.execute("UPDATE user_presets SET is_default = 1 WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/presets/<int:preset_id>', methods=['DELETE'])
def delete_preset(preset_id):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    conn = get_db()
    row = conn.execute("SELECT * FROM user_presets WHERE id = ?", (preset_id,)).fetchone()
    if not row or (row['user_id'] != session['user_id'] and row['user_id'] != 0):
        conn.close()
        return jsonify({'error': 'not found or not yours'}), 403
    if row['user_id'] == 0:
        conn.close()
        return jsonify({'error': 'cannot delete default preset'}), 403
    conn.execute("DELETE FROM user_presets WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ---------- API названий ----------
@app.route('/api/item_name')
def api_item_name():
    region = _get_region()
    item_id = request.args.get('item_id', type=int)
    if not item_id:
        return jsonify({'error': 'item_id required'}), 400
    row = query_db("SELECT name_en FROM items WHERE id = ?", (item_id,), one=True, region=region)
    name = row['name_en'] if row else None
    return jsonify({'name': name})

@app.route('/api/realm_name')
def api_realm_name():
    region = _get_region()
    realm_id = request.args.get('realm_id', type=int)
    if not realm_id:
        return jsonify({'error': 'realm_id required'}), 400
    row = query_db("SELECT name_ru, name_en FROM realms WHERE id = ?", (realm_id,), one=True, region=region)
    if row:
        name = row['name_ru'] if row['name_ru'] else row['name_en']
    else:
        name = None
    return jsonify({'name': name})

@app.route('/api/top_deals')
def api_top_deals():
    """Публичный эндпоинт: топ-5 сделок из user_deals.json с live ценами из БД."""
    import os, json
    deals_file = data('user_deals.json')
    if not os.path.exists(deals_file):
        return jsonify({'items': []})
    try:
        with open(deals_file, 'r', encoding='utf-8') as f:
            all_deals = json.load(f)
    except:
        return jsonify({'items': []})

    # Фильтруем админа (user_id=1) и достаём (item_id, realm_id, price, ilvl=0)
    admin_deals = []
    seen_keys = set()
    for d in all_deals:
        if d.get('user_id') != 1:
            continue
        key = (int(d['item_id']), int(d.get('realm_id', 0)), int(d.get('ilvl', 0)))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        admin_deals.append({
            'item_id': int(d['item_id']),
            'realm_id': int(d.get('realm_id', 0)),
            'ilvl': int(d.get('ilvl', 0)),
            'price': int(d.get('price', 0)),
        })

    if not admin_deals:
        return jsonify({'items': []})

    # Получаем текущие цены из БД (EU)
    results = []
    try:
        conn = get_db('eu')
        for deal in admin_deals:
            item_id = deal['item_id']
            ilvl = deal['ilvl']
            realm_id = deal['realm_id']
            buy_price = deal['price']

            # Ищем имя предмета
            name_row = conn.execute("SELECT name_en, name_ru FROM items WHERE id=?", (item_id,)).fetchone()
            item_name = name_row['name_ru'] or name_row['name_en'] or f"Item {item_id}" if name_row else f"Item {item_id}"
            if ilvl > 0:
                item_name = f"{item_name} [{ilvl}]"

            # Ищем реалм
            realm_row = conn.execute("SELECT name_ru, name_en, slug FROM realms WHERE id=?", (realm_id,)).fetchone()
            realm_name = realm_row['name_ru'] or realm_row['name_en'] or str(realm_id) if realm_row else str(realm_id)
            slug = realm_row['slug'] if realm_row else ''

            # Текущие цены на этот предмет со всех реалмов
            prices = conn.execute("""
                SELECT al.min_buyout, al.realm_id, r.name_ru, r.name_en, r.slug
                FROM auction_latest al
                JOIN realms r ON r.id = al.realm_id
                WHERE al.item_id = ? AND al.ilvl = ? AND al.min_buyout > 0
                ORDER BY al.min_buyout
            """, (item_id, ilvl)).fetchall()

            if not prices or len(prices) < 2:
                continue

            all_prices = [p['min_buyout'] for p in prices]
            top10 = sorted(set(all_prices))[:10]
            avg_top10 = sum(top10) / len(top10) if top10 else 0

            if avg_top10 <= 0 or buy_price <= 0:
                continue

            # Скидка: насколько цена покупки меньше средней
            discount = (1 - buy_price / avg_top10) * 100
            if discount < 10:  # меньше 10% скидки — неинтересно
                continue

            # Самая дешёвая цена сейчас
            cheapest_price = all_prices[0]
            cheapest_realm_row = prices[0]
            cheapest_realm = cheapest_realm_row['name_ru'] or cheapest_realm_row['name_en'] or str(cheapest_realm_row['realm_id'])

            # Самая дорогая (для продажи)
            sale_price = all_prices[-1]
            sale_realm_row = prices[-1]
            sale_realm_name = sale_realm_row['name_ru'] or sale_realm_row['name_en'] or str(sale_realm_row['realm_id'])

            um_prefix = 'eu'
            if ilvl > 0:
                um_url = f"https://undermine.exchange/#{um_prefix}-{slug}/{item_id}-{ilvl}" if slug else "#"
            else:
                um_url = f"https://undermine.exchange/#{um_prefix}-{slug}/{item_id}" if slug else "#"

            top3_str = ", ".join([
                f"{prices[i]['name_ru'] or prices[i]['name_en']} ({round(prices[i]['min_buyout']/10000,1)}g)"
                for i in range(min(3, len(prices)))
            ])

            results.append({
                'item_id': item_id,
                'ilvl': ilvl,
                'item_name': item_name,
                'price_at_add': buy_price,
                'cheapest_price': f"{round(buy_price/10000, 2)}" if buy_price else "—",
                'cheapest_price_raw': buy_price,
                'discount': f"{discount:.1f}%",
                'discount_raw': round(discount, 1),
                'cheapest_realm': realm_name,
                'current_cheapest': cheapest_realm,
                'current_cheapest_price': f"{round(cheapest_price/10000, 2)}g" if cheapest_price else "—",
                'sale_realm': sale_realm_name,
                'sale_price_raw': sale_price,
                'sale_price': f"{round(sale_price/10000, 2)}g" if sale_price else "—",
                'top10avg': f"{round(avg_top10/10000, 2)}g" if avg_top10 else "—",
                'top10avg_raw': round(avg_top10),
                'top3': top3_str,
                'undermine_url': um_url,
                'icon_url': f"/icon/{item_id}?region=eu",
                'quantity': sum(p['min_buyout'] for p in prices)  # не точное кол-во, но хоть что-то
            })
        conn.close()
    except Exception as e:
        return jsonify({'items': [], 'error': str(e)})

    results.sort(key=lambda x: x['discount_raw'], reverse=True)
    return jsonify({'items': results[:5]})

@app.route('/api/deals')
def api_deals():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    user_id = session['user_id']
    live = request.args.get('live', '0') == '1'
    sale_realm_ids_param = request.args.get('sale_realm_ids', '')
    sale_filter = set(int(x) for x in sale_realm_ids_param.split(',') if x.strip()) if sale_realm_ids_param else None

    # Сделки всегда из EU БД
    rows = query_db("SELECT * FROM user_deals WHERE user_id = ? ORDER BY added_at DESC", [user_id])

    # Live-цены: группируем по региону сделки, запрашиваем из нужной БД
    live_prices = {}  # (region, item_id, ilvl) -> best sale info
    if live and rows:
        from collections import defaultdict
        region_keys = defaultdict(list)
        for r in rows:
            deal_region = (r['region'] or 'eu').strip().lower()
            if deal_region not in REGION_CONFIGS:
                deal_region = 'eu'
            region_keys[deal_region].append((int(r['item_id']), int(r['ilvl'] or 0)))
        for deal_region, keys_list in region_keys.items():
            unique_keys = list(set(keys_list))
            if not unique_keys:
                continue
            try:
                conn = get_db(deal_region)
            except:
                continue
            placeholders = ','.join(['(?,?)'] * len(unique_keys))
            params = []
            for item_id, ilvl in unique_keys:
                params.extend([item_id, ilvl])
            live_rows = conn.execute(f"""
                SELECT al.item_id, al.ilvl, al.realm_id, al.min_buyout,
                       r.name_ru, r.name_en, r.slug
                FROM auction_latest al
                JOIN realms r ON r.id = al.realm_id
                WHERE (al.item_id, al.ilvl) IN ({placeholders})
                ORDER BY al.min_buyout DESC
            """, params).fetchall()
            conn.close()
            for lr in live_rows:
                if sale_filter and lr['realm_id'] not in sale_filter:
                    continue
                key = (deal_region, int(lr['item_id']), int(lr['ilvl'] or 0))
                if key not in live_prices or lr['min_buyout'] > live_prices[key]['price_raw']:
                    rname = lr['name_ru'] or lr['name_en'] or str(lr['realm_id'])
                    live_prices[key] = {
                        'realm': rname,
                        'realm_id': lr['realm_id'],
                        'price': format_price(lr['min_buyout']),
                        'price_raw': lr['min_buyout'],
                        'slug': lr['slug'] or ''
                    }

    items = []
    for r in rows:
        deal_region = (r['region'] or 'eu').strip().lower()
        if deal_region not in REGION_CONFIGS:
            deal_region = 'eu'
        um_prefix = _cfg(deal_region)['undermine_prefix']
        item = {
            'item_id': r['item_id'],
            'ilvl': int(r['ilvl'] or 0),
            'item_name': r['item_name'] or f"Item {r['item_id']}",
            'cheapest_price': format_price(r['price_at_add']),
            'discount': r['discount'] or "—",
            'cheapest_realm': r['cheapest_realm'] or "—",
            'sale_realm': r['sale_realm'] or "—",
            'sale_price': r['sale_price'] or "—",
            'top10avg': r['top10avg'] or "—",
            'top3': r['top3'] or "—",
            'undermine_url': r['undermine_url'] or "#",
            'icon_url': r['icon_url'] or "",
            'added_at': r['added_at'],
            'region': deal_region.upper()
        }
        if live:
            lp = live_prices.get((deal_region, int(r['item_id']), int(r['ilvl'] or 0)))
            if lp:
                item['live_realm'] = lp['realm']
                item['live_realm_id'] = lp['realm_id']
                item['live_price'] = lp['price']
                item['live_price_raw'] = lp['price_raw']
                item['live_slug'] = lp['slug']
            else:
                item['live_realm'] = '—'
                item['live_realm_id'] = None
                item['live_price'] = '—'
                item['live_price_raw'] = None
                item['live_slug'] = ''
        items.append(item)
    return jsonify({'items': items})

@app.route('/api/price_chart')
def api_price_chart():
    """Детальная история цены для графика (Chart.js) — top10avg."""
    region = _get_region()
    item_id = request.args.get('item_id', type=int)
    ilvl = request.args.get('ilvl', 0, type=int)
    if not item_id:
        return jsonify({'error': 'item_id required'}), 400

    conn = get_db(region)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT collected_at, min_buyout
        FROM auction_snapshots
        WHERE item_id = ? AND ilvl = ? AND collected_at >= ?
        ORDER BY collected_at, min_buyout
    """, (item_id, ilvl, since)).fetchall()
    conn.close()

    groups = defaultdict(list)
    for r in rows:
        groups[r['collected_at']].append(r['min_buyout'])

    labels = []
    prices = []
    for ts in sorted(groups.keys()):
        top10 = sorted(groups[ts])[:10]
        if top10:
            labels.append(ts[:16])
            prices.append(round(sum(top10) / len(top10)))

    return jsonify({'labels': labels, 'prices': prices})

@app.route('/api/history')
def api_history():
    region = _get_region()
    item_id = request.args.get('item_id', type=int)
    realm_id = request.args.get('realm_id', type=int)
    if not item_id:
        return jsonify({'error': 'item_id required'}), 400
    query = "SELECT min_buyout, recorded_at FROM price_history WHERE item_id = ?"
    params = [item_id]
    if realm_id:
        query += " AND realm_id = ?"
        params.append(realm_id)
    query += " ORDER BY recorded_at ASC"
    rows = query_db(query, params, region=region)
    history = [{'price': r['min_buyout'], 'time': r['recorded_at']} for r in rows]
    return jsonify({'history': history})

# ---------- Tier & Promo ----------
def get_user_tier():
    """Вернуть (tier, premium_until, max_items) для текущего пользователя."""
    if 'user_id' not in session:
        return 'free', None, 10
    u = query_db("SELECT tier, premium_until, max_items, role FROM users WHERE id = ?", [session['user_id']], one=True)
    if not u:
        return 'free', None, 10
    # Админ всегда premium
    if u['role'] == 'admin':
        return 'premium', None, 999
    tier = u['tier'] or 'free'
    pu = u['premium_until']
    if tier == 'premium' and pu:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(pu):
                conn = get_db()
                conn.execute("UPDATE users SET tier='free', premium_until=NULL WHERE id=? AND role!='admin'", (session['user_id'],))
                conn.commit(); conn.close()
                return 'free', None, u['max_items'] or 10
        except: pass
    return tier, pu, u['max_items'] or 10

@app.route('/api/redeem', methods=['POST'])
def api_redeem():
    if 'user_id' not in session:
        return jsonify({'error': 'Login required'}), 401
    data = request.get_json()
    code = (data.get('code') or '').strip().upper()
    if not code:
        return jsonify({'error': 'Enter a promo code'}), 400
    conn = get_db()
    promo = conn.execute("SELECT * FROM promo_codes WHERE code = ?", (code,)).fetchone()
    if not promo:
        conn.close(); return jsonify({'error': 'Invalid code'}), 400
    # Проверка срока
    try:
        if promo['expires_at'] and datetime.now(timezone.utc) > datetime.fromisoformat(promo['expires_at']):
            conn.close(); return jsonify({'error': 'Code expired'}), 400
    except: pass

    # Проверяем, не активен ли уже премиум
    user = conn.execute("SELECT tier FROM users WHERE id=?", (session['user_id'],)).fetchone()
    if user and user['tier'] == 'premium':
        conn.close(); return jsonify({'error': 'You already have Premium'}), 400

    days = promo['duration_days'] or 7
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    conn.execute("UPDATE users SET tier='premium', premium_until=?, max_items=999 WHERE id=?", (until, session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'success': True, 'premium_until': until})

@app.route('/api/me')
def api_me():
    tier, pu, mi = get_user_tier()
    return jsonify({'tier': tier, 'premium_until': pu, 'max_items': mi, 'logged_in': 'user_id' in session})

# ==================== BOOTSTRAP API (для GUI) ====================

@app.route('/api/item_names')
def api_item_names():
    """Вернуть все имена предметов: {item_id: name_en, ...}"""
    region = request.args.get('region', 'eu')
    rows = query_db("SELECT id, name_en FROM items", region=region)
    names = {r['id']: r['name_en'] for r in rows if r['name_en']}
    return jsonify(names)

@app.route('/api/bootstrap')
def api_bootstrap():
    """Вернуть bootstrap-данные для нового GUI (price_history + snapshots + topXavg)."""
    region = request.args.get('region', 'eu').strip().lower()
    if region not in ('eu', 'us'):
        return jsonify({'error': 'Invalid region'}), 400
    try:
        days = int(request.args.get('days', '7'))
    except:
        days = 7
    days = max(1, min(30, days))

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db(region)

    result = {'region': region, 'days': days}

    # price_history (только изменения)
    try:
        rows = conn.execute("""
            SELECT item_id, ilvl, realm_id, min_buyout, recorded_at
            FROM price_history
            WHERE recorded_at >= ?
            ORDER BY recorded_at
        """, (since,)).fetchall()
        result['price_history'] = [
            (r['item_id'], r['ilvl'] or 0, r['realm_id'], r['min_buyout'], r['recorded_at'])
            for r in rows
        ]
    except Exception as e:
        result['price_history'] = []
        print(f"bootstrap price_history error: {e}")

    # Последние N снэпшотов (только TOP-10 avg для каждого предмета)
    try:
        snap_rows = conn.execute("""
            SELECT collected_at, item_id, ilvl, AVG(min_buyout) as avg_price,
                   COUNT(*) as realm_count, MIN(min_buyout) as min_price
            FROM auction_snapshots
            WHERE collected_at >= (SELECT MIN(collected_at) FROM (
                SELECT DISTINCT collected_at FROM auction_snapshots
                ORDER BY collected_at DESC LIMIT 10
            ))
            GROUP BY collected_at, item_id, ilvl
            ORDER BY collected_at, item_id
        """).fetchall()
        result['snapshots'] = [
            (r['collected_at'], r['item_id'], r['ilvl'] or 0,
             r['min_price'], r['realm_count'], int(r['avg_price']))
            for r in snap_rows
        ]
    except Exception as e:
        result['snapshots'] = []
        print(f"bootstrap snapshots error: {e}")

    # topXavg предрасчитанные
    try:
        last_time = conn.execute(
            "SELECT value FROM meta WHERE key='last_snapshot'"
        ).fetchone()
        if last_time:
            lt = last_time['value']
            avg_rows = conn.execute("""
                SELECT item_id, ilvl,
                       AVG(min_buyout) as avg10,
                       COUNT(*) as realms_count,
                       MIN(min_buyout) as min_price
                FROM (
                    SELECT item_id, ilvl, min_buyout,
                           ROW_NUMBER() OVER (
                               PARTITION BY item_id, ilvl
                               ORDER BY min_buyout
                           ) as rn
                    FROM auction_snapshots
                    WHERE collected_at = ? AND min_buyout > 0
                )
                WHERE rn <= 10
                GROUP BY item_id, ilvl
            """, (lt,)).fetchall()
            result['top10avg'] = {
                f"{r['item_id']}_{r['ilvl'] or 0}": {
                    'avg': int(r['avg10']),
                    'min': r['min_price'],
                    'realms': r['realms_count'],
                    'item_id': r['item_id'],
                    'ilvl': r['ilvl'] or 0,
                }
                for r in avg_rows
            }
        else:
            result['top10avg'] = {}
    except Exception as e:
        result['top10avg'] = {}
        print(f"bootstrap top10avg error: {e}")

    conn.close()
    return jsonify(result)

@app.route('/api/realms')
def api_realms():
    """Вернуть список реалмов для региона."""
    region = request.args.get('region', 'eu')
    rows = query_db("SELECT id, name_en, name_ru, slug FROM realms ORDER BY id", region=region)
    realms = [[r['id'], r['name_en'] or '', r['name_ru'] or '', r['slug'] or ''] for r in rows]
    return jsonify({'realms': realms, 'region': region, 'count': len(realms)})

# ==================== HTML ====================
HTML_LANDING = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Auction Monitor — WoW AH Sniper</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0f1117;--surface:#1a1d25;--surface2:#23262f;--gold:#f5c842;--gold-dim:rgba(245,200,66,0.15);--text:#c8c8c8;--text-dim:#888;--green:#4ade80;--red:#f87171;--blue:#60a5fa}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;min-height:100vh}
.hero{text-align:center;padding:80px 24px 40px;background:linear-gradient(180deg,rgba(245,200,66,0.08) 0%,transparent 100%)}
.hero h1{font-size:48px;font-weight:800;color:var(--gold);letter-spacing:-1px}
.hero h1 span{color:#fff;font-weight:300}
.hero p{font-size:18px;color:var(--text-dim);margin-top:12px;max-width:600px;margin-left:auto;margin-right:auto}
.hero .btns{display:flex;gap:12px;justify-content:center;margin-top:28px;flex-wrap:wrap}
.btn{border-radius:10px;padding:14px 28px;font-size:15px;font-weight:600;text-decoration:none;transition:all 0.2s;display:inline-flex;align-items:center;gap:8px;cursor:pointer;border:none}
.btn-gold{background:var(--gold);color:#0d0d0d}.btn-gold:hover{background:#f5d666}
.btn-outline{background:transparent;color:var(--gold);border:1px solid var(--gold)}.btn-outline:hover{background:var(--gold-dim)}
.promo-box{background:var(--surface);border:1px solid var(--gold-dim);border-radius:12px;padding:20px;margin:24px auto;max-width:420px;display:flex;gap:8px;align-items:center}
.promo-box input{flex:1;padding:12px;background:var(--surface2);border:1px solid rgba(255,255,255,0.08);color:#fff;border-radius:8px;font-size:14px}
.promo-box input:focus{outline:none;border-color:var(--gold)}
.promo-box button{white-space:nowrap}
.section{max-width:1100px;margin:0 auto;padding:40px 24px}
.section h2{font-size:28px;font-weight:700;color:var(--gold);margin-bottom:16px}
.section h2 i{margin-right:10px}
.demo-table{overflow-x:auto;border-radius:12px;border:1px solid rgba(255,255,255,0.04);background:var(--surface)}
.demo-table table{width:100%;border-collapse:collapse;font-size:13px}
.demo-table th{background:var(--surface2);color:var(--gold);padding:12px;text-align:left;font-weight:500;white-space:nowrap}
.demo-table td{padding:10px;border-bottom:1px solid rgba(255,255,255,0.03)}
.demo-table a{color:#fff;text-decoration:none}.demo-table a:hover{color:var(--gold)}
.price-g{color:var(--green);font-weight:500}
.disc-g{color:var(--green);font-weight:600}
.realm-g{color:var(--blue)}
.faq-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.faq-card{background:var(--surface);border:1px solid rgba(255,255,255,0.04);border-radius:12px;padding:20px}
.faq-card h3{color:var(--gold);font-size:15px;margin-bottom:8px}
.faq-card p{color:var(--text-dim);font-size:13px;line-height:1.6}
.footer{text-align:center;padding:40px 24px;color:var(--text-dim);font-size:13px;border-top:1px solid rgba(255,255,255,0.04);margin-top:40px}
.footer a{color:var(--gold);text-decoration:none}.footer a:hover{text-decoration:underline}
.footer .tip{background:var(--surface2);border-radius:8px;padding:8px 16px;display:inline-block;margin-top:8px;font-family:monospace;font-size:12px;word-break:break-all;max-width:500px}
.emoji-big{font-size:64px}
</style>
</head>
<body>
<div class="hero">
<div class="emoji-big">🛡️</div>
<h1>Auction<span>Monitor</span></h1>
<p>Real-time WoW auction sniper — EU & US realms. Find underpriced items before anyone else.</p>
<div class="btns">
<a href="/register?promo=PREMIUM7" class="btn btn-gold">🚀 Try Free (7 days Premium)</a>
<a href="/login" class="btn btn-outline">Sign In</a>
</div>
<p style="font-size:12px;color:var(--text-dim);margin-top:8px">No credit card. Premium auto-activates on registration.</p>
</div>

<div class="section">
<h2><i>🔥</i> Live Demo — Recent Top Deals</h2>
<div class="demo-table" id="demoTable">Loading...</div>
</div>

<div class="section">
<h2><i>❓</i> FAQ</h2>
<div class="faq-grid">
<div class="faq-card"><h3>How to add item IDs?</h3><p>Find item on <a href="https://undermine.exchange" target="_blank">Undermine Exchange</a> — copy the number from URL. Paste into "Item IDs" field on the main page.</p></div>
<div class="faq-card"><h3>How to find realm IDs?</h3><p>Hover over any realm name in the deals table — the ID appears in the tooltip. Or use /ah_sale_realms list in Discord.</p></div>
<div class="faq-card"><h3>What is Snipe?</h3><p>Snipe mode shows only items where the cheapest realm is one you're watching. Filter by clicking any realm in the "Cheapest" column.</p></div>
<div class="faq-card"><h3>BoE filters?</h3><p>Bind-on-Equip items have item levels. BoE filters let you target specific ilvl ranges with custom discount thresholds — Premium only.</p></div>
<div class="faq-card"><h3>Discord bot?</h3><p>Auto-posts US top deals every hour. Available by request — <a href="https://t.me/makliyoyo">@makliyoyo</a>. Not included in free tier.</p></div>
<div class="faq-card"><h3>Free vs Premium?</h3><p>Free: 10 items, all sale realms, no BoE. Premium: unlimited items, custom sale realms, BoE filters. Discord bot by request only.</p></div>
<div class="faq-card"><h3>Update frequency?</h3><p>EU & US auction data refreshes every ~60 minutes (Blizzard update window). Site auto-detects new data within seconds.</p></div>
<div class="faq-card"><h3>About the project</h3><p>Built by an AI enthusiast as a hobby project. May get updates, may not. Don't expect 24/7 support. If it works — great. If it breaks — check the <a href="https://t.me/makliyoyo">Telegram</a>.</p></div>
</div>
</div>

<div class="footer">
<p>Contact: <a href="https://t.me/makliyoyo">@makliyoyo</a> (Telegram)</p>
<p>Support the server — TRC20 tip: <span class="tip">TMGNTSewaUwV9MB37aGVNqTsPXTjVF5e52</span></p>
<p style="margin-top:12px">Auction Monitor © 2026</p>
</div>

<script>
(async function loadDemo(){
try{
const resp=await fetch('/api/top_deals');
const data=await resp.json();
const items=data.items||[];
let html='<table><thead><tr><th>Item</th><th>Your Realm</th><th>Your Price</th><th>Avg Price</th><th>Discount</th><th>Current Cheapest</th></tr></thead><tbody>';
items.forEach(i=>{
const icon=i.icon_url?`<img src="${i.icon_url}" style="width:28px;height:28px;border-radius:5px;vertical-align:middle;margin-right:8px" onerror="this.style.display='none'">`:'';
html+=`<tr><td>${icon}<a href="${i.undermine_url}" target="_blank">${i.item_name}</a></td><td class="realm-g">${i.cheapest_realm}</td><td class="price-g">${i.cheapest_price}g</td><td>${i.top10avg}</td><td class="disc-g">${i.discount}</td><td class="realm-g">${i.current_cheapest} (${i.current_cheapest_price})</td></tr>`;
});
html+='</tbody></table>';
document.getElementById('demoTable').innerHTML=items.length?html:'<p style="padding:20px;color:var(--text-dim)">No deals yet. Sign in and start sniping!</p>';
}catch(e){document.getElementById('demoTable').innerHTML='<p style="padding:20px;color:var(--text-dim)">No deals yet.</p>';}
})();
</script>
</body></html>"""

HTML_MAIN = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Auction Monitor</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg: #0f1117;
            --surface: #1a1d25;
            --surface2: #23262f;
            --gold: #f5c842;
            --gold-dim: rgba(245,200,66,0.15);
            --text: #c8c8c8;
            --text-dim: #888;
            --green: #4ade80;
            --red: #f87171;
            --blue: #60a5fa;
            --sidebar-w: 320px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html { scroll-behavior: smooth; }
        body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; padding: 24px; min-height: 100vh; }
        .layout { display: flex; gap: 24px; max-width: 1600px; margin: 0 auto; align-items: flex-start; }
        .sidebar { width: var(--sidebar-w); min-width: var(--sidebar-w); flex-shrink: 0; background: var(--surface); border-radius: 16px; border: 1px solid rgba(255,255,255,0.04); display: flex; flex-direction: column; gap: 0; max-height: calc(100vh - 48px); overflow-y: auto; overflow-x: hidden; transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1), min-width 0.3s cubic-bezier(0.4, 0, 0.2, 1), padding 0.3s ease, margin 0.3s ease, opacity 0.25s ease, border 0s 0.3s; }
        .sidebar.collapsed { width: 0; min-width: 0; padding: 0; margin: 0; opacity: 0; border: none; pointer-events: none; }
        .sidebar-section { padding: 16px 18px; border-bottom: 1px solid rgba(255,255,255,0.04); }
        .sidebar-section:last-child { border-bottom: none; }
        .sidebar-section.search-section { padding-top: 14px; padding-bottom: 14px; }
        .sidebar-section.presets-section { padding-top: 12px; padding-bottom: 4px; }
        .sidebar-section.filters-section { padding-top: 10px; padding-bottom: 10px; }
        .sidebar-section.actions-section { padding-top: 8px; padding-bottom: 16px; display: flex; flex-direction: column; gap: 8px; }
        .section-label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; font-weight: 600; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; }
        .section-label i { font-size: 11px; color: var(--gold); width: 14px; text-align: center; }
        .section-label .collapse-arrow { font-size: 10px; transition: transform 0.2s; margin-left: auto; }
        .section-label .collapse-arrow.open { transform: rotate(90deg); }
        .section-body { transition: max-height 0.3s ease, opacity 0.2s ease; overflow: hidden; }
        .section-body.collapsed { max-height: 0; opacity: 0; margin: 0; padding: 0; }
        .snipe-toggle { display: flex; align-items: center; gap: 4px; font-size: 10px; color: var(--gold); cursor: pointer; margin-left: 8px; }
        .snipe-toggle input { accent-color: var(--gold); cursor: pointer; }
        .block-divider { border-top: 2px solid rgba(245,200,66,0.15); margin: 0 12px; }
        .search-input-wrap { position: relative; }
        .search-input-wrap input { width: 100%; padding: 10px 12px 10px 34px; background: var(--surface2); border: 1px solid rgba(255,255,255,0.06); color: #fff; border-radius: 10px; font-size: 13px; transition: all 0.2s; }
        .search-input-wrap input:focus { outline: none; border-color: var(--gold); box-shadow: 0 0 0 2px rgba(245,200,66,0.1); }
        .search-input-wrap i { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--text-dim); font-size: 13px; pointer-events: none; }
        .sidebar-toggle { position: fixed; left: 20px; top: 20px; z-index: 100; background: var(--surface2); border: 1px solid var(--gold-dim); color: var(--gold); width: 34px; height: 34px; border-radius: 8px; cursor: pointer; font-size: 15px; display: flex; align-items: center; justify-content: center; transition: all 0.2s; }
        .sidebar-toggle:hover { background: var(--gold); color: #0d0d0d; }
        /* --- Пресеты (компактный список) --- */
        .preset-list { display: flex; flex-direction: column; gap: 2px; }
        .preset-item { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 8px; cursor: pointer; transition: all 0.15s; border: 1px solid transparent; }
        .preset-item:hover { background: rgba(255,255,255,0.03); }
        .preset-item.active { background: var(--gold-dim); border-color: rgba(245,200,66,0.25); }
        .preset-item .preset-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; background: var(--text-dim); }
        .preset-item.active .preset-dot { background: var(--gold); box-shadow: 0 0 6px var(--gold-dim); }
        .preset-item .name { font-weight: 500; font-size: 13px; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .preset-item .preset-actions { display: flex; gap: 2px; flex-shrink: 0; opacity: 0; transition: opacity 0.15s; }
        .preset-item:hover .preset-actions { opacity: 1; }
        .preset-item .preset-actions button { background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 11px; padding: 3px 5px; border-radius: 4px; line-height: 1; }
        .preset-item .preset-actions button:hover { color: var(--gold); background: rgba(255,255,255,0.06); }
        .preset-item .star-btn { font-size: 12px; padding: 2px 4px; }
        .preset-item .star-btn.active { color: var(--gold); }
        .preset-add-btn { display: flex; align-items: center; gap: 6px; width: 100%; margin-top: 4px; padding: 7px 10px; background: none; border: 1px dashed rgba(255,255,255,0.08); border-radius: 8px; color: var(--text-dim); cursor: pointer; font-size: 12px; transition: all 0.2s; }
        .preset-add-btn:hover { border-color: var(--gold); color: var(--gold); background: rgba(245,200,66,0.04); }
        /* --- Фильтры --- */
        .filter-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
        .filter-row:last-child { margin-bottom: 0; }
        .filter-row label { font-size: 12px; color: var(--text-dim); white-space: nowrap; min-width: 0; }
        .filter-row input[type="number"] { width: 72px; background: var(--surface2); border: 1px solid rgba(255,255,255,0.06); color: #fff; padding: 8px 10px; border-radius: 8px; font-size: 12px; text-align: right; transition: all 0.2s; }
        .filter-row input[type="number"]:focus { outline: none; border-color: var(--gold); }
        .filter-row .filter-unit { font-size: 11px; color: var(--text-dim); }
        .tags-block { margin-bottom: 8px; }
        .tags-block:last-child { margin-bottom: 0; }
        .tags-container { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; max-height: 80px; overflow-y: auto; }
        .tag { background: rgba(245,200,66,0.1); color: var(--gold); border: 1px solid rgba(245,200,66,0.2); padding: 3px 7px; border-radius: 5px; font-size: 11px; display: inline-flex; align-items: center; gap: 5px; }
        .tag .remove { cursor: pointer; opacity: 0.6; font-weight: bold; font-size: 13px; line-height: 1; }
        .tag .remove:hover { opacity: 1; }
        .tag-input { width: 100%; background: var(--surface2); border: 1px solid rgba(255,255,255,0.06); color: #fff; padding: 7px 10px; border-radius: 7px; font-size: 12px; transition: all 0.2s; }
        .tag-input:focus { outline: none; border-color: var(--gold); }
        .tag-input::placeholder { color: var(--text-dim); font-size: 11px; }
        .btn { background: rgba(245,200,66,0.12); color: var(--gold); border: 1px solid rgba(245,200,66,0.25); padding: 9px 16px; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.2s; }
        .btn:hover { background: var(--gold); color: #0d0d0d; }
        .btn-secondary { background: var(--surface2); color: var(--text-dim); border: 1px solid rgba(255,255,255,0.06); }
        .btn-secondary:hover { background: var(--surface2); color: #fff; border-color: rgba(255,255,255,0.15); }
        .btn-icon { width: 34px; padding: 9px 0; text-align: center; }
        .btn-row { display: flex; gap: 6px; }
        .btn-row .btn { flex: 1; text-align: center; }
        .main-content { flex: 1; min-width: 0; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .logo { font-size: 24px; font-weight: 700; color: var(--gold); letter-spacing: -0.5px; display: flex; align-items: center; gap: 10px; }
        .pulse-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); display: inline-block; animation: pulse 2s ease-in-out infinite; }
        @keyframes pulse { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.75); } }
        .region-toggle { display: flex; gap: 2px; background: var(--surface2); border-radius: 8px; padding: 3px; }
        .region-btn { padding: 6px 14px; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; background: transparent; color: var(--text-dim); transition: all 0.2s; }
        .region-btn.active { background: var(--gold); color: #0d0d0d; }
        .region-btn:hover:not(.active) { color: #fff; background: rgba(255,255,255,0.04); }
        .update-times { font-size: 12px; color: var(--text-dim); background: var(--surface); padding: 6px 12px; border-radius: 10px; display: flex; flex-direction: column; gap: 2px; line-height: 1.5; }
        .update-times span { white-space: nowrap; font-variant-numeric: tabular-nums; }
        .user-info { margin-bottom: 16px; display: flex; gap: 16px; align-items: center; }
        .user-info a, .user-info span { color: var(--gold); text-decoration: none; font-size: 13px; }
        .deals-link { display: inline-flex; align-items: center; gap: 6px; background: var(--gold-dim); color: var(--gold); padding: 8px 16px; border-radius: 8px; border: 1px solid rgba(245,200,66,0.25); font-size: 13px; font-weight: 500; text-decoration: none; margin-bottom: 16px; transition: all 0.2s; }
        .deals-link:hover { background: var(--gold); color: #0d0d0d; }
        .deals-toggle { margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
        .selected-bar { background: var(--surface); border: 1px solid var(--gold-dim); border-radius: 8px; padding: 8px 14px; margin-bottom: 12px; display: none; align-items: center; gap: 12px; }
        .selected-bar span { color: var(--gold); font-size: 12px; word-break: break-all; }
        .table-wrapper { overflow-x: auto; border-radius: 12px; border: 1px solid rgba(255,255,255,0.04); background: var(--surface); position: relative; min-height: 200px; max-height: calc(100vh - 200px); overflow-y: auto; }
        .skeleton { display: flex; flex-direction: column; gap: 12px; padding: 20px; }
        .skeleton-row { display: flex; gap: 16px; }
        .skeleton-cell { background: linear-gradient(90deg, #23262f 25%, #2a2d36 50%, #23262f 75%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 8px; height: 20px; flex: 1; }
        @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
        table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 1050px; }
        thead { position: sticky; top: 0; z-index: 5; }
        th { background: var(--surface2); color: var(--gold); padding: 12px 10px; text-align: left; font-weight: 500; cursor: pointer; user-select: none; white-space: nowrap; position: sticky; top: 0; border-bottom: 2px solid var(--gold-dim); }
        th:hover { background: rgba(245,200,66,0.12); }
        th .sort-arrow { font-size: 10px; margin-left: 4px; opacity: 0.4; }
        th .sort-arrow.active { opacity: 1; }
        th.dragging { opacity: 0.5; background: var(--gold-dim); }
        th.drag-over { border-left: 2px solid var(--gold); }
        td { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.03); transition: background 0.3s ease; }
        tbody tr:nth-child(even) td { background: rgba(255,255,255,0.012); }
        tbody tr:hover td { background: rgba(245,200,66,0.06) !important; }
        tr.deal-row td { background: rgba(245,200,66,0.08) !important; }
        tr.deal-row:nth-child(even) td { background: rgba(245,200,66,0.06) !important; }
        tr.deal-row:hover td { background: rgba(245,200,66,0.14) !important; }
        tr.flash-green td { animation: flashGreen 1.2s ease-out; }
        tr.flash-red td { animation: flashRed 1.2s ease-out; }
        @keyframes flashGreen { 0% { background: rgba(74,222,128,0.3) !important; } 100% { background: inherit; } }
        @keyframes flashRed { 0% { background: rgba(248,113,113,0.3) !important; } 100% { background: inherit; } }
        .item-cell { display: flex; align-items: center; gap: 10px; }
        .item-cell img { width: 32px; height: 32px; border-radius: 6px; background: #1a1a1a; padding: 2px; }
        .item-cell a { color: #fff; text-decoration: none; font-weight: 500; }
        .item-cell a:hover { color: var(--gold); }
        .price { color: var(--green); font-weight: 500; cursor: help; }
        .realm { color: var(--blue); cursor: pointer; position: relative; }
        .realm:hover { color: var(--gold); text-decoration: underline; }
        .realm .realm-plus { display: inline-block; margin-left: 4px; color: var(--gold); font-weight: bold; opacity: 0; transition: opacity 0.2s; }
        .realm:hover .realm-plus { opacity: 1; }
        .realm-filtered { color: var(--gold) !important; }
        .realm-filter-badge { display: inline-flex; align-items: center; gap: 4px; background: var(--gold-dim); color: var(--gold); padding: 2px 8px; border-radius: 4px; font-size: 11px; margin-left: 4px; }
        .realm-filter-badge .clear { cursor: pointer; font-weight: bold; }
        .quantity { color: var(--text-dim); font-size: 12px; }
        .discount-high { color: var(--green); font-weight: 600; }
        .discount-medium { color: var(--gold); font-weight: 500; }
        .discount-low { color: var(--text-dim); }
        .discount-bar-wrap { display: flex; align-items: center; gap: 6px; }
        .discount-bar-bg { flex: 1; min-width: 40px; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; }
        .discount-bar-fill { height: 100%; border-radius: 3px; transition: width 0.4s ease; }
        .discount-bar-fill.high { background: var(--green); }
        .discount-bar-fill.medium { background: var(--gold); }
        .discount-bar-fill.low { background: var(--text-dim); }
        .deal-btn { background: none; border: none; color: var(--gold); cursor: pointer; font-size: 16px; padding: 2px 4px; border-radius: 4px; }
        .deal-btn:hover { background: rgba(245,200,66,0.1); }
        .checkbox-col { text-align: center; width: 36px; }
        .no-data { text-align: center; padding: 60px; color: var(--text-dim); }
        .toast { position: fixed; bottom: 24px; right: 24px; background: var(--surface2); border: 1px solid var(--gold); border-radius: 8px; padding: 12px 20px; color: #fff; z-index: 1000; display: none; }
        .back-to-top { position: fixed; bottom: 24px; right: 80px; width: 40px; height: 40px; background: var(--surface2); border: 1px solid var(--gold-dim); border-radius: 8px; color: var(--gold); cursor: pointer; font-size: 18px; display: none; align-items: center; justify-content: center; z-index: 100; transition: all 0.2s; }
        .back-to-top:hover { background: var(--gold); color: #0d0d0d; }
        .back-to-top.visible { display: flex; }
        .sound-toggle { background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 16px; padding: 4px 6px; border-radius: 4px; vertical-align: middle; }
        .sound-toggle:hover { color: var(--gold); }
        .sound-toggle.enabled { color: var(--gold); }
        .sparkline-tooltip {
            position: absolute; background: var(--surface2); border: 1px solid var(--gold); border-radius: 12px; padding: 6px;
            z-index: 999; display: none; box-shadow: 0 8px 24px rgba(0,0,0,0.6);
            opacity: 0; transform: translateY(4px); transition: opacity 0.2s ease, transform 0.2s ease; pointer-events: none;
        }
        .sparkline-tooltip.show { opacity: 1; transform: translateY(0); }
        /* --- Модалка графика цены --- */
        .chart-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 1000; display: none; align-items: center; justify-content: center; }
        .chart-modal-overlay.show { display: flex; }
        .chart-modal { background: var(--surface); border: 1px solid var(--gold-dim); border-radius: 16px; padding: 24px; width: 700px; max-width: 95vw; position: relative; }
        .chart-modal h3 { color: var(--gold); margin-bottom: 16px; font-size: 16px; }
        .chart-modal canvas { background: var(--surface2); border-radius: 10px; padding: 8px; }
        .chart-modal .close-btn { position: absolute; top: 12px; right: 16px; background: none; border: none; color: var(--text-dim); font-size: 20px; cursor: pointer; }
        .chart-modal .close-btn:hover { color: var(--gold); }
        .chart-icon { display: inline-block; margin-left: 6px; cursor: pointer; font-size: 14px; color: var(--gold); opacity: 0.7; transition: opacity 0.2s; }
        .chart-icon:hover { opacity: 1; }
        /* --- BoE фильтры --- */
        .boe-filter-card { background: var(--surface2); border-radius: 8px; padding: 10px 12px; margin-bottom: 6px; position: relative; }
        .boe-filter-card .bf-row { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
        .boe-filter-card .bf-row:last-child { margin-bottom: 0; }
        .boe-filter-card label { font-size: 11px; color: var(--text-dim); white-space: nowrap; min-width: 38px; }
        .boe-filter-card input[type="number"] { width: 52px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); color: #fff; padding: 5px 6px; border-radius: 5px; font-size: 11px; text-align: right; }
        .boe-filter-card input[type="number"]:focus { outline: none; border-color: var(--gold); }
        .boe-filter-card .bf-delete { position: absolute; top: 6px; right: 8px; background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 14px; }
        .boe-filter-card .bf-delete:hover { color: var(--red); }
        .boe-filter-card .bf-tags { display: flex; flex-wrap: wrap; gap: 3px; margin-bottom: 4px; }
        .boe-filter-card .bf-tags .tag { font-size: 10px; padding: 2px 6px; }
        .boe-filter-card .bf-tag-input { flex: 1; min-width: 60px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); color: #fff; padding: 5px 8px; border-radius: 5px; font-size: 11px; }
        .boe-filter-card .bf-tag-input:focus { outline: none; border-color: var(--gold); }
        .boe-filter-card .bf-tag-input::placeholder { color: var(--text-dim); font-size: 10px; }
        @media (max-width: 1100px) {
            .layout { flex-direction: column; }
            .sidebar { width: 100%; max-height: none; }
            .sidebar.collapsed { width: 0; padding: 0; }
            .sidebar-toggle { display: flex !important; }
        }
    </style>
</head>
<body>
<button class="sidebar-toggle" id="sidebarToggle" title="Скрыть/показать панель">☰</button>
<button class="back-to-top" id="backToTop" title="Наверх">↑</button>
<div class="layout">
    <div class="sidebar" id="sidebar">
        <!-- Поиск -->
        <div class="sidebar-section search-section">
            <div class="search-input-wrap">
                <i class="fas fa-search"></i>
                <input type="text" id="searchBox" placeholder="Поиск предметов..." onkeydown="if(event.key==='Enter')applyFilters()">
            </div>
        </div>
        <!-- Пресеты -->
        <div class="sidebar-section presets-section">
            <div class="section-label"><i class="fas fa-layer-group"></i> Пресеты</div>
            <div class="preset-list" id="presetsList"></div>
            <button class="preset-add-btn" onclick="saveCurrentPreset()"><i class="fas fa-plus"></i> Новый пресет</button>
        </div>
        <!-- Общие настройки -->
        <div class="sidebar-section filters-section">
            <div class="section-label" onclick="toggleSectionBody(this)">
                <i class="fas fa-cog"></i> Общие
                <span class="collapse-arrow open">▶</span>
            </div>
            <div class="section-body">
            <div class="tags-block">
                <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">Реалмы продажи</div>
                <div id="saleRealmTags" class="tags-container"></div>
                <input type="text" class="tag-input" id="saleRealmIdsInput" placeholder="Добавить ID... Enter" value="{{ default_sale_realm_ids }}" onkeydown="if(event.key==='Enter')addSaleRealmTag()">
            </div>
            </div>
        </div>
        <!-- Фильтры: Items -->
        <div class="sidebar-section filters-section">
            <div class="section-label" onclick="toggleSectionBody(this)">
                <i class="fas fa-sliders-h"></i> Items
                <span class="snipe-toggle" onclick="event.stopPropagation()"><input type="checkbox" id="itemsSnipeCb" checked onchange="itemsSnipe=this.checked;renderTable(currentItems,true)"> Snipe</span>
                <span class="collapse-arrow open">▶</span>
            </div>
            <div class="section-body">
            <div class="tags-block">
                <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;">ID предметов</div>
                <div id="itemIdsTags" class="tags-container"></div>
                <input type="text" class="tag-input" id="itemIdsInput" placeholder="Добавить ID... Enter" value="{{ default_item_ids }}" onkeydown="if(event.key==='Enter')addItemTag()">
            </div>
            <div class="filter-row">
                <label>Мин. скидка</label>
                <input type="number" id="discountInput" placeholder="0" step="0.1" oninput="applyFilters()">
                <span class="filter-unit">%</span>
            </div>
            <div class="filter-row">
                <label>Мин. Average</label>
                <input type="number" id="minTop10avgInput" placeholder="0" step="0.01" oninput="applyFilters()">
                <span class="filter-unit">g</span>
            </div>
            <div class="filter-row" style="margin-bottom:0;">
                <button class="sound-toggle" id="soundToggle" onclick="toggleSound()" title="Звук mega-скидок">🔇</button>
            </div>
            </div>
        </div>
        <div class="block-divider"></div>
        <!-- BoE фильтры -->
        <div class="sidebar-section filters-section">
            <div class="section-label" onclick="toggleSectionBody(this)">
                <i class="fas fa-crosshairs"></i> BoE
                <span class="snipe-toggle" onclick="event.stopPropagation()"><input type="checkbox" id="boeSnipeCb" checked onchange="boeSnipe=this.checked;renderTable(currentItems,true)"> Snipe</span>
                <span class="collapse-arrow open">▶</span>
            </div>
            <div class="section-body">
                <div id="boeFiltersContainer"></div>
                <button class="preset-add-btn" onclick="addBoeFilter()"><i class="fas fa-plus"></i> Добавить BoE фильтр</button>
            </div>
        </div>
        <!-- Действия -->
        <div class="sidebar-section actions-section">
            <div class="btn-row">
                <button class="btn" onclick="applyFilters()"><i class="fas fa-search"></i> Применить</button>
                <button class="btn btn-secondary" onclick="resetFilters()"><i class="fas fa-undo"></i> Сброс</button>
            </div>
        </div>
    </div>
    <div class="main-content">
        <div class="header">
            <div class="logo"><i class="fas fa-shield-halved"></i> Auction Monitor<span class="pulse-dot" id="pulseDot" title="Данные актуальны"></span></div>
            <div class="region-toggle" id="regionToggle">
                <button class="region-btn active" data-region="eu" onclick="setRegion('eu')">🇪🇺 EU</button>
                <button class="region-btn" data-region="us" onclick="setRegion('us')">🇺🇸 US</button>
            </div>
            <div class="update-times" id="update-times">
                <span id="eu-time">EU: загрузка...</span>
                <span id="us-time">US: загрузка...</span>
            </div>
        </div>
        <div class="user-info">
            {% if session.user_id %}
                <span><i class="fas fa-user"></i> {{ session.username }}</span>
                <span id="tierBadge" style="font-size:11px;padding:3px 10px;border-radius:12px;background:var(--surface2);color:var(--text-dim)">free</span>
                <a href="/account">Account</a>
                <a href="/logout">Logout</a>
                <a href="/old_deals">History</a>
            {% else %}
                <a href="/login">Войти</a>
                <a href="/register">Регистрация</a>
                <a href="/old_deals">История</a>
            {% endif %}
        </div>
        {% if session.user_id %}
            <a href="/my_deals" class="deals-link"><i class="fas fa-star"></i> Мои сделки</a>
        {% endif %}
        <div class="deals-toggle" style="margin-bottom:14px;">
            <label style="font-size:13px;display:flex;align-items:center;gap:6px;color:var(--text-dim);cursor:pointer;">
                <input type="checkbox" id="dealsOnlyCheck" onchange="applyFilters()"> Только сделки
            </label>
        </div>
        <div class="selected-bar" id="selectedBar">
            <span id="selectedIdsText"></span>
            <button class="btn" onclick="copySelectedIds()"><i class="fas fa-copy"></i> Копировать</button>
        </div>
        <div id="table-container"><div class="no-data">Загрузка...</div></div>
    </div>
</div>
<div class="toast" id="toast"></div>
<div class="sparkline-tooltip" id="sparklineTooltip"><canvas id="sparklineCanvas" width="200" height="70"></canvas></div>
<!-- Модалка графика -->
<div class="chart-modal-overlay" id="chartModalOverlay" onclick="closeChartModal(event)">
    <div class="chart-modal" onclick="event.stopPropagation()">
        <button class="close-btn" onclick="closeChartModal()">✕</button>
        <h3 id="chartModalTitle">История цены</h3>
        <canvas id="priceHistoryChart" width="650" height="300"></canvas>
    </div>
</div>

<script>
let currentItems = [];
let selectedIds = [];
let realmCounts = {};
let presets = [];
let activePresetId = null;
let lastModifiedStr = '';
let blizzardTimeStr = '';
let sortCol = 'discount';
let sortDir = -1; // -1 = desc (high discount first), 1 = asc
let realmFilterId = null; // фильтр по реалму
let soundEnabled = false;
let boeFilters = []; // [{ids:'', ilvl_min:0, ilvl_max:0, discount:0, topx:4}]
let itemsSnipe = true;   // галка Snipe в блоке Items
let boeSnipe = true;     // галка Snipe в блоке BoE
const MAX_BOE_FILTERS = 10;
const sparklineCache = {};
const itemNameCache = {};
const realmNameCache = {};
let prevPrices = {}; // item_id -> price, для flash-анимации
let currentRegion = (() => { try { return localStorage.getItem('wow_ah_region') || 'eu'; } catch(e){ return 'eu'; } })();

const defaultItemIds = "{{ default_item_ids }}";
const defaultSaleRealmIds = "{{ default_sale_realm_ids }}";

function setRegion(region) {
    currentRegion = region;
    try { localStorage.setItem('wow_ah_region', region); } catch(e) {}
    document.querySelectorAll('#regionToggle .region-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.region === region);
    });
    // Сбрасываем кэши
    for (let k in sparklineCache) delete sparklineCache[k];
    for (let k in itemNameCache) delete itemNameCache[k];
    for (let k in realmNameCache) delete realmNameCache[k];
    prevPrices = {};
    selectedIds = [];
    updateSelectedBar();
    loadPresetsFromServer();  // пресеты уникальны для региона
    applyFilters();
}
function regionParam() { return '&region=' + currentRegion; }

function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 2000);
}

// --- Сворачивание секций ---
function toggleSectionBody(labelEl) {
    const body = labelEl.nextElementSibling;
    const arrow = labelEl.querySelector('.collapse-arrow');
    if (body) {
        body.classList.toggle('collapsed');
        arrow.classList.toggle('open');
    }
}

// --- Сайдбар ---
function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('collapsed');
    document.getElementById('sidebarToggle').textContent =
        document.getElementById('sidebar').classList.contains('collapsed') ? '☰' : '✕';
}
document.getElementById('sidebarToggle').addEventListener('click', toggleSidebar);

// --- Кнопка вверх ---
function updateBackToTop() {
    const btn = document.getElementById('backToTop');
    const wrapper = document.querySelector('.table-wrapper');
    if (wrapper && wrapper.scrollTop > 400) btn.classList.add('visible');
    else if (!wrapper || wrapper.scrollTop <= 400) btn.classList.remove('visible');
}
document.getElementById('backToTop').addEventListener('click', () => {
    const wrapper = document.querySelector('.table-wrapper');
    if (wrapper) wrapper.scrollTo({top: 0, behavior: 'smooth'});
    else window.scrollTo({top: 0, behavior: 'smooth'});
});
document.addEventListener('scroll', updateBackToTop);

// --- Живой таймер (EU + US) ---
let euBlizzStr = '', usBlizzStr = '';
let euLastModified = '', usLastModified = '';
function formatBlizzTime(blizzStr, offsetHours, label) {
    if (!blizzStr) return 'нет данных';
    // Blizz формат: "Fri, 26 Jun 2026 15:25:49 GMT"
    const m = blizzStr.match(/(\\d{1,2})\\s(\\w{3})\\s(\\d{4})\\s(\\d{2}:\\d{2}:\\d{2})/i);
    if (!m) return blizzStr;
    const months = {jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',
                    jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'};
    const MM = months[m[2].toLowerCase()] || '??';
    const DD = m[1].padStart(2,'0');
    const YY = m[3].slice(2);
    const time = m[4];
    // Вычисляем локальное время с оффсетом
    const utcDate = new Date(`${m[3]}-${MM}-${DD}T${time}Z`);
    let hh = parseInt(time.split(':')[0]);
    let localH = (hh + offsetHours + 24) % 24;
    const hhStr = String(localH).padStart(2,'0');
    const sign = offsetHours >= 0 ? '+' : '';
    return `${DD}.${MM}.${YY} ${hhStr}:${time.slice(3)} GMT ${sign}${offsetHours}`;
}
function updateLiveTimer() {
    document.getElementById('eu-time').textContent = 'EU: ' + formatBlizzTime(euBlizzStr, 3, 'EU');
    document.getElementById('us-time').textContent = 'US: ' + formatBlizzTime(usBlizzStr, 0, 'US');
    const sec = euLastModified ? Math.floor((new Date() - new Date(euLastModified.replace(' ','T') + 'Z')) / 1000) : 999;
    document.getElementById('pulseDot').style.background = sec < 180 ? 'var(--green)' : 'var(--text-dim)';
}
setInterval(updateLiveTimer, 10000);

// --- Звук ---
function toggleSound() {
    soundEnabled = !soundEnabled;
    const btn = document.getElementById('soundToggle');
    btn.textContent = soundEnabled ? '🔊' : '🔇';
    btn.classList.toggle('enabled', soundEnabled);
    if (soundEnabled) showToast('Звук mega-скидок включён');
}
function playDealSound() {
    if (!soundEnabled) return;
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain); gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime(880, ctx.currentTime);
        osc.frequency.setValueAtTime(1100, ctx.currentTime + 0.1);
        gain.gain.setValueAtTime(0.15, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
        osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.3);
    } catch(e) {}
}

// --- BoE фильтры ---
function renderBoeFilters() {
    const container = document.getElementById('boeFiltersContainer');
    container.innerHTML = '';
    boeFilters.forEach((bf, idx) => {
        const card = document.createElement('div');
        card.className = 'boe-filter-card';
        const tagsHtml = (bf.ids || '').split(',').filter(Boolean).map(id =>
            `<span class="tag">${id.trim()}<span class="remove" onclick="removeBoeTag(${idx},'${id.trim()}')">×</span></span>`
        ).join('');
        card.innerHTML = `
            <button class="bf-delete" onclick="deleteBoeFilter(${idx})">✕</button>
            <div style="font-size:10px;color:var(--gold);margin-bottom:4px;">Фильтр #${idx + 1}</div>
            <div class="bf-tags">${tagsHtml}</div>
            <div style="display:flex;gap:4px;margin-bottom:4px;">
                <input class="bf-tag-input" placeholder="Добавить ID..." value=""
                    onkeydown="if(event.key==='Enter'){addBoeTag(${idx},this);event.preventDefault();}">
            </div>
            <div class="bf-row">
                <label>Ilvl</label>
                <input type="number" value="${bf.ilvl_min || 0}" placeholder="от" onchange="updateBoeField(${idx},'ilvl_min',this.value)">
                <span style="color:var(--text-dim);font-size:11px;">–</span>
                <input type="number" value="${bf.ilvl_max || 999}" placeholder="до" onchange="updateBoeField(${idx},'ilvl_max',this.value)">
            </div>
            <div class="bf-row">
                <label>Скидка ≥</label>
                <input type="number" value="${bf.discount || 0}" step="0.1" onchange="updateBoeField(${idx},'discount',this.value)">
                <span class="filter-unit">%</span>
                <label style="margin-left:6px;">Top</label>
                <input type="number" value="${bf.topx || 4}" min="2" max="50" onchange="updateBoeField(${idx},'topx',this.value)">
            </div>
        `;
        container.appendChild(card);
    });
}
function addBoeFilter() {
    if (boeFilters.length >= MAX_BOE_FILTERS) { showToast('Максимум ' + MAX_BOE_FILTERS + ' фильтров'); return; }
    boeFilters.push({ids: '', ilvl_min: 0, ilvl_max: 999, discount: 0, topx: 4});
    renderBoeFilters();
}
function deleteBoeFilter(idx) { boeFilters.splice(idx, 1); renderBoeFilters(); }
function addBoeTag(idx, input) {
    const val = input.value.trim(); if (!val) return;
    const ids = (boeFilters[idx].ids || '').split(',').filter(Boolean);
    val.split(',').forEach(v => { const t = v.trim(); if (t && !ids.includes(t)) ids.push(t); });
    boeFilters[idx].ids = ids.join(',');
    renderBoeFilters();
}
function removeBoeTag(idx, id) {
    const ids = (boeFilters[idx].ids || '').split(',').filter(Boolean).filter(v => v.trim() !== id);
    boeFilters[idx].ids = ids.join(',');
    renderBoeFilters();
}
function updateBoeField(idx, field, value) {
    boeFilters[idx][field] = value;
}

// --- Пресеты ---
async function loadPresetsFromServer() {
    const resp = await fetch('/api/presets?region=' + currentRegion);
    const data = await resp.json();
    presets = data.presets || [];
    renderPresetsList();
    const defaultPreset = presets.find(p => p.is_default && p.editable);
    if (defaultPreset) {
        applyPreset(defaultPreset.id);
    } else if (presets.length > 0) {
        applyPreset(presets[0].id);
    }
}

function renderPresetsList() {
    const container = document.getElementById('presetsList');
    container.innerHTML = '';
    presets.forEach(p => {
        const div = document.createElement('div');
        div.className = 'preset-item' + (p.id == activePresetId ? ' active' : '');
        div.innerHTML = `
            <span class="preset-dot"></span>
            <span class="name" onclick="applyPreset(${p.id})" title="${p.name}">${p.name}</span>
            <span class="preset-actions">
                ${p.editable ? `<button class="star-btn ${p.is_default ? 'active' : ''}" onclick="event.stopPropagation(); setDefaultPreset(${p.id})" title="По умолчанию"><i class="fas fa-star"></i></button>` : '<span style="font-size:11px;color:var(--text-dim)">общий</span>'}
                ${p.editable ? `<button onclick="event.stopPropagation(); quickSavePreset(${p.id})" title="Перезаписать"><i class="fas fa-save"></i></button>` : ''}
                ${p.editable ? `<button onclick="event.stopPropagation(); renamePreset(${p.id})" title="Переименовать"><i class="fas fa-pen"></i></button>` : ''}
                ${p.editable ? `<button onclick="event.stopPropagation(); deletePreset(${p.id})" title="Удалить"><i class="fas fa-trash"></i></button>` : ''}
            </span>
        `;
        container.appendChild(div);
    });
}

async function quickSavePreset(id) {
    const preset = presets.find(p => p.id == id);
    if (!preset || !preset.editable) return;
    const s = getCurrentSettings();
    const resp = await fetch('/api/presets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ id, name: preset.name, item_ids: s.item_ids, sale_realm_ids: s.sale_realm_ids, discount: s.discount || null, min_top10avg: s.min_top10avg || null, deals_only: s.deals_only, boe_filters: s.boe_filters, region: currentRegion })
    });
    if (resp.ok) { showToast('Пресет обновлён'); } else { showToast('Ошибка'); }
}

async function applyPreset(id) {
    const preset = presets.find(p => p.id == id);
    if (!preset) return;
    applySettings({
        item_ids: preset.item_ids,
        sale_realm_ids: preset.sale_realm_ids,
        discount: preset.discount || '',
        min_top10avg: preset.min_top10avg || '',
        deals_only: preset.deals_only,
        search: '',
        boe_filters: preset.boe_filters || '[]'
    });
    activePresetId = id;
    renderPresetsList();
    applyFilters();
}

async function setDefaultPreset(id) {
    await fetch(`/api/presets/${id}/set_default`, { method: 'POST' });
    await loadPresetsFromServer();
    applyPreset(id);
    showToast('Пресет по умолчанию установлен и применён');
}

async function renamePreset(id) {
    const preset = presets.find(p => p.id == id);
    if (!preset || !preset.editable) return;
    const name = prompt('Новое название:', preset.name);
    if (!name) return;
    const resp = await fetch('/api/presets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ id, name, item_ids: preset.item_ids, sale_realm_ids: preset.sale_realm_ids, discount: preset.discount, min_top10avg: preset.min_top10avg, deals_only: preset.deals_only, boe_filters: preset.boe_filters || '[]', region: currentRegion })
    });
    if (resp.ok) { await loadPresetsFromServer(); showToast('Пресет переименован'); }
}

async function deletePreset(id) {
    const preset = presets.find(p => p.id == id);
    if (!preset || !preset.editable) return;
    if (confirm('Удалить пресет «' + preset.name + '»?')) {
        await fetch('/api/presets/' + id, { method: 'DELETE' });
        if (activePresetId == id) activePresetId = null;
        await loadPresetsFromServer();
        showToast('Пресет удалён');
    }
}

async function saveCurrentPreset() {
    if (!{{ 'true' if session.user_id else 'false' }}) { showToast('Войдите, чтобы сохранять пресеты'); return; }
    const name = prompt('Название нового пресета:');
    if (!name) return;
    const s = getCurrentSettings();
    const resp = await fetch('/api/presets', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name, item_ids: s.item_ids, sale_realm_ids: s.sale_realm_ids, discount: s.discount || null, min_top10avg: s.min_top10avg || null, deals_only: s.deals_only, boe_filters: s.boe_filters, region: currentRegion })
    });
    if (resp.ok) { await loadPresetsFromServer(); showToast('Пресет сохранён'); }
}

// --- Теги и фильтры ---
function getItemIdsFromTags() {
    const container = document.getElementById('itemIdsTags');
    const tags = container.querySelectorAll('.tag');
    return Array.from(tags).map(t => t.dataset.id).join(',');
}
function getSaleRealmIdsFromTags() {
    const container = document.getElementById('saleRealmTags');
    const tags = container.querySelectorAll('.tag');
    return Array.from(tags).map(t => t.dataset.id).join(',');
}

function renderItemTags(idsStr) {
    const container = document.getElementById('itemIdsTags');
    container.innerHTML = '';
    if (!idsStr) return;
    idsStr.split(',').forEach(id => {
        const idTrim = id.trim(); if (!idTrim) return;
        const tag = document.createElement('span'); tag.className = 'tag'; tag.dataset.id = idTrim;
        tag.innerHTML = `${idTrim} <span class="remove" onclick="removeItemTag(this)">×</span>`;
        tag.addEventListener('mouseenter', async () => {
            if (!itemNameCache[idTrim]) {
                const resp = await fetch(`/api/item_name?item_id=${idTrim}${regionParam()}`);
                const data = await resp.json();
                itemNameCache[idTrim] = data.name || '?';
            }
            tag.title = itemNameCache[idTrim];
        });
        container.appendChild(tag);
    });
}
function renderSaleRealmTags(idsStr) {
    const container = document.getElementById('saleRealmTags');
    container.innerHTML = '';
    if (!idsStr) return;
    idsStr.split(',').forEach(id => {
        const idTrim = id.trim(); if (!idTrim) return;
        const tag = document.createElement('span'); tag.className = 'tag'; tag.dataset.id = idTrim;
        tag.innerHTML = `${idTrim} <span class="remove" onclick="removeSaleRealmTag(this)">×</span>`;
        tag.addEventListener('mouseenter', async () => {
            if (!realmNameCache[idTrim]) {
                const resp = await fetch(`/api/realm_name?realm_id=${idTrim}${regionParam()}`);
                const data = await resp.json();
                realmNameCache[idTrim] = data.name || '?';
            }
            tag.title = realmNameCache[idTrim];
        });
        container.appendChild(tag);
    });
}

function addItemTag() {
    const input = document.getElementById('itemIdsInput');
    const val = input.value.trim();
    if (!val) return;
    val.split(',').forEach(id => {
        const idTrim = id.trim(); if (!idTrim) return;
        const container = document.getElementById('itemIdsTags');
        if (!container.querySelector(`[data-id="${idTrim}"]`)) {
            const tag = document.createElement('span'); tag.className = 'tag'; tag.dataset.id = idTrim;
            tag.innerHTML = `${idTrim} <span class="remove" onclick="removeItemTag(this)">×</span>`;
            container.appendChild(tag);
        }
    });
    input.value = '';
}
function removeItemTag(el) { el.parentElement.remove(); }
function addSaleRealmTag() {
    const input = document.getElementById('saleRealmIdsInput');
    const val = input.value.trim();
    if (!val) return;
    val.split(',').forEach(id => {
        const idTrim = id.trim(); if (!idTrim) return;
        const container = document.getElementById('saleRealmTags');
        if (!container.querySelector(`[data-id="${idTrim}"]`)) {
            const tag = document.createElement('span'); tag.className = 'tag'; tag.dataset.id = idTrim;
            tag.innerHTML = `${idTrim} <span class="remove" onclick="removeSaleRealmTag(this)">×</span>`;
            container.appendChild(tag);
        }
    });
    input.value = '';
    localStorage.setItem('saleRealmIds', getSaleRealmIdsFromTags());
}
function removeSaleRealmTag(el) { el.parentElement.remove(); localStorage.setItem('saleRealmIds', getSaleRealmIdsFromTags()); }

function getCurrentSettings() {
    return {
        item_ids: getItemIdsFromTags(),
        sale_realm_ids: getSaleRealmIdsFromTags(),
        discount: document.getElementById('discountInput').value.trim(),
        min_top10avg: document.getElementById('minTop10avgInput').value.trim(),
        deals_only: document.getElementById('dealsOnlyCheck').checked,
        search: document.getElementById('searchBox').value.trim(),
        boe_filters: JSON.stringify(boeFilters)
    };
}

function applySettings(settings) {
    renderItemTags(settings.item_ids || '');
    renderSaleRealmTags(settings.sale_realm_ids || '');
    document.getElementById('discountInput').value = settings.discount || '';
    document.getElementById('minTop10avgInput').value = settings.min_top10avg || '';
    document.getElementById('dealsOnlyCheck').checked = settings.deals_only || false;
    document.getElementById('searchBox').value = settings.search || '';
    // BoE filters
    if (settings.boe_filters) {
        try { boeFilters = JSON.parse(settings.boe_filters); } catch(e) { boeFilters = []; }
    } else { boeFilters = []; }
    renderBoeFilters();
}

function showSkeleton() {
    document.getElementById('table-container').innerHTML = `
        <div class="table-wrapper">
            <div class="skeleton">
                <div class="skeleton-row"><div class="skeleton-cell" style="width:100%"></div></div>
                <div class="skeleton-row"><div class="skeleton-cell" style="width:80%"></div><div class="skeleton-cell" style="width:20%"></div></div>
                <div class="skeleton-row"><div class="skeleton-cell" style="width:70%"></div><div class="skeleton-cell" style="width:30%"></div></div>
                <div class="skeleton-row"><div class="skeleton-cell" style="width:90%"></div><div class="skeleton-cell" style="width:10%"></div></div>
                <div class="skeleton-row"><div class="skeleton-cell" style="width:60%"></div><div class="skeleton-cell" style="width:40%"></div></div>
            </div>
        </div>
    `;
}

// --- Фильтр по реалму ---
function filterByRealm(realmId) {
    if (realmFilterId === realmId) {
        realmFilterId = null; // сброс
        showToast('Фильтр по реалму сброшен');
    } else {
        realmFilterId = realmId;
        const name = realmNameCache[realmId] || realmId;
        showToast('Фильтр: дешёвые на «' + name + '»');
    }
    renderTable(currentItems, true); // перерендер без запроса
}

// --- Сортировка ---
function setSort(col) {
    if (sortCol === col) { sortDir *= -1; }
    else { sortCol = col; sortDir = -1; }
    renderTable(currentItems, true);
}

function sortItems(items) {
    return [...items].sort((a, b) => {
        let va, vb;
        switch (sortCol) {
            case 'item_name': va = a.item_name.toLowerCase(); vb = b.item_name.toLowerCase(); break;
            case 'cheapest_price': va = a.cheapest_price_raw; vb = b.cheapest_price_raw; break;
            case 'discount': va = a.discount_raw != null ? a.discount_raw : -999; vb = b.discount_raw != null ? b.discount_raw : -999; break;
            case 'cheapest_realm': va = a.cheapest_realm.toLowerCase(); vb = b.cheapest_realm.toLowerCase(); break;
            case 'sale_realm': va = a.sale_realm.toLowerCase(); vb = b.sale_realm.toLowerCase(); break;
            case 'sale_price': va = a.sale_price === '—' ? 0 : parseFloat(a.sale_price.replace('.','')) || 0; vb = b.sale_price === '—' ? 0 : parseFloat(b.sale_price.replace('.','')) || 0; break;
            case 'top10avg': va = a.top10avg === '—' ? 0 : parseFloat(a.top10avg.replace('.','')) || 0; vb = b.top10avg === '—' ? 0 : parseFloat(b.top10avg.replace('.','')) || 0; break;
            case 'quantity': va = a.quantity || 0; vb = b.quantity || 0; break;
            default: va = 0; vb = 0;
        }
        if (typeof va === 'string') return sortDir * va.localeCompare(vb);
        return sortDir * (va - vb);
    });
}

function sortArrow(col) {
    if (sortCol !== col) return '';
    return sortDir === -1 ? ' ▼' : ' ▲';
}

// --- Перетаскивание колонок ---
let dragSrcTh = null;
function initDragColumns() {
    document.querySelectorAll('th[draggable]').forEach(th => {
        th.addEventListener('dragstart', e => {
            dragSrcTh = th;
            th.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });
        th.addEventListener('dragend', e => {
            th.classList.remove('dragging');
            document.querySelectorAll('th').forEach(t => t.classList.remove('drag-over'));
            dragSrcTh = null;
        });
        th.addEventListener('dragover', e => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
        });
        th.addEventListener('dragenter', e => {
            e.preventDefault();
            if (th !== dragSrcTh) th.classList.add('drag-over');
        });
        th.addEventListener('dragleave', e => {
            th.classList.remove('drag-over');
        });
        th.addEventListener('drop', e => {
            e.preventDefault();
            th.classList.remove('drag-over');
            if (dragSrcTh && dragSrcTh !== th) {
                const table = th.closest('table');
                const rows = table.querySelectorAll('tr');
                const srcIdx = Array.from(dragSrcTh.parentElement.children).indexOf(dragSrcTh);
                const dstIdx = Array.from(th.parentElement.children).indexOf(th);
                rows.forEach(row => {
                    const cells = row.children;
                    if (srcIdx < dstIdx) {
                        row.insertBefore(cells[srcIdx], cells[dstIdx + 1]);
                    } else {
                        row.insertBefore(cells[srcIdx], cells[dstIdx]);
                    }
                });
            }
        });
    });
}

async function fetchData() {
    showSkeleton();
    const s = getCurrentSettings();
    // Сохраняем реалмы продажи — для страницы «Мои сделки»
    localStorage.setItem('saleRealmIds', s.sale_realm_ids);
    const params = new URLSearchParams();
    if (s.item_ids) params.set('item_ids', s.item_ids);
    if (s.sale_realm_ids) params.set('sale_realm_ids', s.sale_realm_ids);
    if (s.discount) params.set('discount', s.discount);
    if (s.min_top10avg !== '' && !isNaN(parseFloat(s.min_top10avg))) params.set('min_top10avg', s.min_top10avg);
    if (s.deals_only) params.set('deals_only', '1');
    if (s.search) params.set('search', s.search);
    if (s.boe_filters) params.set('boe_filters', s.boe_filters);

    try {
        const resp = await fetch('/api/data?' + params.toString() + regionParam());
        const data = await resp.json();
        lastModifiedStr = data.last_modified || '';
        // update both region timers from api/update_times (polled separately)
        updateLiveTimer();
        currentItems = data.items || [];
        realmCounts = data.realm_counts || {};
        // Don't clear selectedIds — keep checkbox state across refreshes
        updateSelectedBar();

        // Flash-анимация: сравниваем цены
        const newPrices = {};
        currentItems.forEach(it => { newPrices[it.item_id] = it.cheapest_price_raw; });
        if (Object.keys(prevPrices).length > 0) {
            currentItems.forEach(it => {
                const oldP = prevPrices[it.item_id];
                if (oldP !== undefined && oldP !== it.cheapest_price_raw) {
                    it._flash = it.cheapest_price_raw < oldP ? 'green' : 'red';
                }
            });
        }
        prevPrices = newPrices;

        renderTable(currentItems, false);
    } catch (e) {
        document.getElementById('table-container').innerHTML = '<div class="no-data">Ошибка загрузки</div>';
    }
}

function renderTable(items, preserveScroll) {
    const container = document.getElementById('table-container');
    if (!items.length) { container.innerHTML = '<div class="no-data">Нет предметов</div>'; return; }

    // Сохраняем позицию скролла
    const wrapper = container.querySelector('.table-wrapper');
    const scrollTop = wrapper ? wrapper.scrollTop : 0;

    // Применяем фильтр по реалму
    let filtered = items;
    if (realmFilterId !== null) {
        filtered = items.filter(it => it.cheapest_realm_id == realmFilterId);
    }
    // Снайп-фильтры
    if (!itemsSnipe) filtered = filtered.filter(it => (it.ilvl || 0) > 0);   // только BoE
    if (!boeSnipe)   filtered = filtered.filter(it => (it.ilvl || 0) === 0);  // только не-BoE

    // Сортировка
    const sorted = sortItems(filtered);

    // Проверка на mega-скидки
    const hasMega = sorted.some(it => it.discount_raw != null && it.discount_raw >= 80);
    if (hasMega && soundEnabled) playDealSound();

    let html = `<div class="table-wrapper"><table><thead><tr>
        <th class="checkbox-col">☐</th>
        <th draggable="true" onclick="setSort('item_name')">Предмет<span class="sort-arrow${sortCol==='item_name'?' active':''}">${sortArrow('item_name')}</span></th>
        <th draggable="true" onclick="setSort('cheapest_price')">Цена (g)<span class="sort-arrow${sortCol==='cheapest_price'?' active':''}">${sortArrow('cheapest_price')}</span></th>
        <th draggable="true" onclick="setSort('discount')">Скидка<span class="sort-arrow${sortCol==='discount'?' active':''}">${sortArrow('discount')}</span></th>
        <th draggable="true" onclick="setSort('cheapest_realm')">Дешёвый реалм<span class="sort-arrow${sortCol==='cheapest_realm'?' active':''}">${sortArrow('cheapest_realm')}</span></th>
        <th draggable="true" onclick="setSort('quantity')">Кол-во<span class="sort-arrow${sortCol==='quantity'?' active':''}">${sortArrow('quantity')}</span></th>
        <th draggable="true" onclick="setSort('sale_realm')">Продажа<span class="sort-arrow${sortCol==='sale_realm'?' active':''}">${sortArrow('sale_realm')}</span></th>
        <th draggable="true" onclick="setSort('sale_price')">Цена продажи<span class="sort-arrow${sortCol==='sale_price'?' active':''}">${sortArrow('sale_price')}</span></th>
        <th draggable="true" onclick="setSort('top10avg')">Average<span class="sort-arrow${sortCol==='top10avg'?' active':''}">${sortArrow('top10avg')}</span></th>
        <th>Топ-3</th>
        <th></th>
    </tr></thead><tbody>`;

    sorted.forEach(item => {
        const iconHtml = `<img src="${item.icon_url}" onerror="this.style.display='none'" alt="">`;
        const discVal = item.discount === '—' ? -1 : parseFloat(item.discount);
        let discClass = 'discount-low';
        let barClass = 'low';
        let barPct = 0;
        if (discVal >= 50) { discClass = 'discount-high'; barClass = 'high'; }
        else if (discVal >= 30) { discClass = 'discount-medium'; barClass = 'medium'; }
        if (discVal > 0) barPct = Math.min(100, discVal);

        const rowClass = (item.is_deal ? 'deal-row ' : '') + (item._flash ? `flash-${item._flash}` : '');
        const realmFilterClass = realmFilterId === item.cheapest_realm_id ? ' realm-filtered' : '';
        const realmBadge = realmFilterId === item.cheapest_realm_id ? `<span class="realm-filter-badge"><span class="clear" onclick="event.stopPropagation();filterByRealm(${item.cheapest_realm_id})">×</span></span>` : '';

        // Discount bar
        const discountCell = discVal >= 0
            ? `<div class="discount-bar-wrap"><div class="discount-bar-bg"><div class="discount-bar-fill ${barClass}" style="width:${barPct}%"></div></div><span class="${discClass}">${item.discount}</span></div>`
            : `<span class="${discClass}">${item.discount}</span>`;

        // Иконка графика для mega-сделок (скидка >80% и avg10 > 10000g)
        const isMega = discVal >= 80 && item.top10avg_raw && item.top10avg_raw > 100000000;  // 10000g = 100000000 copper
        const chartIconHtml = isMega
            ? `<span class="chart-icon" onclick="event.stopPropagation();openChartModal(${item.item_id}, ${item.ilvl || 0}, '${item.item_name.replace(/'/g, "\\'")}')" title="График цены за 7 дней">📈</span>`
            : '';

        html += `<tr class="${rowClass}">
            <td class="checkbox-col"><input type="checkbox" ${selectedIds.includes(item.item_id) ? 'checked' : ''} onchange="toggleSelect(${item.item_id})"></td>
            <td><div class="item-cell">${iconHtml}<a href="${item.undermine_url}" target="_blank">${item.item_name}</a>${chartIconHtml}</div></td>
            <td class="price" data-item-id="${item.item_id}" data-ilvl="${item.ilvl || 0}" onmouseenter="showSparkline(event, ${item.item_id}, ${item.ilvl || 0})" onmouseleave="hideSparkline()">${item.cheapest_price}</td>
            <td>${discountCell}</td>
            <td class="realm${realmFilterClass}" onclick="filterByRealm(${item.cheapest_realm_id})" title="Фильтровать по этому реалму">${item.cheapest_realm}<span class="realm-plus">+</span>${realmBadge}</td>
            <td class="quantity">${item.quantity || 0}</td>
            <td class="realm">${item.sale_realm}</td>
            <td class="price">${item.sale_price}</td>
            <td style="color:var(--text-dim)">${item.top10avg}</td>
            <td style="font-size:11px;color:var(--text-dim)">${item.top3}</td>
            <td><button class="deal-btn" onclick="toggleDeal(${item.item_id}, ${item.cheapest_realm_id}, ${item.cheapest_price_raw}, this)">${item.is_deal ? '★' : '☆'}</button></td>
        </tr>`;
    });
    html += '</tbody></table></div>';
    container.innerHTML = html;

    // Восстановить скролл
    if (preserveScroll) {
        const newWrapper = container.querySelector('.table-wrapper');
        if (newWrapper) newWrapper.scrollTop = scrollTop;
    }

    // Слушатели скролла для кнопки вверх
    const newWrapper = container.querySelector('.table-wrapper');
    if (newWrapper) newWrapper.addEventListener('scroll', updateBackToTop);

    // Инициализация перетаскивания
    initDragColumns();
}

// --- Sparkline, чекбоксы, сделки ---
async function showSparkline(e, itemId, ilvl) {
    ilvl = ilvl || 0;
    const cacheKey = itemId + '_' + ilvl;
    const tooltip = document.getElementById('sparklineTooltip');
    const canvas = document.getElementById('sparklineCanvas');
    const ctx = canvas.getContext('2d');
    canvas.width = 200; canvas.height = 70;
    tooltip.style.display = 'block';
    if (sparklineCache[cacheKey]) {
        drawSparkline(ctx, sparklineCache[cacheKey]);
        positionTooltip(e);
        return;
    }
    try {
        const resp = await fetch(`/api/sparkline?item_id=${itemId}&ilvl=${ilvl}${regionParam()}`);
        const data = await resp.json();
        const points = data.points || [];
        sparklineCache[cacheKey] = points;
        drawSparkline(ctx, points);
        positionTooltip(e);
    } catch (err) {}
}
function drawSparkline(ctx, points) {
    const w = ctx.canvas.width, h = ctx.canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (points.length < 2) return;
    const prices = points.map(p => p.price);
    const golds  = points.map(p => p.gold);
    const min = Math.min(...prices), max = Math.max(...prices);
    const range = max - min || 1;
    const pad = 12; // отступ для текста сверху/снизу
    const graphH = h - pad * 2;
    const graphTop = pad;
    const stepX = w / (points.length - 1);

    // Фоновая заливка
    const gradient = ctx.createLinearGradient(0, 0, w, 0);
    gradient.addColorStop(0, '#f5c842'); gradient.addColorStop(1, '#f59e0b');
    ctx.beginPath(); ctx.strokeStyle = gradient; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    points.forEach((p, i) => {
        const x = i * stepX;
        const y = graphTop + graphH - ((p.price - min) / range) * graphH;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    // Заполнение под графиком
    const lastX = (points.length - 1) * stepX;
    ctx.lineTo(lastX, graphTop + graphH);
    ctx.lineTo(0, graphTop + graphH);
    ctx.closePath();
    const fillGrad = ctx.createLinearGradient(0, 0, 0, h);
    fillGrad.addColorStop(0, 'rgba(245,200,66,0.2)');
    fillGrad.addColorStop(1, 'rgba(245,200,66,0)');
    ctx.fillStyle = fillGrad; ctx.fill();

    // Подписи: макс (золото), мин (золото), текущая
    ctx.fillStyle = '#888'; ctx.font = '9px Inter, sans-serif';
    ctx.textAlign = 'left';
    // Макс — вверху слева
    const maxG = golds.reduce((a,b) => a > b ? a : b, 0);
    ctx.fillText(maxG.toFixed(0) + 'g', 3, graphTop + 10);
    // Мин — внизу слева
    const minG = golds.reduce((a,b) => a < b ? a : b, 0);
    ctx.fillText(minG.toFixed(0) + 'g', 3, graphTop + graphH - 2);
    // Текущая — справа
    const curG = golds[golds.length - 1];
    ctx.fillStyle = '#f5c842'; ctx.textAlign = 'right';
    ctx.fillText(curG.toFixed(0) + 'g', w - 3, graphTop + 10);
}
function positionTooltip(e) {
    const tooltip = document.getElementById('sparklineTooltip');
    const rect = e.target.getBoundingClientRect();
    const x = rect.left + window.scrollX + rect.width / 2 - 100;
    let y = rect.top + window.scrollY - 80;
    if (y < window.scrollY + 10) y = rect.bottom + window.scrollY + 8;
    tooltip.style.left = x + 'px'; tooltip.style.top = y + 'px';
    tooltip.classList.add('show');
}
function hideSparkline() {
    const tooltip = document.getElementById('sparklineTooltip');
    tooltip.classList.remove('show');
    setTimeout(() => { if (!tooltip.classList.contains('show')) tooltip.style.display = 'none'; }, 200);
}

function updateSelectedBar() {
    const bar = document.getElementById('selectedBar'), text = document.getElementById('selectedIdsText');
    if (selectedIds.length === 0) bar.style.display = 'none';
    else { bar.style.display = 'flex'; text.textContent = selectedIds.map(id => id + ',').join(' '); }
}
function toggleSelect(itemId) { const idx = selectedIds.indexOf(itemId); if (idx > -1) selectedIds.splice(idx, 1); else selectedIds.push(itemId); updateSelectedBar(); renderTable(currentItems, true); }
function copySelectedIds() { if (selectedIds.length === 0) { showToast('Ничего не выбрано'); return; } navigator.clipboard.writeText(selectedIds.join(',') + ','); showToast('Скопировано!'); }

async function toggleDeal(itemId, realmId, price, btn) {
    const item = currentItems.find(it => it.item_id == itemId && it.cheapest_realm_id == realmId && it.cheapest_price_raw == price);
    if (item) {
        item.is_deal = !item.is_deal;
        btn.innerHTML = item.is_deal ? '★' : '☆';
        const row = btn.closest('tr');
        if (row) row.classList.toggle('deal-row', item.is_deal);

        const dealData = {
            item_name: item.item_name,
            ilvl: item.ilvl || 0,
            discount: item.discount,
            cheapest_realm: item.cheapest_realm,
            sale_realm: item.sale_realm,
            sale_price: item.sale_price,
            top10avg: item.top10avg,
            top3: item.top3,
            undermine_url: item.undermine_url,
            icon_url: item.icon_url,
            region: currentRegion
        };

        try {
            const resp = await fetch('/api/toggle_deal', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ item_id: itemId, realm_id: realmId, price: price, ...dealData })
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                showToast(err.error || 'Ошибка сервера');
                item.is_deal = !item.is_deal;
                btn.innerHTML = item.is_deal ? '★' : '☆';
                const row = btn.closest('tr');
                if (row) row.classList.toggle('deal-row', item.is_deal);
                return;
            }
            const data = await resp.json();
            if (data.added !== undefined && data.added !== item.is_deal) {
                item.is_deal = data.added;
                btn.innerHTML = item.is_deal ? '★' : '☆';
                const row = btn.closest('tr');
                if (row) row.classList.toggle('deal-row', item.is_deal);
            }
            // Сразу перерисовать — чтобы «Только сделки» увидел изменения без задержки
            if (document.getElementById('dealsOnlyCheck').checked) {
                renderTable(currentItems, true);
            }
        } catch (e) {
            console.error('toggleDeal error:', e);
            item.is_deal = !item.is_deal;
            btn.innerHTML = item.is_deal ? '★' : '☆';
            const row = btn.closest('tr');
            if (row) row.classList.toggle('deal-row', item.is_deal);
            showToast('Ошибка сети');
        }
    }
}

function applyFilters() { realmFilterId = null; fetchData(); }
function resetFilters() {
    document.getElementById('searchBox').value = '';
    renderItemTags(defaultItemIds);
    renderSaleRealmTags(defaultSaleRealmIds);
    document.getElementById('discountInput').value = '';
    document.getElementById('minTop10avgInput').value = '';
    document.getElementById('dealsOnlyCheck').checked = false;
    realmFilterId = null;
    fetchData();
}

// --- Модалка графика цены ---
let priceChartInstance = null;
async function openChartModal(itemId, ilvl, itemName) {
    ilvl = ilvl || 0;
    document.getElementById('chartModalTitle').textContent = '📈 ' + itemName + ' — 7 дней';
    document.getElementById('chartModalOverlay').classList.add('show');
    try {
        const resp = await fetch(`/api/price_chart?item_id=${itemId}&ilvl=${ilvl}${regionParam()}`);
        const data = await resp.json();
        const ctx = document.getElementById('priceHistoryChart').getContext('2d');
        if (priceChartInstance) priceChartInstance.destroy();
        priceChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.labels || [],
                datasets: [{
                    label: 'Мин. цена (золото)',
                    data: (data.prices || []).map(p => p / 10000),
                    borderColor: '#f5c842',
                    backgroundColor: 'rgba(245,200,66,0.08)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 0,
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#c8c8c8' } } },
                scales: {
                    x: { ticks: { color: '#888', maxTicksLimit: 10, maxRotation: 45 } },
                    y: { ticks: { color: '#888', callback: v => v.toFixed(0) + 'g' } }
                }
            }
        });
    } catch(e) {}
}
function closeChartModal(e) {
    if (e && e.target !== document.getElementById('chartModalOverlay')) return;
    document.getElementById('chartModalOverlay').classList.remove('show');
}

// --- Горячие клавиши ---
document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    switch(e.key.toLowerCase()) {
        case 's': e.preventDefault(); document.getElementById('searchBox').focus(); break;
        case 'r': e.preventDefault(); resetFilters(); break;
        case 'd': e.preventDefault(); const cb = document.getElementById('dealsOnlyCheck'); cb.checked = !cb.checked; applyFilters(); break;
    }
});

// инициализация
renderItemTags(defaultItemIds);
renderSaleRealmTags(defaultSaleRealmIds);
renderBoeFilters();
(async function init() {
    // Tier badge
    fetch('/api/me').then(r=>r.json()).then(d=>{
        if(d.logged_in){
            const b=document.getElementById('tierBadge');
            if(b){b.textContent=d.tier;b.style.background=d.tier==='premium'?'rgba(245,200,66,0.2)':'var(--surface2)';b.style.color=d.tier==='premium'?'var(--gold)':'var(--text-dim)'}
        }
    });
    // Загружаем времена обновления сразу
    fetch('/api/update_times').then(r => r.json()).then(d => {
        ['eu', 'us'].forEach(r => {
            const dr = d[r] || {};
            if (dr.last_modified) {
                if (r === 'eu') euLastModified = dr.last_modified;
                else usLastModified = dr.last_modified;
            }
            if (dr.blizzard_time) {
                if (r === 'eu') euBlizzStr = dr.blizzard_time;
                else usBlizzStr = dr.blizzard_time;
            }
        });
        updateLiveTimer();
    });
    await loadPresetsFromServer();
    fetchData();
})();
setInterval(() => {
    fetch('/api/update_times').then(r => r.json()).then(d => {
        let changed = false;
        ['eu', 'us'].forEach(r => {
            const dr = d[r] || {};
            if (dr.last_modified) {
                const old = r === 'eu' ? euLastModified : usLastModified;
                if (dr.last_modified !== old) { changed = true; }
                if (r === 'eu') euLastModified = dr.last_modified;
                else usLastModified = dr.last_modified;
            }
            if (dr.blizzard_time) {
                if (r === 'eu') euBlizzStr = dr.blizzard_time;
                else usBlizzStr = dr.blizzard_time;
            }
        });
        updateLiveTimer();
        if (changed) fetchData();
    });
}, 5000);
</script>
<div style="text-align:center;padding:24px;color:var(--text-dim);font-size:12px;border-top:1px solid rgba(255,255,255,0.04);margin-top:24px">
    <a href="/faq" style="color:var(--gold);text-decoration:none;margin:0 8px">FAQ</a>
    <a href="https://t.me/makliyoyo" target="_blank" style="color:var(--gold);text-decoration:none;margin:0 8px">@makliyoyo</a>
    <span style="margin:0 8px">Tip TRC20: TMGNTSewaUwV9MB37aGVNqTsPXTjVF5e52</span>
</div>
</body>
</html>
"""

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Sign In — Auction Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
    body { background: #0f1117; color: #c8c8c8; font-family: 'Inter', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    .card { background: #1a1d25; padding: 32px; border-radius: 16px; border: 1px solid rgba(255,255,255,0.04); width: 360px; }
    h2 { color: #f5c842; text-align: center; font-weight: 600; }
    input { width: 100%; padding: 12px; margin: 10px 0; background: #23262f; border: 1px solid rgba(255,255,255,0.06); color: #fff; border-radius: 8px; }
    button { width: 100%; padding: 12px; background: rgba(245,200,66,0.15); color: #f5c842; border: 1px solid #f5c842; border-radius: 8px; cursor: pointer; font-weight: 500; }
    button:hover { background: #f5c842; color: #0d0d0d; }
    a { color: #f5c842; text-decoration: none; font-size: 13px; }
    .error { color: #f87171; text-align: center; font-size: 13px; }
</style></head>
<body>
<div class="card">
    <h2>Sign In</h2>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
        <input type="text" name="email" placeholder="Email or Username" required>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Sign In</button>
    </form>
    <p style="text-align:center;margin-top:16px;"><a href="/register">Register</a> | <a href="/">Home</a></p>
</div>
</body>
</html>
"""

HTML_REGISTER = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Register — Auction Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
    body { background: #0f1117; color: #c8c8c8; font-family: 'Inter', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    .card { background: #1a1d25; padding: 32px; border-radius: 16px; border: 1px solid rgba(255,255,255,0.04); width: 360px; }
    h2 { color: #f5c842; text-align: center; font-weight: 600; }
    input { width: 100%; padding: 12px; margin: 10px 0; background: #23262f; border: 1px solid rgba(255,255,255,0.06); color: #fff; border-radius: 8px; }
    button { width: 100%; padding: 12px; background: rgba(245,200,66,0.15); color: #f5c842; border: 1px solid #f5c842; border-radius: 8px; cursor: pointer; font-weight: 500; }
    button:hover { background: #f5c842; color: #0d0d0d; }
    a { color: #f5c842; text-decoration: none; font-size: 13px; }
    .error { color: #f87171; text-align: center; font-size: 13px; }
    .hint { font-size: 11px; color: #888; margin-top: -6px; margin-bottom: 8px; }
</style></head>
<body>
<div class="card">
    <h2>Register</h2>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
        <input type="text" name="username" placeholder="Username" required>
        <input type="email" name="email" placeholder="Email" required>
        <input type="password" name="password" placeholder="Password" required>
        <input type="text" name="promo" placeholder="Promo code (optional)" value="{{ promo }}">
        <p class="hint">Use PREMIUM7 for 7-day trial</p>
        <button type="submit">Register</button>
    </form>
    <p style="text-align:center;margin-top:16px;"><a href="/login">Sign In</a> | <a href="/">Home</a></p>
</div>
</body>
</html>
"""

HTML_MY_DEALS = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Мои сделки</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
    body { background: #0f1117; color: #c8c8c8; font-family: 'Inter', sans-serif; padding: 24px; }
    .container { max-width: 1600px; margin: 0 auto; background: #1a1d25; padding: 32px; border-radius: 16px; border: 1px solid rgba(255,255,255,0.04); overflow-x: auto; }
    h2 { color: #f5c842; font-weight: 600; }
    .toolbar { display: flex; align-items: center; gap: 16px; margin-bottom: 18px; flex-wrap: wrap; }
    .toggle-label { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #888; cursor: pointer; user-select: none; }
    .toggle-label input { accent-color: #f5c842; cursor: pointer; width: 16px; height: 16px; }
    .toggle-label input:checked + span { color: #f5c842; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 950px; }
    th { background: #23262f; color: #f5c842; padding: 12px; text-align: left; white-space: nowrap; }
    th.live-col { color: #4ade80; }
    td { padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.03); }
    .price { color: #4ade80; }
    .realm { color: #60a5fa; }
    .live-realm { color: #4ade80; }
    .live-price { color: #4ade80; font-weight: 600; }
    .item-cell { display: flex; align-items: center; gap: 10px; }
    .item-cell img { width: 32px; height: 32px; border-radius: 6px; background: #1a1a1a; padding: 2px; }
    a { color: #f5c842; text-decoration: none; }
    .deals-link { display: inline-flex; align-items: center; gap: 6px; background: rgba(245,200,66,0.12); color: #f5c842; padding: 8px 16px; border-radius: 8px; border: 1px solid rgba(245,200,66,0.25); font-size: 13px; font-weight: 500; text-decoration: none; transition: all 0.2s; }
    .deals-link:hover { background: #f5c842; color: #0d0d0d; }
</style></head>
<body>
<div class="container">
    <h2>Мои сделки</h2>
    <div class="toolbar">
        <a href="/" class="deals-link">← На главную</a>
        <label class="toggle-label" id="liveToggleLabel">
            <input type="checkbox" id="liveToggle" onchange="toggleLive()">
            <span>Live: где продать сейчас</span>
        </label>
    </div>
    <div id="deals-container">Загрузка...</div>
</div>
<script>
    let liveEnabled = localStorage.getItem('dealsLive') === 'true';
    let currentRegion = (() => { try { return localStorage.getItem('wow_ah_region') || 'eu'; } catch(e){ return 'eu'; } })();
    document.getElementById('liveToggle').checked = liveEnabled;

    function toggleLive() {
        liveEnabled = document.getElementById('liveToggle').checked;
        localStorage.setItem('dealsLive', liveEnabled);
        loadDeals();
    }

    function loadDeals() {
        const container = document.getElementById('deals-container');
        container.innerHTML = '<p style="color:#888;">Загрузка...</p>';
        let url = liveEnabled ? '/api/deals?live=1' : '/api/deals';
        const saleIds = localStorage.getItem('saleRealmIds') || '';
        if (liveEnabled && saleIds) url += '&sale_realm_ids=' + encodeURIComponent(saleIds);
        fetch(url).then(r=>r.json()).then(data => {
            const items = data.items || [];
            if (!items.length) { container.innerHTML = '<p>Нет сделок</p>'; return; }
            let liveCols = '';
            if (liveEnabled) {
                liveCols = '<th class="live-col">Где продать сейчас</th><th class="live-col">Цена там</th>';
            }
            let html = `<table><thead><tr>
                <th>Предмет</th><th>Цена сделки</th><th>Скидка</th><th>Дешёвый реалм</th><th>Продажа</th><th>Цена продажи</th><th>Average</th><th>Топ-3</th>${liveCols}<th>Region</th><th>Добавлено</th>
            </tr></thead><tbody>`;
            items.forEach(item => {
                const iconHtml = item.icon_url ? `<img src="${item.icon_url}" onerror="this.style.display='none'" alt="">` : '';
                const dealUmPrefix = item.region === 'US' ? 'us' : 'eu';
                let liveCells = '';
                if (liveEnabled) {
                    const undermineLive = item.live_slug
                        ? `https://undermine.exchange/#${dealUmPrefix}-${item.live_slug}/${item.item_id}${item.ilvl > 0 ? '-' + item.ilvl : ''}`
                        : '#';
                    liveCells = `<td class="live-realm"><a href="${undermineLive}" target="_blank" style="color:#4ade80;">${item.live_realm || '—'}</a></td><td class="live-price">${item.live_price || '—'}</td>`;
                }
                const regionBadge = item.region === 'US' ? '<span style="color:#60a5fa;font-weight:600;">🇺🇸 US</span>' : '<span style="color:#f5c842;font-weight:600;">🇪🇺 EU</span>';
                html += `<tr>
                    <td><div class="item-cell">${iconHtml}<a href="${item.undermine_url}" target="_blank">${item.item_name}</a></div></td>
                    <td class="price">${item.cheapest_price}</td>
                    <td>${item.discount}</td>
                    <td class="realm">${item.cheapest_realm}</td>
                    <td class="realm">${item.sale_realm}</td>
                    <td class="price">${item.sale_price}</td>
                    <td>${item.top10avg}</td>
                    <td style="font-size:11px; color:#aaa;">${item.top3}</td>
                    ${liveCells}
                    <td>${regionBadge}</td>
                    <td>${item.added_at}</td>
                </tr>`;
            });
            html += '</tbody></table>';
            container.innerHTML = html;
        }).catch(() => { container.innerHTML = '<p style="color:#f87171;">Ошибка загрузки</p>'; });
    }

    loadDeals();
</script>
</body>
</html>
"""

HTML_OLD_DEALS = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>История цен</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
    body { background: #0f1117; color: #c8c8c8; font-family: 'Inter', sans-serif; padding: 24px; }
    .container { max-width: 1000px; margin: 0 auto; background: #1a1d25; padding: 32px; border-radius: 16px; border: 1px solid rgba(255,255,255,0.04); }
    h2 { color: #f5c842; font-weight: 600; }
    input, button { background: #23262f; border: 1px solid rgba(255,255,255,0.06); color: #fff; padding: 10px; border-radius: 8px; margin: 8px 0; }
    button { background: rgba(245,200,66,0.15); color: #f5c842; border-color: #f5c842; cursor: pointer; }
    canvas { background: #23262f; border-radius: 12px; padding: 16px; margin-top: 16px; }
    a { color: #f5c842; text-decoration: none; }
</style></head>
<body>
<div class="container">
    <h2>История цены</h2>
    <label>Предмет ID: <input type="number" id="itemIdInput"></label>
    <label>Реалм ID: <input type="number" id="realmIdInput"></label>
    <button onclick="loadHistory()">Показать</button>
    <canvas id="priceChart" width="800" height="400"></canvas>
    <p style="margin-top:20px;"><a href="/">← На главную</a></p>
</div>
<script>
let chart;
function loadHistory() {
    const itemId = document.getElementById('itemIdInput').value;
    const realmId = document.getElementById('realmIdInput').value;
    if (!itemId) return alert('Введите ID предмета');
    let url = '/api/history?item_id=' + itemId + '&region=' + (localStorage.getItem('wow_ah_region') || 'eu');
    if (realmId) url += '&realm_id=' + realmId;
    fetch(url).then(r=>r.json()).then(data => {
        const hist = data.history;
        if (chart) chart.destroy();
        const ctx = document.getElementById('priceChart').getContext('2d');
        chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: hist.map(h => h.time),
                datasets: [{
                    label: 'Цена (медь)',
                    data: hist.map(h => h.price),
                    borderColor: '#f5c842',
                    backgroundColor: 'rgba(245,200,66,0.1)',
                }]
            },
            options: { scales: { y: { beginAtZero: false } } }
        });
    });
}
</script>
</body>
</html>
"""

HTML_FAQ = """
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>FAQ — Auction Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0f1117;--surface:#1a1d25;--surface2:#23262f;--gold:#f5c842;--text:#c8c8c8;--text-dim:#888;--green:#4ade80}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;padding:24px;min-height:100vh}
.container{max-width:900px;margin:0 auto}
h1{font-size:32px;font-weight:700;color:var(--gold);margin-bottom:24px}
h1 i{margin-right:10px}
.faq-item{background:var(--surface);border:1px solid rgba(255,255,255,0.04);border-radius:12px;padding:20px 24px;margin-bottom:12px}
.faq-item h3{color:var(--gold);font-size:16px;margin-bottom:8px}
.faq-item p{color:var(--text-dim);font-size:14px;line-height:1.7}
.faq-item code{background:var(--surface2);padding:2px 6px;border-radius:4px;font-size:13px;color:var(--green)}
.faq-item a{color:var(--gold)}
.back{display:inline-flex;align-items:center;gap:6px;color:var(--gold);text-decoration:none;font-size:14px;margin-bottom:24px}
.back:hover{text-decoration:underline}
</style></head>
<body>
<div class="container">
<a href="/" class="back">← Back</a>
<h1><i>❓</i> FAQ</h1>

<div class="faq-item">
<h3>How to add item IDs?</h3>
<p>Go to <a href="https://undermine.exchange" target="_blank">Undermine Exchange</a>, find your item. The number in the URL is the item ID: <code>undermine.exchange/#us-illidan/246500</code> → ID is <code>246500</code>. Paste it into the "Item IDs" field on the main page and press Enter. You can paste multiple IDs separated by commas: <code>246500, 246066, 34061</code>. Also, u can export ID's from WOWHEAD. Check YT how to do it.</p>
</div>

<div class="faq-item">
<h3>How to find realm IDs?</h3>
<p>I'll do a page when have time with ID->Realm for EU/US. For discord user's there is a command for get it</p>
</div>

<div class="faq-item">
<h3>What is the Snipe checkbox?</h3>
<p>When Snipe is ON, the table only shows items where the cheapest price is on one of YOUR configured watch realms. Toggle it off to see all realms. Click any realm in the "Cheapest" column to filter by that specific realm.</p>
</div>

<div class="faq-item">
<h3>How do BoE filters work?</h3>
<p>Bind-on-Equip items have bonus IDs that map to item levels (ilvl). BoE filters let you set: which item IDs to watch, ilvl range (min–max), minimum discount %, and how many cheapest prices to average (Top-X). Each BoE item appears as a separate row. <strong>Premium only.</strong></p>
</div>

<div class="faq-item">
<h3>How do presets work?</h3>
<p>Configure your item IDs, sale realms, discount and average filters, then click "New Preset" to save. Click any preset to load it. Star a preset to make it your default. Presets are per-region (EU/US). <strong>Saving presets requires login.</strong></p>
</div>

<div class="faq-item">
<h3>How does the Discord bot work?</h3>
<p>Available <strong>by request only</strong> — contact <a href="https://t.me/makliyoyo">@makliyoyo</a>. If approved, invite the bot, use slash commands: <code>/ah_items add 246500</code>, <code>/ah_discount 10</code>, <code>/ah_get_data</code>. Auto-posts US deals every hour.</p>
</div>

<div class="faq-item">
<h3>Free vs Premium?</h3>
<p><strong>Free:</strong> max 10 item IDs, all sale realms (cannot pick), no BoE filters.<br><strong>Premium:</strong> unlimited items, custom sale realms, BoE filters. <strong>Discord bot is NOT included</strong> — separate request.</p>
</div>

<div class="faq-item">
<h3>How often does data update?</h3>
<p>Blizzard updates auction data roughly every 60 minutes. EU updates at :22–:30 and :53–:56 past each hour (UTC). US updates dynamically — the bot detects the window automatically. After Blizzard updates, our system collects all 92 EU / 83 US realms in ~17 seconds.</p>
</div>

<div class="faq-item">
<h3>About this project</h3>
<p>Created by an AI enthusiast using neural networks as a hobby. Not a commercial product. Updates are not guaranteed. If something breaks — message <a href="https://t.me/makliyoyo">@makliyoyo</a>. No warranty, no SLA. Use at your own risk. Server costs are covered by tips and premium users. If nobody pays — it goes offline. Simple as that.</p>
</div>
</div>
</body></html>"""

HTML_ACCOUNT = """
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Account — Auction Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0f1117;--surface:#1a1d25;--surface2:#23262f;--gold:#f5c842;--text:#c8c8c8;--text-dim:#888;--green:#4ade80;--red:#f87171}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;padding:24px;min-height:100vh}
.container{max-width:500px;margin:40px auto}
.card{background:var(--surface);border:1px solid rgba(255,255,255,0.04);border-radius:16px;padding:32px}
h1{font-size:24px;font-weight:700;color:var(--gold);margin-bottom:24px}
.row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:14px}
.row .label{color:var(--text-dim)}
.row .value{color:#fff;font-weight:500}
.promo-box{margin-top:20px;display:flex;gap:8px}
.promo-box input{flex:1;padding:12px;background:var(--surface2);border:1px solid rgba(255,255,255,0.08);color:#fff;border-radius:8px;font-size:14px}
.promo-box button{white-space:nowrap;padding:12px 20px;background:var(--gold);color:#0d0d0d;border:none;border-radius:8px;font-weight:600;cursor:pointer}
.badge{display:inline-block;padding:4px 12px;border-radius:12px;font-size:12px;font-weight:600}
.badge-premium{background:rgba(245,200,66,0.2);color:var(--gold)}
.badge-free{background:var(--surface2);color:var(--text-dim)}
.back{margin-top:20px;display:inline-block;color:var(--gold);text-decoration:none;font-size:14px}
</style></head>
<body>
<div class="container">
<div class="card">
<h1>Account</h1>
<div class="row"><span class="label">Username</span><span class="value">{{ user.username }}</span></div>
<div class="row"><span class="label">Email</span><span class="value">{{ user.email or '—' }}</span></div>
<div class="row"><span class="label">Tier</span><span class="value"><span class="badge {{ 'badge-premium' if user.tier=='premium' else 'badge-free' }}">{{ user.tier }}</span></span></div>
{% if user.premium_until %}
<div class="row"><span class="label">Premium until</span><span class="value">{{ user.premium_until[:16] }}</span></div>
{% endif %}
<div class="promo-box">
<input type="text" id="promoInput" placeholder="Promo code">
<button onclick="redeem()">Activate</button>
</div>
<a href="/" class="back">← Back</a>
</div>
</div>
<script>
async function redeem(){
const code=document.getElementById('promoInput').value.trim();
if(!code)return;
const r=await fetch('/api/redeem',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
const d=await r.json();
if(d.error)alert(d.error);else location.reload();
}
</script>
</body></html>"""

if __name__ == '__main__':
    # Подключение с таймаутом — не падает если ahgem.py пишет
    for attempt in range(10):
        try:
            conn = sqlite3.connect("auction_data.db")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 120000")
            conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password_hash TEXT, role TEXT)")
            conn.execute("INSERT OR IGNORE INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')", (generate_password_hash("admin"),))
            # Email + верификация
            for col in ['email', 'email_verified', 'email_token']:
                try: conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
                except: pass
            break
        except sqlite3.OperationalError as e:
            conn.close()
            if attempt < 9:
                print(f"DB locked, retrying ({attempt+1}/10)...")
                time.sleep(3)
            else:
                raise

    # Админ всегда premium, авто-верификация
    conn.execute("UPDATE users SET tier='premium', max_items=999, email_verified='1', email=COALESCE(email,'admin@local') WHERE role='admin'")
    conn.commit()

    # Пресеты
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            item_ids TEXT,
            sale_realm_ids TEXT,
            discount REAL,
            min_top10avg REAL,
            deals_only INTEGER DEFAULT 0,
            is_default INTEGER DEFAULT 0,
            boe_filters TEXT
        )
    """)
    for col in [('min_top10avg', 'REAL'), ('is_default', 'INTEGER DEFAULT 0'), ('boe_filters', 'TEXT')]:
        try: conn.execute(f"ALTER TABLE user_presets ADD COLUMN {col[0]} {col[1]}")
        except: pass
    # Регион для пресетов
    for col in [('region', 'TEXT DEFAULT "eu"')]:
        try: conn.execute(f"ALTER TABLE user_presets ADD COLUMN {col[0]} {col[1]}")
        except: pass
    conn.execute("""
        INSERT OR IGNORE INTO user_presets (user_id, name, item_ids, sale_realm_ids, discount, min_top10avg, deals_only, region)
        VALUES (0, 'Default', ?, ?, NULL, NULL, 0, 'eu')
    """, (DEFAULT_ITEM_IDS, DEFAULT_SALE_REALM_IDS))
    # US default preset — без реалмов продажи
    conn.execute("""
        INSERT OR IGNORE INTO user_presets (user_id, name, item_ids, sale_realm_ids, discount, min_top10avg, deals_only, region)
        VALUES (0, 'Default', ?, '', NULL, NULL, 0, 'us')
    """, (DEFAULT_ITEM_IDS,))

    # Сделки (расширенная)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            item_id INTEGER,
            realm_id INTEGER,
            ilvl INTEGER DEFAULT 0,
            price_at_add INTEGER,
            added_at TEXT DEFAULT (datetime('now')),
            item_name TEXT,
            discount TEXT,
            cheapest_realm TEXT,
            sale_realm TEXT,
            sale_price TEXT,
            top10avg TEXT,
            top3 TEXT,
            undermine_url TEXT,
            icon_url TEXT
        )
    """)
    for col in ['item_name', 'discount', 'cheapest_realm', 'sale_realm', 'sale_price', 'top10avg', 'top3', 'undermine_url', 'icon_url', 'ilvl', 'region']:
        try: conn.execute(f"ALTER TABLE user_deals ADD COLUMN {col} TEXT")
        except: pass

    # Tier-система + промокоды
    for col, coltype in [('tier', 'TEXT DEFAULT "free"'), ('premium_until', 'TEXT'), ('max_items', 'INTEGER DEFAULT 10')]:
        try: conn.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype}")
        except: pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            duration_days INTEGER DEFAULT 7,
            used_by INTEGER,
            used_at TEXT
        )
    """)
    conn.execute("""INSERT OR IGNORE INTO promo_codes (code, expires_at) VALUES ('PREMIUM7', datetime('now', '+14 days'))""")
    conn.commit()
    conn.close()

    # Заполняем items таблицу для обоих регионов из item_names.json
    try:
        with open('item_names.json', 'r', encoding='utf-8') as f:
            item_names = json.load(f)
        for region_key, db_file in [('eu', 'auction_data.db'), ('us', 'auction_data_us.db')]:
            try:
                rconn = sqlite3.connect(db_file)
                rconn.execute("PRAGMA busy_timeout = 120000")
                rconn.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name_en TEXT)")
                for item_id, name_en in item_names.items():
                    rconn.execute("INSERT OR IGNORE INTO items (id, name_en) VALUES (?, ?)", (int(item_id), name_en))
                rconn.commit()
                rconn.close()
                print(f"Seeded {len(item_names)} items into {db_file}")
            except Exception as e:
                print(f"Could not seed {db_file}: {e}")
    except Exception as e:
        print(f"item_names.json not loaded: {e}")

    app.run(debug=False, host='0.0.0.0', port=5000)
