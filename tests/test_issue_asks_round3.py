"""Edition-aware identities and versioned reply outbox. All services are local fakes."""
from copy import deepcopy
import pytest
from assetizer import db, delivery, issue_asks as I, preview
from assetizer.notion import blocks as B, hook
from test_issue_asks import issue, say, read, queue, model, fixed_model, posts, mapped, ago
from test_issue_asks_round2 import source, adopt, template
from test_topic_notion import home, OWN_DB


def prepare(issue, monkeypatch):
    c = say(issue, 'sec:p0'); read(issue, 'sec:p0'); fixed_model(monkeypatch, issue); I.due()
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    return c


def test_same_edition_never_searches_elsewhere_or_exposes_wrong_text(issue, monkeypatch):
    prepare(issue, monkeypatch); I.due()
    issue.plan[1]['block'] = B.block('paragraph', '列表二')
    issue.plan.insert(3, {'key': 'sec:moved', 'block': B.block('paragraph', '旧段 0')})
    c = db.one('SELECT discussion FROM asks')['discussion']
    say(issue, 'sec:p0', '课代表：再改', thread=c); read(issue, 'sec:p0', eid='again')
    p = I._payload(source()); t = p['threads'][0]
    assert t['can_fix'] is False and t['plan_text'] == '' and t['_original'] is None
    assert not any(it.get('_fix_ids') for it in delivery.page_plan(source(), 'r1'))


def test_changed_edition_searches_only_same_section_and_ignores_hint(issue, monkeypatch):
    prepare(issue, monkeypatch)
    # Even an exact old hint is ambiguous after a template change.
    monkeypatch.setattr(delivery, 'target_for', lambda s: {'template': 'chapters'})
    monkeypatch.setitem(I.templates.PLANS, 'chapters', lambda *a: deepcopy(issue.plan))
    issue.plan.append({'key': 'sec:duplicate', 'block': B.block('paragraph', '旧段 0')})
    assert not any(it.get('_fix_ids') for it in delivery.page_plan(source(), 'r1', {'template': 'chapters'}))
    issue.plan[1]['block'] = B.block('paragraph', '列表二')
    issue.plan[-1]['key'] = 'other:duplicate'
    assert not any(it.get('_fix_ids') for it in delivery.page_plan(source(), 'r1', {'template': 'chapters'}))


def test_two_same_originals_keep_distinct_fixes_and_replies(issue, monkeypatch):
    issue.plan[2]['block'] = B.block('paragraph', '旧段 0')
    assert delivery.deliver('s')['status'] == 'DELIVERED'; issue.blocks = mapped()
    a = say(issue, 'sec:p0'); b = say(issue, 'sec:p1'); read(issue, 'sec:p0'); read(issue, 'sec:p1', eid='b')
    model(monkeypatch, {'replies': [dict(id=c['discussion_id'], result='已改', text='对照原话改。', fix=fix)
                                   for c, fix in ((a, '只是观察。'), (b, '不是买入。'))]})
    I.due()
    assert len(db.q('SELECT * FROM issue_fixes')) == 2
    assert len({r['fix_id'] for r in db.q('SELECT * FROM issue_replies')}) == 2
    assert delivery.deliver('s')['status'] == 'DELIVERED'; I.due()
    p = {it['key']: B.plain(it['block']) for it in delivery.page_plan(source(), 'r1') if 'block' in it}
    assert (p['sec:p0'], p['sec:p1']) == ('只是观察。', '不是买入。')
    assert len(posts(issue)) == 2 and all(r['done'] for r in db.q('SELECT * FROM issue_replies'))


def test_same_thread_three_corrections_keep_first_original(issue, monkeypatch):
    c = prepare(issue, monkeypatch); I.due()
    for n in (2, 3):
        say(issue, 'sec:p0', f'课代表：第{n}次改', thread=c['discussion_id']); read(issue, 'sec:p0', eid=f'e{n}')
        model(monkeypatch, {'replies': [dict(id=c['discussion_id'], result='已改', text='保留条件。', fix=f'第{n}次观察。')]})
        I.due(); assert delivery.deliver('s')['status'] == 'DELIVERED'; I.due()
        fixes = db.q('SELECT * FROM issue_fixes')
        assert len(fixes) == 1 and fixes[0]['original'] == '旧段 0' and fixes[0]['text'] == f'第{n}次观察。'
        pos = db.one('SELECT * FROM issue_positions')
        assert (pos['plan_key'], pos['part'], pos['run_id'], pos['template'], pos['original']) == ('sec:p0', '[]', 'r1', 'default', '旧段 0')
    assert len(posts(issue)) == 3


