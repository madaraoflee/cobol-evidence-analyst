"""Isolated crash injector using the production online-inventory connection."""
import json
import os
from pathlib import Path
import sys

repo, root, payload_path, committed, legacy_delete = sys.argv[1:]
sys.path.insert(0, str(Path(repo) / 'poc'))
from online_inventory_store import OnlineInventoryStore

payload = json.loads(Path(payload_path).read_text(encoding='utf-8'))
store = OnlineInventoryStore(root, journal_mode="DELETE" if legacy_delete == "1" else None)
session = store.session(**payload['options'], identifier=payload['session_id'])
with store.connection() as db:
    if legacy_delete == '1':
        actual = db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
        assert actual.lower() == 'delete', actual
    db.execute('PRAGMA cache_size=1')
    db.execute('PRAGMA cache_spill=ON')
    assert db.execute('PRAGMA synchronous').fetchone()[0] == 2
    row = dict(payload['row'])
    row['summary'] = payload['marker'] + ' original-source identity retained. ' * 30000
    session._put(db, 'findings', payload['key'], row, row['path'])
    meta = json.loads(db.execute(
        'SELECT meta FROM sessions WHERE id=?', (session.id,)).fetchone()[0])
    meta['persistence_crash_probe'] = payload['marker']
    session._save_meta(db, meta)
    if committed == '0':
        # No context-manager exit, destructor, rollback or SQLite close.
        os._exit(73)
# The real production connection has committed before this abrupt exit.
os._exit(74)
