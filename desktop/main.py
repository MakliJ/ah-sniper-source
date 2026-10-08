#!/usr/bin/env python3
# main.py — AH Sniper: PyWebView + Flask + RAM-cache collector
import os, sys, json, time, threading, logging, sqlite3, tempfile, atexit, asyncio, secrets, hashlib
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from operator import itemgetter
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from io import BytesIO
from urllib.parse import urlsplit
import html as html_utils
import browser

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    import orjson
    def _json_loads(b):
        return orjson.loads(b)
except ImportError:
    def _json_loads(b):
        return json.loads(b)

# ── Path to root (paths.py is in project root, one level up from desktop/) ──
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from paths import data, static_dir

# ── Build mode: admin (push/web-auth/.env) vs user (локальный снайпер) ──
# _build_flags.py генерируется build_exe.py при сборке (в .gitignore).
# AH_BUILD_FLAGS env: явный путь к файлу флага (используется тестами/CI,
# чтобы корневой _build_flags.py от последней сборки не затенял нужный).
def _load_admin_build_flag():
    override = os.getenv("AH_BUILD_FLAGS", "").strip()
    if override:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_build_flags_override", override)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return bool(mod.ADMIN_BUILD)
    try:
        from _build_flags import ADMIN_BUILD
        return bool(ADMIN_BUILD)
    except ImportError:
        return True  # запуск из исходников = админская машина

ADMIN_BUILD = _load_admin_build_flag()

if ADMIN_BUILD:
    from dotenv import load_dotenv; load_dotenv()
HOST, PORT = "127.0.0.1", 8765

# ── Settings (dynamic, persisted to settings.json + .env) ──
SETTINGS_FILE = data("settings.json")

def _load_settings():
    s = {"region": "eu", "client_id": "", "client_secret": "",
         "pin_avg_gold": 0, "pin_discount": 0,
         "sound_enabled": False, "sound_file": "", "sound_data": "",
         "win_w": 1280, "win_h": 800}
    # 1) from .env
    s["region"] = os.getenv("AHGEN_REGION", s["region"])
    s["client_id"] = os.getenv("CLIENT_ID", "")
    s["client_secret"] = os.getenv("CLIENT_SECRET", "")
    # 2) settings.json overrides
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                js = json.load(f)
            for k in s:
                if k in js and js[k]:
                    s[k] = js[k]
        except: pass
    return s

def _save_settings(settings_dict):
    _atomic_json_write(SETTINGS_FILE, settings_dict)

_settings = _load_settings()
REGION = _settings["region"]

def _db_path():
    return data("auction_data.db" if REGION == "eu" else "auction_data_us.db")

def _avg_cache_path():
    return data(f"topxavg_cache_{REGION}.json" if REGION != "eu" else "topxavg_cache.json")

DB = _db_path()

import flask; from flask import Flask, jsonify, request, send_from_directory, session, redirect, make_response
app = Flask(__name__, static_folder=static_dir(), static_url_path="")

def _load_flask_secret():
    """Keep sessions unpredictable without requiring manual production setup."""
    configured = os.getenv("FLASK_SECRET", "").strip()
    if configured:
        return configured
    secret_path = data(".flask_secret")
    try:
        if os.path.exists(secret_path):
            with open(secret_path, encoding="ascii") as f:
                saved = f.read().strip()
            if len(saved) >= 32:
                return saved
        generated = secrets.token_urlsafe(64)
        fd, tmp_path = tempfile.mkstemp(prefix=".flask_secret.", dir=os.path.dirname(secret_path) or ".")
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(generated)
        os.replace(tmp_path, secret_path)
        return generated
    except Exception:
        # Still use a strong per-process secret if persistence is unavailable.
        return secrets.token_urlsafe(64)

app.secret_key = _load_flask_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("main")

# ── Web Auth (только для example.invalid, localhost не затрагивается) ──
PUBLIC_ORIGIN = os.getenv("PUBLIC_ORIGIN", "https://example.invalid").strip().rstrip("/")
_public_url = urlsplit(PUBLIC_ORIGIN)
if (_public_url.scheme != "https" or not _public_url.hostname or _public_url.username
        or _public_url.password or _public_url.path or _public_url.query or _public_url.fragment
        or any(char.isspace() or char in "\"'<>\\" for char in PUBLIC_ORIGIN)):
    raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin without credentials")
_public_hostname = _public_url.hostname.encode("idna").decode("ascii").lower()
if not all(char.isalnum() or char in ".-" for char in _public_hostname):
    raise ValueError("PUBLIC_ORIGIN contains an invalid hostname")
_public_port = _public_url.port
PUBLIC_ORIGIN = "https://" + _public_hostname + (f":{_public_port}" if _public_port is not None else "")
WEB_HOSTS = {_public_hostname, (_public_hostname[4:] if _public_hostname.startswith("www.") else "www." + _public_hostname)}

def _public_link(name, default="/profile"):
    value = os.getenv(name, default).strip() or default
    parsed = urlsplit(value)
    if any(char in value for char in "\"'<>\\\r\n\t "):
        raise ValueError(f"{name} contains invalid URL characters")
    if not ((value.startswith("/") and not value.startswith("//") and not parsed.netloc)
            or (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)):
        raise ValueError(f"{name} must be an HTTPS URL or a relative application path")
    return value

SUPPORT_URL = _public_link("SUPPORT_URL")
_support_label = os.getenv("SUPPORT_LABEL", "поддержка").strip()
if any(ord(char) < 32 or char in "\\\u2028\u2029" for char in _support_label):
    raise ValueError("SUPPORT_LABEL must be a single line without backslashes or control characters")
SUPPORT_LABEL = html_utils.escape(_support_label)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Пути, которые никогда не существуют: отдаём честный 404 вместо редиректа на /login,
# чтобы сканеры секретов не получали информацию о структуре приложения.
SENSITIVE_404 = {
    "/supabase_config.json", "/.env", "/tunnel_token.txt", "/settings.json",
    "/license.json", "/keys_30d.txt", "/users.json", "/.flask_secret",
    "/config.json", "/secrets.json", "/credentials.json",
}
AUTH_EXEMPT = {
    "/", "/landing", "/login", "/register", "/api/env", "/api/landing/deals", "/gate",
    "/favicon.ico", "/robots.txt", "/sitemap.xml", "/site.webmanifest", "/analytics.js",
}
WEB_ADMIN_ONLY = {"/api/start", "/api/stop", "/api/logs", "/browser/api/region"}

@app.errorhandler(404)
def _not_found(e):
    """Честная 404: тематическая страница для сайта, JSON — для API."""
    if request.path.startswith("/api/") or request.path.startswith("/browser/api/"):
        return jsonify({"error": "not_found"}), 404
    if _is_web_request():
        html = (f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">"""
                f"""<meta name="viewport" content="width=device-width,initial-scale=1">"""
                f"""<meta name="robots" content="noindex,nofollow">"""
                f"""<title>404 — AH Sniper</title><script defer src="/analytics.js"></script><style>{_AUTH_CSS}</style></head>"""
                f"""<body><div class="card"><div class="logo"><img src="/logos/logo_nav_38.png" alt=""></div>"""
                f"""<div class="title">404</div><div class="sub">Такой страницы нет</div>"""
                f"""<div class="link"><a href="/">← На главную</a></div></div></body></html>""")
        return make_response(html, 404, {"X-Robots-Tag": "noindex, nofollow, noarchive"})
    return e

from werkzeug.security import generate_password_hash, check_password_hash

def _supabase_web(method, table, payload=None, params=None):
    """Запрос к Supabase с service_key (для web_users)."""
    if not ADMIN_BUILD:
        return None
    try:
        cfg_path = data("supabase_config.json")
        if not os.path.exists(cfg_path): return None
        with open(cfg_path) as f: cfg = json.load(f)
        url = cfg["supabase_url"]; key = cfg["service_key"]
        headers = {"Content-Type": "application/json", "apikey": key, "Authorization": f"Bearer {key}"}
        if method in ("POST", "PATCH"): headers["Prefer"] = "return=representation"
        if method not in ("GET", "POST", "PATCH"): return None
        endpoint = f"{url}/rest/v1/{table}"
        last_error = None
        for attempt in range(2):
            try:
                r = requests.request(method, endpoint, json=payload, params=params,
                                     headers=headers, timeout=10 if attempt == 0 else 15)
                if r.status_code in (200, 201, 204):
                    if r.status_code == 204 or not r.content: return True
                    try: return r.json()
                    except ValueError: return True
                _dlog(f"Supabase web_users {method} {r.status_code}: {r.text[:200]}")
                return None
            except requests.RequestException as e:
                last_error = e
                if attempt == 0: time.sleep(0.25)
        _dlog(f"Supabase web_users {method} failed after retry: {last_error}")
        return None
    except Exception as e:
        _dlog(f"Supabase web_users error: {e}")
        return None

# ── Rate limiting (in-memory sliding window; для брутфорс-защиты auth-роутов) ──
_RATE = {}; _RATE_LOCK = threading.Lock()

def _client_ip():
    """Реальный IP клиента (Cloudflare пробрасывает CF-Connecting-IP)."""
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"

def _rate_limit(key, limit, window):
    """True если действие разрешено в пределах limit раз за window секунд."""
    now = time.time()
    with _RATE_LOCK:
        lst = [t for t in _RATE.get(key, ()) if now - t < window]
        if len(lst) >= limit:
            _RATE[key] = lst
            return False
        lst.append(now)
        _RATE[key] = lst
        if len(_RATE) > 5000:  # анти-рост памяти
            for k2 in [k2 for k2, v2 in _RATE.items() if not v2 or now - v2[-1] > 3600]:
                _RATE.pop(k2, None)
        return True

_AUTH_CSS = """*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Segoe UI',system-ui,sans-serif;background:#0b0d13;color:#e9e7e2;height:100vh;display:flex;align-items:center;justify-content:center}
:focus-visible{outline:2px solid #ffd75e;outline-offset:2px;border-radius:4px}
.card{background:#131726;border:1px solid rgba(232,185,35,.14);border-radius:16px;padding:40px;text-align:center;max-width:400px;width:90%}
.logo{margin-bottom:10px}.logo img{width:44px;height:44px;display:block;margin:0 auto}.title{font-size:20px;font-weight:700;color:#ffd75e;margin-bottom:6px}
.sub{font-size:13px;color:#8b8e99;margin-bottom:24px;line-height:1.5}
.sub a{color:#ffd75e;font-weight:700;text-decoration:none}
input{width:100%;padding:12px 16px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:#171c2e;color:#e9e7e2;font-size:14px;margin-bottom:12px}
input:focus{outline:none;border-color:#e8b923;box-shadow:0 0 0 3px rgba(232,185,35,.18)}
button{width:100%;padding:12px;border:none;border-radius:8px;background:linear-gradient(180deg,#ffd75e,#e8b923);color:#1a1205;font-size:14px;font-weight:800;cursor:pointer;margin-bottom:8px}
button:hover{filter:brightness(1.07)}button:disabled{opacity:.5;cursor:not-allowed}
.msg{margin-top:10px;font-size:12px;min-height:18px}.err{color:#f87171}.ok{color:#2fd575}
.link{font-size:12px;color:#8b8e99;margin-top:12px}.link a{color:#ffd75e;text-decoration:none;cursor:pointer}
.pro-badge{background:rgba(47,213,117,0.12);color:#2fd575;padding:6px 12px;border-radius:6px;font-size:11px;margin-top:12px;border:1px solid rgba(47,213,117,0.3)}"""

_AUTH_SCRIPT = """
async function submitAuth(path,label,eventName){
var b=document.getElementById('btn'),msg=document.getElementById('msg');
if(b.disabled)return;
var e=document.getElementById('email').value.trim(),p=document.getElementById('pass').value;
msg.className='msg err';msg.textContent='';
if(!e||!p){msg.textContent='Заполните все поля';return;}
if(path==='/register'&&p.length<6){msg.textContent='Пароль мин. 6 символов';return;}
b.disabled=true;b.textContent='Подождите…';
var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},30000);
try{
  var r=await fetch(path,{method:'POST',credentials:'same-origin',cache:'no-store',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e,password:p}),signal:controller.signal});
  var d=await r.json();
  if(!r.ok||!d||!d.ok){msg.textContent=(d&&d.reason)||'Сервис временно недоступен. Попробуйте ещё раз.';return;}
  // Analytics must never prevent navigation after the session has been created.
  try{if(typeof window.ahTrack==='function')window.ahTrack(eventName);}catch(ignored){}
  window.location.replace('/app');
}catch(error){
  msg.textContent=error.name==='AbortError'?'Сервер долго не отвечает. Попробуйте ещё раз.':'Не удалось завершить вход. Проверьте соединение и попробуйте ещё раз.';
}finally{
  clearTimeout(timer);b.disabled=false;b.textContent=label;
}
}
function doLogin(){return submitAuth('/login','Войти','login_success');}
function doReg(){return submitAuth('/register','Зарегистрироваться','registration_success');}
document.getElementById('authForm').addEventListener('submit',function(event){
event.preventDefault();if(location.pathname==='/register')doReg();else doLogin();
});
"""

REGISTER_PAGE = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0d13"><link rel="icon" type="image/png" sizes="32x32" href="/logos/favicon-32.png"><title>AH Sniper — Регистрация</title><script defer src="/analytics.js"></script><style>{_AUTH_CSS}</style></head><body><div class="card">
<div class="logo"><img src="/logos/logo_nav_38.png" alt="AH Sniper"></div><div class="title">AH Sniper</div><div class="sub">Создать аккаунт · <a href="/">← на главную</a></div>
<form id="authForm">
<input id="email" type="email" name="email" autocomplete="username" placeholder="Email" required autofocus>
<input id="pass" type="password" name="password" autocomplete="new-password" placeholder="Пароль (мин. 6 символов)" minlength="6" required>
<button id="btn" type="submit">Зарегистрироваться</button>
<div id="msg" class="msg" role="status" aria-live="polite"></div></form>
<div class="link">Уже есть аккаунт? <a href="/login">Войти</a></div>
</div><script>{_AUTH_SCRIPT}</script></body></html>"""

LOGIN_PAGE = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0d13"><link rel="icon" type="image/png" sizes="32x32" href="/logos/favicon-32.png"><title>AH Sniper — Вход</title><script defer src="/analytics.js"></script><style>{_AUTH_CSS}</style></head><body><div class="card">
<div class="logo"><img src="/logos/logo_nav_38.png" alt="AH Sniper"></div><div class="title">AH Sniper</div><div class="sub">Вход в аккаунт · <a href="/">← на главную</a></div>
<form id="authForm">
<input id="email" type="email" name="email" autocomplete="username" placeholder="Email" required autofocus>
<input id="pass" type="password" name="password" autocomplete="current-password" placeholder="Пароль" required>
<button id="btn" type="submit">Войти</button>
<div id="msg" class="msg" role="status" aria-live="polite"></div></form>
<div class="link">Нет аккаунта? <a href="/register">Регистрация</a></div>
</div><script>{_AUTH_SCRIPT}</script></body></html>"""

