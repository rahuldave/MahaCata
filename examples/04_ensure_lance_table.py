"""Example 04: Ensure Lance table exists for a dataset URI."""
from provcat.datasets import lance_path_from_parts
from provcat.backends_lance import is_available, ensure_table

repo, branch = "agreements-qa_outputs-data", "main"
org, domain, env, family, name = "acme", "shared", "prod", "agreements", "qa_outputs"
path = lance_path_from_parts(repo, branch, org, domain, env, family, name)

print("Lance available:", is_available())
try:
    out = ensure_table(path, schema={"fields":[{"name":"id","type":"int64"}, {"name":"ts","type":"timestamp"}]})
    print(out)
except Exception as e:
    print("Could not create table:", e)
    print("Intended path:", path)
