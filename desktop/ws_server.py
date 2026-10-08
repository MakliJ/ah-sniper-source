#!/usr/bin/env python3
# ws_server.py — WebSocket broadcast server для AH-стриминга
# Запускается как import в ahgem.py или отдельно: python ws_server.py
# v1.0

import asyncio
import json
import threading
import time
import logging
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    # Опциональная зависимость: без неё WS-стриминг отключён, но коллектор работает.
    # (SystemExit здесь убивал коллектор: ahgem ловит только ImportError.)
    websockets = None
    print("WARNING: websockets not installed — WS broadcast disabled")

logging.basicConfig(level=logging.INFO, format="[WS] %(message)s")
log = logging.getLogger("ws_server")


class WSBroadcastServer:
    """
    WebSocket-сервер-ретранслятор.
    Принимает:
      - ahgem.py (издатель данных) — отправляет сообщения типа 'realm_data', 'collection_done'
      - GUI (подписчик) — только получает
    """

    def __init__(self, host="0.0.0.0", port=8766):
        self.host = host
        self.port = port
        self._clients = set()  # все подключённые websocket'ы
        self._server = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop_event = None

    # ── публичный API (вызывается из синхронного кода) ──

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, wait_ready=True):
        """Запустить сервер в фоновом потоке."""
        if websockets is None:
            return self
        if self.is_running:
            return self
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="WS-Server")
        self._thread.start()
        if wait_ready:
            self._ready.wait(timeout=5)
        return self

    def stop(self):
        if self._loop and hasattr(self, '_stop_event') and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        # Закрываем все клиентские соединения
        if self._loop:
            for ws in list(self._clients):
                asyncio.run_coroutine_threadsafe(ws.close(1001, "Server shutting down"), self._loop)
        self._thread = None

    def broadcast(self, message: dict):
        """Разослать JSON-сообщение всем подписчикам."""
        if not self._loop or not self._clients:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast_async(message), self._loop)

    # ── внутреннее (asyncio) ──

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            log.error(f"Server error: {e}")
        finally:
            self._loop.close()

    async def _serve(self):
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=60,
            ping_timeout=30,
        )
        self._ready.set()
        log.info(f"Listening on ws://{self.host}:{self.port}")
        # Wait forever unless stopped
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    async def _handler(self, websocket):
        self._clients.add(websocket)
        remote = websocket.remote_address
        log.info(f"Client connected: {remote}  ({len(self._clients)} total)")
        try:
            # Просто держим соединение открытым
            async for _ in websocket:
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)
            log.info(f"Client disconnected: {remote}  ({len(self._clients)} total)")

    async def _broadcast_async(self, message: dict):
        if not self._clients:
            return
        payload = json.dumps(message, ensure_ascii=False, default=str)
        dead = set()
        for ws in self._clients:
            try:
                await ws.send(payload)
            except websockets.exceptions.ConnectionClosed:
                dead.add(ws)
        self._clients -= dead


# ── глобальный singleton для удобного импорта в ahgem.py ──
_global_server: WSBroadcastServer | None = None


def get_ws_server() -> WSBroadcastServer:
    global _global_server
    if _global_server is None:
        _global_server = WSBroadcastServer()
    return _global_server


def ensure_ws_server(host="0.0.0.0", port=8766) -> WSBroadcastServer:
    """Вернуть запущенный экземпляр (старт если ещё нет)."""
    srv = get_ws_server()
    if not srv.is_running:
        srv.host = host
        srv.port = port
        srv.start()
    return srv


# ── вспомогательные форматеры ──

def make_status_msg(region: str, progress: float, done: int, total: int, msg: str) -> dict:
    return {
        "type": "status",
        "region": region,
        "progress": round(progress, 3),
        "done": done,
        "total": total,
        "msg": msg,
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


def make_realm_msg(region: str, realm_id: int, realm_name: str,
                   agg: dict, collected_at: str) -> dict:
    """
    agg = {(item_id, ilvl): {min_buyout, total, count, avg_price}}
    """
    items = []
    for (item_id, ilvl), v in agg.items():
        items.append({
            "item_id": item_id,
            "ilvl": ilvl,
            "min_buyout": v["min_buyout"],
            "quantity": v["count"],
            "avg_price": v["avg_price"],
        })
    return {
        "type": "realm_data",
        "region": region,
        "realm_id": realm_id,
        "realm_name": realm_name,
        "items": items,
        "count": len(items),
        "collected_at": collected_at,
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


def make_collection_done(region: str, collected_at: str,
                         total_items: int, total_realms: int,
                         duration: float) -> dict:
    return {
        "type": "collection_done",
        "region": region,
        "collected_at": collected_at,
        "total_items": total_items,
        "total_realms": total_realms,
        "duration_sec": round(duration, 1),
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


def make_top10_update(region: str, item_id: int, ilvl: int,
                      top10avg: int, discount: float) -> dict:
    return {
        "type": "top10_update",
        "region": region,
        "item_id": item_id,
        "ilvl": ilvl,
        "top10avg": top10avg,
        "discount": round(discount, 1),
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


def make_monitor_status(region: str, status: str, blizz_time: str,
                        check_ids: list, elapsed: float) -> dict:
    return {
        "type": "monitor_status",
        "region": region,
        "status": status,
        "blizz_time": blizz_time,
        "check_ids": check_ids,
        "elapsed_sec": round(elapsed, 1),
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


# ── точка входа для самостоятельного запуска ──
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AH WebSocket Broadcast Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    print(f"Starting WS Broadcast Server on ws://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    server = WSBroadcastServer(host=args.host, port=args.port)
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.stop()
