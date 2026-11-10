"""Delete one event of a run from the evidence file, to show that the check finds it."""
import sqlite3
import sys

path, run_id, seq = sys.argv[1], sys.argv[2], int(sys.argv[3])
conn = sqlite3.connect(path)
conn.execute("DELETE FROM provevi_events WHERE run_id = ? AND seq = ?", (run_id, seq))
conn.commit()
conn.close()
