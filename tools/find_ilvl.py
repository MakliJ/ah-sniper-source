#!/usr/bin/env python3
# find_ilvl.py — анализ BoE предметов: bonus_lists → ilvl
# Берёт аукционы с одного реалма, группирует по bonus_lists,
# показывает цены чтобы вычислить ilvl эмпирически.

import os
import requests
import urllib3
from collections import defaultdict
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

# ====== НАСТРОЙКИ (меняй здесь) ======
ITEM_IDS = [260377, 260370, 260374, 260371, 260373, 260372, 260376, 260375]
REALM_ID = 3391                        # Connected Realm ID
REGION = "eu"
LOCALE = "ru_RU"
# =====================================

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
if not CLIENT_ID or not CLIENT_SECRET:
    raise ValueError("CLIENT_ID и CLIENT_SECRET должны быть в .env")

AUTH_URL = "https://oauth.battle.net/token"
API_HOST = f"https://{REGION}.api.blizzard.com"
DYNAMIC_NS = f"dynamic-{REGION}"
STATIC_NS = f"static-{REGION}"

# Эталонные маппинги (проверены через find_ilvl + сверка в игре)
KNOWN_ILVL = {
    (6652, 13577, 13332, 12780, 10844): 243,
    (6652, 13577, 13332, 12779, 10844): 240,
    (6652, 13577, 13333, 12788, 10844): 256,
    (6652, 13577, 13333, 12787, 10844): 253,
    (6652, 13577, 13334, 12796, 10844): 269,
    (6652, 13577, 13334, 12795, 10844): 266,
    (6652, 13577, 13335, 12803, 10844): 279,
    (6652, 13577, 13335, 12804, 10844): 282,
}

ESTIMATED_BY_PRICE = {
    (13332, 12780): 243,
    (13332, 12779): 240,
    (13333, 12788): 256,
    (13333, 12787): 253,
    (13334, 12796): 269,
    (13334, 12795): 266,
    (13335, 12803): 279,
    (13335, 12804): 282,
    (13332, 12825): 279,
    (13333, 12833): 292,
    (13333, 12834): 295,
    (13333, 12835): 298,
    (13334, 12841): 305,
    (13334, 12842): 308,
    (13334, 12843): 311,
    (13334, 12844): 315,
    (13335, 12849): 318,
    (13335, 12850): 321,
    (13335, 12851): 324,
}

def ilvl_signature(bonus_tuple):
    """Извлекает ilvl-значимые бонусы из кортежа."""
    sig = []
    for b in bonus_tuple:
        if 12700 <= b <= 12900 or 13300 <= b <= 13400:
            sig.append(b)
    return tuple(sig)

SIG_TO_ILVL = {}
for bt, ilvl in KNOWN_ILVL.items():
    sig = ilvl_signature(bt)
    SIG_TO_ILVL[sig] = ilvl


def get_token():
    resp = requests.post(AUTH_URL,
                         auth=(CLIENT_ID, CLIENT_SECRET),
                         data={"grant_type": "client_credentials"}, verify=False)
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_item_meta(token, item_id):
    """Метаданные предмета из Blizzard API."""
    url = f"{API_HOST}/data/wow/item/{item_id}"
    params = {"namespace": STATIC_NS, "locale": LOCALE}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, verify=False)
    if resp.status_code != 200:
        return None
    data = resp.json()

    def _s(val):
        if isinstance(val, dict):
            return val.get("ru_RU") or val.get("en_US") or str(val)
        return str(val) if val else None

    return {
        'id': item_id,
        'name': _s(data.get('name')),
        'class': _s((data.get('item_class') or {}).get('name')),
        'subclass': _s((data.get('item_subclass') or {}).get('name')),
        'slot': _s((data.get('inventory_type') or {}).get('name')),
        'quality': _s((data.get('quality') or {}).get('name')),
        'level': data.get('level'),
        'required_level': data.get('required_level'),
    }


def get_auctions(token, realm_id):
    """Все аукционы с одного connected realm."""
    url = f"{API_HOST}/data/wow/connected-realm/{realm_id}/auctions"
    params = {"namespace": DYNAMIC_NS, "locale": LOCALE}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, verify=False)
    resp.raise_for_status()
    return resp.json()["auctions"]


def format_g(c):
    """Медь → золото."""
    return f"{c / 10000:.2f}g"


