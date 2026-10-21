"""Example 03 (End-to-End in Python): Dataset register + ensure table."""
from provcat.datasets import create_or_update, lance_path_from_parts
from provcat.backends_lance import is_available, ensure_table

# 0) Dataset registration (card)
out, base, lance_path = create_or_update(
    "agreements-qa_outputs-data", "main",
    "data+table://acme/shared/prod/agreements/qa_outputs",
    "acme", "shared", "prod", "agreements", "qa_outputs",
    title="Agreements QA Outputs", description="Extracted QA outputs"
)
print(out)

# 1) Ensure Lance table exists
print("Lance available:", is_available())
try:
    ensure_table(lance_path, schema={"fields":[{"name":"id","type":"int64"}, {"name":"ts","type":"timestamp"}]})
    print({"ok": True, "created": lance_path})
except Exception as e:
    print({"ok": False, "error": str(e), "path": lance_path})