PROFILE_PAGE = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0d13"><link rel="icon" type="image/png" sizes="32x32" href="/logos/favicon-32.png"><title>AH Sniper — Профиль</title><script defer src="/analytics.js"></script><style>{_AUTH_CSS}</style></head><body><div class="card">
<div class="logo"><img src="/logos/logo_nav_38.png" alt="AH Sniper"></div><div class="title">AH Sniper</div><div class="sub" id="userEmail"></div>
<input id="key" placeholder="XXXXX-XXXXX-XXXXX-XXXXX" maxlength="23" style="text-transform:uppercase;letter-spacing:2px;text-align:center">
<div class="sub" style="font-size:11.5px;margin:-4px 0 14px">Во время беты ключ выдаётся бесплатно и <b>лично в Telegram</b>: <a href="__AH_SUPPORT_URL__" target="_blank" rel="noopener noreferrer">поддержка</a></div>
<button id="btn" onclick="activateKey()">Активировать ключ</button>
<div id="msg" class="msg"></div>
<div id="status" style="margin-top:16px;font-size:13px"></div>
<div id="proMsg"></div>
<button onclick="goApp()" id="goBtn" style="display:none;background:#4ade80;margin-top:16px">🚀 Перейти к AH Sniper</button>
<div id="presetIO" style="display:none;margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.06)">
<div style="font-size:11px;color:#9098a5;margin-bottom:8px">📋 Пресеты</div>
<div style="display:flex;gap:8px">
<a href="/profile/export_presets" style="flex:1;padding:8px;background:#242833;border:1px solid rgba(255,255,255,0.1);border-radius:6px;color:#e0e0e0;text-decoration:none;font-size:11px;text-align:center">⬇ Скачать</a>
<label style="flex:1;padding:8px;background:#242833;border:1px solid rgba(255,255,255,0.1);border-radius:6px;color:#e0e0e0;font-size:11px;text-align:center;cursor:pointer">⬆ Загрузить<input type="file" accept=".json" onchange="importPresets(this)" style="display:none"></label>
</div><div id="ioMsg" style="font-size:11px;margin-top:6px;min-height:14px"></div>
</div>
<div class="link"><a href="/logout">Выйти</a></div>
</div><script>
var _tier='none';
async function loadStatus(){{var r=await fetch('/profile/status');var d=await r.json();
document.getElementById('userEmail').textContent=d.email||'';
if(d.tier&&d.tier!=='none'){{_tier=d.tier;
document.getElementById('status').innerHTML='<span class="ok">✅ Ключ активирован ('+d.tier.toUpperCase()+')</span>';
document.getElementById('goBtn').style.display='block';document.getElementById('key').style.display='none';document.getElementById('btn').style.display='none';
document.getElementById('presetIO').style.display='block';
if(d.tier==='pro'){{document.getElementById('proMsg').innerHTML='<div class="pro-badge">🚀 Pro активирован. Десктоп-версия для Windows — в канале поддержки (раздел Downloads)</div>';}}
}}else{{document.getElementById('status').innerHTML='<span style="color:var(--text2)">Введите лицензионный ключ</span>';}}
}}
async function activateKey(){{var k=document.getElementById('key').value.trim();
if(!k){{document.getElementById('msg').innerHTML='<span class="err">Введите ключ</span>';return;}}
var b=document.getElementById('btn');b.disabled=true;b.textContent='Проверка...';
var r=await fetch('/profile/activate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{key:k}})}});
var d=await r.json();
if(d.ok){{if(window.ahTrack)window.ahTrack('license_activation_success',{{tier:d.tier||''}});document.getElementById('msg').innerHTML='<span class="ok">✅ Ключ активирован!</span>';setTimeout(loadStatus,500);}}
else{{document.getElementById('msg').innerHTML='<span class="err">❌ '+(d.reason||'Ошибка')+'</span>';b.disabled=false;b.textContent='Активировать ключ';}}
}}
function goApp(){{window.location.href='/app';}}
async function importPresets(input){{if(!input.files[0])return;
var fd=new FormData();fd.append('file',input.files[0]);
var r=await fetch('/profile/import_presets',{{method:'POST',body:fd}});
var d=await r.json();
document.getElementById('ioMsg').innerHTML=d.ok?'<span class="ok">✅ Загружено пресетов: '+d.count+'</span>':'<span class="err">❌ '+(d.reason||'Ошибка')+'</span>';
input.value='';}}
loadStatus();
</script></body></html>"""

def _request_hostname():
    """Hostname из заголовка Host без порта (корректно для IPv6)."""
    h = (request.host or "").strip().lower()
    if h.startswith("["):  # [::1]:8765
        end = h.find("]")
        return h[1:end] if end != -1 else h
    if ":" in h:
        h = h.rsplit(":", 1)[0]
    return h

def _is_web_request():
    return _request_hostname() in WEB_HOSTS

def _yandex_metrika_html():
    counter_id = os.getenv("YANDEX_METRIKA_ID", "").strip()
    if not counter_id.isdigit():
        return "", ""
    script = f"""<!-- Yandex.Metrika counter -->
<script type="text/javascript">
(function(m,e,t,r,i,k,a){{
  m[i]=m[i]||function(){{(m[i].a=m[i].a||[]).push(arguments)}};
  m[i].l=1*new Date();
  for(var j=0;j<document.scripts.length;j++){{if(document.scripts[j].src===r){{return;}}}}
  k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a);
}})(window,document,'script','https://mc.yandex.ru/metrika/tag.js?id={counter_id}','ym');
window.dataLayer=window.dataLayer||[];
window.AH_YM_ID={counter_id};
ym({counter_id},'init',{{ssr:true,webvisor:true,clickmap:true,ecommerce:'dataLayer',referrer:document.referrer,url:location.href,accurateTrackBounce:true,trackLinks:true}});
</script>
<!-- /Yandex.Metrika counter -->"""
    noscript = f"""<noscript><div><img src="https://mc.yandex.ru/watch/{counter_id}" style="position:absolute;left:-9999px" alt=""></div></noscript>"""
    return script, noscript

@app.before_request
def _web_auth_gate():
    # ── Host allowlist: web-логика только для example.invalid, админ-доступ только
    #    для localhost. Любой другой Host (DNS-rebinding и т.п.) — 403. ──
    host = _request_hostname()
    if host not in WEB_HOSTS and host not in LOCAL_HOSTS:
        flask.abort(403)
    if request.path in SENSITIVE_404:
        flask.abort(404)
    if not ADMIN_BUILD:
        # User EXE: web-роуты недоступны (сайт работает только на admin EXE)
        if _is_web_request():
            flask.abort(404)
        return None
    if not _is_web_request():
        return None
    if request.path in AUTH_EXEMPT or request.path.startswith("/browser/icon/") or request.path.startswith("/logos/"):
        return None
    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        # Редирект на /login только для реальных приватных страниц.
        # Неизвестные пути получают честную 404 (анти-soft-404 для краулеров).
        if request.path == "/app" or request.path.startswith("/profile") or request.path == "/gate":
            return redirect("/login")
        flask.abort(404)
    if request.path in WEB_ADMIN_ONLY and not (request.path == "/browser/api/region" and request.method == "GET"):
        return jsonify({"error": "desktop_admin_only"}), 403
    if request.path == "/api/license/activate":
        return jsonify({"error": "use_profile_activation"}), 403
    return None

@app.after_request
def _perf_headers(response):
    """Кэш-заголовки вечной статики + сжатие крупных JSON (без зависимостей)."""
    try:
        p = request.path
        if p.startswith("/api/presets") or p == "/api/env" or p.startswith("/profile/") or p in ("/login", "/register", "/logout", "/app"):
            response.headers["Cache-Control"] = "no-store"
        if (p == "/favicon.ico" or p.startswith("/logos/") or p.startswith("/vendor/")
                or p.startswith("/browser/icon/") or p == "/site.webmanifest"):
            if response.status_code in (200, 304):
                # Жёстко перебиваем no-cache от static-view: эти файлы неизменяемы
                response.headers["Cache-Control"] = "public, max-age=604800"
            else:
                # Редиректы/ошибки кэшировать нельзя (иначе закэшируется 302 на /login)
                response.headers["Cache-Control"] = "no-store"
        if (response.status_code == 200 and response.mimetype == "application/json"
                and not response.direct_passthrough):
            ae = (request.headers.get("Accept-Encoding") or "").lower()
            if "gzip" in ae:
                body = response.get_data()
                if len(body) > 2048:
                    import gzip as _gzip
                    response.set_data(_gzip.compress(body, 5))
                    response.headers["Content-Encoding"] = "gzip"
                    response.vary.add("Accept-Encoding")
    except Exception:
        pass
    return response

@app.after_request
def _web_security_headers(response):
    if response.status_code == 200 and response.mimetype == "text/html":
        response.direct_passthrough = False
        rendered = response.get_data(as_text=True)
        for marker, value in {
            "__AH_PUBLIC_ORIGIN__": PUBLIC_ORIGIN,
            "__AH_SUPPORT_URL__": SUPPORT_URL,
            "__AH_SUPPORT_LABEL__": SUPPORT_LABEL,
        }.items():
            rendered = rendered.replace(marker, value)
        response.set_data(rendered)
        response.headers.pop("ETag", None)
    if _is_web_request():
        if response.status_code == 200 and response.mimetype == "text/html":
            script, noscript = _yandex_metrika_html()
            if script:
                response.direct_passthrough = False
                html = response.get_data(as_text=True)
                html = html.replace("<head>", "<head>\n" + script, 1)
                html = html.replace("<body>", "<body>\n" + noscript, 1)
                response.set_data(html)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # служебное имя файла не должно утекать в заголовках HTML-страниц
        if response.mimetype == "text/html" and "Content-Disposition" in response.headers:
            try: del response.headers["Content-Disposition"]
            except Exception: pass
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Report-Only: не влияет на работу, но показывает нарушения до включения enforce
        response.headers.setdefault("Content-Security-Policy-Report-Only",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://mc.yandex.ru https://www.googletagmanager.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://mc.yandex.ru https://render.worldofwarcraft.com https://cdn.jsdelivr.net; "
            "connect-src 'self' https://*.supabase.co https://mc.yandex.ru https://www.google-analytics.com https://analytics.google.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "frame-ancestors 'none'")
        private_paths = ("/app", "/login", "/register", "/profile", "/gate", "/api/", "/browser/")
        if request.path.startswith(private_paths):
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
    return response

@app.route("/register", methods=["GET"])
def _register_get():
    if session.get("user_id"): return redirect("/app")
    return REGISTER_PAGE

@app.route("/register", methods=["POST"])
def _register_post():
    d = request.get_json(force=True) or {}
    email = d.get("email", "").strip().lower()
    password = d.get("password", "")
    if not email or len(password) < 6:
        return jsonify({"ok": False, "reason": "Email и пароль (мин. 6) обязательны"})
    if not _rate_limit(f"reg:{_client_ip()}", 5, 300):
        return jsonify({"ok": False, "reason": "Слишком много регистраций. Подождите 5 минут"}), 429
    existing = _supabase_web("GET", "web_users", params={"email": f"eq.{email}", "select": "id"})
    if existing and len(existing) > 0:
        return jsonify({"ok": False, "reason": "Email уже зарегистрирован"})
    pw_hash = generate_password_hash(password)
    default_preset = json.dumps([{"id": 1, "name": "Стартовый", "item_ids": "41508,44413,49282,49283,49284",
        "sale_realm_ids": "", "discount": 30, "min_top10avg": 0, "deals_only": False,
        "boe_filters": "[]", "editable": True, "is_default": True, "region": "eu"}], ensure_ascii=False)
    result = _supabase_web("POST", "web_users", {"email": email, "password_hash": pw_hash, "tier": "none", "presets": default_preset})
    if result and len(result) > 0:
        user = result[0]
        session["user_id"] = user["id"]
        session["email"] = email
        session["tier"] = "none"
        session.permanent = True
        app.permanent_session_lifetime = timedelta(days=7)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "Ошибка регистрации"})

@app.route("/login", methods=["GET"])
def _login_get():
    if session.get("user_id"): return redirect("/app")
    return LOGIN_PAGE

@app.route("/login", methods=["POST"])
def _login_post():
    d = request.get_json(force=True) or {}
    email = d.get("email", "").strip().lower()
    password = d.get("password", "")
    if not email or not password:
        return jsonify({"ok": False, "reason": "Заполните все поля"})
    # Брутфорс-защита, два слоя (медленный слой ловит «размазанный» перебор,
    # который не виден в секундном окне из-за сетевой задержки каждого запроса):
    #  быстрый: 10/мин с IP;  медленный: 40/15 мин с IP;  5/5 мин на один email
    _ip = _client_ip()
    if (not _rate_limit(f"login_ip:{_ip}", 10, 60)
            or not _rate_limit(f"login_slow:{_ip}", 40, 900)
            or not _rate_limit(f"login_em:{email}", 5, 300)):
        return jsonify({"ok": False, "reason": "Слишком много попыток входа. Попробуйте позже"}), 429
    users = _supabase_web("GET", "web_users", params={"email": f"eq.{email}", "select": "id,password_hash,tier", "limit": "1"})
    if not isinstance(users, list):
        return jsonify({"ok": False, "reason": "Сервис входа временно недоступен. Попробуйте ещё раз."}), 503
    if len(users) == 0:
        return jsonify({"ok": False, "reason": "Неверный email или пароль"})
    user = users[0]
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "reason": "Неверный email или пароль"})
    session["user_id"] = user["id"]
    session["email"] = email
    session["tier"] = user.get("tier", "none") or "none"
    session.permanent = True
    app.permanent_session_lifetime = timedelta(days=7)
    redir = "/app"
    return jsonify({"ok": True, "redirect": redir})

@app.route("/profile", methods=["GET"])
def _profile_get():
    if not session.get("user_id"): return redirect("/login")
    return PROFILE_PAGE

@app.route("/profile/status")
def _profile_status():
    return jsonify({"email": session.get("email", ""), "tier": session.get("tier", "none")})

@app.route("/profile/activate", methods=["POST"])
def _profile_activate():
    if not session.get("user_id"):
        return jsonify({"ok": False, "reason": "Not logged in"})
    if not _rate_limit(f"act:{session.get('user_id')}:{_client_ip()}", 10, 60) \
            or not _rate_limit(f"act_slow:{session.get('user_id')}:{_client_ip()}", 30, 900):
        return jsonify({"ok": False, "reason": "Слишком много попыток. Попробуйте позже"}), 429
    d = request.get_json(force=True) or {}
    key = d.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "reason": "Введите ключ"})
    try:
        import hashlib as _hl
        key_hash = _hl.sha256(key.strip().upper().encode()).hexdigest()
        # Валидация через Supabase RPC (НЕ пишет в license.json — это файл EXE)
        cfg_path = data("supabase_config.json")
        with open(cfg_path) as f: cfg = json.load(f)
        url = cfg["supabase_url"]; anon = cfg["anon_key"]
        r = requests.post(f"{url}/rest/v1/rpc/activate_key",
            json={"p_key_hash": key_hash, "p_hwid": f"web_{session['user_id']}"},
            headers={"Content-Type": "application/json", "apikey": anon, "Authorization": f"Bearer {anon}"},
            timeout=15)
        result = r.json() if r.status_code == 200 else {}
        if not result.get("valid"):
            return jsonify({"ok": False, "reason": result.get("reason", "Неверный ключ")})
        tier = result.get("tier", "pro")
        session["tier"] = tier
        # Сохраняем ключ и tier в web_users
        _supabase_web("PATCH", "web_users",
            {"license_key": key.upper(), "tier": tier},
            params={"id": f"eq.{session['user_id']}"})
        return jsonify({"ok": True, "tier": tier})
    except Exception as e:
        return jsonify({"ok": False, "reason": str(e)})

@app.route("/profile/export_presets")
def _export_presets():
    """Скачать presets.json (для переноса в EXE)."""
    if not session.get("user_id"): return redirect("/login")
    import io
    plist = _load_presets_ctx()
    if plist is None:
        return jsonify({"ok": False, "error": "preset_load_failed"}), 503
    buf = io.BytesIO(json.dumps(plist, ensure_ascii=False, indent=2).encode("utf-8"))
    return flask.send_file(buf, mimetype="application/json", as_attachment=True, download_name="presets.json")

@app.route("/profile/import_presets", methods=["POST"])
def _import_presets():
    """Загрузить presets.json (из EXE или бэкапа)."""
    if not session.get("user_id"): return jsonify({"ok": False, "reason": "Not logged in"})
    f = request.files.get("file")
    if not f: return jsonify({"ok": False, "reason": "Нет файла"})
    try:
        plist = json.loads(f.read().decode("utf-8"))
        if not isinstance(plist, list): return jsonify({"ok": False, "reason": "Неверный формат"})
        if not _save_presets_ctx(plist):
            return jsonify({"ok": False, "reason": "Не удалось сохранить пресеты в облако."}), 503
        return jsonify({"ok": True, "count": len(plist)})
    except Exception as e:
        return jsonify({"ok": False, "reason": str(e)})

@app.route("/logout")
def _logout():
    session.clear()
    return redirect("/login")

# ── State ──
_ct = threading.Event(); _st = {"state": "idle", "realms_total": 0, "realms_done": 0}; _lk = threading.Lock()
_tid = None

# ── Memory caches (instant API) ──
_CL = threading.RLock()
_cache = defaultdict(list)       # {(item_id, ilvl): [(realm_id, price, qty), ...]}
_boe_variant_cache = {}          # {(realm_id, item_id, ilvl): exact variant rows}
_realm_keys = {}                 # {realm_id: set(cache_keys)} — reverse index for O(M) updates
_scan_gen = 0                    # generation counter: incremented each recollect
_scan_gen_map = {}               # {(item_id, ilvl): gen} — which gen each item was last updated in
_proc_pool = None                # persistent ProcessPoolExecutor (created on first scan)
_proc_pool_broken = False        # True → пересоздать пул при следующем проходе
_avg = {}                         # {(item_id, ilvl): top10avg} from previous snapshot
_inames = {}                      # {item_id: name}
_rnames = {}                      # {realm_id: name}
def _boe_live_path():
    return data(f"boe_variants_live_{REGION}.json")

def _save_boe_variant_cache():
    """Only restore variants alongside the snapshot they were collected with."""
    with _CL:
        payload = {"schema": 2, "region": REGION, "collected_at": _cat,
                   "rows": {f"{rid}:{iid}:{il}": rows
                            for (rid, iid, il), rows in _boe_variant_cache.items()}}
    _atomic_json_write(_boe_live_path(), payload)

def _load_boe_variant_cache():
    try:
        with open(_boe_live_path(), encoding="utf-8") as f:
            raw = json.load(f)
        if raw.get("schema") != 2 or raw.get("region") != REGION or raw.get("collected_at") != _cat:
            return
        with _CL:
            for key, rows in raw.get("rows", {}).items():
                rid, iid, il = map(int, key.split(":"))
                if isinstance(rows, list) and any(r[0] == rid for r in _cache.get((iid, il), [])):
                    _boe_variant_cache[(rid, iid, il)] = rows
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        _dlog(f"BoE variant cache load failed: {exc}")

def _boe_variant_rows(item_id, ilvl=0):
    """Current lots only. Legacy snapshots stay unknown until recollected.

    Walk the item's realm index, not the complete variant cache on every row.
    An unknown cheaper realm must never inherit a known realm's stats.
    """
    rows = []
    with _CL:
        for rid, price, qty in _cache.get((item_id, ilvl), []):
            variants = _boe_variant_cache.get((rid, item_id, ilvl)) or [
                {"min_buyout": price, "quantity": qty, "stats": [], "stats_label": "",
                 "sockets": None, "effects": None}]
            for variant in variants:
                rows.append({**variant, "realm_id": rid,
                             "realm_name": _rnames.get(rid, f"R{rid}"),
                             "slug": _rslugs.get(rid, ""),
                             "price": variant["min_buyout"]})
    return sorted(rows, key=lambda r: (r["price"], r["realm_id"], r.get("stats_label", "")))

def _boe_variant_options(item_id, ilvl=0):
    """Per-realm prices and exact attributes for equivalent web filtering."""
    fields = ("realm_id", "realm_name", "slug", "price", "quantity", "stats",
              "stats_label", "effects", "sockets")
    return [{k: row.get(k) for k in fields} for row in _boe_variant_rows(item_id, ilvl)]

def _boe_variant_matches(row, wanted_stats="", wanted_socket="", wanted_effect=""):
    wanted = str(wanted_stats or "").strip().lower()
    if wanted and wanted not in ("all", "any"):
        tokens = wanted.replace(" ", "").split("+")
        if not set(tokens).issubset(row.get("stats") or []):
            return False
    socket = str(wanted_socket or "").lower()
    count = row.get("sockets")
    if socket in ("yes", "no"):
        if count is None or (count > 0) != (socket == "yes"):
            return False
    effect = str(wanted_effect or "").strip().lower()
    effects = row.get("effects")
    if effect and effect not in ("all", "any"):
        if effects is None:
            return False
        if effect == "none":
            return not effects
        return effect in effects
    return True

def _boe_realm_rows(item_id, ilvl, wanted_stats="", wanted_socket="", wanted_effect=""):
    """Cheapest matching variant per realm, ordered by its actual price."""
    realms = {}
    for row in _boe_variant_rows(item_id, ilvl):
        if _boe_variant_matches(row, wanted_stats, wanted_socket, wanted_effect):
            realms.setdefault(row["realm_id"], row)
    return list(realms.values())

def _boe_display_variant(item_id, ilvl, wanted_stats="", wanted_socket="", wanted_effect=""):
    rows = _boe_realm_rows(item_id, ilvl, wanted_stats, wanted_socket, wanted_effect) if ilvl else []
    return rows[0] if rows else None

_rslugs = {}                      # {realm_id: slug}
_cat = ""                         # current collected_at
_blizz_lm = ""                    # blizzard last-modified
_prev_snapshot_items = set()      # {item_id} from previous snapshot (for rare detection)

def _g(v):
    if not v: return "—"
    return f"{v//10000}.{(v%10000)//100:02d}"

def _dlog(msg):
    try:
        with open(data("debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {str(msg).encode('ascii','replace').decode()}\n")
    except: pass

def _atomic_json_write(filepath, obj):
    """Атомарная запись JSON: tmp file → rename (защита от corruption)."""
    try:
        dir_name = os.path.dirname(filepath) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, filepath)
        except:
            try: os.unlink(tmp_path)
            except: pass
            raise
    except Exception as e:
        _dlog(f"JSON write error ({filepath}): {e}")

def _normalize_ids(val):
    """Нормализовать item_ids/sale_realm_ids к строке '123,456'."""
    if isinstance(val, list):
        return ",".join(str(int(x)) for x in val if x)
    if val is None:
        return ""
    return str(val).strip()

# ── Log panel ──
_log = []; _ml = 300
class _LH(logging.Handler):
    def emit(self, r): _log.append(self.format(r)); len(_log) > _ml and _log.pop(0)
_hdl = _LH(); _hdl.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(_hdl); logging.getLogger().setLevel(logging.INFO)

class _SR:
    def write(self, s):
        for l in s.splitlines():
            if l.strip():
                try: _log.append(l.rstrip())
                except: _log.append(l.encode('ascii','replace').decode().rstrip())
            len(_log) > _ml and _log.pop(0)
    def flush(self): pass

# ── Startup: load caches ──
def _load_caches():
    global _cache, _avg, _inames, _rnames, _rslugs, _cat, _blizz_lm, _realm_keys, _boe_variant_cache
    _dlog("Loading caches from DB...")
    # Realm names
    try:
        c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
        for r in c.execute("SELECT id, name_ru, name_en, slug FROM realms").fetchall():
            _rnames[r["id"]] = r["name_ru"] or r["name_en"] or f"R{r['id']}"
            _rslugs[r["id"]] = r["slug"] or ""
        c.close()
    except: pass
    # Item names
    for jf in ["item_names_cache.json", "item_names.json"]:
        jp = data(jf)
        if os.path.exists(jp):
            try:
                with open(jp, encoding="utf-8") as f:
                    raw = json.load(f)
                _inames.update({int(k) if isinstance(k,str) and k.isdigit() else k: v for k,v in raw.items()})
                break
            except: pass
    if not _inames:
        try:
            c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
            for r in c.execute("SELECT id, name_en FROM items").fetchall(): _inames[r["id"]] = r["name_en"]
            c.close()
        except: pass
    # Data cache from last snapshot.
    # Строим локально и подменяем под _CL одним блоком: читатели (_items,
    # landing deals) держат _CL на время обхода — полусобранный кэш невидим.
    new_cache = defaultdict(list); new_rk = {}
    try:
        c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
        r = c.execute("SELECT value FROM meta WHERE key='last_snapshot'").fetchone()
        if r:
            _cat = r[0]
            for row in c.execute("SELECT item_id, ilvl, realm_id, min_buyout, quantity FROM auction_snapshots WHERE collected_at=?", (_cat,)).fetchall():
                new_cache[(row["item_id"], row["ilvl"] or 0)].append((row["realm_id"], row["min_buyout"], row["quantity"]))
        c.close()
    except: pass
    # Sort by price + build reverse index (локально, без блокировки читателей)
    for k in new_cache:
        new_cache[k].sort(key=lambda x: x[1])
        for rid, _, _ in new_cache[k]:
            new_rk.setdefault(rid, set()).add(k)
    with _CL:
        _cache.clear(); _cache.update(new_cache)
        _realm_keys.clear(); _realm_keys.update(new_rk)
        # Legacy snapshots have no per-lot modifiers. Exact variants are
        # repopulated progressively by the next live Blizzard collection.
        _boe_variant_cache.clear()
    _load_boe_variant_cache()
    # Load previous snapshot item IDs for rare detection
    _prev_snapshot_items.clear()
    try:
        c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
        snapshots = c.execute("SELECT DISTINCT collected_at FROM auction_snapshots ORDER BY collected_at DESC LIMIT 2").fetchall()
        if len(snapshots) >= 2:
            prev_cat = snapshots[1]["collected_at"]
            for row in c.execute("SELECT DISTINCT item_id FROM auction_snapshots WHERE collected_at=?", (prev_cat,)).fetchall():
                _prev_snapshot_items.add(row["item_id"])
        c.close()
    except: pass
    _dlog(f"Prev snapshot: {len(_prev_snapshot_items)} items")
    # AVG cache from JSON
    jp = _avg_cache_path()
    if os.path.exists(jp):
        try:
            with open(jp, encoding="utf-8") as f: raw = json.load(f)
            for ks, v in raw.items():
                parts = ks.split("_")
                if len(parts) >= 2: _avg[(int(parts[0]), int(parts[1]) if parts[1] else 0)] = v.get("top10avg", 0)
        except: pass
    if not _avg:
        for k, prices in _cache.items():
            top = prices[:10]; _avg[k] = sum(p[1] for p in top) // len(top) if top else 0
        _save_avg()
    _dlog(f"Cache: {len(_cache)} items, {len(_avg)} avgs, {len(_rnames)} r, {len(_inames)} names, {len(_rslugs)} slugs")
    # Load blizzard last-modified
    try:
        c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
        r = c.execute("SELECT value FROM meta WHERE key='blizzard_last_modified'").fetchone()
        if r: _blizz_lm = r[0]
        c.close()
    except: pass
    # Browser: load item meta & icons
    try:
        browser.load_meta(REGION)
        browser.load_item_icons()
        _dlog(f"Browser: {len(browser._meta)} meta, {len(browser._icon_name_map)} icons")
    except: pass

def _save_avg():
    try:
        d = {}
        for (iid, il), v in _avg.items():
            ps = _cache.get((iid, il), [])
            d[f"{iid}_{il}"] = {"top10avg": v, "realms_used": min(10, len(ps)), "total_realms": len(ps)}
        with open(_avg_cache_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except: pass

def _recalc_avg():
    global _avg
    for k, prices in _cache.items():
        top = prices[:10]; _avg[k] = sum(p[1] for p in top) // len(top) if top else 0
    _save_avg()

_supabase_push_lock = threading.Lock()
_supabase_retry = None

def _push_to_supabase(retry=False):
    """Retry a failed snapshot automatically when the cloud quota recovers."""
    global _supabase_retry
    if not ADMIN_BUILD:
        return
    with _supabase_push_lock:
        if _supabase_retry is not None:
            _supabase_retry.cancel()
            _supabase_retry = None
        # Never publish a half-collected snapshot during a retry.
        ok = False if retry and _st.get("state") == "collecting" else _push_to_supabase_once(retry)
        if ok is False:
            _supabase_retry = threading.Timer(60, _push_to_supabase, kwargs={"retry": True})
            _supabase_retry.daemon = True
            _supabase_retry.start()

def _push_to_supabase_once(retry=False):
    """Push aggregated auction data to Supabase for web users."""
    if not ADMIN_BUILD:
        return
    try:
        cfg_path = data("supabase_config.json")
        if not os.path.exists(cfg_path): return
        with open(cfg_path) as f: cfg = json.load(f)
        url = cfg["supabase_url"]; key = cfg["service_key"]
        # Build items list (same format as /api/items but unfiltered)
        if retry:
            # Quota probes are tiny; do not resend megabytes while the API is blocked.
            probe = requests.get(f"{url}/rest/v1/auction_data",
                params={"select": "region", "region": f"eq.{REGION}", "limit": 1},
                headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=10)
            if probe.status_code != 200:
                return False
        items = []
        with _CL:
            for (iid, il), prices in _cache.items():
                if not prices: continue
                rid0, cp, qty = prices[0]
                display_variant = _boe_display_variant(iid, il) if il else None
                if display_variant:
                    cp = int(display_variant.get("price", display_variant.get("min_price", cp)) or cp)
                    rid0 = int(display_variant.get("realm_id", rid0) or rid0)
                    qty = int(display_variant.get("quantity", qty) or qty)
                ta = _avg.get((iid, il), 0)
                disc = round((1 - cp / ta) * 100, 1) if ta and cp else 0
                nm = _inames.get(iid, f"Item {iid}")
                if il: nm += f" [{il}]"
                t3 = [f"{_rnames.get(rid, f'R{rid}')} ({_g(p)})" for rid, p, _ in prices[:3]]
                br, bp = max(prices, key=lambda x: x[1])[:2]
                sr = _rnames.get(br, f"R{br}")
                sp = _g(bp)
                wow_url = f"https://undermine.exchange/#{REGION}/{rid0}/{iid}"
                items.append({
                    "item_id": iid, "ilvl": il, "name": nm,
                    "price": _g(cp), "price_raw": cp,
                    "discount": f"{disc}%", "discount_raw": disc,
                    "realm": _rnames.get(rid0, f"R{rid0}"), "realm_id": rid0,
                    "quantity": qty, "top10avg": _g(ta), "top10avg_raw": ta,
                    "top3": ", ".join(t3),
                    "sale_realm": sr, "sale_price": sp, "url": wow_url,
                    "boe": bool(il), "region": REGION,
                    "_is_first": iid not in _prev_snapshot_items,
                    "stats": (display_variant or {}).get("stats", []) if il else [],
                    "stats_label": (display_variant or {}).get("stats_label", "") if il else "",
                    "sockets": (display_variant or {}).get("sockets") if il else None,
                    "effects": (display_variant or {}).get("effects") if il else None,
                    "variants": _boe_variant_options(iid, il) if il else [],
                })
        items.sort(key=lambda x: x["discount_raw"], reverse=True)
        payload = json.dumps({"items": items, "total": len(items), "collected_at": _blizz_lm or _cat}, ensure_ascii=False)
        headers = {"Content-Type": "application/json", "apikey": key, "Authorization": f"Bearer {key}"}
        r = requests.patch(f"{url}/rest/v1/auction_data",
            json={"items_json": payload, "updated_at": datetime.now(timezone.utc).isoformat()},
            params={"region": f"eq.{REGION}"},
            headers=headers, timeout=30)
        if r.status_code in (200, 204):
            _dlog(f"Pushed {len(items)} items to Supabase ({REGION})")
            return True
        else:
            _dlog(f"Push failed: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        _dlog(f"Push error: {e}")
        return False

def _update_realm_in_cache(rid, agg):
    """Replace ALL entries for a realm in cache with new data from agg.
    Uses reverse index (_realm_keys) for O(M) instead of O(N×M).
    Tags updated items with current _scan_gen for GUI generation filtering."""
    global _scan_gen_map, _boe_variant_cache
    with _CL:
        # Step 1: Remove old entries for this realm (only touched keys via reverse index)
        old_keys = _realm_keys.get(rid)
        # Remove persisted variant rows even when the legacy reverse index was
        # built before exact BoE tracking existed.
        for variant_key in [k for k in _boe_variant_cache if k[0] == rid]:
            _boe_variant_cache.pop(variant_key, None)
        if old_keys:
            for k in old_keys:
                if k in _cache:
                    _cache[k] = [(r, p, q) for r, p, q in _cache[k] if r != rid]
                    if not _cache[k]:
                        del _cache[k]
        # Step 2: Add new entries from agg
        new_keys = set()
        gen = _scan_gen
        _sort_key = itemgetter(1)
        for (iid, il), v in agg.items():
            k = (iid, il)
            lst = _cache[k]
            lst.append((rid, v["min_buyout"], v["count"]))
            if len(lst) > 1:
                lst.sort(key=_sort_key)
            new_keys.add(k)
            _scan_gen_map[k] = gen
            if il and v.get("variants"):
                _boe_variant_cache[(rid, iid, il)] = v["variants"]
        # Step 3: Update reverse index
        _realm_keys[rid] = new_keys

def _fetch_all_realms_http2(token, realm_ids, cfg, lm_map=None, on_realm=None):
    """Download all realms via httpx HTTP/2 (multiplexed).
    Phase 1: parallel download (fast, no CPU blocking in event loop)
    Phase 2: sequential process + cache update (progressive GUI via on_realm)
    Returns: (all_agg, new_lm_map, failed_count, skipped_count)"""
    if not _HAS_HTTPX:
        _dlog("HTTP/2: httpx not installed, fallback")
        return None
    if lm_map is None:
        lm_map = {}

    base_url = cfg.get("base_url", "https://eu.api.blizzard.com")
    namespace = cfg.get("namespace_dynamic", "dynamic-eu")
    locale = cfg.get("locale", "en_GB")

    # Phase 1: Pure download (no CPU work in event loop!)
    async def _download():
        raw_results = []
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=10)
        async with httpx.AsyncClient(http2=True, timeout=30, limits=limits) as client:
            sem = asyncio.Semaphore(10)

            async def fetch_one(rid):
                url = f"{base_url}/data/wow/connected-realm/{rid}/auctions?namespace={namespace}&locale={locale}"
                headers = {"Authorization": f"Bearer {token}"}
                lm = lm_map.get(rid)
                if lm:
                    headers["If-Modified-Since"] = lm
                last_exc = None
                # Ретраи внутри семафора: 429/5xx/сеть — до 3 попыток с уважением Retry-After
                async with sem:
                    for attempt in range(3):
                        try:
                            r = await client.get(url, headers=headers)
                        except Exception as exc:
                            last_exc = exc
                            if attempt < 2:
                                await asyncio.sleep(1 + attempt)
                            continue
                        if r.status_code == 429 or r.status_code >= 500:
                            ra = r.headers.get("retry-after", "")
                            try: wait_s = min(30.0, float(ra))
                            except ValueError: wait_s = 1 + attempt
                            if attempt < 2:
                                _dlog(f"HTTP/2 R{rid}: {r.status_code}, retry in {wait_s:.0f}s")
                                await asyncio.sleep(wait_s)
                            continue
                        return rid, r.status_code, r.content if r.status_code == 200 else None, r.headers.get("last-modified", "")
                if last_exc is not None:
                    raise last_exc
                return rid, -1, None, ""

            tasks = [fetch_one(rid) for rid in realm_ids]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        return raw_results

    try:
        t0 = time.time()
        raw_results = asyncio.run(_download())
        t_dl = time.time() - t0

        # Phase 2: Parallel parse+process, then sequential cache update
        from ahgem import process_auctions, parse_and_process
        all_agg = {}
        new_lm = {}
        failed = 0
        skipped = 0
        work = []
        for res in raw_results:
            if isinstance(res, Exception):
                failed += 1
                continue
            rid, status, content, lm_val = res
            if status == 304:
                skipped += 1
            elif status != 200 or content is None:
                failed += 1
            else:
                work.append((rid, content, lm_val))

        results = []
        if work:
            global _proc_pool, _proc_pool_broken
            rids = [w[0] for w in work]
            lm_vals = [w[2] for w in work]
            contents = [w[1] for w in work]
            try:
                if _proc_pool is None or _proc_pool_broken:
                    try:
                        if _proc_pool is not None:
                            _proc_pool.shutdown(wait=False)
                    except Exception:
                        pass
                    _proc_pool = ProcessPoolExecutor(max_workers=min(os.cpu_count() or 8, 12))
                    _proc_pool_broken = False
                aggs = list(_proc_pool.map(parse_and_process, contents))
                results = list(zip(rids, aggs, lm_vals))
            except Exception as e:
                # Пул повреждён (BrokenProcessPool и т.п.) — следующий проход пересоздаст
                _proc_pool_broken = True
                _dlog(f"ProcPool failed ({e}), serial fallback")
                for rid, content, lm_val in work:
                    legacy = process_auctions(_json_loads(content))
                    results.append((rid, legacy, lm_val))

        done_count = skipped + failed
        for rid, agg, lm_val in results:
            all_agg[rid] = agg
            if lm_val:
                new_lm[rid] = lm_val
            if on_realm:
                on_realm(rid, agg)
            done_count += 1
            if done_count % 10 == 0:
                with _lk: _st.update(realms_done=done_count)

        _dlog(f"HTTP/2: dl={t_dl:.1f}s process={time.time()-t0-t_dl:.1f}s total={time.time()-t0:.1f}s ({len(all_agg)} changed, {skipped} same)")
        return all_agg, new_lm, failed, skipped
    except Exception as e:
        _dlog(f"HTTP/2 fetch failed ({e}), falling back to threads")
        return None

def _dump_db():
    try:
        with _CL:
            snapshot = {k: list(v) for k, v in _cache.items()}
        c = sqlite3.connect(DB, timeout=10)
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA synchronous=OFF"); c.execute("PRAGMA journal_mode=WAL")
        c.execute("BEGIN")
        c.execute("DELETE FROM auction_snapshots WHERE collected_at=?", (_cat,))
        rows = []
        for (iid, il), prices in snapshot.items():
            for rid, pr, qty in prices: rows.append((_cat, rid, iid, il, pr, qty, 0, 0))
        for i in range(0, len(rows), 50000):
            c.executemany("INSERT INTO auction_snapshots (collected_at, realm_id, item_id, ilvl, min_buyout, quantity, avg_price, median_price) VALUES (?,?,?,?,?,?,?,?)", rows[i:i+50000])
        c.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('last_snapshot',?)", (_cat,))
        c.commit()
        # Cleanup: keep only the 2 newest snapshots
        c.execute("BEGIN")
        c.execute("""
            DELETE FROM auction_snapshots
            WHERE collected_at < (
                SELECT MIN(ts) FROM (
                    SELECT DISTINCT collected_at AS ts
                    FROM auction_snapshots
                    ORDER BY ts DESC
                    LIMIT 2
                )
            )
        """)
        c.commit(); c.close()
    except Exception as e: _dlog(f"DB dump: {e}")

# ═══════════════ API ═══════════════

@app.route("/")
def _idx():
    if _is_web_request():
        return send_from_directory(static_dir(), "landing.html", conditional=False)
    return send_from_directory(static_dir(), "index.html", conditional=False)

@app.route("/app")
def _app(): return send_from_directory(static_dir(), "index.html", conditional=False)

@app.route("/landing")
def _landing():
    if _is_web_request():
        return redirect("/", code=301)
    return send_from_directory(static_dir(), "landing.html", conditional=False)

@app.route("/favicon.ico")
def _favicon():
    return send_from_directory(os.path.join(static_dir(), "logos"), "favicon.ico", mimetype="image/x-icon")

@app.route("/robots.txt")
def _robots():
    # /login и /register НЕ Disallow-им: на них отдаётся X-Robots-Tag noindex,
    # который краулер прочитает только если страница доступна для скачивания.
    body = f"""User-agent: *
