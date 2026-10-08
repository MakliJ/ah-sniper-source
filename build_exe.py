#!/usr/bin/env python3
# build_exe.py — Сборка AH Sniper EXE (два режима)
#
#   python build_exe.py          → ADMIN сборка: AuctionMonitorAdmin.exe
#                                  Push в Supabase, web-auth (example.invalid), .env внутри EXE.
#                                  Только для машины админа, НЕ публикуется.
#
#   python build_exe.py --user   → USER сборка: AuctionMonitor.exe
#                                  Без push, без web-auth, без .env. PyArmor-обфускация.
#                                  Эта версия отдаётся пользователям (см. tools/prepare_release.py).
# v2.0

import glob
import os
import shutil
import subprocess
import sys

# ── Конфигурация ──
APP_NAME_ADMIN = "AuctionMonitorAdmin"
APP_NAME_USER = "AuctionMonitor"
MAIN_SCRIPT = os.path.join("desktop", "main.py")
ICON_FILE = ""

# Модули проекта, обфусцируемые в user-сборке (весь наш код, попадающий в EXE)
OBFUSCATE_SCRIPTS = [
    os.path.join("desktop", "main.py"),
    os.path.join("desktop", "ahgem.py"),
    os.path.join("desktop", "browser.py"),
    os.path.join("desktop", "topx_cache.py"),
    os.path.join("desktop", "ws_server.py"),
    "paths.py",
    "license_manager.py",
]
OBF_DIR = "_obf"
FLAGS_FILE = "_build_flags.py"

# Скрытые импорты (общие для обеих сборок)
HIDDEN_IMPORTS = [
    "flask", "jinja2", "markupsafe", "werkzeug", "click", "itsdangerous",
    "waitress", "waitress.server", "waitress.channel", "waitress.task",
    "waitress.threadpool", "waitress.wasyncore",
    "webview", "webview.platforms.winforms", "clr",
    "ahgem", "topx_cache", "ws_server", "browser",
    "requests", "urllib3", "dotenv", "email.utils",
    "sqlite3",
    "paths", "license_manager", "_build_flags",
    # websockets: импорт спрятан внутри ОБФУЦИРОВАННОГО ws_server.py —
    # статический анализ PyInstaller его не видит, нужен hidden-import
    "websockets",
    # httpx + HTTP/2 stack (для ускоренной загрузки аукциона; без них в EXE включится fallback на потоки)
    "httpx", "httpcore", "h2", "hpack", "hyperframe", "h11", "anyio", "sniffio", "certifi", "idna",
    # orjson — быстрый JSON-парсер (ускоряет обработку аукциона в ~3-5 раз)
    "orjson",
    # multiprocessing — для ProcessPoolExecutor (параллельная обработка реалмов)
    "multiprocessing",
]

# Исключения (не включать в EXE)
EXCLUDES = [
    "tkinter",
    "test",
    "unittest",
    "pydoc",
]


def write_build_flags(admin_build):
    """_build_flags.py читается main.py при старте (в .gitignore)."""
    with open(FLAGS_FILE, "w", encoding="utf-8") as f:
        f.write("# Сгенерирован build_exe.py — не редактировать (в .gitignore)\n")
        f.write(f"ADMIN_BUILD = {bool(admin_build)}\n")
    print(f"  🚩 {FLAGS_FILE}: ADMIN_BUILD={bool(admin_build)}")


