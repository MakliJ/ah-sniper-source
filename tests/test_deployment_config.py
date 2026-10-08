"""Offline checks for a source checkout without private deployment settings."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import license_manager


class PublicLicenseConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'SUPABASE_URL': '', 'SUPABASE_ANON_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path_patch = patch.object(license_manager, 'data', side_effect=lambda name: str(self.folder / name))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def write_config(self, content):
        (self.folder / 'public_config.json').write_text(json.dumps(content), encoding='utf-8')

    def test_missing_config_never_uses_admin_credentials(self):
        (self.folder / 'supabase_config.json').write_text(json.dumps({
            'supabase_url': 'https://private.example.invalid', 'service_key': 'admin-fixture-only'
        }), encoding='utf-8')
        self.assertEqual(license_manager._load_public_config(), ('', ''))

    def test_public_file_and_environment_override(self):
        self.write_config({'supabase_url': 'https://cloud.example.invalid/', 'anon_key': 'public-fixture'})
        self.assertEqual(license_manager._load_public_config(), ('https://cloud.example.invalid', 'public-fixture'))
        with patch.dict(os.environ, {'SUPABASE_URL': 'https://other.example.invalid', 'SUPABASE_ANON_KEY': 'override-fixture'}):
            self.assertEqual(license_manager._load_public_config(), ('https://other.example.invalid', 'override-fixture'))

    def test_rejects_service_fields_and_privileged_keys(self):
        self.write_config({'supabase_url': '', 'anon_key': '', 'service_key': 'admin-fixture-only'})
        with self.assertRaises(ValueError):
            license_manager._load_public_config()
        payload = base64.urlsafe_b64encode(json.dumps({'role': 'service_role'}).encode()).decode().rstrip('=')
        privileged_jwt = 'header.' + payload + '.signature'
        for key in ['sb_secret_' + 'fixture', privileged_jwt]:
            self.write_config({'supabase_url': 'https://cloud.example.invalid', 'anon_key': key})
            with self.assertRaises(ValueError):
                license_manager._load_public_config()

    def test_rejects_non_https_or_credential_urls(self):
        for url in ['http://cloud.example.invalid', 'https://user:password@cloud.example.invalid',
                    'https://cloud.example.invalid/private', 'https://cloud.example.invalid?token=fixture']:
            self.write_config({'supabase_url': url, 'anon_key': 'public-fixture'})
            with self.assertRaises(ValueError):
                license_manager._load_public_config()


class ConfigurableWebTest(unittest.TestCase):
    def test_unsafe_origin_and_label_are_rejected_before_rendering(self):
        cases = [
            {'PUBLIC_ORIGIN': 'https://host.example.invalid" data-x="fixture'},
            {'PUBLIC_ORIGIN': 'https://host.example.invalid\\fixture'},
            {'SUPPORT_LABEL': 'Support\nTeam'},
            {'SUPPORT_LABEL': 'Support\\Team'},
            {'SUPPORT_LABEL': 'Support\u2028Team'},
        ]
        for change in cases:
            env = dict(os.environ, PUBLIC_ORIGIN='https://deployment.example.invalid',
                       SUPPORT_URL='/profile', SUPPORT_LABEL='Support', PYTHONIOENCODING='utf-8')
            env.update(change)
            result = subprocess.run([sys.executable, '-c', "import sys; sys.path.insert(0,'desktop'); import main"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertNotEqual(result.returncode, 0, 'Unsafe template configuration was accepted')

    def test_configured_host_seo_and_private_routes(self):
        code = '''
import sys,json
sys.path.insert(0,'desktop')
import main
main.app.config.update(TESTING=True)
c=main.app.test_client()
host='https://deployment.example.invalid'
landing=c.get('/',base_url=host)
assert landing.status_code==200
text=landing.get_data(as_text=True)
assert 'https://deployment.example.invalid/' in text
assert 'https://support.example.invalid/help' in text
assert '__AH_' not in text
assert 'noindex' not in landing.headers.get('X-Robots-Tag','')
assert 'https://deployment.example.invalid/sitemap.xml' in c.get('/robots.txt',base_url=host).get_data(as_text=True)
assert '<loc>https://deployment.example.invalid/</loc>' in c.get('/sitemap.xml',base_url=host).get_data(as_text=True)
assert c.get('/api/items',base_url=host).status_code==401
for route in ['/app','/login','/register','/api/env','/browser/api/items']:
 r=c.get(route,base_url=host)
 assert 'noindex' in r.headers.get('X-Robots-Tag',''), (route,r.status_code)
assert c.get('/',base_url='https://untrusted.example.invalid').status_code==403
assert c.get('/api/env',base_url='http://127.0.0.1:8765').status_code==200
print('configured host, canonical, sitemap, auth, noindex and localhost: PASS')
'''
        env = dict(os.environ, PUBLIC_ORIGIN='https://deployment.example.invalid',
                   SUPPORT_URL='https://support.example.invalid/help', SUPPORT_LABEL='Support',
                   PYTHONIOENCODING='utf-8')
        with tempfile.TemporaryDirectory() as folder:
            flags = Path(folder) / 'flags.py'
            flags.write_text('ADMIN_BUILD = True\n', encoding='utf-8')
            env['AH_BUILD_FLAGS'] = str(flags)
            result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
