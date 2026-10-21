"""Example 00: Dataset registration (create + card)."""
from provcat.datasets import create_or_update

uri = "data+table://acme/shared/prod/agreements/qa_outputs"
repo = "agreements-qa_outputs-data"
branch = "main"

out, base, lance_path = create_or_update(
    repo, branch, uri,
    org="acme", domain="shared", env="prod", family="agreements", name="qa_outputs",
    title="Agreements QA Outputs", description="Extracted QA outputs for credit agreements"
)
print(out)
print("Dataset base:", base)
print("Lance path:", lance_path)
