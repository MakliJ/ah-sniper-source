"""Exercise the real USER packaging path with temporary data and a fake EXE."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import license_manager

spec = importlib.util.spec_from_file_location('prepare_release', ROOT / 'tools/prepare_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.out = self.folder / 'release'
        self.out.mkdir()
        (self.out / 'previous.txt').write_text('preserve until config validates')
        (self.folder / 'dist').mkdir()
        exe = self.folder / 'dist/AuctionMonitor.exe'
        exe.write_bytes(b'fake-user-exe')
        (self.folder / 'supabase_config.json').write_text('{"service_key":"admin-fixture-only"}')
        (self.folder / '.env').write_text('CLIENT_SECRET=private-fixture-only\n')
        for name in release.PLAIN_FILES:
            (self.folder / name).write_text('{}\n', encoding='utf-8')
        previous_cwd = os.getcwd()
        self.addCleanup(os.chdir, previous_cwd)
        for context in [patch.object(release, 'ROOT', str(self.folder)),
                        patch.object(release, 'RELEASE', str(self.out)),
                        patch.object(release, 'USER_EXE', str(exe)),
                        patch.object(license_manager, 'data', side_effect=lambda name: str(self.folder / name)),
                        patch.dict(os.environ, {'SUPABASE_URL': '', 'SUPABASE_ANON_KEY': ''})]:
            context.start()
            self.addCleanup(context.stop)

    def test_user_package_contains_public_fields_and_excludes_admin_files(self):
        public = {'supabase_url': 'https://cloud.example.invalid', 'anon_key': 'public-fixture'}
        (self.folder / 'public_config.json').write_text(json.dumps(public))
        release.main()
        self.assertEqual(json.loads((self.out / 'public_config.json').read_text()), public)
        self.assertTrue((self.out / 'AuctionMonitor.exe').is_file())
        self.assertFalse((self.out / '.env').exists())
        self.assertFalse((self.out / 'supabase_config.json').exists())
        self.assertFalse((self.out / 'AuctionMonitorAdmin.exe').exists())
        self.assertNotIn('admin-fixture-only', '\n'.join(p.read_text(errors='replace') for p in self.out.iterdir()))

    def test_missing_public_config_preserves_previous_release(self):
        with self.assertRaises(SystemExit) as error:
            release.main()
        self.assertEqual(error.exception.code, 1)
        self.assertTrue((self.out / 'previous.txt').is_file())

    def test_privileged_config_preserves_previous_release(self):
        (self.folder / 'public_config.json').write_text(json.dumps({
            'supabase_url': 'https://cloud.example.invalid', 'anon_key': 'public-fixture',
            'service_key': 'admin-fixture-only'}))
        with self.assertRaises(ValueError):
            release.main()
        self.assertTrue((self.out / 'previous.txt').is_file())


if __name__ == '__main__':
    unittest.main()
