#!/usr/bin/env python3
"""
validate_items.py — Автоматическая валидация предметов в AH Sniper Browser API.
Проверяет 15k+ предметов на корректность отображения.

Запуск: python tests/validate_items.py [--url http://127.0.0.1:8765] [--full]
  --full: полная проверка (иконки, все страницы) — медленнее
"""

import sys
import os
import json
import time
import argparse
import requests
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

DEFAULT_URL = "http://127.0.0.1:8765"


def fetch_all_items(base_url, per_page=10000):
    """Загрузить все предметы из browser API."""
    items = []
    page = 1
    while True:
        r = requests.get(f"{base_url}/browser/api/items", params={
            "page": page, "per_page": per_page, "sort": "discount_desc"
        }, timeout=30)
        if r.status_code != 200:
            print(f"  ❌ HTTP {r.status_code} on page {page}")
            break
        data = r.json()
        batch = data.get("items", [])
        items.extend(batch)
        total = data.get("total", 0)
        print(f"  📦 Page {page}: {len(batch)} items (total: {total})")
        if len(items) >= total or not batch:
            break
        page += 1
    return items


def validate_items(items, base_url, full=False):
    """Валидация предметов. Возвращает отчёт."""
    report = {
        "total": len(items),
        "issues": defaultdict(list),
        "stats": {},
    }

    no_name = 0
    no_meta = 0
    no_price = 0
    bad_discount = 0
    no_quality = 0
    zero_avg = 0
    negative_discount = 0
    icon_errors = 0
    icons_checked = 0

    quality_dist = defaultdict(int)
    class_dist = defaultdict(int)

    for item in items:
        item_id = item.get("item_id", 0)
        ilvl = item.get("ilvl", 0)
        name = item.get("name", "")
        min_price = item.get("min_price", 0)
        avg_price = item.get("avg_price", 0)
        discount = item.get("discount", 0)
        quality = item.get("quality", "")
        class_name = item.get("class_name", "")

        # Проверка имени
        if not name or name.startswith("Item "):
            no_name += 1
            if no_name <= 20:
                report["issues"]["no_name"].append(f"{item_id} (ilvl={ilvl})")

        # Проверка метаданных
        if not class_name:
            no_meta += 1

        # Проверка качества
        if not quality:
            no_quality += 1
        else:
            quality_dist[quality] += 1

        if class_name:
            class_dist[class_name] += 1

        # Проверка цен
        if min_price <= 0:
            no_price += 1
            if no_price <= 10:
                report["issues"]["no_price"].append(f"{item_id} '{name}'")

        # Проверка discount
        if avg_price > 0 and min_price > 0:
            expected_disc = round((1 - min_price / avg_price) * 100, 1)
            if abs(discount - expected_disc) > 0.2:
                bad_discount += 1
                if bad_discount <= 10:
                    report["issues"]["bad_discount"].append(
                        f"{item_id}: got {discount}%, expected {expected_disc}%"
                    )

        if avg_price == 0 and min_price > 0:
            zero_avg += 1

        if discount < -50:
            negative_discount += 1
            if negative_discount <= 10:
                report["issues"]["extreme_negative_discount"].append(
                    f"{item_id} '{name}': {discount}%"
                )

    # Проверка иконок (выборочно)
    if full:
        import random
        sample = random.sample(items, min(200, len(items)))
        for item in sample:
            item_id = item.get("item_id", 0)
            try:
                r = requests.get(f"{base_url}/browser/icon/{item_id}", timeout=5)
                icons_checked += 1
                if r.status_code != 200:
                    icon_errors += 1
                    if icon_errors <= 10:
                        report["issues"]["icon_error"].append(f"{item_id}: HTTP {r.status_code}")
            except Exception as e:
                icon_errors += 1
                if icon_errors <= 5:
                    report["issues"]["icon_error"].append(f"{item_id}: {e}")

    report["stats"] = {
        "no_name": no_name,
        "no_meta": no_meta,
        "no_price": no_price,
        "bad_discount": bad_discount,
        "no_quality": no_quality,
        "zero_avg": zero_avg,
        "negative_discount": negative_discount,
        "icon_errors": icon_errors,
        "icons_checked": icons_checked,
        "quality_distribution": dict(quality_dist),
        "class_distribution": dict(sorted(class_dist.items(), key=lambda x: -x[1])[:15]),
    }

    return report


def check_categories(base_url):
    """Проверить дерево категорий."""
    r = requests.get(f"{base_url}/browser/api/categories", timeout=10)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    data = r.json()
    cats = data.get("categories", [])
    total_in_cats = sum(c.get("count", 0) for c in cats)
    return {
        "categories_count": len(cats),
        "total_items_in_categories": total_in_cats,
        "category_names": [c.get("name", "?") for c in cats],
    }


def check_snipe_list(base_url):
    """Проверить snipe list."""
    r = requests.get(f"{base_url}/browser/api/snipe/list", timeout=10)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    data = r.json()
    items = data.get("items", [])
    return {"snipe_count": len(items)}


