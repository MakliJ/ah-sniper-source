"""Check tracked source files; report locations without printing private values.

Optional --private-root compares against a local installation without copying
its configuration into the repository. Scan history separately with Gitleaks.
"""
import argparse
import ast
import base64
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

FORBIDDEN_NAMES = {
    'supabase_config.json', 'public_config.json', 'settings.json', 'collector_settings.json',
    'license.json', 'users.json', 'presets.json', 'tunnel_token.txt', 'tunnel_interface.txt',
    '.flask_secret', 'cookies.txt', 'history.json',
}
FORBIDDEN_DIRS = {'build', 'dist', 'release', '_obf', '__pycache__', '.venv', 'icons',
                  '_perf_tests', 'archive', 'auction_prices', '.qwen', '.qodo'}
RULES = {
    'github-token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
    'jwt-literal': re.compile(rb'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}'),
    'private-key': re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    'supabase-secret': re.compile(rb'sb_secret_[A-Za-z0-9_-]{20,}'),
    'deployment-project': re.compile(rb'https?://[a-z0-9]{20}\.supabase\.co'),
}
SENSITIVE_FIELD = re.compile(r'(?i)secret|password|token|api.?key|client.?id|anon.?key|service.?key|license.?key|key_hash|hwid|measurement|metrika')


def private_values(root):
    values = set()

    def add(value):
        if isinstance(value, str) and len(value.strip()) >= 8:
            values.add(value.strip())

    def read_json(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if SENSITIVE_FIELD.search(key):
                    add(item)
                if key in {'supabase_url', 'public_origin'} and isinstance(item, str):
                    host = urlsplit(item).hostname
                    if host:
                        add(host)
                        if host.endswith('.supabase.co'):
                            add(host.split('.')[0])
                if isinstance(item, (dict, list)):
                    read_json(item)
        elif isinstance(value, list):
            for item in value:
                read_json(item)

    for name in ['settings.json', 'collector_settings.json', 'supabase_config.json',
                 'public_config.json', 'license.json', 'users.json']:
        path = root / name
        if path.is_file():
            read_json(json.loads(path.read_text(encoding='utf-8-sig')))
    for name in ['.env', '.env copy']:
        path = root / name
        if path.is_file():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                if line.lstrip().startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                value = value.strip().strip('"\'')
                if SENSITIVE_FIELD.search(key):
                    add(value)
                if key.strip() in {'PUBLIC_ORIGIN', 'SUPPORT_URL', 'SUPABASE_URL'}:
                    host = urlsplit(value).hostname
                    if host:
                        add(host)
    for name in ['tunnel_token.txt', 'tunnel_interface.txt', '.flask_secret']:
        path = root / name
        if path.is_file():
            value = path.read_text(encoding='utf-8-sig').strip()
            add(value)
            try:
                payload = json.loads(base64.b64decode(value + '=' * (-len(value) % 4)))
                if isinstance(payload, dict):
                    for field in ['a', 't', 'ref']:
                        add(payload.get(field))
            except (ValueError, UnicodeDecodeError):
                pass
    for path in root.glob('keys_*.txt'):
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            add(line)
    main_path = root / 'desktop/main.py'
    if main_path.is_file():
        text = main_path.read_text(encoding='utf-8-sig')
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'WEB_HOSTS' for target in node.targets):
                try:
                    for host in ast.literal_eval(node.value):
                        add(host)
                except (ValueError, TypeError):
                    pass
        for contact in re.findall(r'https://t\.me/([A-Za-z0-9_]+)', text):
            add(contact)
    return {value.encode('utf-8') for value in values}


def forbidden(path):
    name = path.name.lower()
    return (name in FORBIDDEN_NAMES or (name.startswith('.env') and name != '.env.example')
            or name.startswith('keys_') or name.endswith(('.exe', '.exe.next', '.dll', '.db', '.db-wal',
               '.db-shm', '.zip', '.rar', '.7z', '.bak', '.pyc', '.log', '.pem', '.pfx', '.p12'))
            or any(part.lower() in FORBIDDEN_DIRS or part.startswith(('_backup_', '_checkpoint_')) for part in path.parts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--private-root', type=Path)
    args = parser.parse_args()
    result = subprocess.run(['git', '-C', str(args.repo), 'ls-files', '-z'], capture_output=True)
    if result.returncode:
        parser.error('Run this check inside an initialized Git repository')
    private = private_values(args.private_root) if args.private_root else set()
    issues = []
    files = [value for value in result.stdout.decode('utf-8').split('\0') if value]
    for name in files:
        path = Path(name)
        if forbidden(path):
            issues.append({'file': name, 'rule': 'forbidden-file'})
        local_path = args.repo / path
        if not local_path.is_file():
            issues.append({'file': name, 'rule': 'missing-file'})
            continue
        raw = local_path.read_bytes()
        for rule, pattern in RULES.items():
            for match in pattern.finditer(raw):
                issues.append({'file': name, 'rule': rule, 'line': raw.count(b'\n', 0, match.start()) + 1})
        for value in private:
            position = raw.lower().find(value.lower())
            if position != -1:
                issues.append({'file': name, 'rule': 'private-value', 'line': raw.count(b'\n', 0, position) + 1})
    print(json.dumps({'files_checked': len(files), 'private_values_checked': len(private),
                      'issues': issues, 'ok': not issues}, ensure_ascii=True, indent=2))
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
