"""One mapping row per block: a retained block that changes key drops its old key in the same checkpoint."""
from contextlib import contextmanager

import pytest

from assetizer import db, delivery, issue_asks as I
from assetizer.notion import blocks as B
from assetizer.notion.sync import Sync
from assetizer.util import now
from test_issue_asks import issue, say, read, model, posts, mapped
from test_issue_asks_round2 import source
from test_topic_notion import home


def rows(bid):
    return db.q('SELECT block_key,block_id,hash,run_id,template FROM notion_blocks WHERE source_id=? AND block_id=? ORDER BY block_key',
                's', bid)


def all_rows():
    return db.q('SELECT block_key,block_id,hash,run_id,template FROM notion_blocks WHERE source_id=? ORDER BY block_key,block_id', 's')


def new_run():
    db.exec("INSERT INTO runs(id, source_id, production, summary, created_at) VALUES ('r2','s','COMPLETE','{}',?)", now())
    db.exec("UPDATE sources SET current_run_id='r2' WHERE id='s'")


def shift(issue):
    """A new paragraph takes sec:p1; the retained 旧段 1..3 each move one key down."""
    issue.plan[2:5] = [{'key': 'sec:p1', 'block': B.block('paragraph', '新段')},
                       *[{'key': f'sec:p{i + 1}', 'block': B.block('paragraph', f'旧段 {i}')} for i in range(1, 4)]]


@pytest.fixture
def captured(monkeypatch):
    """Record every checkpoint call and the mapping right after it, inside the delivery."""
    seen = {'calls': []}
    init = Sync.__init__
    def wrap(self, *args, checkpoint=None, **kwargs):
        def recording(items, blocks):
            checkpoint(items, blocks)
            seen['calls'].append(dict(keys=[it['key'] for it in items], ids=[b['id'] for b in blocks], mapping=all_rows()))
        seen['checkpoint'] = checkpoint
        init(self, *args, checkpoint=recording if checkpoint else None, **kwargs)
    monkeypatch.setattr(Sync, '__init__', wrap)
    return seen


def test_retained_key_change_then_new_block_keeps_one_row_each_during_delivery(issue, captured):
    a = issue.blocks['sec:p1']['block_id']
    new_run(); shift(issue)
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    retained, appended = captured['calls'][0], captured['calls'][-1]
    assert a in retained['ids'] and retained['keys'][retained['ids'].index(a)] == 'sec:p2'
    b = appended['ids'][appended['keys'].index('sec:p1')]
    assert b != a
    # Right after the retained checkpoint, before any append or cleanup: A has only its new key and new edition.
    after_retained = [r for r in retained['mapping'] if r['block_id'] == a]
    assert [(r['block_key'], r['run_id'], r['template']) for r in after_retained] == [('sec:p2', 'r2', 'default')]
    after_append = appended['mapping']
    assert [(r['block_key'], r['run_id']) for r in after_append if r['block_id'] == a] == [('sec:p2', 'r2')]
    assert [(r['block_key'], r['run_id']) for r in after_append if r['block_id'] == b] == [('sec:p1', 'r2')]
    for snapshot in (c['mapping'] for c in captured['calls']):
        ids = [r['block_id'] for r in snapshot]
        assert len(ids) == len(set(ids)), snapshot
    assert [r['block_key'] for r in rows(a)] == ['sec:p2'] and [r['block_key'] for r in rows(b)] == ['sec:p1']


@pytest.mark.parametrize('order', ['retained-first', 'new-first'])
def test_checkpoint_drops_only_this_blocks_other_keys(issue, captured, order):
    a = issue.blocks['sec:p1']['block_id']
    new_run()
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    checkpoint = captured['checkpoint']
    # Back to the r1 mapping for A, as a page delivered before this run left it.
    db.exec("UPDATE notion_blocks SET run_id='r1' WHERE block_id=?", a)
    others = [r for r in all_rows() if r['block_id'] != a]
    retained = ([{'key': 'sec:k2', 'hash': 'h-a'}], [{'id': a}])
    fresh = ([{'key': 'sec:p1', 'hash': 'h-b'}], [{'id': 'block-B'}])
    steps = [retained, fresh] if order == 'retained-first' else [fresh, retained]
    checkpoint(*steps[0])
    if order == 'retained-first':
        assert rows(a) == [dict(block_key='sec:k2', block_id=a, hash='h-a', run_id='r2', template='default')]
        assert rows('block-B') == []
    else:
        # B took A's old key over by REPLACE; A has no row until its own checkpoint.
        assert rows(a) == [] and [r['block_key'] for r in rows('block-B')] == ['sec:p1']
    checkpoint(*steps[1])
    assert rows(a) == [dict(block_key='sec:k2', block_id=a, hash='h-a', run_id='r2', template='default')]
    assert rows('block-B') == [dict(block_key='sec:p1', block_id='block-B', hash='h-b', run_id='r2', template='default')]
    assert [r for r in all_rows() if r['block_id'] not in (a, 'block-B')] == [r for r in others if r['block_key'] != 'sec:p1']


