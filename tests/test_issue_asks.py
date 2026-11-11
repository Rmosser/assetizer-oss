"""Channel asks contract. All endpoints, models and media stay local; real Sync placement/readback is exercised."""
from copy import deepcopy
import dataclasses
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
from types import SimpleNamespace

import pytest

from assetizer import config, db, delivery, issue_asks as I, knobs, preview, providers
from assetizer.contracts import prompts as P
from assetizer.llm import ContractError, LLM
from assetizer.notion import blocks as B, hook
from assetizer.notion.sync import Sync
from assetizer.providers.agy import ProviderError
from assetizer.topics import comments as C, notion as TN, run
from assetizer.util import now, write_json
from test_channel_notion import _channel_fixture, mapped
from test_notion_hook import ago, event
from test_topic_notion import BOT, home


@pytest.fixture
def issue(home, monkeypatch):
    n, plan = _channel_fixture(home, monkeypatch)
    db.exec("INSERT INTO channels(id,name,stage,created_at,updated_at) VALUES ('ch','频道','LOCKED',?,?)", now(), now())
    db.exec("UPDATE sources SET channel_id='ch',channel_title='频道' WHERE id='s'")
    db.exec("INSERT INTO units(id,run_id,source_id,label,ord,title,start,end,data) VALUES ('u','r1','s','U1',1,'一节',60,150,'{}')")
    db.exec("INSERT INTO sections(id,run_id,ord,kind,unit_ids,markdown) VALUES ('sec','r1',1,'UNIT','[\"u\"]',?)", '## 一节\n文章原稿')
    db.exec("INSERT INTO sections(id,run_id,ord,kind,unit_ids,markdown) VALUES ('other','r1',2,'UNIT','[]','## 第二节')")
    plan[:] = [{"key": "head", "block": B.callout("抬头")},
               *[{"key": f"sec:p{i}", "block": B.block("paragraph", f"旧段 {i}")} for i in range(4)],
               {"key": "sec:fold", "block": B.toggle("节内折叠", [B.block("paragraph", "里面一块")])},
               {"key": "runinfo", "block": B.toggle("运行信息", [B.block("paragraph", "信息")])}]
    write_json(config.get().runs_dir / 'r1/machine/transcript.json', {"segments": [
        {"start": 59, "end": 61, "text": "前一句与本节相交。"},
        {"start": 72.8, "end": 75, "text": "原话支持：只是观察，不是买入。"},
        {"start": 130, "end": 132, "text": "这里是在回答条件。"},
        {"start": 150, "end": 155, "text": "节外原话不得交给模型。"}]})
    db.flag('s', 'r1', 'UNIT', 'u', 'FIDELITY', 'REVIEW', '注意条件', evidence={"quote": "只是观察"})
    monkeypatch.setattr(C, 'PAUSE', 0)
    monkeypatch.setattr(TN, 'client', lambda: n)
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    pid = db.one("SELECT notion_page_id FROM sources WHERE id='s'")['notion_page_id']
    n.calls.clear(); n.requests.clear()
    return SimpleNamespace(n=n, plan=plan, pid=pid, blocks=mapped(), home=home)


def say(issue, key, text='课代表：这段有问题', *, thread=None, old=True):
    c = issue.n.say(issue.blocks[key]['block_id'] if key else issue.pid, text, thread=thread)
    c['created_time'] = ago(700 if old else 1)
    return c


def queue(issue, key, *, eid='e', authors=None, block=None):
    b = event(eid, issue.pid, block or (issue.blocks[key]['block_id'] if key else None), authors=authors)
    db.exec("""INSERT INTO notion_events(id,at,ts,type,page,parent,parent_type,entity,bots) VALUES (?,?,?,?,?,?,?,?,?)""",
            b['id'], now(), b['timestamp'], b['type'], b['data']['page_id'], b['data']['parent']['id'],
            b['data']['parent']['type'], b['entity']['id'], db.j(b['authors']))


def read(issue, key, **kwargs):
    queue(issue, key, **kwargs)
    hook.process()


def model(monkeypatch, output=None, during=None):
    calls = []
    def structured(self, root, stage, prompt, schema, **kw):
        payload = db.unj(db.one('SELECT input FROM issue_batches ORDER BY id DESC')['input'])
        calls.append(payload)
        assert stage.startswith('comments-') and kw['effort'] == 'medium' and not kw.get('media') and self.retry is False
        assert '节外原话' not in prompt and P.POLICY in prompt
        if during:
            during(payload)
        out = deepcopy(output) if output is not None else {'replies': [
            {'id': t['id'], 'result': '已答', 'text': '这是原话中的观察。', 'line': 2} for t in payload['threads']]}
        kw['check'](out)
        return out
    monkeypatch.setattr(LLM, 'structured', structured)
    return calls


