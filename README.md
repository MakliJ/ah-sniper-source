# AH Sniper

Приложение для мониторинга аукциона World of Warcraft: поиск лотов со скидкой, сравнение цен между реалмами и фильтрация BoE по характеристикам конкретного лота.

В проекте есть Windows-приложение, Flask-сервер, веб-интерфейс и инструменты подготовки пользовательского релиза. Коллектор работает с официальным Blizzard API, локально хранит данные в SQLite и может передавать снимки аукциона в Supabase.

Этот публичный репозиторий содержит актуальные исходники. Реальные адреса сервиса, ключи, аккаунты, пользовательские пресеты и рабочая БД в него не входят. История начинается с очищенного снимка: старые коммиты и резервные копии сюда не перенесены.

## Что умеет

- Искать предметы по заданному списку ID, минимальной скидке и средней цене.
- Отдельно искать BoE по ilvl, вторичным статам, сокетам и дополнительным эффектам.
- Показывать каталог предметов и карточку с ценами по реалмам.
- Сохранять пресеты и локальные черновики; повторять сохранение после восстановления облака.
- Собирать аукционы EU/US и обслуживать веб-аккаунты с отдельными пресетами.
- Собирать две версии EXE: админскую для владельца сервиса и пользовательскую для раздачи.

`Snipe items` и `BoE Snipe` включаются независимо. Пустой список ID не означает поиск всех предметов. Цена и характеристики BoE всегда относятся к одному варианту лота; неизвестные характеристики не подставляются по догадке.

## Быстрый запуск из исходников

Основной сценарий — Windows, Python 3.13 и PowerShell. Для JavaScript-тестов нужен Node.js 24. Для окна приложения нужен Microsoft Edge WebView2 Runtime.

```powershell
git clone https://github.com/MakliJ/ah-sniper-source.git
cd ah-sniper-source
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполните в `.env` свои `CLIENT_ID` и `CLIENT_SECRET`, полученные в [Blizzard Developer Portal](https://develop.battle.net/). Для отдельного US-коллектора предусмотрены `CLIENT_ID_US` и `CLIENT_SECRET_US`.

```powershell
.\.venv\Scripts\python.exe desktop/main.py
```

Откроется окно AH Sniper. Локальный сервер работает на `http://127.0.0.1:8765`; `/landing` показывает предпросмотр лендинга. Запуск из исходников по умолчанию использует админский режим. Если ранее собирали USER-версию, удалите только сгенерированный `_build_flags.py`, чтобы вернуть этот режим.

На чистом клоне нет истории цен, аккаунтов и каталога иконок. Настройте Blizzard API и запустите сбор через приложение: локальная БД заполняется текущими данными. В рабочей копии можно отдельно обновить названия и иконки:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-tools.txt
.\.venv\Scripts\python.exe tools/update_items_patch.py
.\.venv\Scripts\python.exe tools/repair_item_icons.py --check
```

Первая команда обращается к Blizzard и обновляет локальные данные. Вторая только проверяет каталог; варианты дозагрузки описаны в `python tools/repair_item_icons.py --help`. Для восстановления своего окружения используйте отдельные локальные копии данных, а не коммиты в этот репозиторий.

## Настройки и секреты

| Файл | Для чего нужен | Можно коммитить |
| --- | --- | --- |
| `.env.example` | Пустой пример переменных | Да |
| `.env` | Blizzard-ключи, параметры своего сайта, необязательная аналитика | Нет |
| `supabase_config.example.json` | Пустой пример админской конфигурации | Да |
| `supabase_config.json` | URL, anon key и привилегированный `service_key` | Нет |
| `public_config.example.json` | Пустой пример настроек пользовательской лицензии | Да |
| `public_config.json` | Только URL и публичный anon key своего Supabase | Нет, создаётся локально |
| `tunnel_token.txt` | Токен своего Cloudflare Tunnel | Нет |
| `settings.json`, `collector_settings.json`, `license.json`, `presets.json` | Настройки, лицензия и пресеты конкретной установки | Нет |

Не переносите реальные значения в Python, HTML, README или примеры конфигурации. ID аналитики задаются только через `GA_MEASUREMENT_ID` и `YANDEX_METRIKA_ID`; с пустыми значениями счётчики выключены.

## Веб-версия и Supabase

Подробная настройка таблиц, RPC и прав находится в [SUPABASE_GUIDE.md](SUPABASE_GUIDE.md).

В `.env` задайте `PUBLIC_ORIGIN` — HTTPS-адрес своего сайта без пути. По нему строятся разрешённые веб-хосты, canonical, sitemap и проверка хоста для аналитики. `example.invalid` в примере — служебная заглушка, а не адрес работающего сервиса. Неизвестные хосты получают 403; локальный доступ остаётся на порту 8765.

`SUPPORT_URL` задаёт ссылку для связи, `SUPPORT_LABEL` — её подпись. По умолчанию ссылка ведёт на `/profile`. Веб-клиент получает только публичный URL и anon key; `service_key` остаётся на админской машине.

На публичном хосте `/` отдаёт лендинг, `/app` — приложение, `/login` и `/register` — авторизацию. Закрытые страницы, auth и API получают `noindex`; sitemap содержит только лендинг. `/api/landing/deals` и `/browser/icon/<id>` доступны лендингу без входа.

Для Cloudflare Tunnel установите `cloudflared`, настройте свой туннель на `http://localhost:8765` и положите его токен в локальный `tunnel_token.txt`. `start_all.bat` запускает админскую EXE и туннель через HTTP/2; повторный запуск проверяет уже работающие процессы. Не запускайте его на действующей установке ради проверки исходников.

