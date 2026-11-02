"""Make a read-only snapshot of one commit of the orders, for a program to read."""
import sys

from provcat import Catalog

cat = Catalog.open()
snap = cat.snapshot(sys.argv[1])
print("pinned:", snap.pinned)
print("digest:", snap.digest)
for f in sorted(snap.path.rglob("*")):
    print(f.relative_to(snap.path), oct(f.stat().st_mode & 0o777))
