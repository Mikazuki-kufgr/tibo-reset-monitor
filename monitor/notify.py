from __future__ import annotations
import os, json, pathlib, subprocess, hmac, hashlib
from urllib.parse import urlparse
from .http import ReadFailure, UnknownDelivery

CHANNELS = ('windows', 'ntfy', 'bark', 'webhook', 'qq_spool')

def enabled(config):
    return [ch for ch in CHANNELS if config.get('channels', {}).get(ch, {}).get('enabled') is True]

def secret(name):
    value = os.environ.get(name, '')
    if not value:
        raise ReadFailure('missing_channel_environment_variable')
    return value

def endpoint(value):
    u = urlparse(value)
    if u.username or u.password or u.fragment or not u.hostname:
        raise ReadFailure('invalid_delivery_endpoint')
    if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ReadFailure('endpoint_requires_https_or_loopback')
    return value

class Sender:
    def __init__(self, config, http, runtime: pathlib.Path):
        self.config, self.http, self.runtime = config, http, runtime

    def send(self, channel, payload):
        cfg = self.config['channels'][channel]
        if cfg.get('enabled') is not True:
            raise ReadFailure('channel_disabled')
        if channel == 'ntfy':
            headers = {}
            if cfg.get('token_env') and os.environ.get(cfg['token_env']):
                headers['Authorization'] = 'Bearer ' + secret(cfg['token_env'])
            result = self.http.post(endpoint(cfg.get('server', 'https://ntfy.sh')).rstrip('/'), {
                'topic': secret(cfg.get('topic_env', 'NTFY_TOPIC')), 'title': payload['title'],
                'message': payload['body'], 'priority': 5, 'click': payload['url'], 'tags': ['warning']}, headers)
            if not result.get('id'):
                raise UnknownDelivery('ntfy_missing_receipt')
            return {'provider':'ntfy','receipt_id':result['id']}
        elif channel == 'bark':
            result = self.http.post(endpoint(cfg.get('server', 'https://api.day.app')).rstrip('/') + '/push', {
                'device_key': secret(cfg.get('key_env', 'BARK_DEVICE_KEY')), 'title': payload['title'],
                'body': payload['body'], 'url': payload['url'], 'group': 'codex-reset', 'level': 'timeSensitive', 'sound': 'alarm'})
            if result.get('code') != 200:
                raise UnknownDelivery('bark_missing_success_receipt')
        elif channel == 'webhook':
            headers = {'Idempotency-Key': payload['id']}
            raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            if cfg.get('secret_env'):
                headers['X-Reset-Signature'] = hmac.new(secret(cfg['secret_env']).encode(), raw, hashlib.sha256).hexdigest()
            result = self.http.post(endpoint(secret(cfg.get('url_env', 'RESET_WEBHOOK_URL'))), payload, headers)
            if result.get('accepted_id') != payload['id']:
                raise UnknownDelivery('webhook_missing_id_receipt')
        elif channel == 'qq_spool':
            target = self.runtime / 'qq_spool' / (payload['id'] + '.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            # Existing file is the idempotent local receipt. No production DB.
            if not target.exists():
                tmp = target.with_suffix('.tmp')
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(tmp, target)
        elif channel == 'windows':
            if os.name != 'nt':
                raise ReadFailure('windows_channel_requires_windows')
            self.runtime.mkdir(parents=True, exist_ok=True)
            target = self.runtime / 'windows-payload.json'
            target.write_text(json.dumps({**payload, 'tts': cfg.get('tts', True), 'sound': cfg.get('sound', True)}, ensure_ascii=False), encoding='utf-8')
            script = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'alert.ps1'
            try:
                r = subprocess.run(['powershell.exe', '-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', str(script), '-PayloadPath', str(target)], capture_output=True, timeout=45)
            except (subprocess.TimeoutExpired, OSError):
                raise UnknownDelivery('windows_alert_uncertain') from None
            if r.returncode:
                raise UnknownDelivery('windows_alert_failed')
            try:
                return json.loads(r.stdout.decode('utf-8-sig').strip())
            except (ValueError,UnicodeError):
                return {'provider':'windows','script_exit':0}
        else:
            raise ReadFailure('unknown_channel')
