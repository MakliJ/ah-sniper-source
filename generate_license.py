#!/usr/bin/env python3
# generate_license.py — Генерация лицензионных ключей через Supabase
# Использование:
#   python generate_license.py --days 30                # ключ на 30 дней
#   python generate_license.py --permanent              # пожизненный ключ
#   python generate_license.py --days 7 --count 5       # 5 ключей по 7 дней
#   python generate_license.py --promo --max 300 --days 3  # промо на 300 активаций/3 дня
# v2.1 (Supabase)

import os
import sys
import json
import hashlib
import secrets
import string
from datetime import datetime, timedelta, timezone

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase_config.json")


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def _generate_plain_key() -> str:
    """Сгенерировать читаемый ключ формата XXXXX-XXXXX-XXXXX-XXXXX"""
    chars = string.ascii_uppercase + string.digits
    groups = []
    for _ in range(4):
        group = ''.join(secrets.choice(chars) for _ in range(5))
        groups.append(group)
    return '-'.join(groups)


def _supabase_insert(supabase_url, service_key, table, data):
    """Вставить запись в Supabase через REST API (с service key)."""
    import requests
    url = f"{supabase_url}/rest/v1/{table}"
    headers = {
        "Content-Type": "application/json",
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Prefer": "return=representation",
    }
    r = requests.post(url, json=data, headers=headers, timeout=15)
    if r.status_code in (200, 201):
        return r.json()
    print(f"  ❌ Supabase error ({r.status_code}): {r.text[:300]}")
    return None


def generate_key(supabase_url, service_key, days=0, permanent=False, key_type="single", max_activations=1, tier="pro"):
    """Сгенерировать ключ и сохранить в Supabase."""
    plain_key = _generate_plain_key()
    key_hash = hashlib.sha256(plain_key.upper().strip().encode()).hexdigest()

    if permanent:
        expires_at = None
    else:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    data = {
        "key_hash": key_hash,
        "key_type": key_type,
        "max_activations": max_activations,
        "activations_count": 0,
        "expires_at": expires_at,
        "tier": tier,
    }

    result = _supabase_insert(supabase_url, service_key, "licenses", data)

    if result:
        exp_str = "бессрочно" if permanent else f"{days} дней (до {expires_at[:10]})"
        print(f"  ✅ {plain_key}  |  {exp_str}  |  активаций: {max_activations}")
        return plain_key, key_hash
    else:
        print(f"  ❌ Не удалось сохранить ключ")
        return None, None


def main():
    cfg = load_config()
    supabase_url = cfg.get("supabase_url", "")
    service_key = cfg.get("service_key", "")

    if not supabase_url or not service_key:
        print("❌ Supabase не настроен!")
        print(f"   Создай файл {CONFIG_FILE} с supabase_url и service_key")
        sys.exit(1)

    # Парсим аргументы
    days = 0
    permanent = "--permanent" in sys.argv
    key_type = "single"
    max_activations = 1
    count = 1

    if "--promo" in sys.argv:
        key_type = "promo"
        max_activations = 300
        days = 3

    tier = "pro"

    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
        if arg == "--max" and i + 1 < len(sys.argv):
            max_activations = int(sys.argv[i + 1])
        if arg == "--count" and i + 1 < len(sys.argv):
            count = int(sys.argv[i + 1])
        if arg == "--tier" and i + 1 < len(sys.argv):
            tier = sys.argv[i + 1].lower()
            if tier not in ("lite", "pro"):
                print("❌ --tier должен быть 'lite' или 'pro'")
                sys.exit(1)

    if not days and not permanent:
        print("Использование:")
        print("  python generate_license.py --days 30              # PRO ключ на 30 дней")
        print("  python generate_license.py --permanent            # PRO пожизненный")
        print("  python generate_license.py --tier lite --days 30  # LITE ключ (веб)")
        print("  python generate_license.py --days 7 --count 5     # 5 ключей по 7 дней")
        print("  python generate_license.py --promo --max 300 --days 3  # промо")
        sys.exit(1)

    exp_str = "бессрочно" if permanent else f"{days} дней"
    print(f"\n{'='*55}")
    print(f"  🏆 AH Sniper — Генерация ключей")
    print(f"  Тип: {key_type} | Tier: {tier.upper()} | Срок: {exp_str} | Кол-во: {count}")
    print(f"{'='*55}\n")

    generated = []
    for i in range(count):
        key, _ = generate_key(supabase_url, service_key, days, permanent, key_type, max_activations, tier)
        if key:
            generated.append(key)

    print(f"\n{'='*55}")
    print(f"  Сгенерировано: {len(generated)}/{count}")
    if generated:
        print(f"\n  Ключи для раздачи:")
        for k in generated:
            print(f"    {k}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
