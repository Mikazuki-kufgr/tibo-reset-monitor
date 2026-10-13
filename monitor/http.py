from __future__ import annotations
import json, time, socket
import urllib.request, urllib.error
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

class ReadFailure(RuntimeError):
    pass

class RetryDelivery(RuntimeError):
    def __init__(self, code: str, delay: float = 60):
        super().__init__(code)
        self.delay = delay

class UnknownDelivery(RuntimeError):
    pass

def retry_after(value: str | None, now=None) -> float:
    try:
        return max(1, float(value))
    except (TypeError, ValueError):
        try:
            return max(1, (parsedate_to_datetime(value) - (now or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class HTTP:
    def __init__(self, user_agent: str, timeout: int = 20, sleeper=time.sleep):
        self.user_agent, self.timeout, self.sleeper = user_agent, timeout, sleeper
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, url: str) -> tuple[bytes, dict]:
        attempts = 1 if url.startswith('https://codex-reset.com/') else 3
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': self.user_agent, 'Accept': 'application/json, application/rss+xml'})
                with self.opener.open(req, timeout=self.timeout) as r:
                    if r.status != 200:
                        raise ReadFailure('unexpected_http_status')
                    b = r.read(2_000_001)
                    if len(b) > 2_000_000:
                        raise ReadFailure('body_too_large')
                    return b, {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Never retry this endpoint inside its Retry-After window.
                    raise RetryDelivery('read_429', retry_after(e.headers.get('Retry-After')))
                if e.code < 500 or attempt == attempts - 1:
                    raise ReadFailure(f'http_{e.code}') from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == attempts - 1:
                    raise ReadFailure('read_transport_failure') from None
            self.sleeper(2 ** attempt)
        raise ReadFailure('retry_exhausted')

    def json(self, url: str):
        body, headers = self.get(url)
        try:
            return json.loads(body), headers
        except (ValueError, UnicodeError):
            raise ReadFailure('invalid_json') from None

    def post(self, url: str, payload: dict, headers: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST', headers={
            'User-Agent': self.user_agent, 'Content-Type': 'application/json; charset=utf-8', **(headers or {})})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if not 200 <= r.status < 300:
                    raise UnknownDelivery('delivery_status_unknown')
                body = r.read(64000)
                try:
                    return json.loads(body) if body else {}
                except ValueError:
                    raise UnknownDelivery('delivery_response_invalid') from None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RetryDelivery('delivery_429', retry_after(e.headers.get('Retry-After'))) from None
            if 400 <= e.code < 500:
                raise ReadFailure(f'delivery_rejected_{e.code}') from None
            raise UnknownDelivery('delivery_server_response_uncertain') from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.gaierror, ConnectionRefusedError)):
                raise RetryDelivery('delivery_not_connected') from None
            raise UnknownDelivery('delivery_transport_uncertain') from None
        except (TimeoutError, OSError):
            raise UnknownDelivery('delivery_transport_uncertain') from None
