#!/usr/bin/env python3
"""Замер скорости асинхронной версии ahgem.py"""
import asyncio
import time
import sys
sys.path.insert(0, '.')
import importlib
spec = importlib.util.spec_from_file_location('ahgem', 'ahgem.py')
ahgem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ahgem)

async def test():
    cfg = ahgem.REGION_CONFIGS['eu']
    ahgem.init_db(cfg)
    token = ahgem.get_access_token(cfg)

    # Убедимся что реалмы есть
    if not ahgem.get_realm_ids(cfg):
        ahgem.rebuild_realms(token, cfg)

    t0 = time.time()
    await ahgem.collect_and_store_async(token, cfg)
    elapsed = time.time() - t0
    print(f">>> TOTAL ASYNC: {elapsed:.1f}s", flush=True)

if __name__ == '__main__':
    asyncio.run(test())