def main():
    print("=" * 70)
    print("find_ilvl.py — анализ bonus_lists по ценам аукциона")
    print(f"Предметы: {ITEM_IDS}  |  Реалм: {REALM_ID}")
    print("=" * 70)

    token = get_token()

    # 1. Метаданные предметов
    print("\n── Метаданные предметов ──")
    item_meta = {}
    for iid in ITEM_IDS:
        meta = get_item_meta(token, iid)
        item_meta[iid] = meta
        if meta:
            print(f"  [{iid}] {meta['name']} | {meta['class']} / {meta['subclass']} | {meta['slot']} | {meta['quality']} | баз. ilvl={meta['level']}, req={meta['required_level']}")
        else:
            print(f"  [{iid}] ⚠ не найден в API (404)")

    # 2. Аукционы
    print(f"\n── Аукционы (реалм {REALM_ID}) ──")
    all_auctions = get_auctions(token, REALM_ID)
    print(f"  Всего лотов на реалме: {len(all_auctions)}")

    target = [a for a in all_auctions if a["item"]["id"] in ITEM_IDS]
    print(f"  Из них наших предметов: {len(target)}")
    if not target:
        print("  ⚠ Нет лотов с указанными ID. Выход.")
        return

    # 3. Построчный список ВСЕХ лотов с предполагаемым ilvl
    print(f"\n── Построчный список лотов (цена → предполагаемый ilvl) ──")
    for iid in ITEM_IDS:
        item_auctions = [a for a in target if a["item"]["id"] == iid]
        if not item_auctions:
            continue
        name = (item_meta.get(iid) or {}).get('name', f'Item {iid}')
        print(f"\n  [{iid}] {name} ({len(item_auctions)} лотов):")

        # Сортируем по цене
        lots = []
        for a in item_auctions:
            buyout = a.get("buyout") or a.get("unit_price") or 0
            if buyout == 0:
                continue
            bonus_tuple = tuple(a["item"].get("bonus_lists", []))
            ilvl = KNOWN_ILVL.get(bonus_tuple)
            if ilvl is None:
                sig = ilvl_signature(bonus_tuple)
                ilvl = SIG_TO_ILVL.get(sig) or ESTIMATED_BY_PRICE.get(sig)
            lots.append((buyout, ilvl, bonus_tuple))

        lots.sort(key=lambda x: x[0])
        for n, (buyout, ilvl, bt) in enumerate(lots, 1):
            ilvl_str = str(ilvl) if ilvl else "???"
            sig = list(ilvl_signature(bt))
            print(f"    {n:>2d}. {format_g(buyout):>10s}  →  ilvl={ilvl_str:>3s}  |  {name[:50]}  |  sig={sig}")

    # 4. Сводка: что осталось без ilvl
    unknown = []
    for a in target:
        bt = tuple(a["item"].get("bonus_lists", []))
        ilvl = KNOWN_ILVL.get(bt)
        if ilvl is None:
            sig = ilvl_signature(bt)
            ilvl = SIG_TO_ILVL.get(sig) or ESTIMATED_BY_PRICE.get(sig)
        if ilvl is None:
            unknown.append(bt)

    if unknown:
        unique_unknown = sorted(set(unknown))
        print(f"\n── Совсем неизвестные bonus_lists ({len(unique_unknown)} шт.) ──")
        print("  (сигнатура не найдена в ESTIMATED_BY_PRICE — нужно выяснить вручную)")
        for bt in unique_unknown:
            print(f"    sig={list(ilvl_signature(bt))}  bonus={list(bt)}")
    else:
        print(f"\n── Все ilvl определены (точно или по оценке) ✓ ──")

    # 5. Готовый словарь для копирования
    print(f"\n── Готовый BONUS_TO_ILVL для копипасты ──")
    all_bonuses = set()
    for a in target:
        bt = tuple(a["item"].get("bonus_lists", []))
        ilvl = KNOWN_ILVL.get(bt)
        if ilvl is None:
            sig = ilvl_signature(bt)
            ilvl = SIG_TO_ILVL.get(sig) or ESTIMATED_BY_PRICE.get(sig)
        if ilvl:
            all_bonuses.add((bt, ilvl))

    for bt, ilvl in sorted(all_bonuses, key=lambda x: x[1]):
        print(f"    {tuple(bt)}: {ilvl},")

    print("\n" + "=" * 70)
    print("Готово.")


if __name__ == "__main__":
    main()
