"""Read a file by its logical URI from Python, and pin it."""
import sys

from provcat import Catalog

cat = Catalog.open()  # the file in PROVCAT_CONFIG
uri = sys.argv[1]
print("physical:", cat.resolve(uri).physical)
print("pinned:  ", cat.pin(uri))
print("content: ", cat.read(uri).decode().strip())
