"""Print one field of the JSON that provr run printed, such as run_id.
    field.py FILE KEY"""
import json
import sys

print(json.loads(open(sys.argv[1]).read())[sys.argv[2]])