Allow: /
Disallow: /app
Disallow: /profile
Disallow: /gate
Disallow: /api/
Disallow: /browser/

Sitemap: {PUBLIC_ORIGIN}/sitemap.xml
"""
    return make_response(body, 200, {"Content-Type": "text/plain; charset=utf-8"})

@app.route("/sitemap.xml")
def _sitemap():
    # lastmod = реальная дата изменения файла лендинга, а не «сегодня»
    try:
        landing_path = os.path.join(static_dir(), "landing.html")
        lastmod = time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(landing_path)))
    except Exception:
        lastmod = time.strftime("%Y-%m-%d")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{PUBLIC_ORIGIN}/</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""
    return make_response(body, 200, {"Content-Type": "application/xml; charset=utf-8"})

@app.route("/site.webmanifest")
def _manifest():
    body = {
        "name": "AH Sniper - WoW Auction Scanner",
        "short_name": "AH Sniper",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0d13",
        "theme_color": "#0b0d13",
        "icons": [
            {"src": "/logos/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/logos/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    response = jsonify(body)
    response.headers["Content-Type"] = "application/manifest+json; charset=utf-8"
    return response

@app.route("/analytics.js")
def _analytics_script():
    """Load optional public analytics IDs from the admin environment."""
    ga_id = os.getenv("GA_MEASUREMENT_ID", "").strip()
    ym_id = os.getenv("YANDEX_METRIKA_ID", "").strip()
    if not (ga_id.startswith("G-") and ga_id[2:].replace("-", "").isalnum()):
        ga_id = ""
    if not ym_id.isdigit():
        ym_id = ""
    script = f"""(function(){{
  if(!{json.dumps(sorted(WEB_HOSTS))}.includes(location.hostname))return;
  var ga={json.dumps(ga_id)}, ymId={json.dumps(ym_id)};
  if(ga){{
    var s=document.createElement('script');s.async=true;s.src='https://www.googletagmanager.com/gtag/js?id='+encodeURIComponent(ga);document.head.appendChild(s);
    window.dataLayer=window.dataLayer||[];window.gtag=function(){{dataLayer.push(arguments);}};gtag('js',new Date());gtag('config',ga,{{anonymize_ip:true}});
  }}
  if(ymId&&!window.AH_YM_ID){{
    window.ym=window.ym||function(){{(window.ym.a=window.ym.a||[]).push(arguments);}};window.ym.l=Date.now();
    var src='https://mc.yandex.ru/metrika/tag.js?id='+encodeURIComponent(ymId);
    var exists=Array.prototype.some.call(document.scripts,function(x){{return x.src===src;}});
    if(!exists){{var m=document.createElement('script');m.async=true;m.src=src;(document.head||document.documentElement).appendChild(m);}}
    window.AH_YM_ID=Number(ymId);window.ym(window.AH_YM_ID,'init',{{ssr:true,webvisor:true,clickmap:true,ecommerce:'dataLayer',referrer:document.referrer,url:location.href,accurateTrackBounce:true,trackLinks:true}});
  }}
  function track(name,params){{
    try{{if(typeof window.gtag==='function')window.gtag('event',name,params||{{}});}}catch(e){{}}
    try{{if(window.AH_YM_ID&&typeof window.ym==='function')window.ym(window.AH_YM_ID,'reachGoal',name,params||{{}});}}catch(e){{}}
  }}
  window.ahTrack=track;
  document.addEventListener('click',function(e){{
    var el=e.target.closest('a,button');if(!el)return;var href=el.getAttribute('href')||'';
    if(href.indexOf('/register')===0)track('signup_click',{{placement:el.dataset.analytics||'landing'}});
    else if(href.indexOf('/login')===0)track('login_click',{{placement:el.dataset.analytics||'landing'}});
    else if(el.classList.contains('lang-toggle'))track('language_toggle');
    else if(el.closest('.qa'))track('faq_open');
  }});
}})();"""
    return make_response(script, 200, {"Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-cache"})

def _preset_item_ids(name="normal2"):
    """Item ids из пресета (по умолчанию 'normal2') — кураторский список для лендинга."""
    for p in _load_presets():
        if str(p.get("name", "")).strip().lower() == name.lower():
            ids = {int(x.strip()) for x in str(p.get("item_ids", "")).split(",") if x.strip().isdigit()}
            if ids: return ids
    return None

@app.route("/api/landing/deals")
def _landing_deals():
    """Top credible sniper deals for the public landing page (reads RAM cache).
    Показывает только предметы из кураторского пресета 'normal2'.
    Filters out junk (1c listings, stale avgs) so numbers are trustworthy."""
    if not _rate_limit(f"deals:{_client_ip()}", 120, 60):
        return jsonify({"error": "rate_limited"}), 429
    allowed = _preset_item_ids("normal2")
    deals = []
    with _CL:
        total_items = len(_cache)
        for (iid, il), prices in _cache.items():
            if not prices: continue
            if allowed is not None and iid not in allowed: continue
            rid0, cp, qty = prices[0]
            ta = _avg.get((iid, il), 0)
            if not ta or not cp or ta <= cp: continue
            disc = round((1 - cp / ta) * 100, 1)
            # credibility filters: real discount, meaningful price, sane market avg
            if disc < 25 or disc > 92: continue
            if cp < 100000: continue          # buy price >= 10g
            if ta < 200000: continue          # market avg >= 20g
            if ta > 100000000: continue       # avg <= 10000g (drop stale spikes)
            deals.append({
                "item_id": iid, "ilvl": il,
                "name": _inames.get(iid, f"Item {iid}"),
                "price_raw": cp, "avg_raw": ta,
                "profit_raw": ta - cp, "discount": disc,
                "realm": _rnames.get(rid0, f"R{rid0}"),
                "quantity": qty,
            })
    deals.sort(key=lambda x: x["profit_raw"], reverse=True)
    top = deals[:60]
    return jsonify({
        "deals": top,
        "total_deals": len(deals),
        "total_items": total_items,
        "total_profit_raw": sum(d["profit_raw"] for d in deals),
        "realms": len(_rnames),
        "preset": "normal2" if allowed else None,
        "updated_at": _blizz_lm or _cat,
    })

@app.route("/api/items")
def _items():
    iids = request.args.get("item_ids", "")
    srch = request.args.get("search", "").lower()
    md = float(request.args.get("min_discount", 0))
    sma = float(request.args.get("min_avg", 0))
    sids = request.args.get("sale_realms", "")
    sid_set = {int(p.strip()) for p in sids.split(",") if p.strip().isdigit()}
    wanted = {int(p.strip()) for p in iids.split(",") if p.strip().isdigit()}

    # BoE filters: [{"ids":"123,456","ilvl_min":240,"ilvl_max":256,"discount":90,"topx":4}, ...]
    boe_filters_json = request.args.get("boe_filters", "")
    boe_filters = []
    if boe_filters_json:
        try: boe_filters = json.loads(boe_filters_json)
        except: pass
    # The two sniper blocks are independent. An empty ID list never means all items.
    items_enabled = request.args.get("items_enabled", "true").lower() not in ("false", "0", "no")

    items = []
    rare_items = []

    def _passes_main(iid, il, disc, ta, nm):
        """Main filter check (itemIds, discount, avg, search)."""
        if not items_enabled or iid not in wanted: return False
        if md > 0 and disc < md: return False
        if sma > 0 and ta < sma * 10000: return False
        if srch and srch not in nm.lower() and srch not in str(iid): return False
        return True

    # During collection: only show items updated in current generation (GUI sees empty → filling)
    _collecting = _st.get("state") == "collecting"
    _cur_gen = _scan_gen

    with _CL:
        for (iid, il), prices in _cache.items():
            if not prices: continue
            if _collecting and _scan_gen_map.get((iid, il)) != _cur_gen: continue
            rid0, cp, qty = prices[0]
            ta = _avg.get((iid, il), 0)
            disc = round((1 - cp / ta) * 100, 1) if ta and cp else 0
            nm = _inames.get(iid, f"Item {iid}")
            if il: nm += f" [{il}]"

            # ── System A: Main filters (itemIds, min_discount, min_avg, search) ──
            passes_main = _passes_main(iid, il, disc, ta, nm)

            display_variant = _boe_display_variant(iid, il) if il else None
            boe_matched = False
            boe_filter_topx = 3
            matched_prices = prices
            candidates = []
            is_first = iid not in _prev_snapshot_items
            for bf in boe_filters if il else []:
                bf_ids = {int(x.strip()) for x in str(bf.get("ids", "")).split(",") if x.strip().isdigit()}
                if iid not in bf_ids or not int(bf.get("ilvl_min", 0)) <= il <= int(bf.get("ilvl_max", 999)):
                    continue
                rows = _boe_realm_rows(iid, il, bf.get("stats"), bf.get("socket"), bf.get("effect"))
                if not rows:
                    continue
                candidate = rows[0]
                candidate_disc = round((1 - candidate["price"] / ta) * 100, 1) if ta else 0
                if not is_first and candidate_disc < float(bf.get("discount", 0) or 0):
                    continue
                candidates.append((candidate["price"], candidate, rows, bf))
            if candidates:
                _, display_variant, rows, bf = min(candidates, key=lambda x: x[0])
                boe_matched = True
                boe_filter_topx = max(1, int(bf.get("topx", 3) or 3))
                cp, rid0, qty = display_variant["price"], display_variant["realm_id"], display_variant["quantity"]
                disc = round((1 - cp / ta) * 100, 1) if ta else 0
                matched_prices = [(r["realm_id"], r["price"], r["quantity"]) for r in rows]
            if not passes_main and not boe_matched:
                continue

            # Build display fields
            top_n = boe_filter_topx if boe_matched else 3
            t3 = [f"{_rnames.get(rid,f'R{rid}')} ({_g(p)})" for rid, p, _ in matched_prices[:top_n]]
            sopts = [(rid, p) for rid, p, _ in matched_prices if rid in sid_set] if sid_set else [(rid, p) for rid, p, _ in matched_prices]
            sr, sp = "—", "—"
            if sopts:
                br, bp = max(sopts, key=lambda x: x[1])
                sr = _rnames.get(br, f"R{br}"); sp = _g(bp)
            rn = _rnames.get(rid0, f"R{rid0}")
            slug = _rslugs.get(rid0, "")
            url = f"https://undermine.exchange/#{REGION}-{slug}/{iid}" if slug else f"https://undermine.exchange/#{REGION}/{rid0}/{iid}"
            item_obj = {
                "item_id": iid, "ilvl": il, "name": nm, "price": _g(cp), "price_raw": cp,
                "discount": f"{disc}%", "discount_raw": disc, "realm": rn,
                "realm_id": rid0, "quantity": qty, "top10avg": _g(ta), "top10avg_raw": ta,
                "top3": ", ".join(t3), "sale_realm": sr, "sale_price": sp,
                "url": url, "bind_type": browser._meta.get(iid, {}).get("bind_type", 0),
                "boe": bool(il), "stats": (display_variant or {}).get("stats", []) if il else [],
                "stats_label": (display_variant or {}).get("stats_label", "") if il else "",
                "sockets": (display_variant or {}).get("sockets") if il else None,
                "effects": (display_variant or {}).get("effects") if il else None,
                "_is_first": is_first,
                "_pin": False,
                "is_121": iid >= MIN_MIDNIGHT_121_ID or iid in _midnight_121_set(),
            }
            if is_first:
                rare_items.append(item_obj)
            else:
                items.append(item_obj)
    rare_items.sort(key=lambda x: x["discount_raw"], reverse=True)
    items.sort(key=lambda x: x["discount_raw"], reverse=True)
    all_items = rare_items + items
    return jsonify({"items": all_items, "total": len(all_items), "collected_at": _blizz_lm or _cat})

@app.route("/api/status")
def _status():
    with _lk:
        st = dict(_st)
    st["collected_at"] = _blizz_lm or _cat
    st["gen"] = _scan_gen
    return jsonify(st)

@app.route("/api/env")
def _env():
    """Информация о среде: веб или EXE."""
    env = {"is_web": _is_web_request(), "tier": session.get("tier", "none") or "none", "region": REGION,
           "admin": ADMIN_BUILD,
           "preset_scope": ("web:" + hashlib.sha256(str(session["user_id"]).encode()).hexdigest()[:24]
                            if _is_web_request() and session.get("user_id") else
                            (None if _is_web_request() else "local:" + hashlib.sha256(
                                os.path.normcase(os.path.abspath(data("presets.json"))).encode()).hexdigest()[:24]))}
    if env["is_web"] and ADMIN_BUILD:
        try:
            cfg_path = data("supabase_config.json")
            with open(cfg_path) as f: cfg = json.load(f)
            env["supabase_url"] = cfg["supabase_url"]
            env["supabase_anon"] = cfg["anon_key"]
        except: pass
    return jsonify(env)

PRESETS_FILE = data("presets.json")

def _load_presets():
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, encoding="utf-8") as f:
                result = json.load(f)
            return result if isinstance(result, list) else None
        except (OSError, ValueError):
            return None
    return []

def _save_presets(plist):
    _atomic_json_write(PRESETS_FILE, plist)

def _load_presets_ctx():
    """Загрузка пресетов с учётом контекста (web=Supabase per-user, EXE=файл)."""
    if _is_web_request() and session.get("user_id"):
        users = _supabase_web("GET", "web_users", params={"id": f"eq.{session['user_id']}", "select": "presets"})
        if not isinstance(users, list) or len(users) != 1:
            return None
        try:
            raw = users[0].get("presets")
            plist = json.loads(raw) if isinstance(raw, str) else (raw if raw is not None else [])
            return plist if isinstance(plist, list) else None
        except (ValueError, TypeError):
            return None
    return _load_presets()

def _save_presets_ctx(plist):
    """Сохранение пресетов с учётом контекста."""
    if _is_web_request() and session.get("user_id"):
        result = _supabase_web("PATCH", "web_users",
            {"presets": json.dumps(plist, ensure_ascii=False)},
            params={"id": f"eq.{session['user_id']}", "select": "id"})
        return (isinstance(result, list) and len(result) == 1
                and str(result[0].get("id")) == str(session["user_id"]))
    else:
        _save_presets(plist)
        return True

@app.route("/api/presets", methods=["GET"])
def _presets_get():
    plist = _load_presets_ctx()
    if plist is None:
        return jsonify({"ok": False, "error": "preset_load_failed",
                        "reason": "Не удалось загрузить пресеты из облака. Попробуйте ещё раз."}), 503
    return jsonify(plist)

_PRESET_LOCK = threading.RLock()

@app.route("/api/presets", methods=["POST"])
def _presets_post():
    with _PRESET_LOCK:
        return _presets_update()

def _presets_update():
    data = request.get_json(force=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_preset"}), 400
    if "boe_filters" in data:
        try:
            filters = json.loads(data["boe_filters"]) if isinstance(data["boe_filters"], str) else data["boe_filters"]
            if not isinstance(filters, list) or not all(isinstance(f, dict) for f in filters):
                raise ValueError("Invalid BoE filters")
            data["boe_filters"] = json.dumps(filters, ensure_ascii=False)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "invalid_boe_filters",
                            "reason": "Неверный формат BoE-фильтров. Черновик сохранён на устройстве."}), 400
    # Нормализуем ID-поля к строке "123,456"
    if "item_ids" in data:
        data["item_ids"] = _normalize_ids(data["item_ids"])
    if "sale_realm_ids" in data:
        data["sale_realm_ids"] = _normalize_ids(data["sale_realm_ids"])
    # Free tier: max 5 items, 1 preset, no BoE
    is_free = _is_web_request() and session.get("tier", "none") == "none"
    if is_free:
        ids_str = data.get("item_ids", "")
        ids_list = [x.strip() for x in str(ids_str).split(",") if x.strip()] if ids_str else []
        if len(ids_list) > 5:
            return jsonify({"ok": False, "error": "free_limit", "reason": "Free: максимум 5 предметов. Обновите тариф → поддержка"}), 403
        # Keep a paid preset's filters intact when a subscription expires.
        # Feature access is enforced when filtering, without destroying saved settings.
        data.pop("boe_filters", None)
        data.pop("boe_enabled", None)
    plist = _load_presets_ctx()
    if plist is None:
        return jsonify({"ok": False, "error": "preset_load_failed",
                        "reason": "Не удалось загрузить пресеты из облака. Попробуйте ещё раз."}), 503
    pid = data.get("id")
    client_id = data.get("client_id")
    if client_id is not None and (not isinstance(client_id, str) or not 8 <= len(client_id) <= 80):
        return jsonify({"ok": False, "error": "invalid_client_id"}), 400
    if pid is None and client_id:
        # Creation can be retried after a lost response. The token lives in the
        # existing presets JSON; no new Supabase columns or migration required.
        existing = next((p for p in plist if p.get("client_id") == client_id), None)
        if existing is not None:
            pid = existing["id"]
    if is_free and pid is None and len(plist) >= 1:
        return jsonify({"ok": False, "error": "free_limit", "reason": "Free: 1 пресет. Обновите тариф → поддержка"}), 403
    if pid is not None:
        # Update existing
        for p in plist:
            if str(p.get("id")) == str(pid):
                pid = p["id"]
                for k in ("name","item_ids","sale_realm_ids","discount","min_top10avg","is_default","region","boe_filters","boe_enabled","items_enabled"):
                    if k in data: p[k] = data[k]
                break
        else:
            return jsonify({"ok": False, "error": "preset_not_found",
                            "reason": "Пресет больше не существует. Выберите другой пресет."}), 404
    else:
        # Create new – generate unique id
        max_id = max((int(p.get("id", 0)) for p in plist if str(p.get("id", 0)).isdigit()), default=0)
        newp = {"id": max_id + 1, "name": data.get("name","New Preset"),
                "item_ids": data.get("item_ids",""), "sale_realm_ids": data.get("sale_realm_ids",""),
                "discount": data.get("discount"), "min_top10avg": data.get("min_top10avg"),
                "deals_only": False, "boe_filters": data.get("boe_filters", "[]"), "boe_enabled": data.get("boe_enabled", True), "items_enabled": data.get("items_enabled", True), "editable": True,
                "is_default": data.get("is_default", False), "region": data.get("region", REGION)}
        if client_id:
            newp["client_id"] = client_id
        plist.append(newp)
        pid = newp["id"]
    # If marked as default, unmark others for this region
    if data.get("is_default"):
        for p in plist:
            if p.get("id") != pid and p.get("region") == data.get("region", REGION):
                p["is_default"] = False
    if not _save_presets_ctx(plist):
        return jsonify({"ok": False, "error": "preset_save_failed",
                        "reason": "Не удалось сохранить пресет в облако. Попробуйте ещё раз."}), 503
    return jsonify({"ok": True, "id": pid})

@app.route("/api/presets", methods=["DELETE"])
def _presets_delete():
    with _PRESET_LOCK:
        return _presets_delete_locked()

def _presets_delete_locked():
    pid = request.args.get("id")
    if not pid: return jsonify({"ok": False, "error": "no id"}), 400
    plist = _load_presets_ctx()
    if plist is None:
        return jsonify({"ok": False, "error": "preset_load_failed",
                        "reason": "Не удалось загрузить пресеты из облака. Попробуйте ещё раз."}), 503
    plist = [p for p in plist if str(p.get("id")) != str(pid)]
    if not _save_presets_ctx(plist):
        return jsonify({"ok": False, "error": "preset_save_failed",
                        "reason": "Не удалось сохранить пресет в облако. Попробуйте ещё раз."}), 503
    return jsonify({"ok": True})

@app.route("/api/start")
def _start():
    force = request.args.get("force", "0") == "1"
    try:
        c = sqlite3.connect(DB); n = c.execute("SELECT COUNT(*) FROM realms").fetchone()[0]; c.close()
    except: n = 0
    _run_start(force)
    with _lk: _st.update(state="collecting", realms_done=0, realms_total=n)
    return jsonify({"ok": True})

@app.route("/api/stop")
def _stop():
    _ct.set()
    with _lk: _st["state"] = "idle"
    return jsonify({"ok": True})

@app.route("/api/logs")
def _logs(): return jsonify({"logs": list(_log)})

@app.route("/api/sound")
def _api_sound():
    """Отдаёт настроенный файл звука уведомления (только локальный EXE)."""
    if _is_web_request():
        flask.abort(403)
    fn = (_settings.get("sound_file") or "").strip()
    if not fn:
        flask.abort(404)
    from flask import send_file
    candidates = [fn, data(fn),
                  os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", fn))]
    for cpath in candidates:
        try:
            if cpath and os.path.isfile(cpath):
                return send_file(cpath)
        except Exception:
            continue
    flask.abort(404)

# ═══════════════ License ═══════════════

@app.route("/api/license/status")
def _license_status():
    if ADMIN_BUILD and not _is_web_request():
        return jsonify({"valid": True, "tier": "pro", "expires_at": "forever", "reason": "admin_build"})
    if _is_web_request() and session.get("user_id"):
        try:
            users = _supabase_web("GET", "web_users", params={"id": f"eq.{session['user_id']}", "select": "license_key,tier"})
            if not users or not users[0].get("license_key"):
                return jsonify({"valid": False, "tier": session.get("tier", "none"), "reason": "no_key"})
            lk = users[0]["license_key"].strip().upper()
            import hashlib
            kh = hashlib.sha256(lk.encode()).hexdigest()
            lic = _supabase_web("GET", "licenses", params={"key_hash": f"eq.{kh}", "select": "expires_at,tier"})
            exp = lic[0].get("expires_at", "forever") if lic else "forever"
            tier = users[0].get("tier", "none")
            return jsonify({"valid": tier != "none", "tier": tier, "expires_at": exp or "forever"})
        except Exception as e:
            return jsonify({"valid": False, "tier": "none", "reason": str(e)})
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        import license_manager
        result = license_manager.check_license()
        return jsonify(result)
    except Exception as e:
        return jsonify({"valid": False, "tier": "free", "reason": str(e)})

@app.route("/api/license/activate", methods=["POST"])
def _license_activate():
    try:
        data = request.get_json(force=True) or {}
        key = data.get("key", "").strip()
        if not key:
            return jsonify({"valid": False, "reason": "No key provided"})
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        import license_manager
        result = license_manager.activate_key(key)
        return jsonify(result)
    except Exception as e:
        return jsonify({"valid": False, "reason": str(e)})

@app.route("/api/settings", methods=["GET"])
def _get_settings():
    if _is_web_request():
        return jsonify({"region": _settings.get("region", "eu")})
    s = dict(_load_settings())
    # Секрет не отдаём наружу даже на localhost-UI: фронт показывает плейсхолдер,
    # а пустое поле при сохранении означает «оставить как было».
    has_secret = bool(s.get("client_secret"))
    s["client_secret"] = ""
    s["has_client_secret"] = has_secret
    return jsonify(s)

@app.route("/api/settings", methods=["POST"])
def _set_settings():
    global REGION, DB, _settings
    if _is_web_request():
        return jsonify({"ok": False, "error": "desktop_admin_only"}), 403
    payload = request.get_json(force=True) or {}
    changed = False
    region_changed = False
    all_keys = ("region", "client_id", "client_secret", "pin_avg_gold", "pin_discount",
                "sound_enabled", "sound_file", "sound_data", "win_w", "win_h")
    for k in all_keys:
        if k not in payload:
            continue
        val = payload[k]
        # Пустые креды = «не изменять» (защита от затирания секрета маской)
        if k in ("client_id", "client_secret") and not str(val).strip():
            continue
        if val != _settings.get(k):
            _settings[k] = val
            changed = True
            if k == "region": region_changed = True
    if changed:
        _save_settings(_settings)
        # Update .env
        try:
            ep = data(".env")
            lines = []
            if os.path.exists(ep):
                with open(ep, encoding="utf-8") as f: lines = f.read().splitlines()
            def _set_env(lines, key, val):
                found = False
                for i, l in enumerate(lines):
                    if l.startswith(key + "="): lines[i] = f"{key}={val}"; found = True; break
                if not found: lines.append(f"{key}={val}")
            _set_env(lines, "AHGEN_REGION", _settings["region"])
            _set_env(lines, "CLIENT_ID", _settings["client_id"])
            _set_env(lines, "CLIENT_SECRET", _settings["client_secret"])
            with open(ep, "w", encoding="utf-8") as f: f.write("\n".join(lines) + "\n")
            os.environ["AHGEN_REGION"] = _settings["region"]
            os.environ["CLIENT_ID"] = _settings["client_id"]
            os.environ["CLIENT_SECRET"] = _settings["client_secret"]
        except: pass
        # Switch region only if region actually changed
        if region_changed and _settings["region"] != REGION:
            _ct.set()  # stop collector
            time.sleep(0.5)
            # Dump current cache to DB before switching
            with _CL:
                if _cache:
                    _save_avg()
                    _dump_db()
            REGION = _settings["region"]
            DB = _db_path()
            _load_caches()
            _ct.clear()
    resp_settings = dict(_settings)
    if resp_settings.get("client_secret"):
        resp_settings["client_secret"] = ""
        resp_settings["has_client_secret"] = True
    return jsonify({"ok": True, "settings": resp_settings})

# ═══════════════ Browser Routes (/browser/*) ═══════════════

def _estimate_expansion(item_id):
    """Expansion по диапазону item_id (Blizzard mapping)"""
    if item_id <= 25817: return 0       # Classic
    if item_id <= 35573: return 1       # TBC
    if item_id <= 52251: return 2       # WotLK
    if item_id <= 79012: return 3       # Cataclysm
    if item_id <= 106000: return 4      # MoP
    if item_id <= 129392: return 5      # WoD
    if item_id <= 154881: return 6      # Legion
    if item_id <= 175354: return 7      # BfA
    if item_id <= 190955: return 8      # Shadowlands
    if item_id <= 210725: return 9      # Dragonflight
    if item_id <= 229000: return 10     # TWW
    return 11                            # Midnight

# ── Midnight 12.1 ("The Curse of Ula'tek") ──
# Фильтр "Эра → Midnight 12.1": только предметы патча 12.1.0+.
# 12.1.0 начинается с item_id 276571 (проверено по wowhead: 276285-276330 = 12.0.7,
# 276571 = первый 12.1.0). Плюс ручные id из midnight_12_1_items.txt.
MIN_MIDNIGHT_121_ID = 276571
_MIDNIGHT_121_IDS = None

def _midnight_121_set():
    global _MIDNIGHT_121_IDS
    if _MIDNIGHT_121_IDS is None:
        s = set()
        try:
            p = data("midnight_12_1_items.txt")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    content = "\n".join(
                        line.split("#", 1)[0] for line in f
                    )
                    for tok in content.replace("\n", ",").split(","):
                        tok = tok.strip()
                        if tok.isdigit():
                            s.add(int(tok))
        except Exception:
            pass
        _MIDNIGHT_121_IDS = s
    return _MIDNIGHT_121_IDS

import browser

@app.route("/browser/api/status")
def _browser_status():
    return jsonify({
        "region": _settings["region"],
        "items_cached": sum(len(v) for v in _cache.values()),
        "last_collected": _cat,
        "item_meta_count": len(browser._meta),
        "snipe_count": len(browser._snipe_list),
        "realms_count": len(_rnames),
    })

@app.route("/browser/api/categories")
def _browser_categories():
    tree = browser.build_categories(dict(_cache), _avg)
    return jsonify({"categories": tree, "region": _settings["region"]})

@app.route("/browser/api/items")
def _browser_items():
    class_id = request.args.get("class_id", type=int)
    subclass_ids = request.args.get("filter_subclass_ids", "")
    quality = request.args.get("quality", "")
    bind_type_str = request.args.get("bind_type", "")
    boe_flag = request.args.get("boe", "")
    expansion_id = request.args.get("expansion_id", type=int)
    ilvl_min = request.args.get("ilvl_min", type=int)
    ilvl_max = request.args.get("ilvl_max", type=int)
    boe_stats = request.args.get("boe_stats", "").strip().lower()
    boe_socket = request.args.get("boe_socket", "").strip().lower()
    boe_effect = request.args.get("boe_effect", "").strip().lower()
    search = request.args.get("search", "").lower()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    sort = request.args.get("sort", "discount_desc")

    subclass_set = None
    if subclass_ids:
        try: subclass_set = set(int(x) for x in subclass_ids.split(","))
        except: pass

    bind_type_set = None
    if bind_type_str:
        try: bind_type_set = set(int(x) for x in bind_type_str.split(","))
        except: pass

    boe_only = boe_flag == "true"
    if not boe_only:
        boe_stats = boe_socket = boe_effect = ""
    wanted_ids = {int(x) for x in request.args.get("item_ids", "").split(",") if x.isdigit()}

    items = []
    with _CL:
        _cache_snapshot = {k: list(v) for k, v in _cache.items()}
    for (item_id, ilvl), realm_list in _cache_snapshot.items():
        if wanted_ids and item_id not in wanted_ids: continue
        meta = browser._meta.get(item_id) or {}
        cid = meta.get("class_id")
        scid = meta.get("subclass_id")
        qtype = meta.get("quality_type", "")
        bt_item = meta.get("bind_type", 0)

        if class_id and cid != class_id: continue
        if subclass_set and scid not in subclass_set: continue
        if quality and qtype.upper() != quality.upper(): continue
        if bind_type_set and bt_item not in bind_type_set: continue
        if boe_only and ilvl == 0: continue  # BoE = items with ilvl>0 from bonus_lists
        # Filter against exact live variants, never against a price-labelled
        # summary that may merge different stat rolls.
        variant = _boe_display_variant(item_id, ilvl, boe_stats, boe_socket, boe_effect) if ilvl > 0 else None
        if boe_only and not variant: continue
        display_variant = variant or _boe_display_variant(item_id, ilvl) if ilvl > 0 else None
        filter_ilvl = ilvl if ilvl > 0 else (meta.get("item_level") or 0)
        if ilvl_min is not None and filter_ilvl < ilvl_min: continue
        if ilvl_max is not None and filter_ilvl > ilvl_max: continue
        if expansion_id is not None:
            if expansion_id == 12:  # Midnight 12.1: id >= первого предмета 12.1.0 + ручные id из txt
                if item_id < MIN_MIDNIGHT_121_ID and item_id not in _midnight_121_set(): continue
            else:
                eid = _estimate_expansion(item_id)
                if eid != expansion_id: continue

        name = _inames.get(item_id, f"Item {item_id}")
        if ilvl: name += f" [{ilvl}]"
        if search and search not in name.lower() and search not in str(item_id): continue

        prices = sorted([r[1] for r in realm_list])
        min_price = prices[0] if prices else 0
        if display_variant:
            min_price = int(display_variant.get("price", display_variant.get("min_price", min_price)) or min_price)
        realm_count = len(_boe_realm_rows(item_id, ilvl, boe_stats, boe_socket, boe_effect)) if ilvl else len(realm_list)
        avg_price = _avg.get((item_id, ilvl), 0)
        discount = round((1 - min_price / avg_price) * 100, 1) if avg_price and min_price else 0

        items.append({
            "item_id": item_id, "ilvl": ilvl, "name": name,
            "min_price": min_price, "avg_price": avg_price,
            "discount": discount, "realm_count": realm_count,
            "quality": qtype, "class_id": cid, "subclass_id": scid,
            "class_name": meta.get("class_name", ""),
            "subclass_name": meta.get("subclass_name", ""),
            "slot_name": meta.get("slot_name", ""),
            "item_level": meta.get("item_level", 0),
            "required_level": meta.get("required_level", 0),
            "bind_type": bt_item,
            "boe": bool(ilvl), "stats": (display_variant or {}).get("stats", []) if ilvl else [],
            "stats_label": (display_variant or {}).get("stats_label", "") if ilvl else "",
            "sockets": (display_variant or {}).get("sockets") if ilvl else None,
            "effects": (display_variant or {}).get("effects") if ilvl else None,
            "variants": _boe_variant_options(item_id, ilvl) if ilvl else [],
        })

    if sort == "discount_desc": items.sort(key=lambda x: -x["discount"])
    elif sort == "discount_asc": items.sort(key=lambda x: x["discount"])
    elif sort == "price_asc": items.sort(key=lambda x: x["min_price"])
    elif sort == "price_desc": items.sort(key=lambda x: -x["min_price"])
    elif sort == "name": items.sort(key=lambda x: x["name"])
    elif sort == "ilvl_asc": items.sort(key=lambda x: (x["ilvl"], x["name"]))
    elif sort == "ilvl_desc": items.sort(key=lambda x: (-x["ilvl"], x["name"]))

    total = len(items)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]

    return jsonify({"items": page_items, "total": total, "page": page, "per_page": per_page})

_picker_names_ru = None

@app.route("/browser/api/picker")
def _browser_picker():
    """Small, paged item picker; one result per item, independent of sniper rules."""
    global _picker_names_ru
    if _picker_names_ru is None:
        try:
            with open(data("item_names_ru.json"), encoding="utf-8") as f:
                _picker_names_ru = {int(k): v for k, v in json.load(f).items() if str(k).isdigit() and isinstance(v, str)}
        except (OSError, ValueError, TypeError):
            _picker_names_ru = {}
    search = request.args.get("search", "").strip().casefold()[:160]
    offset = max(0, request.args.get("offset", 0, type=int))
    limit = min(100, max(1, request.args.get("limit", 40, type=int)))
    supplied = request.args.get("ids")
    if supplied is not None:
        try:
            candidates = {int(x) for x in supplied.split(",") if x.strip() and int(x) > 0}
        except ValueError:
            return jsonify({"error": "invalid_ids"}), 400
    else:
        candidates = set(_inames) | set(browser._meta)
    with _CL:
        boe_ids = {iid for iid, ilvl in _cache if ilvl > 0}
    boe_ids.update(iid for iid, meta in browser._meta.items() if meta.get("bind_type") == 2)
    if request.args.get("boe") == "true" and supplied is None:
        candidates &= boe_ids
    result = []
    for iid in candidates:
        name = _inames.get(iid, f"Item {iid}")
        ru = _picker_names_ru.get(iid, "")
        if search and search not in str(iid) and search not in name.casefold() and search not in ru.casefold():
            continue
        meta = browser._meta.get(iid, {})
        result.append({"id": iid, "name": name, "name_ru": ru, "quality": meta.get("quality_type", ""),
                       "slot": meta.get("slot_name", ""), "boe": iid in boe_ids})
    result.sort(key=lambda item: (str(item["id"]) != search, item["name"].casefold(), item["id"]))
    return jsonify({"items": result[offset:offset + limit], "total": len(result), "offset": offset})

@app.route("/browser/api/item")
def _browser_item():
    item_id = request.args.get("id", type=int)
    ilvl = request.args.get("ilvl", 0, type=int)
    boe_stats = request.args.get("boe_stats", "")
    boe_socket = request.args.get("boe_socket", "")
    boe_effect = request.args.get("boe_effect", "")
    if not item_id: return jsonify({"error": "id required"}), 400

    meta = browser._meta.get(item_id) or {}
    name = _inames.get(item_id, f"Item {item_id}")
    if ilvl: name += f" [{ilvl}]"
    key = (item_id, ilvl)
    with _CL:
        if ilvl:
            realm_data = _boe_realm_rows(item_id, ilvl, boe_stats, boe_socket, boe_effect)
        else:
            realm_data = [{"realm_id": rid, "realm_name": _rnames.get(rid, f"R{rid}"),
                           "price": price, "quantity": qty, "slug": _rslugs.get(rid, "")}
                          for rid, price, qty in _cache.get(key, [])]
    realm_data = [{**r, "min_buyout": r["price"], "price_g": _g(r["price"])} for r in realm_data]
    realm_data.sort(key=lambda r: r["price"])
    display_variant = realm_data[0] if realm_data else None
    cheapest_rid = display_variant["realm_id"] if display_variant else None
    avg_price = _avg.get(key, 0)
    min_price = display_variant["price"] if display_variant else 0
    discount = round((1 - min_price / avg_price) * 100, 1) if avg_price and min_price else 0

    # Build Undermine URL for cheapest realm
    if cheapest_rid:
        slug = _rslugs.get(cheapest_rid, "")
        url = f"https://undermine.exchange/#{REGION}-{slug}/{item_id}" if slug else f"https://undermine.exchange/#{REGION}/{cheapest_rid}/{item_id}"
    else:
        url = f"https://undermine.exchange/#{REGION}/{item_id}"

    return jsonify({
        "item_id": item_id, "ilvl": ilvl, "name": name,
        "min_price": min_price, "avg_price": avg_price, "discount": discount,
        "realm_count": len(realm_data), "realm_data": realm_data,
        "quality": meta.get("quality_type", ""),
        "class_name": meta.get("class_name", ""),
        "subclass_name": meta.get("subclass_name", ""),
        "item_level": meta.get("item_level", 0),
        "slot_name": meta.get("slot_name", ""),
        "url": url, "region": REGION,
        "boe": bool(ilvl),
        "stats": (display_variant or {}).get("stats", []) if ilvl else [],
        "stats_label": (display_variant or {}).get("stats_label", "") if ilvl else "",
        "sockets": (display_variant or {}).get("sockets") if ilvl else None,
        "effects": (display_variant or {}).get("effects") if ilvl else None,
        "variants": _boe_variant_rows(item_id, ilvl) if ilvl else [],
    })

@app.route("/browser/icon/<int:item_id>")
def _browser_icon(item_id):
    from flask import send_file
    content, mime = browser.get_item_icon(item_id, _settings["region"])
    if content:
        return send_file(BytesIO(content), mimetype=mime)
    return "", 404

@app.route("/browser/api/quality_types")
def _browser_quality_types():
    return jsonify([
        {"type": "POOR", "name": "Poor"},
        {"type": "COMMON", "name": "Common"},
        {"type": "UNCOMMON", "name": "Uncommon"},
        {"type": "RARE", "name": "Rare"},
        {"type": "EPIC", "name": "Epic"},
        {"type": "LEGENDARY", "name": "Legendary"},
    ])

@app.route("/browser/api/snipe/list")
def _browser_snipe_list():
    if _is_web_request():
        return jsonify({"items": []})
    return jsonify({"items": browser.get_snipe_list()})

@app.route("/browser/api/snipe/toggle", methods=["POST"])
def _browser_snipe_toggle():
    if _is_web_request():
        return jsonify({"error": "web_snipe_is_preset_scoped"}), 403
    data = request.get_json(force=True) or {}
    item_id = data.get("item_id", 0)
    added = browser.toggle_snipe(item_id)
    return jsonify({"item_id": item_id, "added": added, "total": len(browser._snipe_list)})

@app.route("/browser/api/snipe/add_bulk", methods=["POST"])
def _browser_snipe_add_bulk():
    if _is_web_request():
        return jsonify({"error": "web_snipe_is_preset_scoped"}), 403
    data = request.get_json(force=True) or {}
    item_ids = data.get("item_ids", [])
    total = browser.add_snipe_bulk(item_ids)
    return jsonify({"total": total})

@app.route("/browser/api/snipe/reset", methods=["POST"])
def _browser_snipe_reset():
    if _is_web_request():
        return jsonify({"error": "web_snipe_is_preset_scoped"}), 403
    data = request.get_json(force=True) or {}
    item_ids = data.get("item_ids", [])
    total = browser.reset_snipe_list(item_ids)
    return jsonify({"total": total})

@app.route("/browser/api/snipe/items")
def _browser_snipe_items():
    """Return items from snipe list with meta data and optional cache prices.
    Different ilvl variants of the same item appear as separate entries."""
    supplied_ids = request.args.get("item_ids", "")
    if _is_web_request():
        try:
            sniped_ids = {
                int(value) for value in supplied_ids.split(",") if value.strip()
            }
        except ValueError:
            return jsonify({"error": "invalid item_ids"}), 400
    else:
        sniped_ids = set(browser.get_snipe_list())
    class_id = request.args.get("class_id", type=int)
    subclass_ids = request.args.get("filter_subclass_ids", "")
    subclass_set = None
    if subclass_ids:
        try: subclass_set = set(int(x) for x in subclass_ids.split(","))
        except: pass
    # Build a set of (item_id, ilvl) pairs from cache for all sniped items
    items = []
    seen_keys = set()
    # First pass: iterate cache to find all (item_id, ilvl) for sniped items
    for (item_id, il), prices in list(_cache.items()):
        if item_id not in sniped_ids:
            continue
        k = (item_id, il)
        if k in seen_keys: continue
        seen_keys.add(k)
        meta = browser._meta.get(item_id) or {}
        cid = meta.get("class_id")
        scid = meta.get("subclass_id")
        if class_id and cid != class_id: continue
        if subclass_set and scid not in subclass_set: continue
        name = _inames.get(item_id, meta.get("name", f"Item {item_id}"))
        if il: name += f" [{il}]"
        prices_sorted = sorted([r[1] for r in prices])
        min_price = prices_sorted[0] if prices_sorted else 0
        display_variant = _boe_display_variant(item_id, il) if il else None
        if display_variant:
            min_price = int(display_variant.get("price", display_variant.get("min_price", min_price)) or min_price)
        avg_price = _avg.get(k, 0)
        discount = round((1 - min_price / avg_price) * 100, 1) if avg_price and min_price else 0
        realm_count = len(prices)
        items.append({
            "item_id": item_id, "ilvl": il, "name": name,
            "min_price": min_price, "avg_price": avg_price,
            "discount": discount, "realm_count": realm_count,
            "quality": meta.get("quality_type", ""),
            "class_id": cid, "subclass_id": scid,
            "class_name": meta.get("class_name", ""),
            "subclass_name": meta.get("subclass_name", ""),
            "slot_name": meta.get("slot_name", ""),
            "item_level": meta.get("item_level", 0),
            "on_auction": True,
            "boe": bool(il),
            "stats": (display_variant or {}).get("stats", []) if il else [],
            "stats_label": (display_variant or {}).get("stats_label", "") if il else "",
            "sockets": (display_variant or {}).get("sockets") if il else None,
            "effects": (display_variant or {}).get("effects") if il else None,
            "variants": _boe_variant_options(item_id, il) if il else [],
            "is_121": item_id >= MIN_MIDNIGHT_121_ID or item_id in _midnight_121_set(),
        })
    # Second pass: add sniped items not in cache (not on AH) with ilvl=0
    for item_id in sniped_ids:
        if item_id not in {i for i, _ in seen_keys}:
            meta = browser._meta.get(item_id) or {}
            cid = meta.get("class_id")
            scid = meta.get("subclass_id")
            if class_id and cid != class_id: continue
            if subclass_set and scid not in subclass_set: continue
            name = _inames.get(item_id, meta.get("name", f"Item {item_id}"))
            items.append({
                "item_id": item_id, "ilvl": 0, "name": name,
                "min_price": 0, "avg_price": 0,
                "discount": 0, "realm_count": 0,
                "quality": meta.get("quality_type", ""),
                "class_id": cid, "subclass_id": scid,
                "class_name": meta.get("class_name", ""),
                "subclass_name": meta.get("subclass_name", ""),
                "slot_name": meta.get("slot_name", ""),
                "item_level": meta.get("item_level", 0),
                "on_auction": False,
                "boe": False, "stats": [], "stats_label": "", "sockets": 0, "variants": [],
                "is_121": item_id >= MIN_MIDNIGHT_121_ID or item_id in _midnight_121_set(),
            })
    items.sort(key=lambda x: -x["discount"])
    return jsonify({"items": items, "total": len(items)})

@app.route("/browser/api/realms")
def _browser_realms():
    """Return realm metadata used by the sale-realm picker.

    ``_rnames`` intentionally stores display-name strings, so reading metadata
    from it as dictionaries used to make this endpoint fail with HTTP 500.
    """
    realm_list = []
    try:
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, name_ru, name_en, slug FROM realms "
            "ORDER BY COALESCE(NULLIF(name_ru, ''), name_en), id"
        ).fetchall()
        c.close()
        for row in rows:
            realm_list.append({
                "id": row["id"],
                "name": row["name_ru"] or row["name_en"] or f"R{row['id']}",
                "name_en": row["name_en"] or "",
                "slug": row["slug"] or "",
            })
    except Exception as e:
        _dlog(f"Realm list error: {e}")
        realm_list = [
            {"id": rid, "name": str(name), "name_en": "", "slug": ""}
            for rid, name in sorted(_rnames.items(), key=lambda pair: str(pair[1]).lower())
        ]
    return jsonify({"realms": realm_list})

@app.route("/browser/api/region", methods=["GET"])
def _browser_region_get():
    return jsonify({"region": _settings["region"]})

@app.route("/browser/api/region", methods=["POST"])
def _browser_region_post():
    data = request.get_json(force=True) or {}
    new_region = data.get("region", "")
    if new_region in ("eu", "us"):
        global REGION, DB
        REGION = new_region
        _settings["region"] = new_region
        _save_settings(_settings)
        DB = _db_path()
        _load_caches()
        browser.load_meta(REGION)
        return jsonify({"ok": True, "region": REGION})
    return jsonify({"ok": False, "error": "invalid region"}), 400

# ═══════════════ Collector ═══════════════

def _run_collector():
    global _cache, _avg, _cat, _blizz_lm, _scan_gen, _scan_gen_map, _realm_keys
    old_stdout, sys.stdout = sys.stdout, _SR()
    try:
        from ahgem import (REGION_CONFIGS, init_db, get_realm_ids, get_access_token,
                           rebuild_realms, get_auctions_for_realm_with_retry, process_auctions,
                           token_expired)
        import random, requests
        from concurrent.futures import ThreadPoolExecutor, as_completed

        cfg = dict(REGION_CONFIGS.get(REGION, {}))
        if not cfg: _dlog("Bad region"); return
        cfg["db_file"] = DB
        cfg["client_id"] = os.getenv("CLIENT_ID", cfg.get("client_id", ""))
        cfg["client_secret"] = os.getenv("CLIENT_SECRET", cfg.get("client_secret", ""))
        if not cfg["client_id"]: _dlog("No creds"); return

        _dlog(f"Starting {REGION.upper()}...")
        # OAuth с ретраями
        token = None
        for oa in range(5):
            try:
                init_db(cfg)
                token = get_access_token(cfg)
                break
            except Exception as e:
                _dlog(f"OAuth attempt {oa+1}/5: {e}")
                if oa < 4:
                    with _lk: _st["state"] = f"OAuth retry {oa+1}/5..."
                    time.sleep(5)
        if not token:
            with _lk: _st["state"] = "OAuth failed (5 attempts)"
            _dlog("OAuth failed after 5 attempts")
            return

        if not get_realm_ids(cfg):
            try: rebuild_realms(token, cfg)
            except: pass
            # Reload realm names after rebuild
            try:
                c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
                for r in c.execute("SELECT id, name_ru, name_en, slug FROM realms").fetchall():
                    _rnames[r["id"]] = r["name_ru"] or r["name_en"] or f"R{r['id']}"
                    _rslugs[r["id"]] = r["slug"] or ""
                c.close()
            except: pass
        realm_ids = get_realm_ids(cfg)
        _dlog(f"{len(realm_ids)} realms")

        # LM cache
        lm = {}
        try:
            c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
            for r in c.execute("SELECT key, value FROM meta WHERE key LIKE 'lm_%'").fetchall():
                lm[int(r["key"].replace("lm_",""))] = r["value"]
            c.close()
        except: pass

        # Upgrade old snapshots immediately: an HTTP 304 has no lot modifiers.
        # Force a full response for realms with legacy BoE aggregates.
        with _CL:
            for (iid, il), prices in _cache.items():
                if il:
                    for rid, _, _ in prices:
                        if (rid, iid, il) not in _boe_variant_cache:
                            lm.pop(rid, None)

        _cat = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _scan_gen += 1; _scan_gen_map = {}

        new_data = {}; new_lm = {}; failed = 0
        t_scan = time.time()
        # Try HTTP/2 first (on_realm updates cache progressively as each realm arrives)
        h2_result = _fetch_all_realms_http2(token, realm_ids, cfg, lm, on_realm=_update_realm_in_cache)
        if h2_result is not None:
            new_data, new_lm, failed, skipped = h2_result
            with _lk: _st.update(realms_done=len(realm_ids))
            _dlog(f"Done: {len(new_data)} changed, {skipped} same, {failed} failed | total={time.time()-t_scan:.1f}s")
        else:
            # Fallback: requests + 10 workers
            _dlog("Fallback: threads (10 workers)")
            session = requests.Session()
            session.mount('https://', requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10))
            done = 0
            with ThreadPoolExecutor(max_workers=10) as ex:
                fs = {ex.submit(get_auctions_for_realm_with_retry, token, rid, cfg, session, 3, lm.get(rid)): rid for rid in realm_ids}
                for f in as_completed(fs):
                    if _ct.is_set(): break
                    rid = fs[f]; done += 1
                    try:
                        result = f.result(timeout=60)
                        data, nlm = result if isinstance(result, tuple) else (result, "")
                        if data:
                            agg = process_auctions(data); new_data[rid] = agg
                            if nlm: new_lm[rid] = nlm
                            _update_realm_in_cache(rid, agg)
                            if done % 5 == 0 or done == len(realm_ids):
                                with _lk: _st.update(realms_done=done)
                    except Exception as e: _dlog(f"R{rid}: {e}"); failed += 1
                    if done % 5 == 0: print(f"   {done}/{len(realm_ids)}", end='\r')
            session.close()
            _dlog(f"Done: {len(new_data)} changed, {failed} failed | total={time.time()-t_scan:.1f}s")
        if new_data:
            _recalc_avg(); _save_avg(); _save_boe_variant_cache()
            threading.Thread(target=_dump_db, daemon=True).start()
            threading.Thread(target=_push_to_supabase, daemon=True).start()
            # Update previous snapshot items for rare detection
            current_ids = set(iid for (iid, il) in _cache.keys())
            global _prev_snapshot_items
            _prev_snapshot_items = current_ids
        else:
            # Все реалмы вернули 304 (данные не изменились) — перезагружаем кэш из DB
            _dlog("No new data (all 304), reloading cache from DB")
            _load_caches()

        # Publish completion only once the restored snapshot is available.
        with _lk: _st.update(realms_done=len(realm_ids), state="monitoring")

        try:
            c = sqlite3.connect(DB)
            for rid, v in new_lm.items(): c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (f"lm_{rid}", v))
            lm.update(new_lm)  # Update in-memory lm so monitoring uses fresh IMS headers
            # Pick latest Blizzard last-modified from any realm
            latest_lm = max((v for v in new_lm.values() if v), default=None, key=lambda x: x)
            if latest_lm:
                c.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('blizzard_last_modified',?)", (latest_lm,))
                _blizz_lm = latest_lm
            c.commit(); c.close()
        except: pass

        # ── Smart Window Monitoring ──
        ci_raw = cfg.get("check_realm_ids_raw", "")
        if ci_raw.strip(): ci = [int(x.strip()) for x in ci_raw.split(",") if x.strip()]
        else:
            pn = cfg.get("pinned_realms", []); ot = [r for r in realm_ids if r not in pn]; random.shuffle(ot)
            ci = (pn + ot)[:25]

        from ahgem import check_update_for_realm
        from email.utils import parsedate_to_datetime

        blizz_lm = _blizz_lm or None
        if not blizz_lm:
            try:
                c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
                r = c.execute("SELECT value FROM meta WHERE key='blizzard_last_modified'").fetchone()
                if r: blizz_lm = r[0]
                c.close()
            except: pass

        _dlog(f"Monitoring {len(ci)} realms, blizz_lm={'yes' if blizz_lm else 'no'}")
        W = 150
        mc = 0
        # SmartMonitor: track which realms detect updates fastest
        smart_scores = {}  # {rid: {"detections": N, "avg_time": seconds}}
        # Постоянная сессия мониторинга: переиспользование TLS-соединений вместо
        # нового handshake на каждую итерацию (быстрее детект, меньше нагрузка)
        mon_session = requests.Session()
        mon_pool = max(len(ci), 25)
        mon_session.mount('https://', requests.adapters.HTTPAdapter(pool_connections=mon_pool, pool_maxsize=mon_pool))
        # Бюджет запросов: защита от упора в лимит Blizzard 36k/час при «late forcing»
        budget_left = 30000
        budget_reset = time.time() + 3600

        while not _ct.is_set():
            mc += 1
            try:
                # Обновление часового бюджета
                if time.time() >= budget_reset:
                    budget_reset = time.time() + 3600
                    budget_left = 30000
                if budget_left <= 0:
                    wait_s = max(5, min(300, int(budget_reset - time.time())))
                    _dlog(f"Blizzard hourly budget exhausted, pause {wait_s}s")
                    _ct.wait(wait_s)
                    continue
                if blizz_lm:
                    try: last_dt = parsedate_to_datetime(blizz_lm)
                    except: last_dt = None
                else:
                    last_dt = None

                if last_dt:
                    now = datetime.now(timezone.utc)
                    exp = last_dt + timedelta(minutes=60)
                    diff = (exp - now).total_seconds()
                    if abs(diff) > W:
                        if diff > W:
                            sleep_for = max(1, min(int(diff - W), 3600))
                            _dlog(f"Outside window, sleep {sleep_for}s (next at {exp.strftime('%H:%M')})")
                            _ct.wait(sleep_for)
                            continue
                        else:
                            # Опоздание обновления: форсируем непрерывную проверку,
                            # но ужимаем набор реалмов (бюджет ограничит шторм)
                            if len(ci) > 15:
                                pn_f = cfg.get("pinned_realms", [])
                                ci = (pn_f + [r for r in ci if r not in pn_f])[:15]
                            _dlog("Late update, forcing continuous check")
                            blizz_lm = None
                            time.sleep(2)
                            continue

                up, ur = False, None
                _t0 = time.time()
                budget_left -= len(ci)
                try:
                    with ThreadPoolExecutor(max_workers=len(ci)) as ex:
                        fs = {ex.submit(check_update_for_realm, token, rid, lm.get(rid), cfg, mon_session): rid for rid in ci}
                        for f in as_completed(fs):
                            if _ct.is_set(): break
                            rid = fs[f]
                            try: updated, new_blizz, _ = f.result()
                            except: continue
                            if updated:
                                up, ur = True, rid
                                # SmartMonitor: track detection speed
                                dt = time.time() - _t0
                                sc = smart_scores.setdefault(rid, {"detections": 0, "avg_time": 0.0})
                                sc["avg_time"] = (sc["avg_time"] * sc["detections"] + dt) / (sc["detections"] + 1)
                                sc["detections"] += 1
                                if new_blizz:
                                    blizz_lm = new_blizz
                                    _blizz_lm = new_blizz
                                    try:
                                        c = sqlite3.connect(DB)
                                        c.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('blizzard_last_modified',?)", (new_blizz,))
                                        c.commit(); c.close()
                                    except: pass
                                break
                except Exception as _mon_e:
                    # Ошибка одной итерации опроса не должна ронять цикл мониторинга
                    _dlog(f"Monitor poll error: {_mon_e}")
                if _ct.is_set(): break

                if up and ur:
                    _dlog(f"Update on {ur}, full recollect")
                    # Refresh token before recollect (fix: expired token → all 401s)
                    try:
                        token = get_access_token(cfg)
                        _dlog("Token refreshed before recollect")
                    except Exception as e:
                        _dlog(f"Token refresh failed: {e}")
                    # Increment scan generation (GUI sees empty → filling)
                    # Cache is NOT cleared — old data retained, new data overwrites per-realm
                    _scan_gen += 1
                    _scan_gen_map = {}
                    with _lk: _st.update(state="collecting", realms_done=0)
                    _dlog(f"Recollect gen={_scan_gen}, trying HTTP/2...")

                    new_data = {}; errs = 0
                    # Try HTTP/2 first (on_realm updates cache progressively)
                    h2_result = _fetch_all_realms_http2(token, realm_ids, cfg, lm, on_realm=_update_realm_in_cache)
                    if h2_result is not None:
                        new_data, new_lm_h2, errs, skipped = h2_result
                        lm.update(new_lm_h2)
                        with _lk: _st.update(realms_done=len(realm_ids))
                        _dlog(f"HTTP/2: {len(new_data)} changed, {skipped} same, {errs} failed")
                    else:
                        # Fallback: requests + threads (10 workers optimal)
                        _dlog("HTTP/2 unavailable, using threads (10 workers)")
                        session2 = requests.Session()
                        session2.mount('https://', requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10))
                        done = 0
                        with ThreadPoolExecutor(max_workers=10) as ex:
                            fs2 = {ex.submit(get_auctions_for_realm_with_retry, token, rid, cfg, session2, 3, lm.get(rid)): rid for rid in realm_ids}
                            for f in as_completed(fs2):
                                if _ct.is_set(): break
                                rid = fs2[f]; done += 1
                                try:
                                    result = f.result(timeout=60)
                                    data, nlm = result if isinstance(result, tuple) else (result, "")
                                    if data:
                                        agg = process_auctions(data); new_data[rid] = agg
                                        if nlm: lm[rid] = nlm
                                        _update_realm_in_cache(rid, agg)
                                except Exception as e:
                                    errs += 1
                                    if errs <= 3: _dlog(f"Recollect R{rid}: {e}")
                                if done % 10 == 0:
                                    with _lk: _st.update(realms_done=done)
                        session2.close()
                        budget_left -= len(realm_ids) + errs * 2  # fallback: до 3 попыток/реалм

                    # Retry once if got 0 results (likely token/network issue)
                    if not new_data and not _ct.is_set():
                        _dlog(f"Recollect got 0 realms ({errs} errors), retrying with fresh token...")
                        try: token = get_access_token(cfg)
                        except Exception as e: _dlog(f"Retry token failed: {e}")
                        session3 = requests.Session()
                        session3.mount('https://', requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10))
                        with ThreadPoolExecutor(max_workers=10) as ex:
                            fs3 = {ex.submit(get_auctions_for_realm_with_retry, token, rid, cfg, session3, 3, None): rid for rid in realm_ids}
                            for f in as_completed(fs3):
                                if _ct.is_set(): break
                                rid = fs3[f]
                                try:
                                    result = f.result(timeout=60)
                                    data, nlm = result if isinstance(result, tuple) else (result, "")
                                    if data:
                                        agg = process_auctions(data); new_data[rid] = agg
                                        if nlm: lm[rid] = nlm
                                        _update_realm_in_cache(rid, agg)
                                except Exception as e:
                                    _dlog(f"Retry R{rid}: {e}")
                        session3.close()
                        _dlog(f"Retry result: {len(new_data)} realms")
                        budget_left -= len(realm_ids) * 2  # retry-проход: до 3 попыток/реалм

                    # Реколлект тоже тратит квоту (92 запроса + ретраи failed-реалмов)
                    budget_left -= len(realm_ids) + errs * 2
                    if budget_left < 0: budget_left = 0

                    _cat = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    if new_data:
                        _recalc_avg(); _save_avg(); _save_boe_variant_cache()
                        threading.Thread(target=_dump_db, daemon=True).start()
                        threading.Thread(target=_push_to_supabase, daemon=True).start()
                        with _CL:
                            _prev_snapshot_items = set(iid for (iid, il) in _cache.keys())
                    else:
                        _dlog("Recollect FAILED: 0 realms after retry, reloading from DB")
                        _load_caches()
                    with _lk: _st.update(state="monitoring", realms_done=len(realm_ids))
                    _dlog(f"Recollect: {len(new_data)} realms")
                else:
                    # SmartMonitor: re-sort ci every 30 cycles by detection speed
                    if mc % 30 == 0 and smart_scores:
                        pn = cfg.get("pinned_realms", [])
                        scored = sorted(
                            [r for r in ci if r not in pn],
                            key=lambda r: smart_scores.get(r, {}).get("avg_time", 999)
                        )
                        ci = (pn + scored)[:15]
                    time.sleep(1)

                # Рефреш токена по реальному сроку жизни (expires_in − 5 мин),
                # с резервной периодической проверкой раз в ~600 циклов
                if token_expired(cfg) or mc % 600 == 0:
                    try:
                        token = get_access_token(cfg)
                        _dlog("Token refreshed (periodic/expiry)")
                    except Exception as e:
                        _dlog(f"Periodic token refresh failed: {e}")
            except Exception as e:
                _dlog(f"Monitor err: {e}"); time.sleep(5)
        mon_session.close()
    except BaseException as e:
        # BaseException: обфускация/frozen-среда может кинуть SystemExit — не теряем причину
        import traceback as _tb
        _dlog(f"FATAL collector: {e}")
        for ln in _tb.format_exc().splitlines():
            _dlog(f"  | {ln}")
    finally:
        sys.stdout = old_stdout; _ct.clear()
        _dlog("Collector thread ended")
def _run_collector_safe():
    """Обёртка с crash recovery: при падении коллектора — перезапуск через 10с."""
    max_retries = 5
    for attempt in range(max_retries):
        if _ct.is_set():
            return
        try:
            _run_collector()
            return
        except BaseException as e:
            import traceback as _tb
            _dlog(f"Collector CRASHED (attempt {attempt+1}/{max_retries}): {e}")
            for ln in _tb.format_exc().splitlines():
                _dlog(f"  | {ln}")
            with _lk:
                _st["state"] = f"error (retry {attempt+1}/{max_retries})"
            if attempt < max_retries - 1 and not _ct.is_set():
                _ct.wait(10)
    _dlog("Collector: max retries reached, giving up")
    with _lk:
        _st["state"] = "error (max retries)"

def _run_start(force=False):
    global _tid, _cache
    # Stop any existing collector/monitor thread
    _ct.set()
    if _tid and _tid.is_alive():
        _tid.join(timeout=5)
    if force:
        try:
            c = sqlite3.connect(DB); c.execute("DELETE FROM meta WHERE key LIKE 'lm_%'"); c.commit(); c.close()
        except: pass
    # Clear cache only AFTER the old collector stopped, so data never mixes
    # and two collectors never race on a freshly wiped cache.
    with _CL:
        _cache.clear()
    _ct.clear()
    _tid = threading.Thread(target=_run_collector_safe, daemon=True, name="Collector")
    _tid.start()

# ═══════════════ Item Meta Enricher (background) ═══════════════

def _item_meta_table():
    """Создать item_meta если ещё нет"""
    try:
        c = sqlite3.connect(DB)
        c.execute("""CREATE TABLE IF NOT EXISTS item_meta (
            item_id INTEGER PRIMARY KEY, class_id INTEGER, class_name TEXT,
            subclass_id INTEGER, subclass_name TEXT, slot_type TEXT, slot_name TEXT,
            quality_type TEXT, quality_name TEXT, item_level INTEGER,
            required_level INTEGER, bind_type INTEGER, is_equippable INTEGER,
            max_stack INTEGER, expansion_id INTEGER
        )""")
        c.commit(); c.close()
    except: pass

def _enrich_ghosts():
    """ID предметов, для которых Blizzard API вернул 404 (не существуют)."""
    try:
        c = sqlite3.connect(DB)
        row = c.execute("SELECT value FROM meta WHERE key='enrich404'").fetchone()
        c.close()
        return {int(x) for x in (row[0] or "").split(",") if x.strip().isdigit()}
    except:
        return set()

def _mark_enrich_ghosts(ids):
    """Запомнить 404-предметы — не переопрашивать API при каждом старте."""
    if not ids:
        return
    try:
        merged = _enrich_ghosts().union(ids)
        c = sqlite3.connect(DB, timeout=15)
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('enrich404',?)",
                  (",".join(str(i) for i in sorted(merged)),))
        c.commit(); c.close()
    except Exception as e:
        _dlog(f"Enricher ghost mark: {e}")

def _get_unenriched(limit=10):
    """Предметы из auction_snapshots, которых нет в item_meta (без 404-призраков)."""
    try:
        ghosts = _enrich_ghosts()
        c = sqlite3.connect(DB)
        rows = c.execute("""
            SELECT DISTINCT s.item_id FROM auction_snapshots s
            WHERE s.item_id NOT IN (SELECT item_id FROM item_meta WHERE class_id IS NOT NULL)
            LIMIT ?
        """, (limit + len(ghosts) + 50,)).fetchall()
        c.close()
        return [r[0] for r in rows if r[0] not in ghosts][:limit]
    except: return []

def _fetch_item_meta(item_id, token):
    """Запросить метаданные предмета из Blizzard API (US endpoint — быстрее)"""
    try:
        url = f"https://us.api.blizzard.com/data/wow/item/{item_id}"
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, params={"namespace": "static-us", "locale": "en_US"}, timeout=15)
        if resp.status_code == 404: return "gone"  # не существует → пометим призраком
        resp.raise_for_status()
        data = resp.json()
        item_class = data.get("item_class") or data.get("itemClass") or {}
        item_subclass = data.get("item_subclass") or data.get("itemSubclass") or {}
        quality = data.get("quality") or {}
        inv_type = data.get("inventory_type") or {}
        preview = data.get("preview_item") or {}
        bind_type = data.get("bind_type") or 0
        is_equip = 1 if (data.get("is_equippable") or inv_type.get("type") != "NON_EQUIP") else 0
        max_stack = preview.get("quantity") or data.get("max_count") or 1
        return {
            "item_id": item_id,
            "class_id": item_class.get("id"),
            "class_name": _s(item_class.get("name")),
            "subclass_id": item_subclass.get("id"),
            "subclass_name": _s(item_subclass.get("name")),
            "slot_type": (inv_type.get("type") or "").upper(),
            "slot_name": _s(inv_type.get("name")),
            "quality_type": (quality.get("type") or "").upper(),
            "quality_name": _s(quality.get("name")),
            "item_level": data.get("level") or data.get("item_level"),
            "required_level": data.get("required_level"),
            "bind_type": bind_type if isinstance(bind_type, int) else 0,
            "is_equippable": is_equip,
            "max_stack": max_stack if isinstance(max_stack, int) else 1,
            "expansion_id": -1
        }
    except: return None

def _s(val):
    if val is None: return None
    if isinstance(val, dict): return val.get("ru_RU") or val.get("en_US") or str(val)
    return str(val)

def _enrich_worker():
    """При старте: проверить новые предметы и обогатить если есть"""
    _item_meta_table()
    time.sleep(10)
    ids = _get_unenriched(limit=500)  # бюджетный батч: не сжигаем квоту Blizzard при старте
    if not ids:
        _dlog("Enricher: all items up to date")
        return
    _dlog(f"Enricher: {len(ids)} new items, fetching...")
    try:
        cid = os.getenv("CLIENT_ID_US", "") or os.getenv("CLIENT_ID", "")
        csec = os.getenv("CLIENT_SECRET_US", "") or os.getenv("CLIENT_SECRET", "")
        if not cid: return
        auth = (cid, csec)
        tr = requests.post("https://oauth.battle.net/token", auth=auth, data={"grant_type": "client_credentials"}, timeout=15)
        if tr.status_code != 200: return
        token = tr.json()["access_token"]
        saved = 0
        ghosts = []  # 404-предметы: Blizzard не знает таких ID
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_fetch_item_meta, iid, token): iid for iid in ids}
            for f in as_completed(futures):
                meta = f.result()
                if meta == "gone":
                    ghosts.append(futures[f])
                    continue
                if meta:
                    try:
                        c = sqlite3.connect(DB)
                        c.execute("""INSERT OR IGNORE INTO item_meta
                            (item_id, class_id, class_name, subclass_id, subclass_name,
                             slot_type, slot_name, quality_type, quality_name,
                             item_level, required_level, bind_type, is_equippable, max_stack)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (meta["item_id"], meta["class_id"], meta["class_name"],
                             meta["subclass_id"], meta["subclass_name"],
                             meta["slot_type"], meta["slot_name"],
                             meta["quality_type"], meta["quality_name"],
                             meta["item_level"], meta["required_level"],
                             meta["bind_type"], meta["is_equippable"], meta["max_stack"]))
                        c.commit(); c.close()
                        saved += 1
                    except Exception as e:
                        _dlog(f"Enricher save error: {e}")
        _mark_enrich_ghosts(ghosts)
        _dlog(f"Enricher: saved {saved}/{len(ids)} new items ({len(ghosts)} gone=404)")
    except Exception as e:
        _dlog(f"Enricher error: {e}")

