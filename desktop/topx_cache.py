#!/usr/bin/env python3
# topx_cache.py — кеш topXavg из price_history для быстрого старта
# Загружает предыдущие topXavg из БД при старте сбора
# v1.0

import sqlite3
from collections import defaultdict


def load_topx_cache(db_file: str, top_n: int = 10) -> dict:
    """
    Загрузить последний topXavg для каждого (item_id, ilvl) из auction_snapshots.

    Returns:
        {(item_id, ilvl): {
            "top10avg": int (copper),
            "realms_used": int (сколько реалмов вошло в top-N),
            "total_realms": int (сколько всего реалмов с этим предметом),
            "min_price": int,
            "sources": [(realm_id, min_buyout), ...]  # top-N список
        }}
    """
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row

    # Получаем последний collected_at
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'last_snapshot'"
    ).fetchone()
    if not row:
        conn.close()
        return {}

    last_time = row["value"]

    # Достаём все цены для последнего снимка
    rows = conn.execute("""
        SELECT item_id, ilvl, realm_id, min_buyout
        FROM auction_snapshots
        WHERE collected_at = ? AND min_buyout > 0
        ORDER BY item_id, ilvl, min_buyout
    """, (last_time,)).fetchall()
    conn.close()

    if not rows:
        return {}

    # Группируем по (item_id, ilvl), сортируем по цене
    groups = defaultdict(list)
    for r in rows:
        key = (r["item_id"], r["ilvl"] or 0)
        groups[key].append((r["realm_id"], r["min_buyout"]))

    cache = {}
    for key, prices in groups.items():
        prices.sort(key=lambda x: x[1])  # сортируем по цене
        top_prices = prices[:top_n]
        avg = sum(p[1] for p in top_prices) // len(top_prices) if top_prices else 0
        cache[key] = {
            "top10avg": avg,
            "realms_used": len(top_prices),
            "total_realms": len(prices),
            "min_price": top_prices[0][1] if top_prices else 0,
            "sources": top_prices,
        }

    return cache


def load_realm_names(db_file: str) -> dict:
    """Загрузить {realm_id: realm_name_ru_or_en}."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name_ru, name_en FROM realms"
    ).fetchall()
    conn.close()
    names = {}
    for r in rows:
        names[r["id"]] = r["name_ru"] if r["name_ru"] else r["name_en"]
    return names


def load_item_names(db_file: str) -> dict:
    """Загрузить {item_id: name_en}."""
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name_en FROM items").fetchall()
    conn.close()
    return {r["id"]: r["name_en"] for r in rows}


def snapshot_realm_items(db_file: str, collected_at: str,
                         realm_id: int, agg: dict):
    """
    Сохранить данные одного реалма в auction_snapshots сразу (для потоковой записи).
    Вызывается после обработки каждого реалма.
    """
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA synchronous=OFF")
    rows = []
    for (item_id, ilvl), v in agg.items():
        rows.append((collected_at, realm_id, item_id, ilvl or 0,
                     v["min_buyout"], v["count"], v["avg_price"]))
    conn.executemany("""
        INSERT INTO auction_snapshots (collected_at, realm_id, item_id, ilvl, min_buyout, quantity, avg_price)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()
