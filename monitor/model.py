from __future__ import annotations
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

UTC = timezone.utc
BJT = timezone(timedelta(hours=8), '北京时间')

def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        result = parsedate_to_datetime(value)
    if result.tzinfo is None:
        raise ValueError('timezone_missing')
    return result.astimezone(UTC)

def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()

def beijing(value: str) -> str:
    return parse_time(value).astimezone(BJT).strftime('%Y-%m-%d %H:%M:%S 北京时间')

def canonical(url: str) -> tuple[str, str]:
    m = re.fullmatch(r'https://(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)/status/(\d+)(?:\?[^#]*)?', url)
    if not m:
        raise ValueError('untrusted_original_url')
    return m[1].lower(), m[2]

@dataclass
class Post:
    id: str
    author: str
    author_id: str
    text: str
    published: str
    url: str
    parent: str | None = None
    quote: str | None = None
    verified: bool = False

    def to_dict(self):
        return asdict(self)

@dataclass
class Decision:
    kind: str = 'IGNORE'
    reason: str = 'ordinary_post'
    scope: str = 'codex_work'
    time_note: str = '原帖没有精确重置时间；不提供截止时间'
    effective_at: str | None = None
    context_id: str | None = None

    def to_dict(self):
        return asdict(self)
