"""Corrections stay in one edition; discussion relocation must be unique in both editions and the batch."""
from copy import deepcopy
import sqlite3
import pytest
from assetizer import config, db, delivery, issue_asks as I
from assetizer.notion import blocks as B, mapping as M
from assetizer.util import write_json
from test_issue_asks import issue, say, read, model, posts, mapped
from test_issue_asks_round2 import source
from test_topic_notion import home, OWN_DB

REAL_DEFAULT = delivery._default_plan
CFG = {'database_id': OWN_DB, 'template': 'chapters', 'properties': {'名称': {'name': '名称', 'type': 'title', 'source': 'video_title'}}}


def real_page(issue, monkeypatch, heading='仓位管理', item='仓位管理'):
    monkeypatch.setattr(delivery, '_default_plan', REAL_DEFAULT)
    write_json(config.get().runs_dir/'r1/composition.json', {'blocks': [dict(section_id='sec', kind='UNIT', unit_id='u',
               markdown=f'## {heading}\n正文\n\n- {item}\n- 列表二', screenshots=[])]})
    assert delivery.deliver('s')['status'] == 'DELIVERED'; issue.blocks = mapped()


def switch():
    M.save('ch', deepcopy(CFG))


def answer(monkeypatch):
    return model(monkeypatch)


def test_unchanged_text_correction_is_void_on_template_change(issue, monkeypatch):
    real_page(issue, monkeypatch, item='独一段')
    c = say(issue, 'sec:p2'); read(issue, 'sec:p2')
    model(monkeypatch, {'replies': [dict(id=c['discussion_id'], result='已改', text='改。', fix='只作观察。')]})
    I.due(); assert delivery.deliver('s')['status'] == 'DELIVERED'; I.due()
    switch(); plan = delivery.page_plan(source(), 'r1', CFG)
    assert B.plain(next(x['block'] for x in plan if x['key'] == 'sec:p1')) == '独一段'
    assert not any(x.get('_fix_ids') for x in plan)
    assert delivery.deliver('s')['status'] == 'DELIVERED'; I.due()
    assert not db.q('SELECT * FROM issue_fixes') and len(posts(issue)) == 2
    assert I.VOID_TEXT in db.j(posts(issue)[-1]) and not I.pending()


@pytest.mark.parametrize('freeze_original', [False, True])
def test_old_section_duplicate_cannot_relocate_single_title_to_list(issue, monkeypatch, freeze_original):
    real_page(issue, monkeypatch)
    c = say(issue, 'sec:p0'); read(issue, 'sec:p0')
    if freeze_original:
        answer(monkeypatch); I.due()
        say(issue, 'sec:p0', '课代表：再问', thread=c['discussion_id']); read(issue, 'sec:p0', eid='follow')
    before = db.one('SELECT * FROM issue_positions'); switch()
    t = I._payload(source())['threads'][0]
    assert not t['can_fix'] and t['plan_text'] == '' and t['_original'] is None
    assert db.one('SELECT * FROM issue_positions') == before
    assert B.plain(next(x['block'] for x in delivery.page_plan(source(), 'r1', CFG) if x['key'] == 'sec:p1')) == '仓位管理'


def test_migrated_threads_converging_on_same_unique_paragraph_are_both_read_only(issue, monkeypatch):
    real_page(issue, monkeypatch, item='独一段')
    for _ in range(2): say(issue, 'sec:p2')
    read(issue, 'sec:p2'); before = db.q('SELECT * FROM issue_positions'); switch()
    p = I._payload(source())
    assert len(p['threads']) == 2
    assert all(not t['can_fix'] and t['plan_text'] == '' and t['_original'] is None for t in p['threads'])
    assert db.q('SELECT * FROM issue_positions') == before


def test_old_plan_built_once_per_batch_and_broken_old_plan_is_read_only(issue, monkeypatch):
    real_page(issue, monkeypatch, item='独一段')
    say(issue, 'sec:p1'); say(issue, 'sec:p2'); read(issue, 'sec:p1'); read(issue, 'sec:p2', eid='two'); switch()
    real = delivery.page_plan; calls = []
    def counted(s, r, target=None, **kw):
        calls.append((target or {}).get('template'))
        return real(s, r, target, **kw)
    monkeypatch.setattr(delivery, 'page_plan', counted)
    p = I._payload(source()); assert all(t['can_fix'] for t in p['threads'])
    assert calls.count('default') == 1
    def broken(s, r, target=None, **kw):
        if (target or {}).get('template') == 'default': raise ValueError('old plan unavailable')
        return real(s, r, target, **kw)
    monkeypatch.setattr(delivery, 'page_plan', broken)
    assert all(t['_original'] is None and not t['can_fix'] for t in I._payload(source())['threads'])


def test_saved_template_window_records_actual_delivered_edition(issue, monkeypatch):
    real_page(issue, monkeypatch, item='独一段'); assert source()['notion_synced_template'] == 'default'
    switch(); c = say(issue, 'sec:p2'); read(issue, 'sec:p2')
    assert db.one('SELECT template FROM issue_positions')['template'] == 'default'
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    assert source()['notion_synced_template'] == 'chapters'
    calls = model(monkeypatch, {'replies': [dict(id=c['discussion_id'], result='已改', text='改。', fix='只是观察。')]})
    I.due(); t = calls[0]['threads'][0]; assert t['can_fix'] and t['key'] == 'sec:p1' and t['plan_text'] == '独一段'
    assert delivery.deliver('s')['status'] == 'DELIVERED'; I.due()
    assert db.one('SELECT template,plan_key FROM issue_positions') == dict(template='chapters', plan_key='sec:p1')
    assert len(posts(issue)) == 1 and not I.pending()