def test_new_thread_on_corrected_block_records_raw_original(issue, monkeypatch):
    prepare(issue, monkeypatch); I.due(); issue.blocks = mapped()
    say(issue, 'sec:p0', '课代表：新的讨论'); read(issue, 'sec:p0', eid='new-thread')
    t = I._payload(source())['threads'][0]
    assert t['can_fix'] and t['_original']['original'] == '旧段 0'
    assert t['plan_text'] == '只是观察，不是买入。'


@pytest.mark.parametrize('point', ['claim-before-post', 'post-before-finish'])
@pytest.mark.parametrize('killed', [False, True])
def test_outbox_version_preserves_void_during_post_and_recovery(issue, monkeypatch, point, killed):
    prepare(issue, monkeypatch)
    real = I.C._post
    class Killed(BaseException): pass
    def race(*a, **kw):
        got = real(*a, **kw) if point == 'post-before-finish' else None
        adopt(); assert delivery.deliver('s')['status'] == 'DELIVERED'
        row = db.one('SELECT * FROM issue_replies')
        assert row['version'] == 1 and not row['sending'] and row['done'] is None and row['head'] == '〔课代表〕没改'
        if killed: raise Killed()
        return got if got else real(*a, **kw)
    monkeypatch.setattr(I.C, '_post', race)
    if killed:
        with pytest.raises(Killed): I.reply('s')
    else: I.reply('s')
    assert db.one('SELECT done FROM issue_replies')['done'] is None
    monkeypatch.setattr(I.C, '_post', real); I.due(); I.due()
    heads = [p[2]['rich_text'][0]['text']['content'] for p in posts(issue)]
    expected = ['〔课代表〕没改'] if killed and point == 'claim-before-post' else ['〔课代表〕已改 · 改正在下一块', '〔课代表〕没改']
    assert heads == expected
    assert db.one('SELECT done FROM issue_replies')['done'] and not I.pending()
    assert len(db.q('SELECT * FROM issue_comments WHERE ours=1')) == (1 if killed else len(expected))
    assert db.one('SELECT result,answered_at FROM asks')['result'] == '没改'


def test_stale_claim_is_skipped(issue, monkeypatch):
    prepare(issue, monkeypatch)
    original_q = db.q
    def race(sql, *a):
        rows = original_q(sql, *a)
        if sql.startswith('SELECT * FROM issue_replies WHERE source_id='):
            adopt(); assert delivery.deliver('s')['status'] == 'DELIVERED'
        return rows
    monkeypatch.setattr(db, 'q', race); assert I.reply('s') == 0
    assert not posts(issue) and db.one('SELECT done FROM issue_replies')['done'] is None
    monkeypatch.setattr(db, 'q', original_q); I.due()
    assert len(posts(issue)) == 1 and '没改' in db.j(posts(issue)) and not I.pending()


def test_only_successful_readback_warns_once_for_two_voids(issue, monkeypatch):
    for i in (0, 1): say(issue, f'sec:p{i}'); read(issue, f'sec:p{i}', eid=f'e{i}')
    fixed_model(monkeypatch, issue); I.due()
    issue.plan[1]['block'] = B.block('paragraph', '换了'); issue.plan[2]['block'] = B.block('paragraph', '也换了')
    for _ in range(10): preview.body(source(), 'r1', {})
    issue.n.fail_props = True
    for _ in range(5): assert delivery.deliver('s')['status'] != 'DELIVERED'
    assert not db.q("SELECT * FROM events WHERE kind='issue-asks' AND level='warn'")
    issue.n.fail_props = False; assert delivery.deliver('s')['status'] == 'DELIVERED'
    warns = db.q("SELECT * FROM events WHERE kind='issue-asks' AND level='warn'")
    assert len(warns) == 1 and source()['title'] in warns[0]['message'] and '2 条' in warns[0]['message']
    delivery.deliver('s'); assert len(db.q("SELECT * FROM events WHERE kind='issue-asks' AND level='warn'")) == 1


def test_eight_terminal_events_one_title_warning(issue, monkeypatch):
    say(issue, 'sec:p0')
    for i in range(8): queue(issue, 'sec:p0', eid=f'e{i}')
    db.exec('UPDATE notion_events SET tries=5')
    def broken(*a, **kw): raise TimeoutError()
    monkeypatch.setattr(I, 'scan_one', broken); hook.process()
    assert len(db.q("SELECT * FROM notion_events WHERE done IS NOT NULL AND outcome='error'")) == 8
    warns = db.q("SELECT * FROM events WHERE kind='notion-hook' AND level='warn'")
    assert len(warns) == 1 and source()['title'] in warns[0]['message'] and '8 个' in warns[0]['message']
    assert issue.pid not in warns[0]['message']
