#!/usr/bin/env python3
"""Audit the complete catalog and fetch missing original item icons.

Reads auction databases without changing them. Valid existing images are retained.
Only item media is fetched; auction stats and presets are never modified.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from urllib.parse import unquote, urlparse

from dotenv import dotenv_values
from PIL import Image
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]


def valid_image(source):
    try:
        with Image.open(source) as im:
            im.load()
            return im.format == 'JPEG' and min(im.size) >= 16
    except (OSError, ValueError, SyntaxError):
        return False


def catalog():
    ids, boe = set(), set()
    for name in ['item_names.json', 'item_names_ru.json']:
        path = ROOT / name
        if path.exists():
            ids.update(int(k) for k in json.loads(path.read_text(encoding='utf-8')) if str(k).isdigit())
    for path in [ROOT / 'auction_data.db', ROOT / 'auction_data_us.db']:
        if not path.exists():
            continue
        db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=10)
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, column in [('items', 'id'), ('item_meta', 'item_id'), ('auction_latest', 'item_id'), ('auction_snapshots', 'item_id')]:
                if table in tables:
                    ids.update(r[0] for r in db.execute(f'SELECT DISTINCT {column} FROM {table}') if r[0] > 0)
            for table in ['auction_latest', 'auction_snapshots']:
                if table in tables:
                    boe.update(r[0] for r in db.execute(f'SELECT DISTINCT item_id FROM {table} WHERE ilvl>0'))
            if 'item_meta' in tables:
                boe.update(r[0] for r in db.execute('SELECT item_id FROM item_meta WHERE bind_type=2'))
        finally:
            db.close()
    for path in ROOT.glob('boe_variants_live_*.json'):
        raw = json.loads(path.read_text(encoding='utf-8'))
        boe.update(int(k.split(':')[1]) for k in raw.get('rows', {}))
    ids.update(boe)
    return ids, boe


def write_json(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    os.replace(tmp, path)


class IconPage(HTMLParser):
    """Read only item identity and the original icon from a Wowhead item page."""
    def __init__(self):
        super().__init__()
        self.canonical = None
        self.icon = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonical = attrs.get('href')
        if tag == 'meta' and attrs.get('property') == 'og:image':
            self.icon = attrs.get('content')


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Audit only; do not download or change files.')
    parser.add_argument('--backup-dir', type=Path, help='Required for downloads; originals and the report are saved here.')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 9))
    parser.add_argument('--wowhead-fallback', action='store_true', help='For missing Blizzard media, verify the same item ID on Wowhead and fetch its original icon.')
    args = parser.parse_args()
    ids, boe = catalog()
    icons = ROOT / 'icons'
    missing = sorted((iid for iid in ids if not valid_image(icons / f'{iid}.jpg')), key=lambda iid: (iid not in boe, iid))
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'catalog_count': len(ids),
              'missing_before': missing, 'boe_count': len(boe), 'boe_missing_before': sorted(set(missing) & boe), 'downloaded': [], 'unresolved': []}
    print(json.dumps({k: v for k, v in report.items() if k not in ['missing_before', 'downloaded', 'unresolved']}) + f' Missing: {len(missing)}', flush=True)
    if args.check or not missing:
        return 0
    if not args.backup_dir:
        parser.error('--backup-dir is required before modifying icon data')
    backup = args.backup_dir.resolve()
    backup.mkdir(parents=True, exist_ok=False)
    map_path = ROOT / 'item_icons.json'
    mapping = json.loads(map_path.read_text(encoding='utf-8')) if map_path.exists() else {}
    if map_path.exists():
        shutil.copy2(map_path, backup / map_path.name)
    for iid in missing:
        path = icons / f'{iid}.jpg'
        if path.exists():
            (backup / 'icons').mkdir(exist_ok=True)
            shutil.copy2(path, backup / 'icons' / path.name)
    write_json(backup / 'audit_before.json', report)
    credentials = dotenv_values(ROOT / '.env')
    client_id = credentials.get('CLIENT_ID') or os.getenv('CLIENT_ID')
    client_secret = credentials.get('CLIENT_SECRET') or os.getenv('CLIENT_SECRET')
    if not client_id or not client_secret:
        raise RuntimeError('Blizzard client credentials are unavailable')
    auth = requests.post('https://oauth.battle.net/token', auth=(client_id, client_secret),
                         data={'grant_type': 'client_credentials'}, timeout=(8, 20))
    auth.raise_for_status()
    token = auth.json()['access_token']
    tls = threading.local()

    def session():
        if not hasattr(tls, 'http'):
            tls.http = requests.Session()
            retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])
            tls.http.mount('https://', HTTPAdapter(max_retries=retry))
        return tls.http

    def fetch(iid):
        http = session()
        api_base = 'https://eu.api.blizzard.com'
        params = {'namespace': 'static-eu', 'locale': 'en_US'}
        headers = {'Authorization': 'Bearer ' + token}
        urls, statuses = [], []
        try:
            response = http.get(f'{api_base}/data/wow/media/item/{iid}', params=params, headers=headers, timeout=(5, 15))
            statuses.append(response.status_code)
            if response.status_code == 404:
                item = http.get(f'{api_base}/data/wow/item/{iid}', params=params, headers=headers, timeout=(5, 15))
                statuses.append(item.status_code)
                if item.status_code == 200 and item.json().get('id') == iid:
                    media_id = (item.json().get('media') or {}).get('id')
                    if isinstance(media_id, int) and media_id != iid:
                        response = http.get(f'{api_base}/data/wow/media/item/{media_id}', params=params, headers=headers, timeout=(5, 15))
                        statuses.append(response.status_code)
            if response.status_code == 200:
                urls.extend(a['value'] for a in response.json().get('assets', []) if a.get('key') == 'icon' and a.get('value'))
            known = mapping.get(str(iid))
            if isinstance(known, str) and known and all(c.isalnum() or c in '_-' for c in known):
                urls.append(f'https://render.worldofwarcraft.com/eu/icons/56/{known}.jpg')
            for url in dict.fromkeys(urls):
                parsed = urlparse(url)
                if parsed.scheme != 'https' or parsed.hostname != 'render.worldofwarcraft.com':
                    continue
                # Credentials belong only to the Blizzard API, never to the image CDN.
                asset = http.get(url, timeout=(5, 15))
                if asset.status_code == 200 and valid_image(BytesIO(asset.content)):
                    return {'id': iid, 'source': url, 'name': Path(parsed.path).stem, 'content': asset.content}
            if args.wowhead_fallback:
                page = http.get(f'https://www.wowhead.com/item={iid}', timeout=(5, 15))
                if page.status_code == 200:
                    metadata = IconPage()
                    metadata.feed(page.text)
                    canonical = urlparse(metadata.canonical or '')
                    image = urlparse(metadata.icon or '')
                    name = Path(image.path).stem
                    if (canonical.hostname == 'www.wowhead.com'
                            and re.match(rf'^/item={iid}(?:/|$)', unquote(canonical.path))
                            and image.scheme == 'https' and image.hostname == 'wow.zamimg.com'
                            and image.path.startswith('/images/wow/icons/') and image.path.endswith('.jpg')
                            and name not in ['inv_misc_questionmark', 'questionmark']):
                        asset = http.get(metadata.icon, timeout=(5, 15))
                        if asset.status_code == 200 and valid_image(BytesIO(asset.content)):
                            return {'id': iid, 'source': metadata.icon, 'source_page': metadata.canonical,
                                    'name': name, 'content': asset.content}
            return {'id': iid, 'media_http': statuses, 'error': 'Original icon unavailable'}
        except requests.RequestException as exc:
            return {'id': iid, 'error': type(exc).__name__}

    icons.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(fetch, iid) for iid in missing]
        for count, job in enumerate(as_completed(jobs), 1):
            result = job.result()
            if 'content' in result:
                blob = result.pop('content')
                path = icons / f'{result["id"]}.jpg'
                tmp = path.with_suffix('.jpg.tmp')
                tmp.write_bytes(blob)
                os.replace(tmp, path)
                mapping[str(result['id'])] = result['name']
                report['downloaded'].append(result)
                write_json(map_path, mapping)
            else:
                report['unresolved'].append(result)
            write_json(backup / 'result.json', report)
            if count <= len(report['boe_missing_before']) or count % 20 == 0 or count == len(missing):
                print(f'{count}/{len(missing)}: downloaded {len(report["downloaded"])}, unresolved {len(report["unresolved"])}', flush=True)
    print(f'Report: {backup / "result.json"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
