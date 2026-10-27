"""Exercise the real scheduler, adapters and outbox with controlled I/O boundaries."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from forgetmenot import codex_runtime, providers
from forgetmenot.app import create_app
from forgetmenot.store import Store
from forgetmenot.worker import Worker, send_delivery

SOURCE = 'https://example.com/product'
MODEL = {'provider': 'codex', 'id': 'test-model'}
RESULT = {'outcome': 'matched', 'title': 'Product available', 'approach': 'Read the official listing.',
          'summary': 'The exact product is available.', 'interval': 3600,
          'evidence': [{'url': SOURCE, 'title': 'Official listing', 'detail': 'In stock.'}]}


def events(result=None, url=SOURCE):
    return '\n'.join(json.dumps(event) for event in [
        {'type': 'item.completed', 'item': {'type': 'web_search', 'action': {'type': 'open_page', 'url': url}}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result or RESULT)}},
        {'type': 'turn.completed'},
    ])


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        directory = self.store.directory / 'codex'
        directory.mkdir()
        (directory / 'auth.json').write_text('{}')

    def test_model_pinning_and_unconfigured_queue(self):
        unconfigured = self.store.create('A waiting goal', 60, [])
        self.assertIsNone(self.store.claim(configured_only=True))
        self.store.save_setting('model', MODEL)
        self.store.save_setting('model', {'provider': 'openrouter', 'id': 'another-model'})
        self.assertEqual(self.store.watches()[0]['model'], MODEL)
        self.assertEqual(self.store.claim(configured_only=True)['id'], unconfigured)
        self.store.edit(unconfigured, 'A revised goal', 3600, [])
        self.assertEqual(self.store.watches()[0]['model'], MODEL)
        explicit = {'provider': 'claude', 'id': 'selected-model'}
        self.store.edit(unconfigured, 'A revised goal', 3600, [], explicit)
        self.assertEqual(self.store.watches()[0]['model'], explicit)

    def test_migration_preserves_watches_and_history(self):
        self.store.save_setting('model', MODEL)
        watch_id = self.store.create('An existing goal', 60, [])
        self.store.finish(self.store.claim(), RESULT, 'old-model')
        with self.store.connect() as db:
            db.execute('ALTER TABLE watches DROP COLUMN model')
            db.execute('ALTER TABLE checks DROP COLUMN activity')
        restarted = Store(self.temp.name)
        self.assertEqual(restarted.watches()[0]['id'], watch_id)
        self.assertEqual(restarted.watches()[0]['model'], MODEL)
        self.assertEqual(restarted.history(watch_id)[0]['summary'], RESULT['summary'])
        self.assertEqual(restarted.history(watch_id)[0]['activity'], [])
        self.assertEqual(len(restarted.inbox()['notifications']), 1)

    def test_codex_worker_wait_match_restart_and_channel_retry(self):
        watch_id = self.store.create('Watch the exact product', 60, ['telegram', 'push'], MODEL)
        self.store.save_setting('telegram_token', '123456:controlled-test-token')
        self.store.save_setting('telegram_chat', '-123456')
        self.store.save_setting('ntfy_topic', 'controlled-test-topic')
        launches, sends = [], []
        def launch(args, **kwargs):
            launches.append((args, kwargs))
            result = dict(RESULT, outcome='waiting') if len(launches) == 1 else RESULT
            return subprocess.CompletedProcess(args, 0, events(result), '')
        client = httpx.Client
        def transport(request):
            sends.append(request)
            if request.url.host == 'api.telegram.org':
                return httpx.Response(200, json={'ok': len([r for r in sends if r.url.host == 'api.telegram.org']) > 1})
            self.assertEqual(request.url.host, 'ntfy.sh')
            return httpx.Response(200, json={'id': 'accepted'})
        def http_client(**kwargs):
            return client(transport=httpx.MockTransport(transport), **kwargs)
        with patch('forgetmenot.codex_runtime.shutil.which', return_value='/installed/codex'), \
             patch('forgetmenot.codex_runtime.subprocess.run', side_effect=launch), \
             patch.dict(os.environ, {'FORGETMENOT_PASSWORD': 'must-not-inherit', 'SMTP_PASSWORD': 'must-not-inherit'}), \
             patch('forgetmenot.providers.httpx.Client', side_effect=http_client):
            worker = Worker(self.store)
            worker.tick()
            self.assertEqual(self.store.inbox()['notifications'], [])
            self.assertEqual(self.store.watches()[0]['interval'], 60)
            self.store.update(watch_id, 'check')
            worker.tick()
            self.assertEqual(self.store.watches()[0]['status'], 'matched')
            restarted = Store(self.temp.name)
            worker = Worker(restarted)
            worker.tick()
            deliveries = {d['channel']: d for d in restarted.inbox()['deliveries']}
            self.assertIsNotNone(deliveries['push']['delivered'])
            self.assertIsNone(deliveries['telegram']['delivered'])
            self.assertNotIn('controlled-test-token', deliveries['telegram']['error'])
            with restarted.connect() as db:
                db.execute('UPDATE deliveries SET next_attempt=0 WHERE delivered IS NULL')
            worker.tick()
            worker.tick()
        self.assertEqual(len(launches), 2)
        self.assertEqual(len(sends), 3)
        self.assertEqual(len(restarted.inbox()['notifications']), 1)
        self.assertTrue(all(d['delivered'] for d in restarted.inbox()['deliveries']))
        self.assertEqual(restarted.history(watch_id)[0]['activity'][0]['url'], SOURCE)
        for args, kwargs in launches:
            self.assertIn('--ignore-user-config', args)
            self.assertIn('read-only', args)
            self.assertIn('shell_tool', args)
            self.assertIn('multi_agent', args)
            self.assertEqual(kwargs['timeout'], 180)
            self.assertNotIn('SMTP_PASSWORD', kwargs['env'])
            self.assertNotIn('FORGETMENOT_PASSWORD', kwargs['env'])
            self.assertEqual(kwargs['env']['CODEX_HOME'], str((self.store.directory / 'codex').resolve()))
            self.assertNotEqual(kwargs['cwd'], os.getcwd())
        for request in sends:
            body = json.loads(request.content)
            self.assertIn(SOURCE, body.get('text', body.get('message', '')))

    def test_codex_requires_completed_grounded_web_research(self):
        self.assertEqual(codex_runtime.parse_events(events())['outcome'], 'matched')
        self.assertEqual(codex_runtime.parse_events(events(url='https://invented.example/'))['outcome'], 'inconclusive')
        for extra in [{'type': 'turn.failed'}, {'type': 'error'},
                      {'type': 'item.completed', 'item': {'type': 'command_execution'}}]:
            with self.subTest(extra=extra), self.assertRaises(providers.ProviderError):
                codex_runtime.parse_events(events() + '\n' + json.dumps(extra))
        with self.assertRaises(providers.ProviderError):
            codex_runtime.parse_events('\n'.join(events().splitlines()[:-1]))
        with self.assertRaises(providers.ProviderError):
            codex_runtime.parse_events('\n'.join(events().splitlines()[1:]))

    def test_telegram_chunking_keeps_full_text_and_sources(self):
        self.store.save_setting('telegram_token', '123456:controlled-test-token')
        self.store.save_setting('telegram_chat', '123456')
        self.store.create('A long report', 60, ['telegram'])
        result = deepcopy(RESULT)
        result['summary'] = 'x' * 4000
        self.store.finish(self.store.claim(), result, 'test')
        with patch('forgetmenot.providers.request_json', return_value={'ok': True}) as request:
            send_delivery(self.store, self.store.pending_delivery())
        chunks = [call.kwargs['json']['text'] for call in request.call_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertIn(result['summary'], '\n'.join(chunks))
        self.assertIn(SOURCE, '\n'.join(chunks))

    def test_api_model_and_telegram_validation_hides_credentials(self):
        app = create_app(self.temp.name, password='test-password-only', start_worker=False)
        with TestClient(app, base_url='http://127.0.0.1:8787', headers={'X-ForgetMeNot': '1'}) as client:
            client.post('/api/login', json={'password': 'test-password-only'})
            body = {'goal': 'Watch the exact product', 'channels': ['telegram'], 'model': dict(MODEL)}
            self.assertEqual(client.post('/api/watches', json=body).status_code, 400)
            token = '123456:' + 'a' * 30
            response = client.put('/api/settings', json={'telegram_token': token, 'telegram_chat': '-123456'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(token, response.text)
            self.assertTrue(response.json()['telegram_token_set'])
            for invalid in [{'telegram_token': 'bad/token'}, {'telegram_chat': 'bad chat'}]:
                self.assertEqual(client.put('/api/settings', json=invalid).status_code, 400)
            with patch('forgetmenot.app.providers.models', return_value=[{'id': 'test-model'}]):
                self.assertEqual(client.post('/api/watches', json=body).status_code, 201)
                body['model']['id'] = '--unsafe-option'
                self.assertEqual(client.post('/api/watches', json=body).status_code, 422)
            self.assertNotIn(token, client.get('/api/state').text)

    def test_connect_copies_session_privately_and_rejects_api_keys(self):
        with tempfile.TemporaryDirectory() as home:
            source = Path(home) / '.codex'
            source.mkdir()
            auth = source / 'auth.json'
            raw = json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'controlled-session'}})
            auth.write_text(raw)
            with patch('forgetmenot.codex_runtime.Path.home', return_value=Path(home)), \
                 patch('forgetmenot.codex_runtime.shutil.which', return_value='/installed/codex'):
                codex_runtime.connect(self.store)
                target = self.store.directory / 'codex' / 'auth.json'
                self.assertEqual(target.read_text(), raw)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
                for invalid in ['{"auth_mode":"apikey"}', '[]', '{"auth_mode":"chatgpt","tokens":null}']:
                    auth.write_text(invalid)
                    with self.subTest(invalid=invalid), self.assertRaises(providers.ProviderError):
                        codex_runtime.connect(self.store)
                self.assertEqual(target.read_text(), raw)