## Сборка EXE

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt

# Только для машины владельца сервиса. Сборка может включать локальную .env.
.\.venv\Scripts\python.exe build_exe.py

# Версия для пользователей, без .env, push и веб-авторизации.
.\.venv\Scripts\python.exe build_exe.py --user
```

Админская сборка создаёт `AuctionMonitorAdmin.exe` и копирует его в корень установки. Пользовательская — `dist/AuctionMonitor.exe`. Админскую EXE нельзя загружать в пользовательские релизы: она может содержать секреты.

Перед подготовкой USER-релиза создайте `public_config.json` по примеру и укажите публичный URL и anon key. Затем выполните:

```powershell
.\.venv\Scripts\python.exe tools/prepare_release.py
```

Инструмент формирует `release/`: USER EXE, публичную конфигурацию лицензии, необходимые справочники, очищенную БД, иконки и [README_USER.md](README_USER.md). В БД сохраняются только разрешённые таблицы справочников; рабочая история и аккаунты исключаются. `public_config.json` проверяется на привилегированные ключи и посторонние поля до замены `release/`.

PyArmor используется для USER-сборки. Trial может не обфусцировать большие модули; смотрите предупреждения сборщика. Это не гарантия защиты исходников внутри EXE.

Сборка, публикация исходников и обновление действующего сервиса — отдельные действия. Изменение кода и `git push` сами по себе не обновляют работающую админскую EXE. Её пересборка, замена и перезапуск туннеля требуют отдельного разрешения владельца.

## Проверки перед коммитом

Запускайте из корня проекта. Тесты используют синтетические данные и не требуют действующих ключей, облачных аккаунтов или рабочей БД.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
python tests/test_build_gates.py
python tests/test_deployment_config.py
python tests/test_repo_safety.py
python tests/test_release_safety.py
python tests/test_source_bootstrap.py
python tests/test_boe_variants.py
node tests/test_boe_web.js
python tests/test_preset_persistence.py
node tests/test_preset_store.js
python tests/test_web_auth.py
node tests/test_web_auth.js
python -m py_compile desktop/main.py desktop/ahgem.py license_manager.py
python tools/check_repo_safety.py
git diff --check
```

Используйте `python` из созданного виртуального окружения: активируйте его либо подставьте `.\.venv\Scripts\python.exe` в команды. Для Node-тестов `python` также должен быть доступен в `PATH`.

Дополнительно перед отправкой истории можно проверить её [Gitleaks](https://github.com/gitleaks/gitleaks): `gitleaks git . --redact=100`. Для полного каталога на своей заполненной установке есть `python tests/validate_items.py --full`; это отдельная проверка данных, а не тест чистого клона.

## Структура

| Путь | Назначение |
| --- | --- |
| `desktop/main.py` | Flask, коллектор, локальный кэш, облачная публикация и веб-авторизация |
| `desktop/ahgem.py` | Blizzard API и декодирование аукционных лотов |
| `desktop/browser.py` | Каталог, метаданные и иконки |
| `static/` | Лендинг, интерфейс, общие фильтры и очередь сохранения пресетов |
| `license_manager.py`, `generate_license.py` | Проверка и генерация лицензий |
| `build_exe.py`, `tools/prepare_release.py` | Сборки ADMIN/USER и подготовка раздачи |
| `tools/` | SQL, обновление справочников, диагностика и проверки публикации |
| `tests/` | Проверки сборочных границ, BoE, пресетов, входа и конфигурации |

Правила дальнейших изменений: [AGENTS.md](AGENTS.md) и [QWEN.md](QWEN.md). Рабочие данные, резервные копии, временные прототипы и EXE исключены из Git.
