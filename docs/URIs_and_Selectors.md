
# URIs and Selectors (updated)

## Dataset (Lance-backed table)
`data+table://{org}/{domain}/{env}/{family}/{name}[?at=stage:NAME|commit:ID|ts:ISO]`

## Experiment
`experiment://{org}/{team}/{env}/{project}/{pipeline}`

## Run
`run://{project}/{pipeline}/{run_id}`

## Model
`model://{org}/{team}/{env}/{project}/{model_name}[?at=version:N|stage:NAME|commit:ID]`

## Artifact (typed)
`artifact+{type}://{org}/{team}/{env}/{project}/{name}[?at=commit:ID]`

Common artifact types:
- `prompt` — prompt templates & few-shot sets
- `card` — generic ABOUT/README pair
- `dataset-card` — dataset ABOUT/README
- `misc` — any other

### Selectors
- `stage:NAME`  → dataset/model stage pointers (lakeFS) or MLflow stage (models)
- `version:N`   → model version directory (versions/vN)
- `commit:ID`   → resolve to exact commit (caller may materialize a read branch)
- `ts:ISO`      → future: resolve to nearest commit by timestamp

### Prompt modeling
- Store prompts as `artifact+prompt://...` and write `prompt.txt` alongside ABOUT/README.
- For per-run prompts, also store under the run folder to capture exact provenance.
