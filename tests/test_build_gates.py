#!/usr/bin/env python3
# test_build_gates.py — проверка гейтов ADMIN_BUILD (разделение user/admin EXE)
# Запуск: python tests/test_build_gates.py
#
# Каждый режим прогоняется в отдельном subprocess:
# пишется _build_flags.py → импортируется desktop/main.py → Flask test client.
# После тестов _build_flags.py удаляется (запуск из исходников = admin по умолчанию).

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAGS_FILE = os.path.join(ROOT, "_build_flags.py")
WEB_HOST = "example.invalid"

CHECK_CODE = r'''
import json, os, sys
from flask.sessions import SecureCookieSessionInterface
mode = sys.argv[1]
# Флаг режима приходит через AH_BUILD_FLAGS (см. run_mode) — корневой
# _build_flags.py от последней сборки затенять его не может.
sys.path.insert(0, os.path.join(os.getcwd(), "desktop"))
import main

# Fixtures are independent of an operator's accounts, databases and .env.
import tempfile
fixture_dir = tempfile.TemporaryDirectory()
fixture_config = os.path.join(fixture_dir.name, 'supabase_config.json')
with open(fixture_config, 'w', encoding='utf-8') as f:
    json.dump({'supabase_url': 'https://cloud.example.invalid',
               'anon_key': 'public-fixture', 'service_key': 'admin-fixture-only'}, f)
original_data = main.data
main.data = lambda name: fixture_config if name == 'supabase_config.json' else original_data(name)
main._rnames[1] = 'Fixture realm'

app = main.app
app.config["TESTING"] = True
c = app.test_client()
results = []

def check(name, cond, extra=""):
    results.append([name, bool(cond), str(extra)[:120]])

is_admin = (mode == "admin")
check("flag ADMIN_BUILD", main.ADMIN_BUILD is is_admin, main.ADMIN_BUILD)

# ── localhost: работает в обоих режимах ──
r = c.get("/api/env")
env = r.get_json() if r.status_code == 200 else {}
check("localhost /api/env 200", r.status_code == 200, r.status_code)
check("env.admin корректен", env.get("admin") is is_admin, env.get("admin"))
check("env без supabase_url на localhost", "supabase_url" not in env)
check("localhost /app 200", c.get("/app").status_code == 200)
check("localhost /api/items 200", c.get("/api/items").status_code == 200)
check("localhost /gate 200", c.get("/gate").status_code == 200)
check("localhost /api/license/status 200", c.get("/api/license/status").status_code == 200)

# ── push в Supabase ──
if not is_admin:
    check("user: _push_to_supabase() = no-op", main._push_to_supabase() is None)
    check("user: _supabase_web() = None", main._supabase_web("GET", "web_users") is None)

# ── web-host (example.invalid) ──
H = {"Host": "%WEB_HOST%"}
if is_admin:
    expected_ym = os.getenv("YANDEX_METRIKA_ID", "").encode()
    landing = c.get("/", headers=H)
    check("admin: web landing indexable", landing.status_code == 200)
    check("admin: Yandex code injected into HTML",
          expected_ym in landing.data and b"mc.yandex.ru/metrika/tag.js" in landing.data and
          b"mc.yandex.ru/watch/" in landing.data)
    check("admin: web /landing canonical redirect",
          c.get("/landing", headers=H).status_code == 301)
    robots = c.get("/robots.txt", headers=H)
    check("admin: robots.txt public", robots.status_code == 200 and b"Sitemap:" in robots.data)
    sitemap = c.get("/sitemap.xml", headers=H)
    check("admin: sitemap.xml public", sitemap.status_code == 200 and b"https://example.invalid/" in sitemap.data)
    check("admin: manifest public", c.get("/site.webmanifest", headers=H).status_code == 200)
    analytics = c.get("/analytics.js", headers=H)
    check("admin: analytics config endpoint public", analytics.status_code == 200)
    check("admin: Yandex Metrika configured",
          bool(expected_ym) and expected_ym in analytics.data and b"webvisor:true" in analytics.data)
    check("admin: auth pages load analytics",
          b'/analytics.js' in c.get("/login", headers=H).data and
          b'/analytics.js' in c.get("/register", headers=H).data)
    check("admin: web /login 200", c.get("/login", headers=H).status_code == 200)
    r = c.get("/app", headers=H)
    check("admin: web /app без сессии → /login", r.status_code == 302 and "/login" in r.headers.get("Location", ""), r.status_code)
    check("admin: web /api/items 401", c.get("/api/items", headers=H).status_code == 401)
    r = c.get("/api/env", headers=H)
    envw = r.get_json() if r.status_code == 200 else {}
    check("admin: web /api/env отдаёт supabase_url", bool(envw.get("supabase_url")), list(envw.keys()))
    cookie = SecureCookieSessionInterface().get_signing_serializer(app).dumps(
        {"user_id": "test-user", "tier": "pro"})
    authed = app.test_client(use_cookies=False)
    AH = {"Host": "%WEB_HOST%", "Cookie": f"session={cookie}"}
    check("admin: web settings без секретов",
          authed.get("/api/settings", headers=AH).get_json() == {"region": main.REGION})
    check("admin: web collector start запрещён",
          authed.get("/api/start", headers=AH).status_code == 403)
    check("admin: web local license activate запрещён",
          authed.post("/api/license/activate", headers=AH, json={"key": "x"}).status_code == 403)
    check("admin: web global snipe mutation запрещена",
          authed.post("/browser/api/snipe/toggle", headers=AH, json={"item_id": 1}).status_code == 403)
    rr = authed.get("/browser/api/realms", headers=AH)
    check("admin: realm picker возвращает список",
          rr.status_code == 200 and len(rr.get_json().get("realms", [])) > 0)
    check("admin: web security headers",
          authed.get("/api/env", headers=AH).headers.get("X-Frame-Options") == "DENY")
    check("admin: private web routes noindex",
          authed.get("/app", headers=AH).headers.get("X-Robots-Tag", "").startswith("noindex"))

    # ── Новые security-гейты ──
    check("admin: неизвестный Host → 403 (анти DNS-rebinding)",
          c.get("/", headers={"Host": "evil.example.com"}).status_code == 403)
    check("admin: localhost остался разрешён",
          c.get("/api/status").status_code == 200)
    check("admin: чувствительные пути → 404 (не 302)",
          c.get("/supabase_config.json", headers=H).status_code == 404 and
          c.get("/.env", headers=H).status_code == 404)
    # Брутфорс-защита: подменяем Supabase (офлайн-детерминизм), долбим логином
    main._supabase_web = lambda *a, **k: []
    last_rl = None
    for i in range(12):
        last_rl = c.post("/login", headers=H, json={"email": f"rl{i}@test.io", "password": "x"})
    check("admin: login rate limit → 429 после серии попыток",
          last_rl.status_code == 429, last_rl.status_code)
    gz = authed.get("/api/env", headers={**AH, "Accept-Encoding": "gzip"})
    check("admin: gzip не ломает мелкие JSON-ответы", gz.status_code == 200, gz.status_code)
else:
    check("user: web /app 404", c.get("/app", headers=H).status_code == 404)
    check("user: web /login 404", c.get("/login", headers=H).status_code == 404)
    check("user: web /profile 404", c.get("/profile", headers=H).status_code == 404)
    check("user: web /api/items 404", c.get("/api/items", headers=H).status_code == 404)
    check("user: web / 404", c.get("/", headers=H).status_code == 404)

print(json.dumps(results))
'''