def fixed_model(monkeypatch, issue):
    payload = I._payload(db.one("SELECT * FROM sources WHERE id='s'"))
    out = {'replies': [{'id': t['id'], 'result': '已改', 'text': '对照原话修正。', 'fix': '**只是观察**，不是买入。', 'line': 2}
                       for t in payload['threads']]}
    return model(monkeypatch, out)


def posts(issue):
    return [x for x in issue.n.requests if x[:2] == ('POST', '/comments')]


def test_targeted_read_asks_and_reconciliation(issue):
    a = say(issue, 'sec:p0'); b = say(issue, 'sec:p1')
    read(issue, 'sec:p0')
    assert issue.n.calls == [('GET', '/users/me'), ('GET', f"/comments?block_id={issue.blocks['sec:p0']['block_id']}&page_size=100"),
                             ('GET', f"/blocks/{issue.blocks['sec:p0']['block_id']}")]
    assert db.one('SELECT * FROM asks')['source_id'] == 's'
    assert db.one('SELECT * FROM issue_comments')['plan_key'] == 'sec:p0'
    read(issue, 'sec:p1', eid='e1')
    a['resolved'] = True
    read(issue, 'sec:p0', eid='e2')
    assert db.one('SELECT open FROM issue_comments WHERE id=?', a['id'])['open'] == 0
    assert db.one('SELECT open FROM issue_comments WHERE id=?', b['id'])['open'] == 1
    b['rich_text'][0]['plain_text'] = b['rich_text'][0]['text']['content'] = '课代表：编辑了'
    read(issue, 'sec:p1', eid='e3')
    assert db.one('SELECT text FROM asks WHERE id=?', b['id'])['text'] == '编辑了'


def test_page_scope_no_extra_reads_and_unasked_threads_omitted(issue, monkeypatch):
    say(issue, 'sec:p0', '自己的笔记')
    say(issue, None)
    read(issue, 'sec:p0')
    issue.n.calls.clear()
    read(issue, None, eid='p')
    assert issue.n.calls == [('GET', '/users/me'), ('GET', f'/comments?block_id={issue.pid}&page_size=100')]
    calls = model(monkeypatch, {'replies': [{'id': db.one('SELECT discussion FROM asks')['discussion'], 'result': '没改', 'text': '页讨论没有原话定位。'}]})
    I.due()
    assert len(calls) == 1 and len(calls[0]['threads']) == 1
    assert calls[0]['threads'][0]['can_fix'] is False and calls[0]['threads'][0]['titles'] == ['一节', '第二节']
    assert len(posts(issue)) == 1


@pytest.mark.parametrize('mode', ['ours', 'unknown', 'transport'])
def test_channel_batch_identity_once_and_outcome(issue, monkeypatch, mode):
    say(issue, 'sec:p0')
    authors = [{'id': BOT, 'type': 'bot'}] if mode == 'ours' else None
    if mode != 'ours':
        def me(**kw):
            issue.n.calls.append(('GET', '/users/me'))
            assert kw == {'tries': 1, 'timeout': 5}
            if mode == 'transport':
                raise OSError('secret exception must not leak')
            return {}
        monkeypatch.setattr(issue.n, 'me', me)
    queue(issue, 'sec:p0', authors=authors)
    queue(issue, 'sec:p0', eid='second', authors=authors)
    hook.process()
    assert issue.n.calls.count(('GET', '/users/me')) == 1
    assert len(issue.n.calls) == (1 if mode == 'ours' else 3)
    assert [e['outcome'] for e in db.q('SELECT outcome FROM notion_events')] == ['channel', 'channel']
    assert bool(db.q('SELECT * FROM asks')) is (mode != 'ours')


@pytest.mark.parametrize('field,value', [('stage','TUNING'), ('delivery','REGISTERED')])
def test_unowned_pages_only_record(issue, monkeypatch, field, value):
    say(issue, 'sec:p0')
    db.exec(f"UPDATE {'channels' if field == 'stage' else 'sources'} SET {field}=?", value)
    monkeypatch.setattr(TN, 'client', lambda: pytest.fail('must not query identity'))
    read(issue, 'sec:p0')
    assert issue.n.calls == [] and db.one('SELECT outcome FROM notion_events')['outcome'] == 'channel'


