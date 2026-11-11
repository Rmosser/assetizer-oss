"""Checkpointed block keys and editions stay together, including unfinished deliveries and legacy rows."""
import ast
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from assetizer import db, delivery, issue_asks as I
from assetizer.notion import blocks as B
from assetizer.notion.sync import Sync
from test_issue_asks import issue, say, read, model, posts, mapped
from test_issue_asks_round4 import real_page, switch, source, CFG
from test_topic_notion import home


@pytest.mark.parametrize('retained', [False, True])
def test_checkpoint_edition_during_delivery_and_followup_can_fix(issue, monkeypatch, retained):
    real_page(issue, monkeypatch, item='独一段')
    old = issue.blocks['sec:p2']['block_id']
    if not retained:
        # Remote deletion forces a fresh append; the retained case uses the very same block id.
        issue.n._req('DELETE', f'/blocks/{old}')
    switch()
    captured = {}
    check = Sync.check
    def during_check(self, *args, **kwargs):
        result = check(self, *args, **kwargs)
        issue.blocks = mapped()
        row = issue.blocks['sec:p1']
        assert (row['block_id'] == old) is retained
        assert row['run_id'] == 'r1' and row['template'] == 'chapters'
        assert source()['notion_synced_template'] == 'default'
        c = say(issue, 'sec:p1')
        read(issue, 'sec:p1')
        pos = db.one('SELECT * FROM issue_positions')
        assert (pos['run_id'], pos['template'], pos['plan_key']) == ('r1', 'chapters', 'sec:p1')
        captured.update(comment=c, row=row)
        return result
    with monkeypatch.context() as mp:
        mp.setattr(Sync, 'check', during_check)
        assert delivery.deliver('s')['status'] == 'DELIVERED'
    model(monkeypatch); I.due()
    say(issue, 'sec:p1', '课代表：请按原话改', thread=captured['comment']['discussion_id'])
    read(issue, 'sec:p1', eid='followup')
    calls = model(monkeypatch, {'replies': [dict(id=captured['comment']['discussion_id'], result='已改',
                                               text='保留条件。', fix='只是观察。')]})
    I.due()
    assert len(calls) == 1 and calls[0]['threads'][0]['can_fix']
    fix = db.one('SELECT * FROM issue_fixes')
    assert (fix['run_id'], fix['template'], fix['plan_key']) == ('r1', 'chapters', 'sec:p1')
    assert len(posts(issue)) == 1 and '已改' not in db.j(posts(issue))
    assert delivery.deliver('s')['status'] == 'DELIVERED'; I.due()
    assert B.plain(next(x['block'] for x in delivery.page_plan(source(), 'r1', CFG) if x['key'] == 'sec:p1')) == '只是观察。'
    assert len(posts(issue)) == 2 and '已改' in db.j(posts(issue)[-1]) and not I.pending()


@pytest.mark.parametrize('saved_template', ['default', None])
def test_legacy_mapping_uses_sources_pair_then_target_fallback(issue, monkeypatch, saved_template):
    switch()
    db.exec('UPDATE notion_blocks SET run_id=NULL,template=NULL')
    db.exec('UPDATE sources SET notion_synced_run=?,notion_synced_template=?', 'r-legacy', saved_template)
    say(issue, 'sec:p0'); read(issue, 'sec:p0')
    pos = db.one('SELECT * FROM issue_positions')
    assert (pos['run_id'], pos['template'], pos['plan_key']) == ('r-legacy', saved_template or 'chapters', 'sec:p0')


def test_round4_mapping_migration_only_appends_nullable_columns(home):
    # The Round 4 CREATE has the same four original columns; all other tables use current CREATEs.
    module = ast.parse(Path(db.__file__).read_text())
    schema = next(ast.literal_eval(n.value) for n in module.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'SCHEMA' for t in n.targets))
    start = schema.index('CREATE TABLE IF NOT EXISTS notion_blocks (')
    end = schema.index('\n);', start) + len('\n);')
    old = '''CREATE TABLE IF NOT EXISTS notion_blocks (
  source_id TEXT NOT NULL,
  block_key TEXT NOT NULL,
  block_id TEXT NOT NULL,
  hash TEXT NOT NULL,
  PRIMARY KEY (source_id, block_key)
);'''
    cx = sqlite3.connect(':memory:', isolation_level=None); cx.row_factory = sqlite3.Row
    cx.executescript(schema[:start] + old + schema[end:])
    cx.execute("INSERT INTO notion_blocks VALUES ('s','sec:p0','block-old','hash-old')")
    before = dict(cx.execute('SELECT * FROM notion_blocks').fetchone())
    db._migrate(cx); db._migrate(cx)
    after = dict(cx.execute('SELECT * FROM notion_blocks').fetchone())
    assert list(after) == [*before, 'run_id', 'template']
    assert after.pop('run_id') is None and after.pop('template') is None and after == before
    assert cx.execute('SELECT COUNT(*) FROM notion_blocks').fetchone()[0] == 1
    cx.close()


def test_parent_mapping_supplies_both_key_and_edition(issue, monkeypatch):
    parent = issue.blocks['sec:fold']['block_id']
    block = issue.n.kids[parent][0]
    block['parent'] = {'type': 'block_id', 'block_id': parent}
    child = block['id']
    db.exec("UPDATE notion_blocks SET run_id='r-parent',template='chapters' WHERE block_id=?", parent)
    c = issue.n.say(child, '课代表：折叠里的话'); c['created_time'] = '2020-01-01T00:00:00Z'
    I.scan_one('s', issue.pid, child, n=issue.n, bot=None)
    pos = db.one('SELECT * FROM issue_positions')
    assert (pos['run_id'], pos['template'], pos['plan_key']) == ('r-parent', 'chapters', 'sec:fold')
    assert db.one('SELECT depth FROM issue_comments')['depth'] == 1


def test_scan_keeps_one_mapping_snapshot_when_checkpoint_changes_during_get(issue, monkeypatch):
    old = deepcopy(issue.blocks['sec:p0']); say(issue, 'sec:p0')
    req = issue.n._req
    def checkpoint(method, path, *args, **kwargs):
        if method == 'GET' and path == f"/blocks/{old['block_id']}":
            db.exec("UPDATE notion_blocks SET block_key='sec:next',run_id='r-next',template='chapters' WHERE block_id=?", old['block_id'])
        return req(method, path, *args, **kwargs)
    monkeypatch.setattr(issue.n, '_req', checkpoint)
    read(issue, 'sec:p0')
    pos = db.one('SELECT * FROM issue_positions')
    assert (pos['plan_key'], pos['run_id'], pos['template']) == ('sec:p0', 'r1', 'default')
    current = db.one('SELECT * FROM notion_blocks WHERE block_id=?', old['block_id'])
    assert (current['block_key'], current['run_id'], current['template']) == ('sec:next', 'r-next', 'chapters')