def check_presets(base_url):
    """Проверить пресеты."""
    r = requests.get(f"{base_url}/api/presets", timeout=10)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    presets = r.json()
    return {
        "count": len(presets),
        "names": [p.get("name", "?") for p in presets],
        "default": [p.get("name") for p in presets if p.get("is_default")],
    }


def main():
    parser = argparse.ArgumentParser(description="AH Sniper Item Validator")
    parser.add_argument("--url", default=DEFAULT_URL, help="Base URL сервера")
    parser.add_argument("--full", action="store_true", help="Полная проверка (иконки)")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    print(f"{'='*60}")
    print(f"  AH Sniper — Валидация предметов")
    print(f"  URL: {base_url}")
    print(f"  Mode: {'FULL' if args.full else 'FAST'}")
    print(f"{'='*60}\n")

    # 1. Проверка доступности
    print("1️⃣  Проверка сервера...")
    try:
        r = requests.get(f"{base_url}/api/status", timeout=5)
        if r.status_code != 200:
            print(f"  ❌ Сервер недоступен (HTTP {r.status_code})")
            sys.exit(1)
        status = r.json()
        print(f"  ✅ Сервер работает. State: {status.get('state', '?')}")
    except Exception as e:
        print(f"  ❌ Сервер недоступен: {e}")
        print(f"  💡 Запустите: python desktop/main.py")
        sys.exit(1)

    # 2. Пресеты
    print("\n2️⃣  Пресеты...")
    presets = check_presets(base_url)
    print(f"  📋 {presets}")

    # 3. Категории
    print("\n3️⃣  Категории...")
    cats = check_categories(base_url)
    print(f"  📂 {cats.get('categories_count', 0)} категорий, {cats.get('total_items_in_categories', 0)} предметов")

    # 4. Snipe list
    print("\n4️⃣  Snipe list...")
    snipe = check_snipe_list(base_url)
    print(f"  ⭐ {snipe}")

    # 5. Загрузка всех предметов
    print("\n5️⃣  Загрузка предметов...")
    t0 = time.time()
    items = fetch_all_items(base_url)
    elapsed = time.time() - t0
    print(f"  ⏱ {len(items)} предметов за {elapsed:.1f}с")

    if not items:
        print("\n  ⚠️  Нет предметов! Запустите коллектор (Start) сначала.")
        sys.exit(0)

    # 6. Валидация
    print(f"\n6️⃣  Валидация {'(FULL)' if args.full else '(FAST)'}...")
    report = validate_items(items, base_url, full=args.full)

    # 7. Отчёт
    print(f"\n{'='*60}")
    print(f"  📊 ОТЧЁТ О ВАЛИДАЦИИ")
    print(f"{'='*60}")
    stats = report["stats"]
    print(f"\n  Всего предметов: {report['total']}")
    print(f"  Без имени:       {stats['no_name']}")
    print(f"  Без метаданных:  {stats['no_meta']}")
    print(f"  Без цены:        {stats['no_price']}")
    print(f"  Без качества:    {stats['no_quality']}")
    print(f"  Avg=0:           {stats['zero_avg']}")
    print(f"  Ошибки discount: {stats['bad_discount']}")
    print(f"  Экстрем. дисконт:{stats['negative_discount']}")
    if args.full:
        print(f"  Иконки проверено:{stats['icons_checked']}")
        print(f"  Ошибки иконок:   {stats['icon_errors']}")

    print(f"\n  📈 Распределение по качеству:")
    for q, cnt in sorted(stats.get("quality_distribution", {}).items(), key=lambda x: -x[1]):
        print(f"     {q}: {cnt}")

    print(f"\n  📈 Топ-15 категорий:")
    for cls, cnt in list(stats.get("class_distribution", {}).items())[:15]:
        print(f"     {cls}: {cnt}")

    # Issues detail
    if report["issues"]:
        print(f"\n  ⚠️  ДЕТАЛИ ПРОБЛЕМ:")
        for issue_type, items_list in report["issues"].items():
            print(f"\n  [{issue_type}] ({len(items_list)} шт.):")
            for item in items_list[:10]:
                print(f"    - {item}")
            if len(items_list) > 10:
                print(f"    ... и ещё {len(items_list)-10}")

    # Вердикт
    critical = stats["no_name"] + stats["bad_discount"] + stats["icon_errors"]
    if critical == 0:
        print(f"\n  ✅ ВСЁ ОТЛИЧНО! Критических проблем не найдено.")
    elif critical < 50:
        print(f"\n  ⚠️  Найдено {critical} критических проблем (см. выше)")
    else:
        print(f"\n  ❌ СЕРЬЁЗНЫЕ ПРОБЛЕМЫ: {critical} критических ошибок!")

    # Сохраняем отчёт в JSON
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "validation_report.json")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n  📄 Отчёт сохранён: {os.path.abspath(report_path)}")
    except:
        pass

    print()
    return 0 if critical == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
