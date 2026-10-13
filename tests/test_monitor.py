from __future__ import annotations
import unittest, tempfile, pathlib, json, copy, io, urllib.error, socket, os
from datetime import datetime, timezone, timedelta
from monitor.model import Post, Decision, parse_time, beijing, canonical, iso
from monitor.classify import classify, classify_chain, timing
from monitor.store import Store
from monitor.sources import discover, fx_post, Verifier
from monitor.http import HTTP, ReadFailure, RetryDelivery, UnknownDelivery, retry_after
from monitor.notify import Sender
from monitor.engine import poll, dispatch
from monitor.__main__ import lock, config_read

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
AID = '1953337039510003712'

def post(pid='1', text='Global reset by EOD.', at=None, parent=None, quote=None, verified=True):
    return Post(pid, 'thsottiaux', AID, text, iso(at or NOW), 'https://x.com/thsottiaux/status/'+pid, parent, quote, verified)

def fx(p):
    return {'code':200,'tweet':{'id':p.id,'text':p.text,'raw_text':{'text':p.text},'author':{'screen_name':p.author,'id':p.author_id},'created_timestamp':parse_time(p.published).timestamp(),'replying_to_status':p.parent}}

class FakeHTTP:
    def __init__(self, feed=None, posts=None):
        self.feed = feed or {'fetched_at':iso(NOW),'stale':False,'tweets':[]}
        self.posts = posts or {}
        self.bad_source = False; self.bad_fx = False; self.calls=[]
    def get(self, url):
        self.calls.append(url)
        if self.bad_source:
            raise ReadFailure('offline')
        return json.dumps(self.feed).encode(), {}
    def json(self, url):
        self.calls.append(url)
        if self.bad_fx:
            raise ReadFailure('offline')
        pid=url.rsplit('/',1)[-1]
        return fx(self.posts[pid]), {}
    def post(self, url, payload, headers=None):
        self.calls.append((url,payload,headers))
        return {'id':'mock-receipt','code':200,'accepted_id':payload.get('id')}

class FakeSender:
    def __init__(self, failure=None):
        self.calls=[];self.failure=failure
    def send(self,ch,payload):
        self.calls.append((ch,payload))
        if self.failure:
            raise self.failure

