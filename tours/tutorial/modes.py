"""Print the permissions of each path, as ls -l shows them, and the path.
    modes.py PATH..."""
import os
import stat
import sys

for path in sys.argv[1:]:
    print(stat.filemode(os.lstat(path).st_mode), path)
