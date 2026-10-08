"""Publication checks must fail closed and never echo matching values."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CHECKER = Path(__file__).resolve().parents[1] / 'tools' / 'check_repo_safety.py'


class RepoSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, capture_output=True)

    def stage(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        subprocess.run(['git', '-C', str(self.repo), 'add', name], check=True, capture_output=True)

    def check(self, *args):
        return subprocess.run([sys.executable, str(CHECKER), '--repo', str(self.repo), *args],
                              capture_output=True, text=True, encoding='utf-8', timeout=15)

    def test_empty_examples_are_safe(self):
        self.stage('.env.example', 'CLIENT_SECRET=\n')
        self.stage('public_config.example.json', '{"supabase_url":"","anon_key":""}\n')
        self.assertEqual(self.check().returncode, 0)

    def test_rejects_tracked_runtime_credentials_and_binaries(self):
        for name in ['.env', 'nested/supabase_config.json', 'AuctionMonitorAdmin.exe',
                     'data/auction_data.db', 'presets.json', 'tunnel_token.txt']:
            self.stage(name, 'fixture')
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('forbidden-file', result.stdout)

    def test_detects_tokens_without_echoing_them(self):
        token = 'ghp_' + 'A' * 36
        self.stage('source.py', 'token = ' + repr(token))
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_private_values_and_domains_are_compared_without_echo(self):
        private = self.root / 'private'
        (private / 'desktop').mkdir(parents=True)
        value = 'sensitive-' + 'fixture-value'
        domain = 'tenant.' + 'example.invalid'
        (private / 'settings.json').write_text(json.dumps({'client_secret': value}), encoding='utf-8')
        (private / 'desktop/main.py').write_text('WEB_HOSTS = {' + repr(domain) + '}\n', encoding='utf-8')
        self.stage('source.py', 'secret = ' + repr(value) + '\nurl = ' + repr('https://' + domain))
        result = self.check('--private-root', str(private))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(value, result.stdout + result.stderr)
        self.assertNotIn(domain, result.stdout + result.stderr)
        self.assertIn('private-value', result.stdout)


if __name__ == '__main__':
    unittest.main()