def test_legacy_synced_template_null_falls_back_to_target(issue, monkeypatch):
    db.exec('UPDATE sources SET notion_synced_template=NULL')
    c = say(issue, 'sec:p0'); read(issue, 'sec:p0')
    assert db.one('SELECT template FROM issue_positions')['template'] == 'default'


def test_synced_template_column_migrates_without_rewriting_sources(home):
    cx = db.connect(); cx.execute('ALTER TABLE sources DROP COLUMN notion_synced_template')
    db._migrate_locked(cx)
    assert 'notion_synced_template' in {r[1] for r in cx.execute('PRAGMA table_info(sources)')}


def test_mixed_stable_and_migrated_threads_colliding_are_both_read_only(issue, monkeypatch):
    real_page(issue, monkeypatch, item='独一段')
    old = say(issue, 'sec:p2'); read(issue, 'sec:p2'); switch()
    assert delivery.deliver('s')['status'] == 'DELIVERED'; issue.blocks = mapped()
    say(issue, 'sec:p1'); read(issue, 'sec:p1', eid='new')
    p = I._payload(source())
    assert len(p['threads']) == 2 and all(not t['can_fix'] and t['_original'] is None for t in p['threads'])
    assert all(p['original'] is None for p in db.q('SELECT * FROM issue_positions'))


def test_synced_template_uses_delivered_plan_even_when_setting_changes_during_check(issue, monkeypatch):
    from assetizer.notion.sync import Sync
    real_page(issue, monkeypatch, item='独一段'); switch(); real = Sync.check
    def race(self, *a, **kw):
        result = real(self, *a, **kw)
        M.save('ch', {**CFG, 'template': 'reading-guide'})
        return result
    monkeypatch.setattr(Sync, 'check', race)
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    assert source()['notion_synced_template'] == 'chapters'
    issue.blocks = mapped(); say(issue, 'sec:p1'); read(issue, 'sec:p1')
    assert db.one('SELECT template FROM issue_positions')['template'] == 'chapters'


def test_recreated_page_clears_both_synced_markers_before_build(issue, monkeypatch):
    real_page(issue, monkeypatch); issue.n.pages[issue.pid]['archived'] = True
    real = delivery.build_plan; seen = []
    def build(*a, **kw):
        s = source(); seen.append((s['notion_synced_run'], s['notion_synced_template']))
        return real(*a, **kw)
    monkeypatch.setattr(delivery, 'build_plan', build)
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    assert seen == [(None, None)]
    assert (source()['notion_synced_run'], source()['notion_synced_template']) == ('r1', 'default')


def test_sigkill_before_delivery_commit_preserves_old_edition_and_retry_records_new_one(issue, monkeypatch):
    import contextlib, multiprocessing as mp, os, signal
    real_page(issue, monkeypatch); switch()
    db.connect().close(); db._local.conn = None
    def worker():
        real = db.tx
        class Proxy:
            def __init__(self, cx): self.cx = cx
            def execute(self, sql, *args):
                result = self.cx.execute(sql, *args)
                if 'notion_synced_template=?' in sql: os.kill(os.getpid(), signal.SIGKILL)
                return result
        @contextlib.contextmanager
        def kill_tx():
            with real() as cx: yield Proxy(cx)
        db.tx = kill_tx; delivery.deliver('s')
    p = mp.get_context('fork').Process(target=worker); p.start(); p.join(10)
    assert not p.is_alive() and p.exitcode == -9
    assert (source()['notion_synced_run'], source()['notion_synced_template']) == ('r1', 'default')
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    assert (source()['notion_synced_run'], source()['notion_synced_template']) == ('r1', 'chapters')


def test_discussion_never_relocates_across_sections(issue, monkeypatch):
    real_page(issue, monkeypatch, item='独一段')
    say(issue, 'sec:p2'); read(issue, 'sec:p2'); switch()
    real = I.templates.PLANS['chapters']
    def outside(*a, **kw):
        plan = real(*a, **kw)
        for it in plan:
            if it['key'] == 'sec:p1': it['key'] = 'other:moved'
        return plan
    monkeypatch.setitem(I.templates.PLANS, 'chapters', outside)
    t = I._payload(source())['threads'][0]
    assert not t['can_fix'] and t['plan_text'] == '' and t['_original'] is None


def test_missing_page_clears_synced_template_even_if_recreation_fails(issue, monkeypatch):
    real_page(issue, monkeypatch); issue.n.pages[issue.pid]['archived'] = True
    def fail(*a, **kw): raise RuntimeError('synthetic creation failure')
    monkeypatch.setattr(delivery, '_create', fail)
    assert delivery.deliver('s')['status'] == 'FAILED'
    assert (source()['notion_page_id'], source()['notion_synced_run'], source()['notion_synced_template']) == (None, None, None)
