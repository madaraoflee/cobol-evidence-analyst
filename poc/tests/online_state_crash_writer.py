"""Abrupt-process fixture for production OnlineState; no network or model."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from online_state import OnlineState

root, committed = sys.argv[1:]
state = OnlineState(root, journal_mode='DELETE' if committed == '0' else None)
with state.analysis_writes():
    with state.transaction() as db:
        db.execute('PRAGMA cache_size=1')
        db.execute('PRAGMA cache_spill=ON')
        state.put(db, 'jobs', 'job', {'id': 'job', 'value': 'committed' if committed == '1' else 'uncommitted',
            'payload': 'Original fixture state remains atomic. ' * 30000})
        state.put(db, 'settings', 'marker', {'value': 'committed' if committed == '1' else 'uncommitted'})
        if committed == '0':
            os._exit(73)
    # Exit after the actual production transaction commit, while its analysis
    # lease is still live; no Python close/finalizer can stand in for recovery.
    os._exit(74)
