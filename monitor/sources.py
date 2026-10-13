from __future__ import annotations
import html, json, re, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from .model import Post, canonical, parse_time, iso
from .http import ReadFailure

SOURCES = {
    'rss': 'https://reset.onlyax.com/feed.xml',
    'feed': 'https://codex-reset.com/api/feed',
    'timeline': 'https://codex-reset.com/api/timeline',
    'openai': 'https://api.fxtwitter.com/2/profile/id:4398626122/statuses?count=100&with_replies=1',
}

def fresh(value: str, now: datetime, minutes: int = 30):
    delta = (now - parse_time(value)).total_seconds()
    if delta < -300 or delta > minutes * 60:
        raise ReadFailure('source_stale_or_future')

def discover(name: str, body: bytes, headers: dict, now: datetime) -> list[dict]:
    result = []
    if name == 'rss':
        root = ET.fromstring(body)
        if root.tag != 'rss':
            raise ReadFailure('invalid_rss')
        fresh(root.findtext('./channel/lastBuildDate') or '', now)
        for item in root.findall('./channel/item'):
            result.append({'url': item.findtext('link'), 'hint': html.unescape(item.findtext('description') or '')})
    elif name == 'openai':
        doc = json.loads(body)
        if not isinstance(doc, dict) or doc.get('code') != 200 or doc.get('stale') is True or doc.get('errors'):
            raise ReadFailure('official_timeline_unsuccessful')
        items = doc.get('results')
        if not isinstance(items, list):
            raise ReadFailure('official_timeline_schema')
        for item in items:
            if not isinstance(item, dict):
                raise ReadFailure('official_timeline_schema')
            author = item.get('author') or {}
            if str(author.get('id')) != '4398626122' or str(author.get('screen_name','')).lower() != 'openai' or item.get('reposted_by'):
                continue
            pid = str(item.get('id') or '')
            if not pid.isdigit():
                raise ReadFailure('official_timeline_schema')
            text = (item.get('raw_text') or {}).get('text') or item.get('text') or ''
            result.append({'url': f'https://x.com/openai/status/{pid}', 'hint': text})
    else:
        doc = json.loads(body)
        if not isinstance(doc, dict):
            raise ReadFailure('invalid_document')
        if headers.get('x-published-checked-at'):
            fresh(headers['x-published-checked-at'], now)
        if name == 'feed':
            if doc.get('stale') is not False:
                raise ReadFailure('feed_stale_or_unknown')
            fresh(doc.get('fetched_at') or '', now)
            items = doc.get('tweets')
            field = 'text'
        else:
            fresh(doc.get('updated_at') or '', now)
            items = doc.get('events')
            field = 'summary'
        if not isinstance(items, list):
            raise ReadFailure('invalid_items')
        for item in items:
            if not isinstance(item, dict):
                raise ReadFailure('invalid_item')
            result.append({'url': item.get('url'), 'hint': str(item.get(field) or '')})
    # Labels/probabilities never enter the classifier. Source text is only a
    # cheap candidate filter and must later be replaced with verified text.
    keep = []
    for c in result:
        if not re.search(r'reset|重置|propagat|landed across all accounts', c['hint'], re.I):
            continue
        try:
            author, pid = canonical(c['url'] or '')
        except ValueError:
            continue
        if author not in ('thsottiaux', 'openai'):
            continue
        keep.append({'id': pid, 'url': f'https://x.com/{author}/status/{pid}', 'source': name})
    return keep

def fx_post(raw: dict, expected_id: str, trusted_ids: dict[str, str]) -> Post:
    if not isinstance(raw, dict) or raw.get('code') != 200 or raw.get('stale') is True or raw.get('errors'):
        raise ReadFailure('fx_unsuccessful')
    t = raw.get('tweet') or raw.get('status')
    if not isinstance(t, dict) or str(t.get('id')) != expected_id:
        raise ReadFailure('fx_id_mismatch')
    a = t.get('author') or {}
    handle = str(a.get('screen_name') or '').lower()
    aid = str(a.get('id') or '')
    if not trusted_ids.get(handle) or trusted_ids[handle] != aid or t.get('reposted_by'):
        raise ReadFailure('fx_author_mismatch')
    text = (t.get('raw_text') or {}).get('text') or t.get('text')
    if not isinstance(text, str) or not text:
        raise ReadFailure('fx_empty_text')
    timestamp = t.get('created_timestamp')
    published = iso(datetime.fromtimestamp(timestamp, timezone.utc)) if isinstance(timestamp, (int, float)) else iso(parse_time(t['created_at']))
    parent = t.get('replying_to_status')
    reply = t.get('replying_to')
    if isinstance(reply, dict):
        parent = reply.get('status')
    quote = t.get('quote') or {}
    quote_id = str(quote['id']) if str((quote.get('author') or {}).get('id')) == aid and quote.get('id') else None
    if parent is not None and not str(parent).isdigit():
        raise ReadFailure('invalid_parent_id')
    return Post(expected_id, handle, aid, text, published, f'https://x.com/{handle}/status/{expected_id}', str(parent) if parent else None, quote_id, True)

class Verifier:
    def __init__(self, http, trusted_ids: dict):
        self.http, self.trusted_ids = http, trusted_ids
        self.cache = {}

    def post(self, pid: str, handle: str = 'thsottiaux') -> Post:
        if pid not in self.cache:
            raw, _ = self.http.json(f'https://api.fxtwitter.com/{handle}/status/{pid}')
            self.cache[pid] = fx_post(raw, pid, self.trusted_ids)
        return self.cache[pid]

    def context(self, post: Post) -> Post | None:
        # One lookup at a time; the engine also loads one further ancestor.
        # No quote-body concatenation and no conversation-wide crawl.
        ref = post.quote or post.parent
        if not ref:
            return None
        try:
            parent = self.post(ref, post.author)
        except ReadFailure as e:
            if str(e) == 'fx_author_mismatch':
                return None
            raise
        return parent if parent.author_id == post.author_id else None
