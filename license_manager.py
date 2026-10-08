#!/usr/bin/env python3
# license_manager.py — Лицензирование через Supabase REST API
# v2.0 (Supabase)

import os
import json
import hashlib
import hmac
import time
import base64
from urllib.parse import urlsplit
from datetime import datetime, timezone, timedelta
from typing import Optional

from paths import data

LICENSE_FILE = data("license.json")

# ═══════════════════════════════════════════════════════════
# Supabase (AH Sniper License)
def _load_public_config():
    """Only public licensing settings; never read the admin configuration."""
    cfg = {}
    path = data("public_config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict) or set(cfg) - {"supabase_url", "anon_key"}:
            raise ValueError("public_config.json may contain only supabase_url and anon_key")
    url = (os.getenv("SUPABASE_URL") or cfg.get("supabase_url", "")).strip().rstrip("/")
    key = (os.getenv("SUPABASE_ANON_KEY") or cfg.get("anon_key", "")).strip()
    if url:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("SUPABASE_URL must be an HTTPS origin without credentials")
    if key.startswith("sb_secret_"):
        raise ValueError("A privileged Supabase key cannot be used as an anon key")
    parts = key.split(".")
    if len(parts) == 3:
        try:
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("role") == "service_role":
            raise ValueError("A service_role key cannot be used as an anon key")
    return url, key


SUPABASE_URL, SUPABASE_ANON_KEY = _load_public_config()
# ═══════════════════════════════════════════════════════════


def _get_hwid() -> str:
    """Получить HWID (хэш железа) для привязки ключа.
    Использует стабильные идентификаторы (не зависят от console/windowed режима)."""
    parts = []
    try:
        import subprocess
        # UUID материнской платы (стабилен, не зависит от локали)
        CREATE_NO_WINDOW = 0x08000000
        r = subprocess.run(["wmic", "csproduct", "get", "UUID"], capture_output=True, text=True, timeout=5, creationflags=CREATE_NO_WINDOW)
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if line and line != "UUID":
                parts.append(line)
                break
    except:
        pass
    if not parts:
        try:
            import subprocess
            r = subprocess.run(["cmd", "/c", "vol", "C:"], capture_output=True, text=True, timeout=5, creationflags=0x08000000)
            for line in r.stdout.split("\n"):
                if "Serial" in line or "серийный" in line.lower():
                    parts.append(line.strip())
        except:
            parts.append("novol")
    try:
        import uuid
        parts.append(hex(uuid.getnode()))
    except:
        parts.append("nomac")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _supabase_request(rpc_name: str, payload: dict) -> Optional[dict]:
    """Вызвать Supabase RPC функцию."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return {"valid": False, "reason": "Supabase not configured"}
    import requests
    try:
        url = f"{SUPABASE_URL}/rest/v1/rpc/{rpc_name}"
        headers = {
            "Content-Type": "application/json",
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        }
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        return {"valid": False, "reason": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"valid": False, "reason": f"Network error: {e}"}


def activate_key(key: str) -> dict:
    """
    Активировать ключ через Supabase.
    Поддерживает суммирование: новый ключ добавляет время к оставшемуся.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return {"valid": False, "reason": "Supabase not configured"}

    key_hash = hashlib.sha256(key.strip().upper().encode()).hexdigest()
    hwid = _get_hwid()

    # Считаем оставшееся время текущей лицензии (для суммирования)
    remaining_seconds = 0
    local = _load_local()
    if local and local.get("hwid") == hwid:
        old_exp = local.get("expires_at", "")
        if old_exp == "forever":
            remaining_seconds = -1  # бессрочно
        elif old_exp:
            try:
                old_dt = datetime.fromisoformat(old_exp)
                diff = (old_dt - datetime.now(timezone.utc)).total_seconds()
                if diff > 0:
                    remaining_seconds = diff
            except:
                pass

    result = _supabase_request("activate_key", {
        "p_key_hash": key_hash,
        "p_hwid": hwid,
    })

    if result and result.get("valid"):
        new_exp = result.get("expires_at", "forever")

        # Суммирование: если была активная лицензия + новый ключ не бессрочный
        effective_exp = new_exp
        if remaining_seconds == -1:
            effective_exp = "forever"
        elif remaining_seconds > 0 and new_exp != "forever":
            try:
                new_dt = datetime.fromisoformat(new_exp)
                # Добавляем оставшееся время к новому ключу
                effective_dt = new_dt + timedelta(seconds=remaining_seconds)
                effective_exp = effective_dt.isoformat()
            except:
                pass

        try:
            with open(LICENSE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "key_hash": key_hash,
                    "hwid": hwid,
                    "activated_at": datetime.now(timezone.utc).isoformat(),
                    "last_check": time.time(),
                    "expires_at": effective_exp,
                    "key_type": result.get("key_type", "single"),
                    "tier": result.get("tier", "pro"),
                }, f, indent=2)
        except:
            pass
        return {"valid": True, "expires_at": effective_exp, "tier": result.get("tier", "pro")}

    return {"valid": False, "reason": (result or {}).get("reason", "Unknown error")}


def check_license() -> dict:
    """
    Проверить текущую лицензию.
    Сначала локально (быстро), потом фоновая сверка с Supabase.
    """
    local = _load_local()
    if not local:
        return {"valid": False, "tier": "free", "reason": "No license file"}

    key_hash = local.get("key_hash", "")
    hwid = local.get("hwid", "")
    expires_at = local.get("expires_at", "forever")
    last_check = local.get("last_check", 0)

    # Проверяем локальный HWID
    if hwid != _get_hwid():
        return {"valid": False, "tier": "free", "reason": "HWID mismatch (different PC?)"}

    # Проверяем срок локально
    if expires_at and expires_at != "forever":
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp < datetime.now(timezone.utc):
                return {"valid": False, "tier": "free", "reason": "License expired"}
        except:
            pass

    # Если прошло больше часа — сверяемся с Supabase
    if time.time() - last_check > 3600 and SUPABASE_URL and SUPABASE_ANON_KEY:
        result = _supabase_request("check_key", {
            "p_key_hash": key_hash,
            "p_hwid": hwid,
        })
        if result and result.get("valid"):
            # Обновляем локальный кэш
            local["last_check"] = time.time()
            try:
                with open(LICENSE_FILE, "w", encoding="utf-8") as f:
                    json.dump(local, f, indent=2)
            except:
                pass
            return {"valid": True, "tier": "premium", "expires_at": result.get("expires_at", "forever")}
        elif result and not result.get("valid"):
            # Сервер сказал что ключ невалиден
            return {"valid": False, "tier": "free", "reason": result.get("reason", "Server rejected")}
        # Если Supabase недоступен — работаем по кэшу (grace period)

    return {"valid": True, "tier": "premium", "expires_at": expires_at}


def _load_local() -> Optional[dict]:
    if not os.path.exists(LICENSE_FILE):
        return None
    try:
        with open(LICENSE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None


def is_premium() -> bool:
    """Быстрая проверка — премиум или нет."""
    result = check_license()
    return result.get("valid", False)


def status_text() -> str:
    """Текст статуса для UI."""
    result = check_license()
    if result["valid"]:
        exp = result.get("expires_at", "forever")
        if exp == "forever":
            return "🟢 Premium (permanent)"
        return f"🟢 Premium (until {exp})"
    return f"🔴 Free ({result.get('reason', 'no license')})"
