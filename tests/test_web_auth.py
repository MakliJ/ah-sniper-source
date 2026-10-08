"""Login creates a usable session; cloud outages are retryable and never cached."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
import main


class WebAuthTest(unittest.TestCase):
    def setUp(self):
        main.app.config.update(TESTING=True, SECRET_KEY='auth-test-only')
        main._RATE.clear()
        self.client = main.app.test_client()

    def login(self):
        return self.client.post('/login', base_url='https://example.invalid',
                                json={'email':'TEST@example.invalid','password':'test-password'})

    def test_login_session_opens_app_without_refresh(self):
        user = {'id':'test-only', 'tier':'pro',
                'password_hash':main.generate_password_hash('test-password')}
        with patch.object(main, '_supabase_web', return_value=[user]) as cloud:
            response = self.login()
        self.assertEqual(response.json, {'ok':True,'redirect':'/app'})
        self.assertEqual(cloud.call_args.kwargs['params']['select'], 'id,password_hash,tier')
        self.assertIn('Secure', response.headers['Set-Cookie'])
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        app = self.client.get('/app', base_url='https://example.invalid')
        self.assertEqual(app.status_code, 200)
        self.assertEqual(app.headers['Cache-Control'], 'no-store')
        self.assertIn('noindex', app.headers['X-Robots-Tag'])
        self.assertEqual(self.client.get('/login', base_url='https://example.invalid').location, '/app')

    def test_cloud_failure_and_invalid_credentials_create_no_session(self):
        for cloud_result, status in [(None,503),(True,503),([],200)]:
            with patch.object(main, '_supabase_web', return_value=cloud_result):
                response = self.login()
            self.assertEqual(response.status_code, status)
            self.assertFalse(response.json['ok'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertNotIn('Set-Cookie', response.headers)

    def test_wrong_password_and_rate_limit(self):
        user = {'id':'test-only','password_hash':main.generate_password_hash('different')}
        with patch.object(main, '_supabase_web', return_value=[user]) as cloud:
            for _ in range(5):
                self.assertFalse(self.login().json['ok'])
            self.assertEqual(self.login().status_code, 429)
            self.assertEqual(cloud.call_count, 5)


if __name__ == '__main__':
    unittest.main()