def run_cmd(cmd):
    print(f"    $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("  ❌ Command FAILED:")
        print("\n".join(r.stdout.splitlines()[-25:]))
        print("\n".join(r.stderr.splitlines()[-25:]))
        raise RuntimeError(f"Command failed: {cmd[0]}")
    return r


def obfuscate():
    """PyArmor: обфускация модулей проекта в _obf/ (user-сборка).
    Возвращает (имя runtime-пакета или None, список необфусцированных файлов).

    Trial-ограничение PyArmor: скрипты крупнее ~44KB не обфусцируются
    (main.py = ~95KB). Они копируются в _obf как есть (Python 3.13 bytecode
    в PyInstaller — публичных декомпиляторов под 3.13 нет).
    После покупки лицензии PyArmor (pyarmor reg <файл-лицензии>) обфусцируется всё."""
    try:
        shutil.rmtree(OBF_DIR)
    except FileNotFoundError:
        pass
    scripts = [s for s in OBFUSCATE_SCRIPTS if os.path.exists(s)]
    if not scripts:
        raise RuntimeError("Нет скриптов для обфускации")

    obf, plain = [], []
    r = subprocess.run(["pyarmor", "gen", "-O", OBF_DIR] + scripts,
                       capture_output=True, text=True)
    if r.returncode == 0:
        obf = list(scripts)  # лицензия есть — всё обфусцировано
    else:
        # Trial: по одному; что не влезло в лимит — копируем как есть
        for s in scripts:
            rr = subprocess.run(["pyarmor", "gen", "-O", OBF_DIR, s],
                                capture_output=True, text=True)
            if rr.returncode == 0:
                obf.append(s)
            else:
                dst = os.path.join(OBF_DIR, s)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(s, dst)
                plain.append(s)

    # Флаг сборки — тоже в _obf (main.py его импортирует)
    shutil.copy2(FLAGS_FILE, os.path.join(OBF_DIR, FLAGS_FILE))

    rt = [p for p in glob.glob(os.path.join(OBF_DIR, "pyarmor_runtime_*")) if os.path.isdir(p)]
    runtime = os.path.basename(rt[0]) if rt else None
    print(f"  🔒 PyArmor: обфусцировано {len(obf)}/{len(scripts)} модулей"
          + (f", runtime={runtime}" if runtime else ""))
    for s in plain:
        print(f"  ⚠ {s} — без обфускации (trial-лимит размера; лечится лицензией PyArmor)")
    return runtime, plain


def pyinstaller(app_name, main_script, data_files, paths, extra_hidden=None, extra_args=None):
    cmd = [
        "pyinstaller",
        "--onefile",           # один EXE-файл
        "--noconfirm",         # перезаписать без подтверждения
        "--clean",             # очистить кеш
        "--name", app_name,
        "--windowed",          # без консоли (GUI)
    ]
    if ICON_FILE and os.path.exists(ICON_FILE):
        cmd.extend(["--icon", ICON_FILE])
    for imp in HIDDEN_IMPORTS + (extra_hidden or []):
        cmd.extend(["--hidden-import", imp])
    for exc in EXCLUDES:
        cmd.extend(["--exclude-module", exc])
    # Optimization — strip symbols (smaller EXE, harder to decompile)
    cmd.extend(["--strip"])
    for p in paths:
        cmd.extend(["--paths", p])
    for src, dst in data_files:
        if os.path.exists(src):
            cmd.extend(["--add-data", f"{src}{os.pathsep}{dst}"])
    cmd.extend(extra_args or [])
    cmd.append(main_script)
    run_cmd(cmd)


def build(admin=True):
    mode = "ADMIN" if admin else "USER"
    app_name = APP_NAME_ADMIN if admin else APP_NAME_USER
    print(f"{'='*60}")
    print(f"  Building {app_name}.exe  [{mode}]")
    print(f"{'='*60}\n")

    write_build_flags(admin)

    extra_hidden = []
    extra_args = []
    if admin:
        # Admin: исходники как есть, .env внутри (машина админа)
        main_script = MAIN_SCRIPT
        paths = ["desktop", "."]
        data_files = [("static", "static"), (".env", ".")]
    else:
        # User: обфусцированные исходники из _obf/, БЕЗ .env
        runtime, _plain = obfuscate()
        main_script = os.path.join(OBF_DIR, "desktop", "main.py")
        paths = [os.path.join(OBF_DIR, "desktop"), OBF_DIR]
        data_files = [("static", "static")]
        if runtime:
            extra_hidden = [runtime]
            extra_args = [f"--collect-all={runtime}"]

    pyinstaller(app_name, main_script, data_files, paths, extra_hidden, extra_args)

    print("\n  ✅ Build SUCCESS!\n")
    exe_path = os.path.join("dist", f"{app_name}.exe")
    exe_size = os.path.getsize(exe_path) / (1024 * 1024)
    print(f"  📦 {exe_path}")
    print(f"     Size: {exe_size:.1f} MB")

    if admin:
        # Admin EXE — в корень проекта (там же .env, settings.json, БД)
        root_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{app_name}.exe")
        try:
            shutil.copy2(exe_path, root_exe)
            print(f"  📋 Copied to: {root_exe}")
        except PermissionError:
            staged_exe = root_exe + ".next"
            shutil.copy2(exe_path, staged_exe)
            print(f"  ⚠ Running EXE is locked; staged replacement: {staged_exe}")
            print("     Stop AuctionMonitorAdmin.exe, then replace it with the .next file.")
    else:
        # User EXE — в release/ собирает tools/prepare_release.py
        print(f"  📋 Next: python tools/prepare_release.py")
    print()
    return True


def clean():
    """Очистить временные файлы сборки."""
    dirs_to_remove = ["build", "__pycache__", OBF_DIR]
    files_to_remove = [f"{APP_NAME_ADMIN}.spec", f"{APP_NAME_USER}.spec"]
    for d in dirs_to_remove:
        if os.path.exists(d):
            shutil.rmtree(d)
            print(f"  Removed {d}/")
    for f in files_to_remove:
        if os.path.exists(f):
            os.remove(f)
            print(f"  Removed {f}")


if __name__ == "__main__":
    # Переходим в директорию скрипта
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if "--clean" in sys.argv:
        clean()
        print("Clean done.")

    user_mode = "--user" in sys.argv
    try:
        success = build(admin=not user_mode)
    except RuntimeError as e:
        print(f"\n  ❌ {e}")
        sys.exit(1)

    if success:
        if user_mode:
            print("  📋 USER build: соберите релизную папку: python tools/prepare_release.py\n")
        else:
            print("  📋 ADMIN build: запустите AuctionMonitorAdmin.exe (start_all.bat уже на него ссылается)\n")

    sys.exit(0 if success else 1)
