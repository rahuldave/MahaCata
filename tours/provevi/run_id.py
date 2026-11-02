"""Print the run ID from the JSON that provr run printed into a file."""
import json
import sys

print(json.load(open(sys.argv[1]))["run_id"])