def test_two_parent_hops_and_restrictions(issue):
    fold = issue.blocks['sec:fold']['block_id']
    child = issue.n.kids[fold][0]
    child['parent'] = {'type': 'block_id', 'block_id': fold}
    grand = issue.n._block(B.block('paragraph', '最里面'))
    grand['parent'] = {'type': 'block_id', 'block_id': child['id']}
    issue.n.kids[child['id']] = [grand]
    issue.plan[5]['block']['toggle']['children'][0]['paragraph']['children'] = [B.block('paragraph','最里面')]
    issue.n.say(grand['id'], '课代表：问里面的字')['created_time'] = ago(700)
    read(issue, None, block=grand['id'])
    assert [p for m,p,*_ in issue.n.calls if m == 'GET' and p.startswith('/blocks/')] == [f"/blocks/{grand['id']}", f"/blocks/{child['id']}", f'/blocks/{fold}']
    assert db.one('SELECT plan_key FROM issue_comments')['plan_key'] == 'sec:fold'
    assert I._payload(db.one("SELECT * FROM sources WHERE id='s'"))['threads'][0]['can_fix'] is True
    human = issue.n._block(B.block('paragraph','用户块'), by='you-user')
    human['parent'] = {'type': 'page_id', 'page_id': issue.pid}
    issue.n.kids[issue.pid].append(human)
    issue.n.say(human['id'], '课代表：这块')['created_time'] = ago(700)
    read(issue, None, block=human['id'], eid='human')
    say(issue,'runinfo'); read(issue,'runinfo',eid='tail')
    payload = I._payload(db.one("SELECT * FROM sources WHERE id='s'"))
    assert [t['can_fix'] for t in payload['threads']] == [True,False,False]
    # A media plan item may belong to a section but cannot be corrected as text.
    issue.plan.append({'key':'sec:video', 'media':{'path':'unused','kind':'video','caption':''}})
    c = payload['threads'][0]
    db.exec("UPDATE issue_comments SET plan_key='sec:video',kind='video' WHERE discussion=?",c['id'])
    assert I._payload(db.one("SELECT * FROM sources WHERE id='s'"))['threads'][0]['can_fix'] is False


def test_quiet_combines_threads_and_four_results(issue, monkeypatch):
    cs = [say(issue,f'sec:p{i}',old=i!=3) for i in range(4)]
    for i in range(4): read(issue,f'sec:p{i}',eid=f'e{i}')
    payload = I._payload(db.one("SELECT * FROM sources WHERE id='s'"))
    out = {'replies': [dict(id=t['id'],result=r,text='对照了本期原话。', **({'fix':'只是观察，不是买入。'} if i==0 else {}),
                            **({'line':2} if i in (1,2) else {}))
                       for i,(t,r) in enumerate(zip(payload['threads'], ['已改','已答','没改','记进待修']))]}
    calls = model(monkeypatch,out)
    I.due(); assert calls == []
    db.exec('UPDATE issue_comments SET touched=?', ago(700))
    I.due()
    assert len(calls) == 1 and len(calls[0]['threads']) == 4
    assert db.one('SELECT COUNT(*) n FROM issue_fixes')['n'] == 1
    assert db.one("SELECT delivery FROM sources WHERE id='s'")['delivery'] == 'STALE'
    assert len(posts(issue)) == 3 and not any('已改' in db.j(x) for x in posts(issue))
    answer = next(x for x in posts(issue) if '已答' in db.j(x)); no = next(x for x in posts(issue) if '没改' in db.j(x))
    assert 'https://www.youtube.com/watch?v=s&t=72s' in db.j(answer) and '▶ 01:12' in db.j(answer)
    assert 'https://www.youtube.com/watch?v=s&t=72s' in db.j(no) and '▶ 01:12' in db.j(no)
    assert '原话支持' in db.j(answer)
    assert all(x[2]['rich_text'][0]['annotations']['bold'] for x in posts(issue))
    before = issue.blocks['sec:p0']['block_id']
    newplan = delivery.build_plan(db.one("SELECT * FROM sources WHERE id='s'"),'r1')
    pre = preview.body(db.one("SELECT * FROM sources WHERE id='s'"),'r1',{})
    assert next(x for x in pre if x['key']=='sec:p0')['text'][0]['t'] == B.plain(newplan[1]['block'])
    assert len(posts(issue)) == 3
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    assert len(posts(issue)) == 3
    after = mapped()['sec:p0']['block_id']
    ids = [x['id'] for x in issue.n.kids[issue.pid]]
    assert ids[ids.index(before)+1] == after
    I.due()
    assert len(posts(issue)) == 4 and '〔课代表〕已改 · 改正在下一块' in db.j(posts(issue)[-1])
    assert all(a['answered_at'] for a in db.q('SELECT * FROM asks'))
    # A second delivery/reply neither changes the run artifacts nor posts again.
    assert db.one("SELECT markdown FROM sections WHERE id='sec'")['markdown'] == '## 一节\n文章原稿'
    delivery.deliver('s'); I.due(); assert len(posts(issue)) == 4
    cs[0]['resolved'] = True
    # Resolve the entire discussion, including our reply.
    for c in issue.n.comments[before]: c['resolved'] = True
    delivery.deliver('s')
    assert before not in [x['id'] for x in issue.n.kids[issue.pid]]


