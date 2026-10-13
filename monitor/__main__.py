from __future__ import annotations
import argparse, json, logging, os, pathlib, sys, uuid
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from .store import Store
from .model import Post, parse_time
from .classify import classify_chain
from .http import HTTP
from .notify import Sender, enabled
from .private import load_channels
from .engine import poll, dispatch

@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('another_monitor_process_holds_state_lock') from None
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def config_read(path):
    cfg = json.loads(path.read_text('utf-8-sig'))
    if not isinstance(cfg, dict) or not isinstance(cfg.get('trusted_author_ids'), dict):
        raise ValueError('invalid_config')
    if set(cfg.get('sources', [])) - {'rss', 'feed', 'timeline', 'openai'}:
        raise ValueError('unknown_source')
    if not 1 <= int(cfg.get('max_verifications_per_run', 20)) <= 100:
        raise ValueError('invalid_verification_budget')
    if not 1 <= float(cfg.get('notification_ttl_hours', 24)) <= 72:
        raise ValueError('invalid_notification_ttl')
    return cfg

def replay(path, cfg):
    doc = json.loads(path.read_text('utf-8'))
    posts = [Post(**p) for p in doc['posts']]
    posts.sort(key=lambda p: parse_time(p.published))
    by_id = {p.id: p for p in posts}
    start = parse_time(posts[0].published) - timedelta(seconds=1)
    store = Store(':memory:', start)
    for post in posts:
        context = by_id.get(post.quote or post.parent)
        d = classify_chain(post, by_id)
        now = parse_time(post.published) + timedelta(seconds=1)
        r = store.record(post, d, now, ['preview'], cfg.get('notify_banked', False))
        r['expected'] = doc.get('expected', {}).get(post.id)
        print(json.dumps(r, ensure_ascii=False))
    for r in store.db.execute("SELECT payload FROM notices WHERE id IN (SELECT notice_id FROM deliveries WHERE status='pending')"):
        print(json.loads(r[0])['body'])
    store.close()
    return 0

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Quiet Codex/Work reset monitor; all channels disabled by default')
    parser.add_argument('command', choices=['run', 'poll', 'dispatch', 'status', 'replay','daemon','stop','service-status','channel-test'])
    parser.add_argument('--config', default='config.example.json')
    parser.add_argument('--state', default='runtime/state.sqlite3')
    parser.add_argument('--dry-run', action='store_true', help='isolated memory state; no notification calls or live-state writes')
    parser.add_argument('--send', action='store_true', help='explicitly dispatch configured channels')
    parser.add_argument('--fixtures', default='fixtures/announcements.json')
    parser.add_argument('--claim-only', action='store_true')
    parser.add_argument('--claim-id')
    parser.add_argument('--private-channels')
    parser.add_argument('--interval',type=int,default=300)
    parser.add_argument('--channel',choices=['windows','ntfy','bark','webhook','qq_spool'])
    args = parser.parse_args()
    if args.dry_run and args.send or args.command == 'replay' and args.send:
        parser.error('dry-run/replay cannot send')
    if args.claim_only and args.command != 'dispatch':
        parser.error('claim-only requires dispatch')
    if args.send and args.command not in ('run','dispatch','channel-test','daemon'):
        parser.error('send requires run or dispatch')
    if args.command=='daemon' and (not args.send or args.dry_run):
        parser.error('daemon requires explicit --send; dry-run uses run')
    cfg = config_read(pathlib.Path(args.config))
    if args.command == 'replay':
        return replay(pathlib.Path(args.fixtures), cfg)
    now = datetime.now(timezone.utc)
    state_path = pathlib.Path(args.state).resolve()
    runtime = state_path.parent
    private_path=pathlib.Path(args.private_channels).resolve() if args.private_channels else runtime/'channels.private.json'
    if args.command in ('run','dispatch','channel-test') and not args.dry_run:
        load_channels(private_path)
    cfg['user_agent'] = cfg.get('user_agent') or 'tibo-reset-monitor/0.1 (private evaluation; contact not configured)'
    if 'contact not configured' in cfg['user_agent'] and args.send:
        raise ValueError('configure_identifying_user_agent_before_live_notifications')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command in ('daemon','stop','service-status'):
        from .service import daemon,stop,status
        if args.command=='daemon':
            if args.interval<60:raise ValueError('interval_must_be_at_least_60_seconds')
            daemon(pathlib.Path(args.config).resolve(),state_path,args.interval,private_path)
            return 0
        print(json.dumps(stop(runtime) if args.command=='stop' else status(runtime),ensure_ascii=False,indent=2))
        return 0
    if args.command=='channel-test':
        if not args.send or args.dry_run or not args.channel:
            parser.error('channel-test requires an explicit channel and --send')
        payload={'schema':1,'id':'connection-test-'+str(uuid.uuid4()),'kind':'TEST','title':'监测工具连接测试（非额度重置）','body':'这是一条连接测试，不表示额度重置。正常运行只提醒已核验的重置事件。','url':'https://github.com/Mikazuki-kufgr/tibo-reset-monitor'}
        receipt=Sender(cfg,HTTP(cfg['user_agent']),runtime).send(args.channel,payload)
        print(json.dumps({'channel':args.channel,'status':'provider_accepted','receipt':receipt},ensure_ascii=False))
        return 0

    def execute():
        marker = runtime / '.initialized'
        if not args.dry_run and marker.exists() and not state_path.exists():
            raise ValueError('state_lost_restore_backup_do_not_rebootstrap')
        if args.command == 'status' and not args.dry_run and not state_path.exists():
            print('{"state":"not_initialized"}')
            return 0
        store = Store(':memory:' if args.dry_run else str(state_path), now)
        if not args.dry_run:
            marker.write_text('schema=1\n', encoding='ascii')
            handler = RotatingFileHandler(runtime / 'monitor.log', maxBytes=1_000_000, backupCount=3, encoding='utf-8')
            logging.getLogger().addHandler(handler)
        try:
            http = HTTP(cfg['user_agent'])
            errors = 0
            if args.command in ('run', 'poll'):
                store.recover()
                errors += poll(store, cfg, http, now)
            if args.command == 'dispatch' and not args.claim_only and not args.send:
                print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
                return 0
            if args.claim_only:
                if args.dry_run:
                    parser.error('claim-only is a durable preparation command')
                store.recover()
                print('claimed', store.claim(now, args.claim_id or str(uuid.uuid4())))
            elif args.send and args.command in ('run', 'dispatch'):
                cid = args.claim_id or str(uuid.uuid4())
                if not args.claim_id:
                    store.recover()
                    store.claim(now, cid)
                errors += dispatch(store, Sender(cfg, http, runtime), datetime.now(timezone.utc), cid, clock=lambda:datetime.now(timezone.utc))
            if args.command == 'status' or args.dry_run:
                print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
            return 2 if errors else 0
        finally:
            store.close()
    if args.dry_run:
        return execute()
    with lock(runtime / 'state.lock'):
        return execute()

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        # Avoid printing endpoint URLs, channel secrets, or arbitrary bodies.
        safe = str(e) if isinstance(e, ValueError) and str(e).replace('_', '').isalnum() else type(e).__name__
        print('monitor_failed:', safe, file=sys.stderr)
        sys.exit(1)
