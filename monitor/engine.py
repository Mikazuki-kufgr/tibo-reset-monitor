from __future__ import annotations
import json, logging
from datetime import timedelta
from .model import canonical, parse_time
from .classify import classify_chain
from .sources import SOURCES, discover, Verifier
from .notify import enabled
from .http import RetryDelivery, UnknownDelivery, ReadFailure

log = logging.getLogger('monitor')

def poll(store, config, http, now, emit=print):
    verifier = Verifier(http, config['trusted_author_ids'])
    errors = 0
    for name in config.get('sources', ['rss', 'feed', 'timeline']):
        if not store.source_due(name, now):
            continue
        try:
            body, headers = http.get(SOURCES[name])
            candidates = discover(name, body, headers, now)
            store.add_candidates(candidates)
            store.health(name, now)
        except Exception as e:
            # Logs contain only public source keys and stable error categories.
            delay = e.delay if isinstance(e, RetryDelivery) else 300
            code = str(e) if isinstance(e, (ReadFailure, RetryDelivery)) else type(e).__name__
            store.health(name, now, code, delay)
            if name == 'openai' and isinstance(e, RetryDelivery):
                store.health('fxembed', now, code, delay)
            log.error('source=%s error=%s', name, code)
            errors += 1
    rows = store.pending(now, config.get('max_verifications_per_run', 20)) if store.source_due('fxembed', now) else []
    verified = []
    fx_limited = False
    for row in rows:
        try:
            handle, pid = canonical(row['url'])
            post = verifier.post(pid, handle)
            context = verifier.context(post)
            if context:
                verified.append((context, verifier.context(context)))
            verified.append((post, context))
        except Exception as e:
            delay = e.delay if isinstance(e, RetryDelivery) else 300
            store.postpone(row['id'], now, delay)
            code = str(e) if isinstance(e,(ReadFailure,RetryDelivery)) else type(e).__name__
            log.error('source=fxembed error=%s', code)
            errors += 1
            if isinstance(e, RetryDelivery):
                store.health('fxembed', now, code, delay)
                fx_limited = True
                break
    if verified and not fx_limited:
        store.health('fxembed', now)
    # Process announcements in chronological order, then dispatch latest state.
    done = set()
    for post, context in sorted(verified, key=lambda pair: parse_time(pair[0].published)):
        if post.id in done:
            continue
        done.add(post.id)
        decision = classify_chain(post, verifier.cache)
        result = store.record(post, decision, now, enabled(config), config.get('notify_banked', False), config.get('notification_ttl_hours', 24))
        emit(json.dumps(result, ensure_ascii=False))
    return errors

def dispatch(store, sender, now, claim_id, emit=print, clock=None):
    errors = 0
    for row in store.claimed(claim_id):
        before_errors = errors
        payload = json.loads(row['payload'])
        current = clock() if clock else now
        if parse_time(payload['expires_at']) <= current:
            store.delivery_result(row,'expired',current,'expired_before_delivery')
            continue
        try:
            sender.send(row['channel'], payload)
            store.delivery_result(row, 'sent', now)
            emit(json.dumps({'notice_id': row['notice_id'], 'channel': row['channel'], 'status': 'accepted'}, ensure_ascii=False))
        except RetryDelivery as e:
            store.delivery_result(row, 'pending', now, str(e), e.delay)
            errors += 1
        except ReadFailure as e:
            store.delivery_result(row, 'failed', now, str(e))
            errors += 1
        except Exception as e:
            # Receiver acceptance and phone/user receipt are different facts.
            store.delivery_result(row, 'unknown', now, type(e).__name__)
            errors += 1
        if errors > before_errors:
            log.error('channel=%s delivery_failure_recorded', row['channel'])
    return errors
