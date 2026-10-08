#!/usr/bin/env python3
# paths.py — единый резолвер путей для всего проекта
# Модули desktop/, tools/ и tests/ импортят этот файл,
# чтобы найти общие данные (БД, settings.json, .env, icons/ и т.д.)
# в КОРНЕ проекта, независимо от того, в какой они подпапке.

import os
import sys


def root() -> str:
    """
    Корень проекта — где лежат БД, .env, settings.json и т.д.
    В скрипте: папка, где лежит сам paths.py (т.е. корень).
    В EXE: папка, где лежит .exe (там же и data-файлы).
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def data(relative_path: str) -> str:
    """
    Абсолютный путь к data-файлу в корне проекта.
    Пример: data("settings.json") → C:/.../wow auc/settings.json
    """
    return os.path.join(root(), relative_path)


def static_dir() -> str:
    """
    Папка static/ для Flask.
    В EXE: внутри _MEIPASS (запакована PyInstaller).
    В скрипте: в корне проекта.
    """
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, "static")  # type: ignore
    return os.path.join(root(), "static")