def test_overlay_only_its_run_and_reprint(issue, monkeypatch):
    say(issue,'sec:p0'); read(issue,'sec:p0'); fixed_model(monkeypatch,issue); I.due()
    source = db.one("SELECT * FROM sources WHERE id='s'")
    assert B.plain(delivery.build_plan(source,'r1')[1]['block']) == '只是观察，不是买入。'
    # Deliberately retain identical section keys in a different run: the run guard must still prevent the overlay.
    db.exec("INSERT INTO runs(id,source_id,production,summary,created_at) VALUES ('r2','s','COMPLETE','{}',?)",now())
    db.exec("UPDATE sources SET current_run_id='r2'")
    source = db.one("SELECT * FROM sources WHERE id='s'")
    assert B.plain(delivery.build_plan(source,'r2')[1]['block']) == '旧段 0'
    assert B.plain(delivery.build_plan(source,'r1')[1]['block']) == '只是观察，不是买入。'
    db.exec("UPDATE sources SET current_run_id='r1'"); delivery.deliver('s'); I.due()
    assert len(posts(issue)) == 1


@pytest.mark.parametrize('bad', ['illegal','cannot','long','line','missing','duplicate'])
def test_check_rejects_contract(issue, bad):
    say(issue,'sec:p0');read(issue,'sec:p0')
    payload = I._payload(db.one("SELECT * FROM sources WHERE id='s'"))
    r = {'id':payload['threads'][0]['id'],'result':'已改','text':'改了。','fix':'原话支持的字','line':2}
    if bad=='illegal': r['result']='别的'
    if bad=='cannot': payload['threads'][0]['can_fix']=False
    if bad=='long': r['fix']='字'*1801
    if bad=='line': r['line']=4
    if bad=='missing': r['fix']=' '
    rs = [r,r] if bad=='duplicate' else [r]
    with pytest.raises(ContractError): I._check(payload, {'replies':rs})


def test_failed_ticks_wait_for_new_words_and_include_old(issue,monkeypatch):
    c = say(issue,'sec:p0'); read(issue,'sec:p0')
    calls = []
    def failed(*a,**kw): calls.append(True); raise RuntimeError('bad model')
    monkeypatch.setattr(LLM,'structured',failed)
    for _ in range(5): I.due()
    assert len(calls)==1 and db.one('SELECT failed_at FROM issue_state')['failed_at']
    # A new utterance in an asked thread is a new request, even when it doesn't repeat the name.
    say(issue,'sec:p0','再核对这句',thread=c['discussion_id']); read(issue,'sec:p0',eid='new')
    good=model(monkeypatch); I.due()
    assert len(good)==1 and len(good[0]['threads'][0]['said'])==2
    assert all(s['new'] for s in good[0]['threads'][0]['said'])


@pytest.mark.parametrize('crash', ['model-before-fix','fix-before-delivery','readback-before-reply'])
def test_killed_three_durable_boundaries(issue,monkeypatch,crash):
    say(issue,'sec:p0'); read(issue,'sec:p0'); calls=fixed_model(monkeypatch,issue)
    if crash=='model-before-fix':
        original=I._apply
        with monkeypatch.context() as mp:
            mp.setattr(I,'_apply',lambda *a: (_ for _ in ()).throw(SystemExit('kill')))
            with pytest.raises(SystemExit): I.due()
        assert db.one('SELECT state FROM issue_batches')['state']=='APPLY' and not db.q('SELECT * FROM issue_fixes')
        I.due()
    else: I.due()
    assert len(calls)==1 and not posts(issue) and db.one('SELECT COUNT(*) n FROM issue_fixes')['n']==1
    assert delivery.deliver('s')['status']=='DELIVERED'
    if crash=='readback-before-reply':
        # The persisted ready outbox is what a new worker sees after a kill.
        db._local.conn.close(); db._local.conn=None
    I.due(); I.due()
    assert len(posts(issue))==1 and db.one('SELECT answered_at FROM asks')['answered_at']