def run_mode(mode):
    # Флаги пишутся во временную папку (не в рабочее дерево) и передаются
    # subprocess'у через AH_BUILD_FLAGS — тест не мутирует репозиторий.
    import shutil
    import tempfile
    flags_dir = tempfile.mkdtemp(prefix="ah_flags_")
    flags_file = os.path.join(flags_dir, "_build_flags.py")
    try:
        with open(flags_file, "w", encoding="utf-8") as f:
            f.write(f"ADMIN_BUILD = {mode == 'admin'}\n")
        env = dict(os.environ)
        env["AH_BUILD_FLAGS"] = flags_file
        env["PUBLIC_ORIGIN"] = "https://" + WEB_HOST
        env["YANDEX_METRIKA_ID"] = "12345"  # Synthetic counter, never a real measurement ID.
        env["GA_MEASUREMENT_ID"] = ""
        code = CHECK_CODE.replace("%WEB_HOST%", WEB_HOST)
        r = subprocess.run([sys.executable, "-c", code, mode],
                           cwd=ROOT, capture_output=True, text=True, timeout=180,
                           env=env)
    finally:
        shutil.rmtree(flags_dir, ignore_errors=True)
    if r.returncode != 0:
        print(f"  ❌ [{mode}] subprocess упал:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
        return False
    try:
        results = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        print(f"  ❌ [{mode}] не удалось распарсить результат:\n{r.stdout[-1500:]}")
        return False
    ok = True
    for name, passed, extra in results:
        mark = "✅" if passed else "❌"
        if not passed:
            ok = False
        print(f"  {mark} [{mode}] {name}" + (f"  ({extra})" if extra and not passed else ""))
    return ok


def main():
    total_ok = True
    for mode in ("user", "admin"):
        print(f"\n── Режим: {mode.upper()} ──")
        if not run_mode(mode):
            total_ok = False
    print("\n" + ("✅ Все проверки пройдены" if total_ok else "❌ Есть проваленные проверки"))
    sys.exit(0 if total_ok else 1)


if __name__ == "__main__":
    main()
