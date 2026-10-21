# provc CLI Reference (resolver-backed)

Global: `--cfg config/resolver.yaml`

## resolve
- Show mapping for a logical URI.
- `provc resolve data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production`

## branch
- `provc branch create --uri <any-URI> --name <BRANCH>`
- `provc branch list   --uri <any-URI>`
- `provc branch merge  --uri <any-URI> --from-branch <SRC> [--to main] [--message MSG]`

## stage
- `provc stage set --uri <data|model URI> --stage <NAME> --commit-id <ID>`
- `provc stage get --uri <data|model URI> --stage <NAME>`

## dataset
- `provc dataset create --uri data+table://ORG/DOMAIN/ENV/FAMILY/NAME [--name T] [--desc D]`
- `provc dataset meta-set --uri data+table://... [--name T] [--desc D]`

## data
- `provc data ensure-table --uri data+table://... [--schema-json JSON] [--schema-file FILE]`

## experiment
- `provc experiment create --uri experiment://ORG/TEAM/ENV/PROJECT/PIPELINE [--name T] [--desc D]`
- `provc experiment meta-set --uri experiment://... [--name T] [--desc D]`

## run
- `provc run start --experiment-uri experiment://ORG/TEAM/ENV/PROJECT/PIPELINE [--name N] [--desc D] [--parent RUN_ID]`
- `provc run end`

## model
- `provc model publish --from-run run://PROJECT/PIPELINE/RUN_ID --to model://ORG/TEAM/ENV/PROJECT/MODEL_NAME --version N [--inputs ...]`

## artifact
- `provc artifact create-prompt --uri artifact+prompt://ORG/TEAM/ENV/PROJECT/NAME --prompt "..." [--name T] [--desc D]`
- `provc artifact create-card   --uri artifact+card://ORG/TEAM/ENV/PROJECT/NAME --type card [--name T] [--desc D]`


---

## Resolver backends

Pass `--cfg` as either a **YAML path** or an **etcd URL**:

- YAML/OmegaConf: `--cfg config/resolver.yaml`
- etcd: `--cfg etcd://HOST:PORT/prefix`  
  (Bootstrap etcd with `examples/etcd_bootstrap.py`. You can set `PROVCAT_FALLBACK_CFG=config/resolver.yaml` to fill any missing sections.)

## LakeFS S3 gateway

`provcat` writes to S3 using **boto3** if available. Set:

- `S3_ENDPOINT_URL` (e.g., `http://localhost:8000` for lakeFS S3 gateway)
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (optional, defaults `us-east-1`)

If boto3 is not installed or the gateway is unreachable, provcat **falls back** to writing under the local path `/mnt/data/_s3/...` for development.



## resolver (admin for etcd backend)

- **put** a section:
```
python -m provcat.provc --cfg etcd://localhost:2379/provcat   resolver put --key repos --json-file config/repos.json
```
or:
```
python -m provcat.provc --cfg etcd://localhost:2379/provcat   resolver put --key routing --json '{"data+table":{"repo_kind":"data","path_kind":"data_table"}}'
```

- **get** a section:
```
python -m provcat.provc --cfg etcd://localhost:2379/provcat resolver get --key paths
```

Sections: `repos`, `paths`, `routing`.


---

## Environment variables

Resolver selection (so you don't need `--cfg` every time):
- `PROVCAT_CFG` : Full resolver URL/path. Examples:
  - `config/resolver.yaml` (YAML)
  - `etcd://localhost:2379/provcat` (etcd)
- `PROVCAT_RESOLVER` : `yaml` | `etcd` (used if `PROVCAT_CFG` not set)
- `PROVCAT_YAML_PATH` : Path to YAML if using `yaml` mode (default: `config/resolver.sample.yaml`)
- `PROVCAT_ETCD_ENDPOINT` : `host:port` for etcd (default: `localhost:2379`)
- `PROVCAT_ETCD_PREFIX` : etcd key prefix (default: `provcat`)
- `PROVCAT_FALLBACK_CFG` : optional YAML path to merge missing etcd sections

LakeFS / S3 (for boto3 client):
- `S3_ENDPOINT_URL` (e.g., `http://localhost:8000`)
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (default: `us-east-1`)

---

## data

- **ensure-table**  
  `provc data ensure-table --uri data+table://... [--schema-json JSON] [--schema-file FILE]`

- **card** *(new)*  
  Regenerate dataset cards (ABOUT.json + README.md) at the dataset's base folder.  
  `provc data card --uri data+table://... [--name T] [--desc D]`

## resolver (admin for etcd backend)

- **put** section: see earlier
- **get** section: see earlier
- **list** *(new)*: dump all sections  
  `python -m provcat.provc --cfg etcd://localhost:2379/provcat resolver list`