class MonitorTests(unittest.TestCase):
    def setUp(self):
        temp_root = pathlib.Path(os.environ.get('RESET_TEST_TEMP_ROOT',str(ROOT/'work'/'tests')))
        temp_root.mkdir(parents=True,exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.assertTrue(pathlib.Path(self.tmp.name).resolve().is_relative_to(temp_root.resolve()))
        self.addCleanup(self.tmp.cleanup)
        self.path = str(pathlib.Path(self.tmp.name)/'state.sqlite3')
        self.cfg = config_read(ROOT/'config.example.json')
        self.cfg['sources']=['feed'];self.cfg['channels']['qq_spool']['enabled']=True
    def store(self):
        s=Store(self.path,NOW-timedelta(seconds=1));self.addCleanup(s.close);return s
    def record(self,s,p,ctx=None,now=NOW):
        return s.record(p,classify(p,ctx),now,['fake'])

    def test_real_and_synthetic_fixture_matrix(self):
        doc=json.loads((ROOT/'fixtures/announcements.json').read_text('utf-8'))
        posts={p['id']:Post(**p) for p in doc['posts']}
        for pid,p in posts.items():
            with self.subTest(pid=pid,text=p.text):
                d=classify(p,posts.get(p.quote or p.parent))
                self.assertEqual(d.kind,doc['expected'][pid])

    def test_bare_completion_requires_correct_parent_and_banked_stays_banked(self):
        bare=post('2','Confirmed landed across all accounts.',parent='1')
        self.assertNotEqual(classify(bare).kind,'COMPLETED')
        self.assertEqual(classify(bare,post('1',"Loading a banked reset in everyone's paid accounts.")).kind,'BANKED')
        self.assertEqual(classify(post('3','Done!',parent='1'),post()).kind,'COMPLETED')
        other=post();other.author_id='other'
        self.assertNotEqual(classify(bare,other).kind,'COMPLETED')
        self.assertNotEqual(classify(bare,post(at=NOW-timedelta(days=4))).kind,'COMPLETED')

    def test_two_hop_reply_chain_preserves_banked_scope(self):
        grant=post('1',"Loading a banked reset in everyone's paid accounts.")
        follow=post('2','Will be there by EOD PST.',NOW+timedelta(minutes=1),parent='1')
        landed=post('3','Confirmed landed across all accounts.',NOW+timedelta(minutes=2),parent='2')
        self.assertEqual(classify_chain(landed,{p.id:p for p in [grant,follow,landed]}).kind,'BANKED')
        s=self.store();h=FakeHTTP({'fetched_at':iso(NOW+timedelta(minutes=3)),'stale':False,'tweets':[{'url':landed.url,'text':landed.text}]},{p.id:p for p in [grant,follow,landed]})
        self.assertEqual(poll(s,self.cfg,h,NOW+timedelta(minutes=3),lambda x:None),0)
        self.assertNotIn('global',[e['family'] for e in s.summary()['events']])

    def test_unverified_posts_and_openai_wrong_product_are_quiet(self):
        self.assertEqual(classify(post(verified=False)).kind,'REVIEW')
        p=post(text='Global reset landing tomorrow for all ChatGPT users.');p.author='openai'
        self.assertEqual(classify(p).kind,'REVIEW')
        p.text='Global reset landing tomorrow for all Codex and ChatGPT Work users.'
        self.assertEqual(classify(p).kind,'UPCOMING')
        self.assertEqual(classify(post(text='Global reset by EOD. We could improve product performance.')).kind,'UPCOMING')

    def test_baseline_restart_and_no_old_notification(self):
        s=Store(self.path,NOW)
        p=post(at=NOW-timedelta(hours=2));r=self.record(s,p)
        self.assertEqual(r['result'],'baseline');self.assertEqual(s.claim(NOW,'a'),0)
        s.close()
        s=Store(self.path,NOW+timedelta(minutes=5));self.addCleanup(s.close)
        self.assertEqual(self.record(s,p)['result'],'duplicate_post')
        self.assertEqual(self.record(s,post('2',at=NOW+timedelta(minutes=2)),now=NOW+timedelta(minutes=5))['result'],'duplicate_event_stage')

    def test_same_post_two_sources_and_cross_platform_same_event(self):
        s=self.store()
        s.add_candidates([{'id':'1','url':post().url,'source':'rss'},{'id':'1','url':post().url,'source':'feed'}])
        self.assertEqual(len(s.pending(NOW)),1)
        self.assertEqual(set(json.loads(s.pending(NOW)[0]['sources'])),{'rss','feed'})
        self.assertEqual(self.record(s,post())['result'],'queued')
        same=post('2',text='We will reset all Codex limits tomorrow.',at=NOW+timedelta(minutes=2));same.author='openai';same.author_id='official-id'
        self.assertEqual(self.record(s,same,now=NOW+timedelta(minutes=3))['result'],'duplicate_event_stage')
        self.assertEqual(s.db.execute('SELECT count(*) FROM notices').fetchone()[0],1)
        s.add_candidates([{'id':'1','url':post().url,'source':'timeline'}])
        self.assertEqual(s.db.execute("SELECT status FROM candidates WHERE id='1'").fetchone()[0],'processed')

    def test_completion_updates_state_supersedes_pending_and_no_downgrade(self):
        s=self.store();start=post();self.record(s,start)
        finish=post('2','Reset all propagated.',NOW+timedelta(minutes=3),quote='1')
        r=self.record(s,finish,start,NOW+timedelta(minutes=4))
        self.assertEqual(r['result'],'queued')
        self.assertEqual(s.summary()['events'][0]['stage'],'COMPLETED')
        self.assertIn('superseded',[d['status'] for d in s.summary()['deliveries']])
        late=post('3','Global reset by EOD.',NOW+timedelta(minutes=5),quote='1')
        self.assertEqual(self.record(s,late,start,NOW+timedelta(minutes=6))['result'],'state_already_completed')
        self.assertEqual(s.summary()['events'][0]['stage'],'COMPLETED')

    def test_notices_for_announcement_then_completion_only_once_each(self):
        s=self.store();p=post();self.record(s,p);s.claim(NOW,'a')
        sender=FakeSender();self.assertEqual(dispatch(s,sender,NOW,'a',lambda x:None),0)
        done=post('2','Reset all propagated.',NOW+timedelta(hours=2),quote='1')
        self.record(s,done,p,NOW+timedelta(hours=2));s.claim(NOW+timedelta(hours=2),'b')
        dispatch(s,sender,NOW+timedelta(hours=2),'b',lambda x:None)
        self.record(s,post('3','Reset all propagated.',NOW+timedelta(hours=3)),now=NOW+timedelta(hours=3))
        self.assertEqual(len(sender.calls),2)
        self.assertEqual([c[1]['kind'] for c in sender.calls],['UPCOMING','COMPLETED'])

    def test_banked_default_off_optional_on_and_parent_followup_dedup(self):
        s=self.store();p=post(text="Loading a banked reset in everyone's paid accounts.")
        self.assertEqual(self.record(s,p)['result'],'banked_disabled')
        self.assertEqual(s.claim(NOW,'a'),0)
        s2=Store(':memory:',NOW-timedelta(seconds=1));self.addCleanup(s2.close)
        self.assertEqual(s2.record(p,classify(p),NOW,['fake'],True)['result'],'queued')
        reply=post('2','Confirmed landed across all accounts.',parent='1')
        self.assertEqual(s2.record(reply,classify(reply,p),NOW,['fake'],True)['result'],'duplicate_event_stage')
        self.assertEqual(s2.summary()['events'][0]['family'],'banked')

    def test_offline_source_and_verification_failure_restore_without_advancing(self):
        s=self.store();p=post();h=FakeHTTP({'fetched_at':iso(NOW),'stale':False,'tweets':[{'url':p.url,'text':p.text}]},{'1':p})
        h.bad_source=True
        self.assertGreater(poll(s,self.cfg,h,NOW,lambda x:None),0)
        self.assertEqual(len(s.pending(NOW)),0)
        h.bad_source=False;h.bad_fx=True;h.feed['fetched_at']=iso(NOW+timedelta(minutes=6))
        self.assertGreater(poll(s,self.cfg,h,NOW+timedelta(minutes=6),lambda x:None),0)
        self.assertEqual(len(s.pending(NOW+timedelta(minutes=12))),1)
        h.bad_fx=False;h.feed['fetched_at']=iso(NOW+timedelta(minutes=12))
        self.assertEqual(poll(s,self.cfg,h,NOW+timedelta(minutes=12),lambda x:None),0)
        self.assertEqual(len(s.pending(NOW+timedelta(minutes=13))),0)
        self.assertEqual(len(s.summary()['deliveries']),1)

    def test_self_reply_not_in_rss_discovered_in_feed(self):
        s=self.store();parent=post('1','Day 6/ Reset',NOW-timedelta(minutes=2));reply=post('2',parent='1')
        h=FakeHTTP({'fetched_at':iso(NOW),'stale':False,'tweets':[{'url':reply.url,'text':reply.text}]},{'1':parent,'2':reply})
        self.assertEqual(poll(s,self.cfg,h,NOW,lambda x:None),0)
        kinds=[r[0] for r in s.db.execute('SELECT kind FROM posts')]
        self.assertIn('UPCOMING',kinds)
        self.assertEqual(s.summary()['deliveries'][0]['status'],'pending')

    def test_channel_failure_retry_and_other_channel_independent(self):
        s=self.store();p=post();s.record(p,classify(p),NOW,['bad','good'])
        class Multi:
            def send(self,ch,payload):
                if ch=='bad':raise RetryDelivery('delivery_429',600)
        s.claim(NOW,'a');self.assertEqual(dispatch(s,Multi(),NOW,'a',lambda x:None),1)
        statuses={r['channel']:r['status'] for r in s.summary()['deliveries']}
        self.assertEqual(statuses,{'bad':'pending','good':'sent'})
        self.assertEqual(s.claim(NOW+timedelta(minutes=5),'b'),0)
        self.assertEqual(s.claim(NOW+timedelta(minutes=11),'b'),1)
        dispatch(s,FakeSender(),NOW+timedelta(minutes=11),'b',lambda x:None)
        self.assertEqual([r['status'] for r in s.summary()['deliveries']],['sent','sent'])

    def test_retry_limit_unknown_and_crash_recovery_do_not_resend(self):
        s=self.store();self.record(s,post());sender=FakeSender(RetryDelivery('429',60))
        for i in range(5):
            now=NOW+timedelta(hours=i);cid=str(i);s.claim(now,cid);dispatch(s,sender,now,cid,lambda x:None)
        self.assertEqual(s.summary()['deliveries'][0]['status'],'failed')
        s.record(post('2','Another global reset by EOD.',NOW+timedelta(hours=6)),Decision('UPCOMING','test'),NOW+timedelta(hours=6),['fake'])
        s.claim(NOW+timedelta(hours=6),'crash');s.recover()
        self.assertIn('unknown',[r['status'] for r in s.summary()['deliveries']])
        self.assertEqual(s.claim(NOW+timedelta(hours=7),'after'),0)

    def test_age_expiry_future_and_separate_next_event(self):
        s=self.store();self.record(s,post());self.assertEqual(s.claim(NOW+timedelta(hours=25),'a'),0)
        self.assertEqual(s.summary()['deliveries'][0]['status'],'expired')
        second=post('2','Another global reset by EOD.',NOW+timedelta(hours=26))
        self.assertEqual(s.record(second,classify(second),NOW+timedelta(hours=26),['fake'])['result'],'queued')
        self.assertEqual(len(s.summary()['events']),2)
        future=post('3','Another global reset by EOD.',NOW+timedelta(days=4))
        self.assertEqual(s.record(future,classify(future),NOW,['fake'])['result'],'future_timestamp')
        old_context=post('10','Global reset by EOD.',NOW-timedelta(days=5))
        s.record(old_context,classify(old_context),NOW,['fake'])
        fresh=post('11','Another global reset by EOD.',NOW+timedelta(days=5),quote='10')
        r=s.record(fresh,classify(fresh,old_context),NOW+timedelta(days=5),['fake'])
        self.assertEqual(r['result'],'queued')
        past=post('12','Another global reset at 2026-10-11T11:00:00Z.',NOW)
        r=s.record(past,classify(past),NOW,['fake'])
        self.assertEqual(r['result'],'announced_time_passed')
        self.assertEqual(classify(past).kind,'UPCOMING')

    def test_fx_rate_limit_cools_down_whole_verification_host(self):
        s=self.store();posts=[post('1'),post('2')]
        h=FakeHTTP({'fetched_at':iso(NOW),'stale':False,'tweets':[{'url':p.url,'text':p.text} for p in posts]})
        def limited(url):
            h.calls.append(url);raise RetryDelivery('read_429',900)
        h.json=limited
        self.assertGreater(poll(s,self.cfg,h,NOW,lambda x:None),0)
        self.assertEqual(sum('status/' in x for x in h.calls),1)
        self.assertFalse(s.source_due('fxembed',NOW+timedelta(minutes=10)))

    def test_http_read_retry_and_post_uncertainty(self):
        class Opener:
            calls=0
            def open(self,*args,**kwargs):
                self.calls+=1;raise urllib.error.HTTPError('redacted',503,'service unavailable',{},None)
        sleeps=[];h=HTTP('test',sleeper=sleeps.append);o=Opener();h.opener=o
        with self.assertRaises(ReadFailure):h.get('https://api.fxtwitter.com/thsottiaux/status/1')
        self.assertEqual(o.calls,3);self.assertEqual(sleeps,[1,2])
        o.calls=0;sleeps.clear()
        with self.assertRaises(ReadFailure):h.get('https://codex-reset.com/api/feed')
        self.assertEqual(o.calls,1);self.assertEqual(sleeps,[])
        with self.assertRaises(UnknownDelivery):h.post('https://example.test',{'title':'public event'})

    def test_source_schema_stale_and_predictions_never_promoted(self):
        for body in [b'<html>error</html>',b'{',json.dumps({'stale':True,'fetched_at':iso(NOW),'tweets':[]}).encode(),json.dumps({'stale':False,'fetched_at':iso(NOW-timedelta(days=1)),'tweets':[]}).encode()]:
            with self.subTest(body=body):
                with self.assertRaises(Exception):discover('feed',body,{},NOW)
        docs={'stale':False,'fetched_at':iso(NOW),'tweets':[{'url':post().url,'text':'Global reset by EOD.'},{'url':'https://evil.example/status/2','text':'Global reset'},{'url':'https://x.com/random/status/3','text':'100% reset odds'},{'url':'https://x.com/thsottiaux/status/4','text':'New faster model'}]}
        self.assertEqual([p['id'] for p in discover('feed',json.dumps(docs).encode(),{},NOW)],['1'])
        with self.assertRaises(ReadFailure):discover('feed',json.dumps(docs).encode(),{'x-published-checked-at':iso(NOW-timedelta(days=1))},NOW)

    def test_openai_targeted_discovery_author_filter(self):
        official={'id':'3','text':'Global reset landing tomorrow for all Codex users.','author':{'id':'4398626122','screen_name':'OpenAI'}}
        impostor={**official,'id':'4','author':{'id':'other','screen_name':'OpenAI'}}
        ordinary={**official,'id':'5','text':'New model ships today.'}
        result=discover('openai',json.dumps({'code':200,'results':[official,impostor,ordinary],'cursor':{'bottom':'unused'}}).encode(),{},NOW)
        self.assertEqual([x['id'] for x in result],['3'])

    def test_author_identity_reposts_raw_text_and_quote_injection(self):
        good=fx(post());good['tweet']['text']='No reset today.'
        self.assertEqual(fx_post(good,'1',{'thsottiaux':AID}).text,'Global reset by EOD.')
        for mutate in [lambda d:d.update(code=500),lambda d:d.update(stale=True),lambda d:d['tweet'].update(id='2'),lambda d:d['tweet']['author'].update(id='fake'),lambda d:d['tweet'].update(reposted_by={}),lambda d:d['tweet'].update(reposted_by={'id':'x'})]:
            d=copy.deepcopy(good);mutate(d)
            if d['tweet'].get('reposted_by')=={}:continue
            with self.assertRaises(ReadFailure):fx_post(d,'1',{'thsottiaux':AID})
        p=post(text='New model shipped.');d=fx(p);d['tweet']['quote']={'id':'999','text':'Global reset by EOD.','author':{'id':'fake'}}
        normalized=fx_post(d,'1',{'thsottiaux':AID})
        self.assertIsNone(normalized.quote);self.assertEqual(classify(normalized).kind,'IGNORE')

    def test_time_precision_beijing_cross_date_and_no_invented_deadline(self):
        self.assertEqual(beijing('2026-10-10T22:16:20Z'),'2026-10-11 06:16:20 北京时间')
        for text in ['Global reset by EOD.','Global reset landing tomorrow 10am PST.','Global reset by midnight today.','Global reset tomorrow.']:
            with self.subTest(text=text):
                note,exact=timing(text,'2026-10-10T22:16:20Z');self.assertIsNone(exact);self.assertIn('不提供',note)
        note,exact=timing('Global reset tomorrow 10am UTC.','2026-10-10T22:16:20Z')
        self.assertEqual(beijing(exact),'2026-10-11 18:00:00 北京时间')
        self.assertEqual(timing('Global reset at 2026-10-12T09:30:00Z.','2026-10-11T00:00:00Z')[1],'2026-10-12T09:30:00+00:00')
        with self.assertRaises(ValueError):parse_time('2026-10-11T10:00:00')

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(retry_after('120',NOW),120)
        self.assertEqual(retry_after('Sun, 11 Oct 2026 12:02:00 GMT',NOW),120)
        self.assertEqual(retry_after('bad',NOW),60)
        class Opener:
            calls=0
            def open(self,*args,**kwargs):
                self.calls+=1;raise urllib.error.HTTPError('redacted',429,'limit',{'Retry-After':'600'},None)
        h=HTTP('test',sleeper=lambda n:self.fail('should not sleep'));o=Opener();h.opener=o
        with self.assertRaises(RetryDelivery) as r:h.get('https://codex-reset.com/api/feed')
        self.assertEqual(r.exception.delay,600);self.assertEqual(o.calls,1)

    def test_spool_idempotent_and_phone_webhook_payloads_without_real_send(self):
        s=self.store();self.record(s,post());payload=json.loads(s.db.execute('SELECT payload FROM notices').fetchone()[0])
        h=FakeHTTP();sender=Sender(self.cfg,h,pathlib.Path(self.tmp.name))
        sender.send('qq_spool',payload);sender.send('qq_spool',payload)
        self.assertEqual(len(list((pathlib.Path(self.tmp.name)/'qq_spool').glob('*.json'))),1)
        self.assertEqual(h.calls,[])
        from unittest.mock import patch
        for ch in ['ntfy','bark','webhook']:
            self.cfg['channels'][ch]['enabled']=True
            with patch.dict('os.environ',{'NTFY_TOPIC':'mock-topic','NTFY_TOKEN':'mock-token','BARK_DEVICE_KEY':'mock-key','RESET_WEBHOOK_URL':'http://127.0.0.1:9123/reset','RESET_WEBHOOK_SECRET':'mock-secret'}):sender.send(ch,payload)
        self.assertEqual(len(h.calls),3)
        self.assertEqual(h.calls[1][1]['device_key'],'mock-key')
        self.assertNotIn('mock-key',h.calls[1][0]);self.assertEqual(h.calls[2][2]['Idempotency-Key'],payload['id'])

    def test_corrupt_state_and_concurrent_lock_fail_closed(self):
        pathlib.Path(self.path).write_bytes(b'not sqlite')
        with self.assertRaises(Exception):Store(self.path,NOW)
        p=pathlib.Path(self.tmp.name)/'empty.sqlite3';p.touch()
        with self.assertRaises(ValueError):Store(str(p),NOW)
        lp=pathlib.Path(self.tmp.name)/'state.lock'
        with lock(lp):
            with self.assertRaises(RuntimeError):
                with lock(lp):pass

    def test_cli_dry_run_isolated_and_lost_state_guard(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout, redirect_stderr
        from monitor.__main__ import main
        target=pathlib.Path(self.tmp.name)/'dry'/ 'state.sqlite3'
        args=['monitor','run','--dry-run','--config',str(ROOT/'config.example.json'),'--state',str(target)]
        with patch('sys.argv',args),patch('monitor.__main__.poll',return_value=0),redirect_stdout(io.StringIO()):
            self.assertEqual(main(),0)
        self.assertFalse(target.parent.exists())
        with patch('sys.argv',args+['--send']),redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as e:main()
        self.assertEqual(e.exception.code,2)
        target.parent.mkdir();(target.parent/'.initialized').write_text('schema=1')
        with patch('sys.argv',['monitor','status','--config',str(ROOT/'config.example.json'),'--state',str(target)]):
            with self.assertRaisesRegex(ValueError,'state_lost'):main()

    def test_tentative_completion_and_reported_assertions_are_quiet(self):
        for text in ['Maybe the reset has been processed.','If the reset is completed tomorrow, enjoy.','The global reset is probably complete.','I heard the reset has been processed.','Global reset by EOD, hopefully.','Reset has been processed?']:
            with self.subTest(text=text):
                self.assertNotIn(classify(post(text=text)).kind,('UPCOMING','COMPLETED','BANKED'))
        self.assertEqual(classify(post(text='We could improve product metrics. The reset has been processed.')).kind,'COMPLETED')

    def test_inflight_expiry_checked_before_each_send(self):
        s=self.store();self.record(s,post());s.claim(NOW,'a');sender=FakeSender()
        dispatch(s,sender,NOW,'a',lambda x:None,clock=lambda:NOW+timedelta(hours=25))
        self.assertEqual(sender.calls,[]);self.assertEqual(s.summary()['deliveries'][0]['status'],'expired')

    def test_private_file_whitelist_and_generated_topic_override(self):
        from monitor.private import load_channels
        from unittest.mock import patch
        p=pathlib.Path(self.tmp.name)/'channels.private.json'
        p.write_text('{"NTFY_TOPIC":"generated-test-only"}')
        with patch.dict(os.environ,{'NTFY_TOPIC':'other-test-only'}):
            load_channels(p);self.assertEqual(os.environ['NTFY_TOPIC'],'generated-test-only')
        p.write_text('{"PATH":"wrong"}')
        with self.assertRaises(ValueError):load_channels(p)

    def test_stop_nonce_and_running_process_stamp(self):
        from monitor.service import atomic,stop,status,process_stamp
        r=pathlib.Path(self.tmp.name)/'service';r.mkdir()
        atomic(r/'service.json',{'status':'running','pid':os.getpid(),'process_stamp':process_stamp(os.getpid()),'nonce':'own-test-only'})
        self.assertEqual(status(r)['status'],'running')
        self.assertEqual(stop(r)['status'],'stop_requested')
        self.assertEqual(json.loads((r/'stop.json').read_text())['nonce'],'own-test-only')

if __name__=='__main__':unittest.main()
