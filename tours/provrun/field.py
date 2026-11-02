"""Print one field of the JSON that provr run printed, such as run_id.
    field.py FILE KEY
A field that holds more than one value prints as JSON."""
import json
import sys

value = json.loads(open(sys.argv[1]).read())[sys.argv[2]]
print(json.dumps(value, indent=2) if isinstance(value, (dict, list)) else value)