# ═══════════════ Launcher ═══════════════

def _maybe_vacuum():
    """Раз в неделю при старте: VACUUM если БД раздута freelist-страницами.
    Ежечасный DELETE+INSERT не возвращает страницы ОС — файл только растёт."""
    try:
        c = sqlite3.connect(DB, timeout=10)
        c.execute("PRAGMA busy_timeout=10000")
        now_ts = time.time()
        row = c.execute("SELECT value FROM meta WHERE key='vacuum_last'").fetchone()
        if row:
            try:
                if now_ts - float(row[0]) < 7 * 86400:
                    c.close(); return
            except Exception:
                pass
        fr = c.execute("PRAGMA freelist_count").fetchone()[0]
        pc = c.execute("PRAGMA page_count").fetchone()[0] or 1
        if fr / pc < 0.25:
            c.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('vacuum_last',?)", (str(now_ts),))
            c.commit(); c.close(); return  # чисто — вакуумить нечего
        t0 = time.time()
        c.execute("VACUUM")
        _dlog(f"VACUUM done in {time.time()-t0:.1f}s (freelist {fr}/{pc} pages)")
        # Флаг только ПОСЛЕ успешного вакуума: крэш посреди → повтор на следующем старте
        c.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('vacuum_last',?)", (str(now_ts),))
        c.commit(); c.close()
    except Exception as e:
        try: c.close()
        except Exception: pass
        _dlog(f"VACUUM skipped: {e}")

