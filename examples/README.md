
# provcat Examples

This folder contains small, **well-documented** Python examples using the module APIs
(so you don't need to shell out to the CLI). Adjust S3/repo names for your environment.

## 01_prompt_and_run.py
- Creates/updates an experiment card
- Starts a run
- Writes a project-level typed prompt (`artifact+prompt://...`) and also a frozen copy under the run folder
- Ends the run

## 02_dataset_wam_and_publish.py
- Dataset Write→Audit→Merge (branch create/merge)
- Sets a stage pointer
- Publishes a model version (lineage + card + MLflow registration)

## 03_e2e_python_hello_world.py
- Full E2E: dataset WAM, experiment init, parent+child runs, publish v1 (child), merge to main, publish v2 (main)

### Prereqs
- lakeFS server + S3 gateway env vars set
- `pip install mlflow lakefs-client boto3 jinja2 omegaconf`
- A resolver config mapping experiments/data/models to repos (see `config/resolver.sample.yaml` for hints)