def test_llm_accepted_cache_survives_before_batch_output(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0')
    provider=SimpleNamespace(calls=0)
    def call(d,prompt,**kw):
        provider.calls+=1
        payload=db.unj(db.one('SELECT input FROM issue_batches')['input'])
        out={'replies':[{'id':payload['threads'][0]['id'],'result':'已答','text':'这是观察。','line':2}]}
        d.mkdir(parents=True,exist_ok=True)
        return SimpleNamespace(text=db.j(out),reused=False,meta={},receipt={'status':'SUCCESS'})
    provider.call=call
    monkeypatch.setattr(providers,'get',lambda:provider);monkeypatch.setattr(providers,'for_stage',lambda s:provider)
    original=db.exec
    def kill(sql,*args):
        if "output=?,state='APPLY'" in sql: raise SystemExit('kill after accepted.json')
        return original(sql,*args)
    with monkeypatch.context() as mp:
        mp.setattr(db,'exec',kill)
        with pytest.raises(SystemExit): I.due()
    assert provider.calls==1 and db.one('SELECT state FROM issue_batches')['state']=='MODEL'
    I.due()
    assert provider.calls==1 and len(posts(issue))==1


def test_actual_llm_one_contract_regeneration_no_provider_retries(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0')
    calls=[]
    def call(d,prompt,**kw):
        calls.append(prompt); d.mkdir(parents=True,exist_ok=True)
        return SimpleNamespace(text='{"replies":[]}',reused=False,meta={},receipt={'status':'SUCCESS'})
    p=SimpleNamespace(call=call)
    monkeypatch.setattr(providers,'get',lambda:p);monkeypatch.setattr(providers,'for_stage',lambda s:p)
    db.set_setting('retry_counts',{'CONTRACT':5,'PROVIDER_TRANSIENT':5})
    for _ in range(4): I.due()
    assert len(calls)==2 and '上一次的输出没有通过校验' in calls[1]
    def failure(*a,**kw): calls.append('failure');raise ProviderError('PROVIDER_TRANSIENT','bad')
    p.call=failure
    say(issue,'sec:p1');read(issue,'sec:p1',eid='new');I.due();I.due()
    assert calls.count('failure')==1


def test_same_page_concurrent_events_and_processing_arrival(issue,monkeypatch):
    c=say(issue,'sec:p0');queue(issue,'sec:p0',eid='one');queue(issue,'sec:p0',eid='two')
    enter,release=threading.Event(),threading.Event()
    original=C._list
    def paused(n,bid):
        enter.set(); assert release.wait(3); return original(n,bid)
    monkeypatch.setattr(C,'_list',paused)
    es=db.q('SELECT * FROM notion_events')
    errors=[]
    def work(rows):
        try: hook._batch(rows)
        except BaseException as e: errors.append(e)
    a=threading.Thread(target=work,args=(es[:1],));b=threading.Thread(target=work,args=(es[1:],))
    a.start();assert enter.wait(2);b.start();b.join(2);release.set();a.join(2)
    assert not errors and not a.is_alive() and not b.is_alive()
    hook.process();assert db.one('SELECT COUNT(*) n FROM asks')['n']==1
    monkeypatch.setattr(C,'_list',original)
    entered,finish=threading.Event(),threading.Event()
    def during(payload): entered.set(); assert finish.wait(3)
    calls=model(monkeypatch,during=during)
    t=threading.Thread(target=I.due);t.start();assert entered.wait(2)
    say(issue,'sec:p0','课代表：又一句',thread=c['discussion_id'])
    queue(issue,'sec:p0',eid='during')
    assert hook._batch(db.q('SELECT * FROM notion_events WHERE done IS NULL')) is True
    assert db.one("SELECT done FROM notion_events WHERE id='during'")['done']
    finish.set();t.join(2);assert not t.is_alive()
    hook.process()
    model(monkeypatch);I.due()
    assert db.one('SELECT COUNT(*) n FROM issue_batches')['n']==2 and len(posts(issue))==2


def test_fix_arriving_during_delivery_stays_stale(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue)
    original=Sync.check
    entered=[]
    def during(sync,*a,**kw):
        if not entered:
            entered.append(True)
            # Execute on another thread, as the real issue-asks thread would, after delivery captured its old plan.
            t=threading.Thread(target=I.due);t.start();t.join(3);assert not t.is_alive()
            assert db.one("SELECT delivery FROM sources")['delivery']=='STALE'
            assert not posts(issue)
        return original(sync,*a,**kw)
    with monkeypatch.context() as mp:
        mp.setattr(Sync,'check',during)
        delivery.deliver('s')
    assert db.one('SELECT delivery FROM sources')['delivery']=='STALE'
    I.due();assert not posts(issue)
    assert delivery.deliver_pending() is True
    assert db.one('SELECT delivery FROM sources')['delivery']=='DELIVERED'
    I.due();assert len(posts(issue))==1
    assert B.plain(next(b for b in issue.n.kids[issue.pid] if b['id']==mapped()['sec:p0']['block_id']))=='只是观察，不是买入。'


def test_reply_403_waits_404_drops_and_lost_post_reconciles(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');model(monkeypatch)
    issue.n.can.remove('insert');I.due()
    assert db.one('SELECT error FROM issue_state')['error']=='NO_INSERT' and not db.one('SELECT done FROM issue_replies')['done']
    issue.n.can.add('insert')
    original=I._finish_reply
    with monkeypatch.context() as mp:
        mp.setattr(I,'_finish_reply',lambda *a: (_ for _ in ()).throw(SystemExit('post accepted, process killed')))
        with pytest.raises(SystemExit): I.reply('s')
    count=len(posts(issue));I.reply('s')
    assert len(posts(issue))==count and db.one('SELECT done FROM issue_replies')['done'] is None
    db.exec('UPDATE issue_replies SET tried_at=?',ago(700));I.reply('s')
    assert len(posts(issue))==count and db.one('SELECT done FROM issue_replies')['done']
    assert db.one('SELECT COUNT(*) n FROM issue_comments WHERE ours=1')['n']==1
    say(issue,'sec:p1');read(issue,'sec:p1',eid='gone')
    # Model runs, but the discussion has disappeared at the reply boundary.
    def erase(payload): issue.n.comments.pop(issue.blocks['sec:p1']['block_id'])
    model(monkeypatch,during=erase);I.due()
    assert all(r['done'] for r in db.q('SELECT done FROM issue_replies'))
    assert db.one('SELECT answered_at FROM asks ORDER BY n DESC')['answered_at'] is None


def test_fixed_shared_numbers_usage_logs_and_knob(issue,monkeypatch,capsys):
    from assetizer import cli,logs
    from assetizer.web.app import _asks
    say(issue,'sec:p0');read(issue,'sec:p0')
    d=db.one('SELECT discussion FROM asks')['discussion']
    model(monkeypatch,{'replies':[{'id':d,'result':'记进待修','text':'这是程序问题。'}]});I.due()
    n=db.one('SELECT n FROM asks')['n']
    assert C.used(30)['todo'][0]['source_id']=='s' and C.used(30)['todo'][0]['source_title']=='一期'
    assert _asks()=={'n':1,'todo':1} and logs.export()['asks']['asks']==1
    assert any('这周 1 次' in s for s in logs.digest_lines())
    cli.cmd_asks(SimpleNamespace(fixed=n,version='0.9.0'));C._tell_fixed()
    assert f'〔课代表〕已修 #{n} · 0.9.0' in db.j(posts(issue)[-1])
    assert C.used(30)['todo']==[]
    saved=knobs.save({'stage_route':{'comments':'claude/claude-opus-5-5'}})
    assert knobs.get('stage_providers')['comments']=='claude/claude-opus-5-5'
    assert '批注' in db.one("SELECT message FROM events WHERE kind='config' ORDER BY id DESC")['message']
    assert 'comments' not in knobs.VIDEO_STAGES and 'comments' in knobs.EFFORT_KEYS
    knobs.save({'effort':{'comments':'high'}})
    assert knobs.effort('comments-5')=='high' and providers.stage_key('comments-5:repair')=='comments'
    with pytest.raises(knobs.KnobError): knobs.save({'stage_route':{'wrong':'agy'}})


def test_resending_same_token_is_silent_constant_time(home,monkeypatch):
    monkeypatch.setattr(hook,'token',lambda:'same-token')
    monkeypatch.setattr(hook,'file_present',lambda *a:True)
    calls=[]
    original=hook.hmac.compare_digest
    monkeypatch.setattr(hook.hmac,'compare_digest',lambda a,b: (calls.append((a,b)) or original(a,b)))
    for value in ['same-token','different-token']:
        raw=db.j({'verification_token':value}).encode()
        assert hook.receive(raw,None)==200
    assert calls==[(b'same-token',b'same-token'),(b'same-token',b'different-token')]
    assert db.one("SELECT COUNT(*) n FROM events WHERE level='warn'")['n']==1


def test_asks_migration_only_adds_column(home):
    conn=sqlite3.connect(':memory:', isolation_level=None)
    old=db.SCHEMA.replace('  source_id TEXT,                                      -- 哪一期频道页（与专题共用编号）\n','')
    conn.executescript(old)
    conn.execute("INSERT INTO asks(n,id,at,topic_id,discussion,text) VALUES(7,'old','t',3,'d','旧数据')")
    db._migrate(conn)
    row=conn.execute('SELECT n,id,topic_id,source_id FROM asks').fetchone()
    assert row==(7,'old',3,None)
    conn.close()


def test_housekeeping_starts_single_issue_thread(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0')
    entered,release=threading.Event(),threading.Event()
    def due(): entered.set();assert release.wait(3)
    monkeypatch.setattr(I,'due',due);monkeypatch.setattr(run,'_watching',{})
    monkeypatch.setattr(run,'_last',dict.fromkeys(run._last,10000))
    run.housekeeping(10000);assert entered.wait(2)
    first=run._watching['i'];run.housekeeping(10000)
    assert run._watching['i'] is first and first.name=='issue-asks'
    release.set();first.join(2)




def test_input_limits_and_absent_transcript_are_conservative(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0')
    p=I._payload(db.one("SELECT * FROM sources WHERE id='s'"))
    assert p['sections']['sec']['lines'][0]['line']==1
    assert '注意条件' in p['sections']['sec']['notes']
    with monkeypatch.context() as mp:
        mp.setattr(I,'MAX_TRANSCRIPT',100)
        small=I._payload(db.one("SELECT * FROM sources WHERE id='s'"))['sections']['sec']
        assert small['truncated'] and len(db.j(small['lines']))<200
    calls=model(monkeypatch)
    with monkeypatch.context() as mp:
        mp.setattr(I,'MAX_INPUT',1)
        I.due();I.due()
    assert calls==[] and db.one('SELECT failed_at FROM issue_state')['failed_at']
    (config.get().runs_dir/'r1/machine/transcript.json').unlink()
    assert I._payload(db.one("SELECT * FROM sources WHERE id='s'"))['threads'][0]['can_fix'] is False


def test_failed_readback_cannot_release_fix_reply(issue,monkeypatch):
    say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due()
    with monkeypatch.context() as mp:
        mp.setattr(Sync,'check',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('readback rejected')))
        assert delivery.deliver('s')['status']=='FAILED'
    I.due()
    assert not posts(issue) and not db.one('SELECT ready FROM issue_replies')['ready']
    assert delivery.deliver('s')['status']=='DELIVERED'
    I.due();assert len(posts(issue))==1


def test_later_fix_supersedes_pending_fix_without_losing_reply(issue,monkeypatch):
    c=say(issue,'sec:p0');read(issue,'sec:p0');fixed_model(monkeypatch,issue);I.due()
    first=db.one('SELECT id FROM issue_fixes')['id']
    say(issue,'sec:p0','课代表：也保留条件',thread=c['discussion_id']);read(issue,'sec:p0',eid='new')
    p=I._payload(db.one('SELECT * FROM sources'))
    model(monkeypatch,{'replies':[{'id':p['threads'][0]['id'],'result':'已改','text':'保留条件。','fix':'这是有条件的观察，不是买入。'}]});I.due()
    later=db.one('SELECT id FROM issue_fixes')['id']
    assert later!=first and db.one('SELECT COUNT(*) n FROM issue_fixes')['n']==1
    assert {r['fix_id'] for r in db.q('SELECT * FROM issue_replies')}=={later}
    delivery.deliver('s');I.due()
    assert len(posts(issue))==2 and all(r['done'] for r in db.q('SELECT * FROM issue_replies'))


def test_mixed_batch_and_busy_source_leave_other_pages_free(issue):
    from test_topic_notion import RESEARCH
    db.exec("INSERT INTO sources(id,channel_id,notion_page_id,created_at,updated_at) VALUES ('s2','ch',?,?,?)", RESEARCH,now(),now())
    other=SimpleNamespace(n=issue.n,pid=RESEARCH,blocks={})
    issue.n.say(RESEARCH,'课代表：第二页')['created_time']=ago(700)
    say(issue,'sec:p0');queue(issue,'sec:p0',eid='busy');queue(other,None,eid='free')
    entered,release=threading.Event(),threading.Event()
    def held():
        with I.read_lock('s'): entered.set();assert release.wait(3)
    t=threading.Thread(target=held);t.start();assert entered.wait(2)
    try:
        hook._batch(db.q('SELECT * FROM notion_events'))
        assert db.one("SELECT done FROM notion_events WHERE id='busy'")['done'] is None
        assert db.one("SELECT outcome FROM notion_events WHERE id='free'")['outcome']=='channel'
        assert db.one("SELECT source_id FROM asks")['source_id']=='s2'
    finally:release.set();t.join(2)
    hook.process()
    assert db.one('SELECT COUNT(*) n FROM asks')['n']==2


def test_hook_error_is_terminal_for_batch_but_new_event_can_read(issue,monkeypatch):
    say(issue,'sec:p0');queue(issue,'sec:p0',eid='fail1');queue(issue,'sec:p0',eid='fail2')
    calls=[]
    with monkeypatch.context() as mp:
        def failed(*a,**kw):calls.append(True);raise RuntimeError('private body')
        mp.setattr(I,'scan_one',failed)
        hook.process()
    assert calls==[True] and [e['outcome'] for e in db.q('SELECT * FROM notion_events')]==['error','error']
    assert not db.q("SELECT 1 FROM events WHERE message LIKE '%private body%'")
    read(issue,'sec:p0',eid='new');assert db.one('SELECT COUNT(*) n FROM asks')['n']==1


def test_user_child_under_our_section_cannot_be_corrected(issue):
    fold=issue.blocks['sec:fold']['block_id']
    child=issue.n._block(B.block('paragraph','用户自己加在折叠里的字'),by='you-user')
    child['parent']={'type':'block_id','block_id':fold};issue.n.kids[fold].append(child)
    issue.n.say(child['id'],'课代表：这块不能替我改')['created_time']=ago(700)
    read(issue,None,block=child['id'])
    t=I._payload(db.one("SELECT * FROM sources WHERE id='s'"))['threads'][0]
    assert t['key']=='sec:fold' and t['can_fix'] is False


def test_reply_keeps_transcript_markdown_literal(issue,monkeypatch):
    p=config.get().runs_dir/'r1/machine/transcript.json'
    data=json.loads(p.read_text());data['segments'][1]['text']='**观察** [原文](https://outside.invalid)';write_json(p,data)
    say(issue,'sec:p0');read(issue,'sec:p0');model(monkeypatch);I.due()
    rt=posts(issue)[0][2]['rich_text']
    assert '**观察** [原文](https://outside.invalid)' in rt[2]['text']['content']
    links=[r['text']['link']['url'] for r in rt if r.get('text',{}).get('link')]
    assert links==['https://www.youtube.com/watch?v=s&t=72s']


def test_nested_fix_edits_the_child_and_keeps_parent_and_sibling(issue,monkeypatch):
    fold=issue.blocks['sec:fold']['block_id'];child=issue.n.kids[fold][0]
    child['parent']={'type':'block_id','block_id':fold}
    c=issue.n.say(child['id'],'课代表：请核对这一小块');c['created_time']=ago(700)
    read(issue,None,block=child['id'])
    payload=I._payload(db.one("SELECT * FROM sources WHERE id='s'"))
    assert payload['threads'][0]['part']==[0] and payload['threads'][0]['plan_kind']=='paragraph'
    fixed_model(monkeypatch,issue);I.due()
    stored=delivery.page_plan(db.one("SELECT * FROM sources"),'r1')[5]['block']
    assert stored['type']=='toggle' and B.plain(stored)=='节内折叠'
    assert B.plain(stored['toggle']['children'][0])=='只是观察，不是买入。'
    assert delivery.deliver('s')['status']=='DELIVERED'
    I.due();assert len(posts(issue))==1
    new=mapped()['sec:fold']['block_id'];ids=[b['id'] for b in issue.n.kids[issue.pid]]
    assert ids[ids.index(fold)+1]==new
    assert B.plain(issue.n.kids[new][0])=='只是观察，不是买入。'


def test_ambiguous_or_unplanned_child_is_answer_only(issue):
    fold=issue.blocks['sec:fold']['block_id'];child=issue.n.kids[fold][0]
    child['parent']={'type':'block_id','block_id':fold}
    issue.n.say(child['id'],'课代表：哪个子块')['created_time']=ago(700)
    issue.plan[5]['block']['toggle']['children'].append(deepcopy(issue.plan[5]['block']['toggle']['children'][0]))
    read(issue,None,block=child['id'])
    t=I._payload(db.one("SELECT * FROM sources WHERE id='s'"))['threads'][0]
    assert t['part'] is None and t['can_fix'] is False


def test_two_child_corrections_in_same_parent_are_merged(issue,monkeypatch):
    fold=issue.blocks['sec:fold']['block_id']
    second=B.block('paragraph','另一子块')
    issue.plan[5]['block']['toggle']['children'].append(second)
    issue.n.kids[fold].append(issue.n._block(second))
    for child in issue.n.kids[fold]:
        child['parent']={'type':'block_id','block_id':fold}
        issue.n.say(child['id'],'课代表：核对')['created_time']=ago(700)
        read(issue,None,block=child['id'],eid=child['id'])
    p=I._payload(db.one("SELECT * FROM sources WHERE id='s'"))
    assert [t['part'] for t in p['threads']]==[[0],[1]]
    fixes=['只是观察，不是买入。','原话说明这是观察。']
    model(monkeypatch,{'replies':[{'id':t['id'],'result':'已改','fix':fix,'text':'已核对。','line':2}
                                for t,fix in zip(p['threads'],fixes)]})
    I.due()
    b=delivery.page_plan(db.one("SELECT * FROM sources"),'r1')[5]['block']
    assert [B.plain(child) for child in b['toggle']['children']]==fixes
    assert db.one('SELECT COUNT(*) n FROM issue_fixes')['n']==2
    delivery.deliver('s');I.due()
    assert len(posts(issue))==2 and all(r['done'] for r in db.q('SELECT * FROM issue_replies'))