def _graceful_shutdown():
    """Корректное завершение: остановить коллектор, сохранить данные."""
    _dlog("Shutdown: stopping collector...")
    _ct.set()
    global _tid, _proc_pool
    if _tid and _tid.is_alive():
        _tid.join(timeout=10)
    if _proc_pool is not None:
        _proc_pool.shutdown(wait=False)
        _proc_pool = None
    # Сохраняем avg-кэш и дампим DB если есть данные
    try:
        if _cache:
            _save_avg()
            _dump_db()
            _save_boe_variant_cache()
            _dlog("Shutdown: DB saved")
    except Exception as e:
        _dlog(f"Shutdown save error: {e}")
    _dlog("Shutdown: done")

def _run_server():
    """Запуск HTTP-сервера: waitress (production) или Flask (fallback)."""
    try:
        from waitress import serve
        log.info("Using waitress (production server)")
        serve(app, host=HOST, port=PORT, threads=8, channel_timeout=120)
    except ImportError:
        log.info("waitress not found, using Flask dev server")
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)

ACTIVATION_PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" type="image/png" sizes="32x32" href="/logos/favicon-32.png"><title>AH Sniper — Активация</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Segoe UI',system-ui,sans-serif;background:#0b0d13;color:#e9e7e2;height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#131726;border:1px solid rgba(232,185,35,.14);border-radius:16px;padding:48px;text-align:center;max-width:420px;width:90%}
.logo{margin-bottom:10px}.logo img{width:44px;height:44px;display:block;margin:0 auto}.title{font-size:20px;font-weight:700;color:#ffd75e;margin-bottom:6px}
.sub{font-size:13px;color:#8b8e99;margin-bottom:28px}
input{width:100%;padding:14px 18px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:#171c2e;color:#e9e7e2;font-size:16px;letter-spacing:2px;text-align:center;text-transform:uppercase;margin-bottom:14px}
input:focus{outline:none;border-color:#e8b923;box-shadow:0 0 0 3px rgba(232,185,35,.18)}
button{width:100%;padding:14px;border:none;border-radius:8px;background:linear-gradient(180deg,#ffd75e,#e8b923);color:#1a1205;font-size:15px;font-weight:800;cursor:pointer}
button:hover{filter:brightness(1.07)}button:disabled{opacity:.5;cursor:not-allowed}
.msg{margin-top:14px;font-size:12px;min-height:20px}.err{color:#f87171}.ok{color:#2fd575}
</style></head><body><div class="card">
<div class="logo"><img src="/logos/logo_nav_38.png" alt="AH Sniper"></div><div class="title">AH Sniper</div>
<div class="sub">Введите лицензионный ключ для активации</div>
<input id="key" placeholder="XXXXX-XXXXX-XXXXX-XXXXX" maxlength="23" autofocus>
<button id="btn" onclick="activate()">Активировать</button>
<div id="msg" class="msg"></div>
</div><script>
async function activate(){
var key=document.getElementById('key').value.trim();
if(!key){document.getElementById('msg').innerHTML='<span class="err">Введите ключ</span>';return;}
var btn=document.getElementById('btn');btn.disabled=true;btn.textContent='Проверка...';
try{
var r=await fetch('/api/license/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key})});
var d=await r.json();
if(d.valid){document.getElementById('msg').innerHTML='<span class="ok">✅ Активировано! Запуск...</span>';
setTimeout(function(){window.close();},1500);}
else{document.getElementById('msg').innerHTML='<span class="err">❌ '+(d.reason||'Ошибка')+'</span>';btn.disabled=false;btn.textContent='Активировать';}
}catch(e){document.getElementById('msg').innerHTML='<span class="err">Нет соединения</span>';btn.disabled=false;btn.textContent='Активировать';}
}
document.getElementById('key').addEventListener('keydown',function(e){if(e.key==='Enter')activate();});
</script></body></html>"""

@app.route("/gate")
def _gate():
    return ACTIVATION_PAGE

def _check_license_valid():
    """Проверить лицензию EXE. Только tier='pro' (lite не подходит)."""
    # The private collector must remain operable during Supabase outages.
    # Public user builds retain the PRO/HWID gate; web auth is independent.
    if ADMIN_BUILD:
        return True
    try:
        import license_manager
        result = license_manager.check_license()
        if not result.get("valid"):
            _dlog(f"License: invalid ({result.get('reason','?')})")
            return False
        # EXE принимает только pro (или ключи без tier = старые, считаем pro)
        local = license_manager._load_local()
        tier = (local or {}).get("tier", "pro")
        if tier != "pro":
            _dlog(f"License: tier={tier}, EXE requires pro")
            return False
        _dlog("License: valid PRO")
        return True
    except Exception as e:
        _dlog(f"License check error: {e}")
        return False

def main():
    import webview

    # ── License gate ──
    if not _check_license_valid():
        _dlog("License: not activated, showing gate")
        # Запускаем сервер для activation API
        t = threading.Thread(target=_run_server, daemon=True)
        t.start(); time.sleep(0.5)
        win = webview.create_window("AH Sniper — Активация", f"http://{HOST}:{PORT}/gate",
                                     width=460, height=520, resizable=False)
        # Ждём активацию (проверяем каждые 2с)
        webview.start()
        # После закрытия окна — проверяем ещё раз
        if not _check_license_valid():
            return

    # ── Full app ──
    _load_caches()
    _maybe_vacuum()  # синхронно ДО старта сервера/коллектора: не конкурирует с записью в БД
    threading.Thread(target=_enrich_worker, daemon=True, name="Enricher").start()
    log.info("Starting backend...")
    t = threading.Thread(target=_run_server, daemon=True)
    t.start(); time.sleep(0.5)
    log.info(f"Backend: http://{HOST}:{PORT}")
    w = _settings.get("win_w", 1280); h = _settings.get("win_h", 800)
    webview.create_window("AH Sniper", f"http://{HOST}:{PORT}", width=int(w), height=int(h), resizable=True, min_size=(900,500))
    atexit.register(_graceful_shutdown)
    webview.start()
    _graceful_shutdown()

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
