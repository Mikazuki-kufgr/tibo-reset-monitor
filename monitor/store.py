from __future__ import annotations
import json, sqlite3, uuid, hashlib, re, pathlib
from datetime import datetime, timedelta
from .model import Post, Decision, parse_time, iso, beijing

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health(source TEXT PRIMARY KEY,last_ok TEXT,last_error TEXT,retry_at TEXT);
CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,url TEXT NOT NULL,sources TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,retry_at TEXT);
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY,event_id TEXT,kind TEXT,reason TEXT,published TEXT);
CREATE TABLE IF NOT EXISTS aliases(id TEXT PRIMARY KEY,event_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,family TEXT,scope TEXT,stage TEXT,published TEXT,effective_at TEXT,url TEXT);
CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,event_id TEXT,kind TEXT,payload TEXT,created TEXT,expires TEXT,UNIQUE(event_id,kind));
CREATE TABLE IF NOT EXISTS deliveries(notice_id TEXT,channel TEXT,status TEXT,attempts INTEGER DEFAULT 0,retry_at TEXT,last_error TEXT,claim_id TEXT,PRIMARY KEY(notice_id,channel));
'''

class Store:
    def __init__(self, path: str, now: datetime):
        existed = path != ':memory:' and pathlib.Path(path).exists()
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        if existed:
            try:
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.DatabaseError:
                self.db.close()
                raise ValueError('state_corrupt') from None
            if not {'meta', 'events', 'deliveries'} <= tables:
                self.db.close()
                raise ValueError('state_missing_schema_do_not_rebootstrap')
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                self.db.close()
                raise ValueError('state_corrupt')
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('started_at', iso(now)))
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('schema', '1'))
        schema = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
        if schema != '1':
            raise ValueError('unsupported_state_schema')

    def recover(self):
        with self.db:
            # A crash after provider acceptance is not safely retryable.
            self.db.execute("UPDATE deliveries SET status='unknown',last_error='interrupted_while_sending' WHERE status='sending'")

    def close(self):
        self.db.close()

    def health(self, source, now, error=None, delay=0):
        with self.db:
            if error:
                self.db.execute('INSERT INTO health(source,last_error,retry_at) VALUES (?,?,?) ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error,retry_at=excluded.retry_at', (source, error, iso(now + timedelta(seconds=max(60, delay)))))
            else:
                self.db.execute('INSERT INTO health(source,last_ok,last_error,retry_at) VALUES (?,?,NULL,?) ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok,last_error=NULL,retry_at=excluded.retry_at', (source, iso(now), iso(now + timedelta(seconds=60))))

    def source_due(self, source, now):
        r = self.db.execute('SELECT retry_at FROM health WHERE source=?', (source,)).fetchone()
        return not r or not r[0] or parse_time(r[0]) <= now

    def add_candidates(self, candidates):
        with self.db:
            for c in candidates:
                r = self.db.execute('SELECT sources FROM candidates WHERE id=?', (c['id'],)).fetchone()
                sources = set(json.loads(r[0])) if r else set()
                sources.add(c['source'])
                known = self.db.execute('SELECT 1 FROM posts WHERE id=?', (c['id'],)).fetchone()
                self.db.execute('INSERT INTO candidates(id,url,sources,status) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET sources=excluded.sources', (c['id'], c['url'], json.dumps(sorted(sources)), 'processed' if known else 'pending'))

    def pending(self, now, limit=20):
        return self.db.execute("SELECT * FROM candidates WHERE status='pending' AND (retry_at IS NULL OR retry_at<=?) ORDER BY length(id) DESC,id DESC LIMIT ?", (iso(now), limit)).fetchall()

    def postpone(self, pid, now, delay=300):
        with self.db:
            self.db.execute('UPDATE candidates SET attempts=attempts+1,retry_at=? WHERE id=?', (iso(now + timedelta(seconds=delay)), pid))

    def record(self, post: Post, decision: Decision, now: datetime, channels: list[str], banked=False, ttl_hours=24) -> dict:
        started = parse_time(self.db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()[0])
        ptime = parse_time(post.published)
        with self.db:
            if self.db.execute('SELECT 1 FROM posts WHERE id=?', (post.id,)).fetchone():
                self.db.execute("UPDATE candidates SET status='processed' WHERE id=?", (post.id,))
                return {'post_id': post.id, 'result': 'duplicate_post'}
            family = 'banked' if decision.kind == 'BANKED' else 'global'
            actionable = decision.kind in ('UPCOMING', 'COMPLETED', 'BANKED')
            event_id = None
            if actionable:
                explicit_new = bool(re.search(r'\banother\b|\bsecond\b|再次|第二次', post.text, re.I))
                context_ref = None if explicit_new else decision.context_id
                refs = [post.id, context_ref]
                for ref in filter(None, refs):
                    r = self.db.execute('SELECT e.* FROM aliases a JOIN events e ON e.id=a.event_id WHERE a.id=? AND e.family=?', (ref, family)).fetchone()
                    if r:
                        event_id = r['id']; break
                if not event_id and not explicit_new:
                    # Conservative correlation for announcements cross-posted
                    # without a reply/quote link. Never downgrade completed.
                    candidates = self.db.execute('SELECT * FROM events WHERE family=? AND scope=? ORDER BY published DESC', (family, decision.scope)).fetchall()
                    matches = []
                    for e in candidates:
                        age = abs((ptime - parse_time(e['published'])).total_seconds())
                        same_time = decision.effective_at and decision.effective_at == e['effective_at']
                        transition = decision.kind == 'COMPLETED' and e['stage'] == 'UPCOMING' and age <= 72*3600
                        same_stage = decision.kind == e['stage'] and age <= 6*3600
                        if same_time or transition or same_stage:
                            matches.append(e['id'])
                    if len(matches) == 1:
                        event_id = matches[0]
                event_id = event_id or str(uuid.uuid4())
                old = self.db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
                stage = decision.kind
                if old and old['stage'] == 'COMPLETED' and stage == 'UPCOMING':
                    stage = 'COMPLETED'
                self.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET stage=excluded.stage,url=excluded.url,effective_at=coalesce(excluded.effective_at,events.effective_at)', (event_id, family, decision.scope, stage, post.published, decision.effective_at, post.url))
                # Alias only validated relevant context, not arbitrary parent.
                for ref in {post.id, context_ref} - {None}:
                    self.db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (ref, event_id))
                if stage == 'COMPLETED':
                    self.db.execute("UPDATE deliveries SET status='superseded' WHERE status='pending' AND notice_id IN (SELECT id FROM notices WHERE event_id=? AND kind='UPCOMING')", (event_id,))
            self.db.execute('INSERT INTO posts VALUES (?,?,?,?,?)', (post.id, event_id, decision.kind, decision.reason, post.published))
            self.db.execute("UPDATE candidates SET status='processed' WHERE id=?", (post.id,))
            result = 'quiet'
            if actionable:
                suppressed = ('baseline' if ptime <= started else 'future_timestamp' if ptime > now + timedelta(minutes=5) else 'too_old' if now - ptime > timedelta(hours=ttl_hours) else 'announced_time_passed' if decision.kind == 'UPCOMING' and decision.effective_at and parse_time(decision.effective_at) <= now else 'banked_disabled' if decision.kind == 'BANKED' and not banked else 'state_already_completed' if stage != decision.kind else None)
                nid = hashlib.sha256(f'{event_id}:{decision.kind}'.encode()).hexdigest()[:24]
                labels = {'UPCOMING':'即将全局重置', 'COMPLETED':'官方确认全局重置已完成', 'BANKED':'储存式重置券发放（需手动使用）'}
                src = self.db.execute('SELECT sources FROM candidates WHERE id=?',(post.id,)).fetchone()
                source_map = {'rss':'https://reset.onlyax.com/', 'feed':'https://codex-reset.com/', 'timeline':'https://codex-reset.com/', 'openai':'https://docs.fxembed.com/'}
                links = sorted({source_map[s] for s in json.loads(src[0]) if s in source_map}) if src else ['https://docs.fxembed.com/']
                body = '\n'.join([labels[decision.kind], f'原帖发布：{beijing(post.published)}', decision.time_note, f'原帖：{post.url}', '数据来源：'+' / '.join(links)+'；原文核验：FxEmbed'])
                payload = {'schema': 1, 'id': nid, 'event_id': event_id, 'kind': decision.kind, 'title': labels[decision.kind], 'body': body, 'url': post.url, 'published_at': post.published, 'expires_at': iso(ptime + timedelta(hours=ttl_hours)), 'sources': links, 'evidence': decision.reason}
                cur = self.db.execute('INSERT OR IGNORE INTO notices VALUES (?,?,?,?,?,?)', (nid, event_id, decision.kind, json.dumps(payload, ensure_ascii=False), iso(now), iso(ptime + timedelta(hours=ttl_hours))))
                if cur.rowcount:
                    for ch in channels:
                        self.db.execute('INSERT INTO deliveries(notice_id,channel,status) VALUES (?,?,?)', (nid, ch, suppressed or 'pending'))
                    result = suppressed or ('queued' if channels else 'no_channel')
                else:
                    result = suppressed or 'duplicate_event_stage'
            return {'post_id': post.id, 'kind': decision.kind, 'reason': decision.reason, 'result': result, 'event_id': event_id, 'time_note': decision.time_note}

    def claim(self, now, claim_id):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='expired' WHERE status='pending' AND notice_id IN (SELECT id FROM notices WHERE expires<?)", (iso(now),))
            rows = self.db.execute("SELECT d.notice_id,d.channel FROM deliveries d JOIN notices n ON n.id=d.notice_id WHERE d.status='pending' AND (d.retry_at IS NULL OR d.retry_at<=?) ORDER BY n.created LIMIT 10", (iso(now),)).fetchall()
            for r in rows:
                self.db.execute("UPDATE deliveries SET status='sending',claim_id=? WHERE notice_id=? AND channel=?", (claim_id, r['notice_id'], r['channel']))
        return len(rows)

    def claimed(self, claim_id):
        return self.db.execute("SELECT d.*,n.payload FROM deliveries d JOIN notices n ON n.id=d.notice_id WHERE d.status='sending' AND d.claim_id=?", (claim_id,)).fetchall()

    def delivery_result(self, row, status, now, error=None, delay=60):
        attempts = row['attempts'] + 1
        if status == 'pending' and attempts >= 5:
            status = 'failed'
        with self.db:
            self.db.execute('UPDATE deliveries SET status=?,attempts=?,retry_at=?,last_error=? WHERE notice_id=? AND channel=?', (status, attempts, iso(now + timedelta(seconds=max(delay, 60 * 2 ** min(attempts, 8)))), error, row['notice_id'], row['channel']))

    def summary(self):
        return {table: [dict(r) for r in self.db.execute(sql)] for table,sql in {
            'health':'SELECT * FROM health', 'events':'SELECT * FROM events',
            'deliveries':'SELECT notice_id,channel,status,attempts,last_error FROM deliveries',
            'candidates':'SELECT status,count(*) AS count FROM candidates GROUP BY status'}.items()}