@pytest.mark.parametrize('nested', [False, True])
def test_legacy_duplicate_rows_have_no_plan_key_and_do_not_climb(issue, monkeypatch, nested):
    if nested:
        fold = issue.blocks['sec:fold']['block_id']
        child = issue.n.kids[fold][0]
        child['parent'] = {'type': 'block_id', 'block_id': fold}
        bid = child['id']
        # The child itself has two legacy rows; the parent fold still has its single key.
        db.exec("INSERT INTO notion_blocks(source_id,block_key,block_id,hash,run_id,template) VALUES ('s','sec:fold-a',?,'h1','r1','default')", bid)
        db.exec("INSERT INTO notion_blocks(source_id,block_key,block_id,hash,run_id,template) VALUES ('s','sec:fold-b',?,'h2',NULL,NULL)", bid)
        assert len(rows(fold)) == 1
        issue.n.say(bid, '课代表：这一块按原话改')['created_time'] = '2020-01-01T00:00:00Z'
        read(issue, None, block=bid)
        gets = [p for m, p, *_ in issue.n.calls if m == 'GET' and p.startswith('/blocks/')]
        assert gets == [f'/blocks/{bid}']
    else:
        bid = issue.blocks['sec:p2']['block_id']
        db.exec("INSERT INTO notion_blocks(source_id,block_key,block_id,hash,run_id,template) VALUES ('s','sec:old',?,'h-old','r0','default')", bid)
        say(issue, 'sec:p2', '课代表：这一块按原话改'); read(issue, 'sec:p2')
    assert len(rows(bid)) == 2
    assert db.one('SELECT plan_key FROM issue_comments')['plan_key'] is None
    assert db.q('SELECT * FROM issue_positions') == []
    t = I._payload(source())['threads'][0]
    assert t['can_fix'] is False and t['plan_text'] == ''
    before = [B.plain(b) for b in issue.n.kids[issue.pid]]
    calls = model(monkeypatch, {'replies': [dict(id=t['id'], result='已答', text='这一块现在不能定位到计划里的段落，先回答。')]})
    I.due()
    assert len(calls) == 1 and calls[0]['threads'][0]['can_fix'] is False
    assert delivery.deliver('s')['status'] == 'DELIVERED'; I.due()
    assert db.q('SELECT * FROM issue_fixes') == []
    assert [B.plain(b) for b in issue.n.kids[issue.pid]] == before
    assert len(posts(issue)) == 1 and '已答' in db.j(posts(issue)[0])


def test_failure_between_delete_and_insert_rolls_back(issue, monkeypatch):
    a = issue.blocks['sec:p1']['block_id']
    before = all_rows()
    new_run(); shift(issue)
    seen = {'deleted': 0}
    real = db.tx

    class Guarded:
        def __init__(self, c):
            self.c = c
        def __getattr__(self, name):
            return getattr(self.c, name)
        def execute(self, sql, params=()):
            if sql.startswith('INSERT OR REPLACE INTO notion_blocks') and seen['deleted']:
                raise RuntimeError('injected after DELETE')
            cur = self.c.execute(sql, params)
            if sql.startswith('DELETE FROM notion_blocks WHERE source_id=? AND block_id=?'):
                seen['deleted'] += cur.rowcount
            return cur

    @contextmanager
    def tx():
        with real() as c:
            yield Guarded(c)
    with monkeypatch.context() as mp:
        mp.setattr(db, 'tx', tx)
        assert delivery.deliver('s')['status'] == 'FAILED'
    assert seen['deleted'] >= 1
    assert all_rows() == before and [r['block_key'] for r in rows(a)] == ['sec:p1']
    assert delivery.deliver('s')['status'] == 'DELIVERED'
    assert [(r['block_key'], r['run_id']) for r in rows(a)] == [('sec:p2', 'r2')]
    ids = [r['block_id'] for r in all_rows()]
    assert len(ids) == len(set(ids))
