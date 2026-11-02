"""Delete one event of a run from a sqlite evidence store, to show that the
check finds it.
    drop_event.py DB RUN SEQ"""
import sqlite3
import sys

db, run, seq = sys.argv[1], sys.argv[2], int(sys.argv[3])
with sqlite3.connect(db) as conn:
    conn.execute("DELETE FROM provevi_events WHERE run_id = ? AND seq = ?", (run, seq))
