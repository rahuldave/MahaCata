"""Read logical URIs through fsspec, as pandas and other libraries do."""
import fsspec

from provcat import Catalog

cat = Catalog.open()
fs = cat.fs()  # also lets fsspec.open take the logical schemes
print(fs.ls("files://sales/orders", detail=False))
with fsspec.open("files://sales/orders/day1.csv", "rt") as f:
    print(f.readline().strip())
