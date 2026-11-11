"""Corrections stay attached to exact source text; retry, concurrency and accounting contracts use local fakes."""
from copy import deepcopy
import datetime as dt
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

from assetizer import config, db, delivery, ingest, issue_asks as I, knobs, providers
from assetizer.llm import ContractError, LLM
from assetizer.notion import blocks as B, hook, mapping as M
from assetizer.notion.client import NotionError
from assetizer.util import now, write_json
from test_issue_asks import issue, say, read, queue, model, fixed_model, posts, mapped, ago
from test_topic_notion import home, OWN_DB


def source():
    return db.one("SELECT * FROM sources WHERE id='s'")


def adopt():
    db.exec("INSERT INTO runs(id,source_id,production,summary,created_at) VALUES ('r2','s','COMPLETE','{}',?)", now())
    db.exec("UPDATE sources SET current_run_id='r2',delivery='STALE'")


def test_original_moves_away_from_the_position_hint(issue, monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due()
    fix=db.one('SELECT * FROM issue_fixes')
    assert fix['original']=='旧段 0' and fix['original_hash']==I._hash('旧段 0')
    issue.plan[1]['block']=B.block('paragraph','列表二')
    issue.plan.insert(3,{'key':'sec:moved','block':B.block('heading_2','旧段 0')})
    monkeypatch.setattr(delivery,'target_for',lambda s:{'template':'chapters'})
    monkeypatch.setitem(I.templates.PLANS, 'chapters', lambda *a: deepcopy(issue.plan))
    plan=delivery.page_plan(source(),'r1',{'template':'chapters'})
    assert B.plain(next(it['block'] for it in plan if it['key']=='sec:p0'))=='列表二'
    assert B.plain(next(it['block'] for it in plan if it['key']=='sec:moved'))=='旧段 0'
    assert not any(it.get('_fix_ids') for it in plan)


def test_ambiguous_original_does_not_guess_or_warn_twice_in_one_plan(issue, monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due()
    issue.plan[1]['block']=B.block('paragraph','列表二')
    issue.plan.extend([{'key':k,'block':B.block('paragraph','旧段 0')} for k in ('sec:x','sec:y')])
    before=db.one("SELECT count(*) n FROM events WHERE kind='issue-asks' AND level='warn'")['n']
    plan=delivery.page_plan(source(),'r1')
    assert not any(it.get('_fix_ids') for it in plan)
    assert db.one("SELECT count(*) n FROM events WHERE kind='issue-asks' AND level='warn'")['n']==before
    assert delivery.deliver('s')['status']=='DELIVERED'
    assert db.one("SELECT count(*) n FROM events WHERE kind='issue-asks' AND level='warn'")['n']==before+1
    I.due()
    assert not db.q('SELECT * FROM issue_fixes')
    assert posts(issue)[-1][2]['rich_text'][0]['text']['content']=='〔课代表〕没改'


def template(issue):
    write_json(config.get().runs_dir/'r1/composition.json',{'blocks':[
        {'section_id':'sec','kind':'UNIT','unit_id':'u','markdown':'## 旧标题\n这节正文','screenshots':[]}]})
    cfg={'database_id':OWN_DB,'template':'reading-guide','properties':{'名称':{'name':'名称','type':'title','source':'video_title'}}}
    M.save('ch',cfg);assert delivery.deliver('s')['status']=='DELIVERED';issue.blocks=mapped()
    return cfg


def test_real_template_title_is_voided_once_and_does_not_revive(issue,monkeypatch):
    cfg=template(issue);say(issue,'sec:h');read(issue,'sec:h');fixed_model(monkeypatch,issue);I.due()
    delivery.deliver('s');I.due();assert len(posts(issue))==1
    cfg['template']='chapters';M.save('ch',cfg);delivery.deliver('s');I.due()
    assert not db.q('SELECT * FROM issue_fixes')
    assert B.plain(next(it['block'] for it in delivery.page_plan(source(),'r1',cfg) if it['key']=='sec:h'))=='第一章：旧标题'
    assert [p[2]['rich_text'][0]['text']['content'] for p in posts(issue)]==['〔课代表〕已改 · 改正在下一块','〔课代表〕没改']
    assert I.VOID_TEXT in db.j(posts(issue)[-1]) and 'youtube.com' not in db.j(posts(issue)[-1])
    cfg['template']='reading-guide';M.save('ch',cfg);delivery.deliver('s');I.due();delivery.deliver('s');I.due()
    assert B.plain(next(it['block'] for it in delivery.page_plan(source(),'r1',cfg) if it['key']=='sec:h'))=='旧标题'
    assert len(posts(issue))==2


def test_real_template_body_with_new_key_voids_correction(issue,monkeypatch):
    cfg=template(issue);say(issue,'sec:p0_0');read(issue,'sec:p0_0');fixed_model(monkeypatch,issue);I.due()
    delivery.deliver('s');I.due();assert len(posts(issue))==1
    cfg['template']='chapters';M.save('ch',cfg)
    plan=delivery.page_plan(source(),'r1',cfg)
    assert B.plain(next(it['block'] for it in plan if it['key']=='sec:p0'))=='这节正文'
    delivery.deliver('s');I.due();assert len(posts(issue))==2 and not db.q('SELECT * FROM issue_fixes')
    assert I.VOID_TEXT in db.j(posts(issue)[-1])


def test_sent_correction_is_voided_once_after_new_run_readback(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due();delivery.deliver('s');I.due()
    assert len(posts(issue))==1
    adopt();I.reply('s');assert len(posts(issue))==1  # sent notices wait for the new delivery's readback
    delivery.deliver('s');I.due();delivery.deliver('s');I.due()
    assert len(posts(issue))==2 and '〔课代表〕没改' in db.j(posts(issue)[-1])
    assert not db.q('SELECT * FROM issue_fixes')
    assert db.one('SELECT count(*) n FROM issue_replies WHERE void_of IS NOT NULL')['n']==1
    assert db.one('SELECT result,reply FROM asks')=={'result':'没改','reply':I.VOID_TEXT}


def test_reply_rechecks_run_even_before_new_delivery(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due();delivery.deliver('s')
    adopt();assert I.reply('s')==1
    assert '〔课代表〕没改' in db.j(posts(issue)[-1]) and '已改' not in db.j(posts(issue))


def test_model_run_switch_voids_without_writing_fix_or_stale(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0')
    out={'replies':[{'id':db.one('SELECT discussion FROM asks')['discussion'],'result':'已改','text':'改正。','fix':'只是观察。'}]}
    def during(p):
        adopt();db.exec("UPDATE sources SET delivery='DELIVERED'")
    calls=model(monkeypatch,out,during=during);I.due()
    assert len(calls)==1 and not db.q('SELECT * FROM issue_fixes')
    assert source()['delivery']=='DELIVERED' and '〔课代表〕没改' in db.j(posts(issue)[-1])
    assert not I.pending()


def test_followup_on_retained_original_keeps_location_and_quote(issue,monkeypatch):
    c=say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due();delivery.deliver('s');I.due()
    new=say(issue,'sec:p0','课代表：再核对',thread=c['discussion_id']);read(issue,'sec:p0',eid='again')
    rows=db.q('SELECT plan_key,quote,kind,owned,depth FROM issue_comments WHERE id IN (?,?)',c['id'],new['id'])
    assert rows==[dict(plan_key='sec:p0',quote='旧段 0',kind='paragraph',owned=1,depth=0)]*2
    calls=model(monkeypatch);I.due()
    assert calls[0]['threads'][0]['section']=='sec'
    assert '原话支持' in db.j(posts(issue)[-1]) and 't=72s' in db.j(posts(issue)[-1])


def test_followup_fix_keeps_template_original_and_current_display(issue,monkeypatch):
    c=say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due();delivery.deliver('s');I.due()
    say(issue,'sec:p0','课代表：还要改',thread=c['discussion_id']);read(issue,'sec:p0',eid='again')
    p=I._payload(source());assert p['threads'][0]['plan_text']=='只是观察，不是买入。'
    model(monkeypatch,{'replies':[{'id':c['discussion_id'],'result':'已改','text':'保留条件。','fix':'有条件的观察。'}]});I.due()
    fix=db.one('SELECT * FROM issue_fixes');assert fix['original']=='旧段 0' and fix['text']=='有条件的观察。'
    assert len(db.q('SELECT * FROM issue_fixes'))==1


def test_eight_threads_share_section_once(issue):
    for i in range(8):say(issue,'sec:p0',f'课代表：问题 {i}')
    read(issue,'sec:p0')
    segments=[{'start':60+i,'end':61+i,'text':'唯一原话'+'字'*80} for i in range(90)]
    write_json(config.get().runs_dir/'r1/machine/transcript.json',{'segments':segments})
    p=I._payload(source())
    assert len(p['threads'])==8 and set(p['sections'])=={'sec'}
    assert all(t['section']=='sec' for t in p['threads'])
    assert db.j(p).count('唯一原话')==90 and len(db.j(p))<I.MAX_INPUT


def test_comments_only_quota_is_not_production_in_use(home,monkeypatch):
    db.set_setting('llm_provider','agy');knobs.save({'stage_route':{'comments':'claude/claude-opus-5-5'}})
    monkeypatch.setattr(providers,'_make',lambda route:route)
    assert providers.in_use()==providers.routes() and not any('claude' in r for r in providers.in_use())


def test_apply_operational_error_warns_once_and_reuses_output(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');calls=fixed_model(monkeypatch,issue)
    real=I._apply
    with monkeypatch.context() as mp:
        mp.setattr(I,'_apply',lambda *a:(_ for _ in ()).throw(sqlite3.OperationalError('busy')))
        I.due();I.due();assert db.one('SELECT state FROM issue_batches')['state']=='APPLY'
    I.due();assert len(calls)==1 and db.one('SELECT state FROM issue_batches')['state']=='DONE'
    assert db.one("SELECT count(*) n FROM events WHERE kind='issue-asks' AND level='warn'")['n']==1


@pytest.mark.parametrize('result',['已答','没改'])
def test_supported_answers_require_line_and_no_section_has_no_fabricated_citation(issue,result):
    say(issue,'sec:p0');read(issue,'sec:p0');p=I._payload(source())
    r={'id':p['threads'][0]['id'],'result':result,'text':'核对了。'}
    with pytest.raises(ContractError,match='line'):I._check(p,{'replies':[r]})
    r['line']=2;I._check(p,{'replies':[r]})
    body=I._body(p['threads'][0],r,'s',p['sections']);assert '原话支持' in body and 't=72s' in body
    assert I._body({'section':None},{'result':result,'text':'没有节。'},'s',{})=='没有节。'


@pytest.mark.parametrize('error',[NotionError(500,'temporary'),NotionError(429,'rate limited'),httpx.ConnectError('offline'),httpx.ReadTimeout('timeout')])
def test_channel_read_retry_is_persisted_and_only_due_events_run(issue,monkeypatch,error):
    say(issue,'sec:p0');queue(issue,'sec:p0');original=I.scan_one
    with monkeypatch.context() as mp:
        mp.setattr(I,'scan_one',lambda *a,**kw:(_ for _ in ()).throw(error))
        hook.process()
    e=db.one('SELECT * FROM notion_events');assert e['done'] is None and e['tries']==1 and e['retry_at'] and not hook.pending()
    db.exec('UPDATE notion_events SET retry_at=?',ago(1));assert hook.pending();hook.process()
    assert db.one('SELECT outcome,done FROM notion_events')['outcome']=='channel'
    assert db.one('SELECT * FROM asks')['source_id']=='s'


def test_all_five_retry_delays_then_one_terminal_warning(issue,monkeypatch):
    say(issue,'sec:p0');queue(issue,'sec:p0');calls=[]
    def failed(*a,**kw):calls.append(1);raise NotionError(503,'private error')
    monkeypatch.setattr(I,'scan_one',failed)
    for tries,seconds in enumerate((60,300,1800,7200,21600),1):
        before=dt.datetime.now(dt.timezone.utc);hook.process();e=db.one('SELECT * FROM notion_events')
        assert e['done'] is None and e['tries']==tries
        assert abs((dt.datetime.fromisoformat(e['retry_at'])-before).total_seconds()-seconds)<2
        assert not hook.pending();db.exec('UPDATE notion_events SET retry_at=?',ago(1))
    hook.process();hook.process()
    assert len(calls)==6 and db.one('SELECT outcome FROM notion_events')['outcome']=='error'
    assert db.one("SELECT count(*) n FROM events WHERE kind='notion-hook' AND level='warn'")['n']==1


def test_channel_rereads_do_not_consume_numbers(issue):
    say(issue,'sec:p0');read(issue,'sec:p0');first=db.one('SELECT n FROM asks')['n']
    for i in range(3):read(issue,'sec:p0',eid=f'again{i}')
    say(issue,'sec:p1');read(issue,'sec:p1',eid='next')
    assert [r['n'] for r in db.q('SELECT n FROM asks ORDER BY n')]==[first,first+1]


def test_unchanged_fix_is_rejected_and_done_call_directory_is_removed(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');p=I._payload(source())
    with pytest.raises(ContractError,match='相同'):
        I._check(p,{'replies':[{'id':p['threads'][0]['id'],'result':'已改','text':'改了。','fix':'**旧段 0**'}]})
    root=config.get().home/'issue-asks'/'1';root.mkdir(parents=True);(root/'proof').write_text('fake')
    model(monkeypatch);I.due();assert not root.exists()


def test_notion_events_migration_only_adds_retry_columns(home):
    schema=db.SCHEMA.replace('  tries INTEGER NOT NULL DEFAULT 0,\n  retry_at TEXT\n','')
    schema=schema.replace('  topic_id INTEGER,\n);','  topic_id INTEGER\n);')
    conn=sqlite3.connect(':memory:',isolation_level=None);conn.executescript(schema)
    conn.execute("INSERT INTO notion_events(id,at,type,bots,outcome,done) VALUES ('old','at','comment.created','[]','topic','done')")
    db._migrate(conn)
    assert conn.execute('SELECT id,outcome,done,tries,retry_at FROM notion_events').fetchone()==('old','topic','done',0,None)
    conn.close()

import test_issue_asks as T


def test_comments_route_cannot_hold_video_backfill(home):
    db.set_setting('llm_provider', 'agy')
    db.set_setting('magpie_state', {'ok': True, 'read_at': now(), 'subs': [
        {'provider': 'claude', 'name': 'Claude', 'windows': [{'window': 'weekly', 'remaining': 0}]}]})
    before = ingest.backfill_held()
    if 'comments' in knobs.STAGES:
        knobs.save({'stage_route': {'comments': 'claude/claude-opus-5-5'}})
    else:
        with pytest.raises(knobs.KnobError):
            knobs.save({'stage_route': {'comments': 'claude/claude-opus-5-5'}})
    after = ingest.backfill_held()
    assert before == after == None, 'comments-only subscription incorrectly holds video backfill'


def test_old_ready_fix_must_not_reply_after_replacing_run(issue, monkeypatch):
    c = T.say(issue, 'sec:p0'); T.read(issue, 'sec:p0')
    T.fixed_model(monkeypatch, issue); I.due()
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    assert db.one('SELECT ready FROM issue_replies')['ready'] == 1
    fixed_bid = T.mapped()['sec:p0']['block_id']
    db.exec("INSERT INTO runs(id,source_id,production,summary,created_at) VALUES ('r2','s','COMPLETE','{}',?)", now())
    db.exec("UPDATE sources SET current_run_id='r2',delivery='STALE'")
    assert delivery.deliver_pending()
    assert fixed_bid not in [b['id'] for b in issue.n.kids[issue.pid]]
    I.due()
    assert not any('已改' in db.j(p) for p in T.posts(issue)), 'claims correction in next block after correction was removed'


def test_apply_transient_failure_resumes_accepted_output(issue, monkeypatch):
    import sqlite3
    from assetizer import config
    T.say(issue, 'sec:p0'); T.read(issue, 'sec:p0'); calls=T.fixed_model(monkeypatch, issue)
    original = I._apply
    with monkeypatch.context() as mp:
        def busy(*args):
            # A real competing SQLite writer; shorten only the test connection's 120-second wait.
            lock = sqlite3.connect(config.get().db_path, isolation_level=None)
            lock.execute('BEGIN IMMEDIATE')
            db.connect().execute('PRAGMA busy_timeout=10')
            try:
                return original(*args)
            finally:
                lock.rollback(); lock.close()
                db.connect().execute('PRAGMA busy_timeout=120000')
        mp.setattr(I, '_apply', busy)
        I.due()
    I.due()
    assert db.one('SELECT state FROM issue_batches')['state'] == 'DONE', 'APPLY failure quarantines already-accepted output as model failure'


def test_one_regeneration_never_gets_a_third_call(issue, monkeypatch):
    from types import SimpleNamespace
    c=T.say(issue,'sec:p0');T.read(issue,'sec:p0');calls=[]
    def call(d,prompt,**kw):
        calls.append({'kind':kw['kind'],'prompt':prompt})
        d.mkdir(parents=True,exist_ok=True)
        outputs=['{"replies":[]}', 'Here is a malformed answer with lots of content but no JSON envelope.',
                 db.j({'replies':[{'id':c['discussion_id'],'result':'已答','text':'只是观察。','line':2}]})]
        return SimpleNamespace(text=outputs[len(calls)-1],reused=False,meta={},receipt={'status':'SUCCESS'})
    p=SimpleNamespace(call=call)
    monkeypatch.setattr(providers,'get',lambda:p);monkeypatch.setattr(providers,'for_stage',lambda stage:p)
    I.due()
    assert len(calls)<=2, 'one regeneration is followed by an extra contract-repair provider call'


def test_lost_post_with_lagging_comment_read_is_not_proven_exactly_once(issue, monkeypatch):
    T.say(issue,'sec:p0');T.read(issue,'sec:p0');T.model(monkeypatch)
    with monkeypatch.context() as mp:
        mp.setattr(I,'_finish_reply',lambda *args: (_ for _ in ()).throw(SystemExit('POST committed; worker died')))
        with pytest.raises(SystemExit): I.due()
    assert len(T.posts(issue))==1
    original=I.C._list
    with monkeypatch.context() as mp:
        # Explicit countermodel for an unverified assumption, not a claim about observed Notion consistency.
        def lag(n,bid):
            return [c for c in original(n,bid) if c['created_by']['id']!='bot-user']
        mp.setattr(I.C,'_list',lag)
        I.reply('s')
    assert len(T.posts(issue))==1, 'exact-once proof assumes immediate complete read-after-write visibility'


def test_actual_provider_receipt_keeps_provenance_and_reasoning(issue, monkeypatch):
    from types import SimpleNamespace
    from assetizer.providers import magpie_state
    c=T.say(issue,'sec:p0');T.read(issue,'sec:p0')
    # The exact receipt shape produced by providers/magpie.py:197-202,264-273.
    receipt={'provider':'magpie','agy_version':'magpie:claude/claude-opus-5-5','actual_model':'claude/claude-opus-5-5',
             'status':'SUCCESS','wall_seconds':2.5,'usage':{'input_tokens':10,'output_tokens':20,'thinking_tokens':5}}
    def call(d,prompt,**kw):
        d.mkdir(parents=True,exist_ok=True)
        return SimpleNamespace(text=db.j({'replies':[{'id':c['discussion_id'],'result':'已答','text':'只是观察。','line':2}]}),
                               reused=False,meta={},receipt=deepcopy(receipt))
    p=SimpleNamespace(call=call)
    monkeypatch.setattr(providers,'get',lambda:p);monkeypatch.setattr(providers,'for_stage',lambda stage:p)
    I.due()
    posted=[]
    def ask(path,**kw):
        posted.append({'path':path,'records':json.loads(kw['data'])});return {'accepted':1}
    monkeypatch.setattr(magpie_state,'_ask',ask)
    reported=magpie_state.report()
    row=db.one('SELECT * FROM model_calls')
    assert row['run_id']=='issue-s'
    assert row['thinking_tokens']==5 and row['agy_version']==receipt['agy_version'] and reported==0, \
        'comments recorder loses reasoning usage and re-reports a magpie call as agy'


def test_many_waiting_followups_share_only_one_void_notice(issue,monkeypatch):
    c=say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due()
    say(issue,'sec:p0','课代表：继续改',thread=c['discussion_id']);read(issue,'sec:p0',eid='again')
    model(monkeypatch,{'replies':[{'id':c['discussion_id'],'result':'已改','text':'加上条件。','fix':'条件下的观察。'}]});I.due()
    assert len(db.q('SELECT * FROM issue_replies'))==2
    adopt();I.due()
    assert len(posts(issue))==1 and '〔课代表〕没改' in db.j(posts(issue)[0])
    assert all(a['result']=='没改' and a['answered_at'] for a in db.q('SELECT * FROM asks'))


def test_void_transaction_rollback_and_post_ack_recovery(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due();delivery.deliver('s');I.due()
    adopt();real=I._void
    def killed(*a,**kw):real(*a,**kw);raise SystemExit('after enqueuing, before COMMIT')
    with monkeypatch.context() as mp:
        mp.setattr(I,'_void',killed)
        with pytest.raises(SystemExit):delivery.deliver('s')
    assert not db.q('SELECT * FROM issue_replies WHERE void_of IS NOT NULL')
    assert db.one('SELECT * FROM issue_fixes')
    assert delivery.deliver('s')['status']=='DELIVERED'
    with monkeypatch.context() as mp:
        mp.setattr(I,'_finish_reply',lambda *a:(_ for _ in ()).throw(SystemExit('POST succeeded before bookkeeping')))
        with pytest.raises(SystemExit):I.reply('s')
    assert len(posts(issue))==2
    I.reply('s');assert len(posts(issue))==2
    db.exec('UPDATE issue_replies SET tried_at=? WHERE done IS NULL',ago(700));I.reply('s')
    delivery.deliver('s');I.due();assert len(posts(issue))==2
    assert db.one('SELECT count(*) n FROM issue_replies WHERE void_of IS NOT NULL')['n']==1


def test_resolved_thread_does_not_get_void_post(issue,monkeypatch):
    c=say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due();delivery.deliver('s');I.due()
    for comment in issue.n.comments[issue.blocks['sec:p0']['block_id']]:comment['resolved']=True
    adopt();delivery.deliver('s');I.due()
    assert len(posts(issue))==1 and db.one('SELECT done FROM issue_replies WHERE void_of IS NOT NULL')['done']


def test_call_recorder_keeps_comment_tokens_out_of_run_detail(issue):
    from assetizer.llm import call_recorder
    from assetizer.web.app import run_detail
    receipt={'stage':'comments-1','attempt':'1','conversation_id':'comment-conversation','actual_model':'fake',
             'agy_version':'magpie:fake','status':'SUCCESS','usage':{'input_tokens':100,'output_tokens':20,'thinking_tokens':5}}
    call_recorder('issue-s')(receipt)
    call_recorder('r1')({**receipt,'stage':'knowledge','usage':{'input_tokens':1,'output_tokens':2,'thinking_tokens':3}})
    calls=run_detail('r1')['calls']
    assert len(calls)==1 and sum(calls[0][k] for k in ('input_tokens','output_tokens','thinking_tokens'))==6
    assert db.one("SELECT thinking_tokens FROM model_calls WHERE run_id='issue-s'")['thinking_tokens']==5


def test_no_event_run_change_during_delivery_preserves_existing_completion(issue,monkeypatch):
    from assetizer.notion.sync import Sync
    real=Sync.check
    def switch(sync,*args,**kw):
        result=real(sync,*args,**kw);adopt();return result
    monkeypatch.setattr(Sync,'check',switch)
    assert delivery.deliver('s')['status']=='DELIVERED'
    assert source()['current_run_id']=='r2' and source()['delivery']=='DELIVERED'
    assert not db.q('SELECT * FROM notion_events') and not db.q('SELECT * FROM issue_fixes')


def test_failed_call_directory_is_kept(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0')
    root=config.get().home/'issue-asks'/'1';root.mkdir(parents=True);(root/'proof').write_text('fake')
    model(monkeypatch,{'replies':[]});I.due()
    assert db.one('SELECT state FROM issue_batches')['state']=='FAILED' and (root/'proof').exists()
