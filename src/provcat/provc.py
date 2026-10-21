
import argparse, json
from provcat.impl_yaml import OmegaConfResolver
from provcat.impl_etcd import EtcdResolver
from provcat.urls import split_logical_id
from provcat.branches import create as br_create, list as br_list, merge as br_merge
from provcat.stages import set_stage as st_set, get_stage as st_get
from provcat.experiments import create_or_update as exp_upsert
from provcat.runs import start as run_start, end as run_end
from provcat.models import write_lineage, write_model_card, register_version, promote_stage
from provcat.artifacts import write_prompt, write_card
from provcat.datasets import create_or_update as ds_create
from provcat.backends_lance import ensure_table as lance_ensure

def _print(obj): print(json.dumps(obj, indent=2))


def _pick_cfg(cli_cfg: str | None) -> str:
    # Priority: CLI --cfg > PROVCAT_CFG > PROVCAT_RESOLVER + env parts > default sample
    if cli_cfg:
        return cli_cfg
    env_cfg = os.getenv("PROVCAT_CFG")
    if env_cfg:
        return env_cfg
    # Build from PROVCAT_RESOLVER + parts
    mode = os.getenv("PROVCAT_RESOLVER", "yaml").lower()
    if mode == "etcd":
        endpoint = os.getenv("PROVCAT_ETCD_ENDPOINT", "localhost:2379")
        prefix = os.getenv("PROVCAT_ETCD_PREFIX", "provcat")
        return f"etcd://{endpoint}/{prefix}"
    # default yaml
    return os.getenv("PROVCAT_YAML_PATH", "config/resolver.sample.yaml")

def _resolver(cfg_url: str):
    return EtcdResolver(cfg_url) if cfg_url.startswith('etcd://') else OmegaConfResolver(cfg_url)

    return EtcdResolver(cfg_url) if cfg_url.startswith('etcd://') else OmegaConfResolver(cfg_url)

def cmd_resolve(args):
    r = _resolver(_pick_cfg(args.cfg)); res=r.resolve(args.logical_id, context={})
    _print(dict(
        logical=res.logical,
        selector=(vars(res.selector) if res.selector else None),
        lakefs=res.lakefs
    ))

# ---- branch ----
def cmd_branch_create(args):
    r = _resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    out = br_create(repo=res.lakefs["repo"], source_branch=res.lakefs["branch"], new_branch=args.name)
    _print(out)

def cmd_branch_list(args):
    r = _resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    out = br_list(repo=res.lakefs["repo"])
    _print(out)

def cmd_branch_merge(args):
    r = _resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    out = br_merge(repo=res.lakefs["repo"], source_branch=args.from_branch, dest_branch=args.to, message=args.message or f"merge {args.from_branch}->{args.to}")
    _print(out)

# ---- stage ----
def cmd_stage_set(args):
    r=_resolver(_pick_cfg(args.cfg)); cfg=r.cfg
    scheme, parts, _ = split_logical_id(args.uri)
    res=r.resolve(args.uri, context={})
    if scheme=="data+table":
        resource=f"{parts[3]}/{parts[4]}"
        env = parts[2]
    elif scheme=="model":
        resource="versions"
        env = parts[2]
    else:
        raise SystemExit("stage set only supports data+table:// and model://")
    out = st_set(cfg, env=env, repo=res.lakefs["repo"], resource=resource, stage=args.stage, commit_id=args.commit_id)
    _print(out)

def cmd_stage_get(args):
    r=_resolver(_pick_cfg(args.cfg)); cfg=r.cfg
    scheme, parts, _ = split_logical_id(args.uri)
    res=r.resolve(args.uri, context={})
    if scheme=="data+table":
        resource=f"{parts[3]}/{parts[4]}"
        env = parts[2]
    elif scheme=="model":
        resource="versions"
        env = parts[2]
    else:
        raise SystemExit("stage get only supports data+table:// and model://")
    out = st_get(cfg, env=env, repo=res.lakefs["repo"], resource=resource, stage=args.stage)
    _print(out)

# ---- dataset ----
def cmd_dataset_create(args):
    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    org, domain, env, family, name = args.uri.split('://',1)[1].split('/')
    repo, branch = res.lakefs["repo"], res.lakefs["branch"]
    out, base, lance_path = ds_create(repo, branch, args.uri, org, domain, env, family, name, title=args.name, description=args.desc)
    _print(out)

def cmd_dataset_meta_set(args):
    return cmd_dataset_create(args)

# ---- data ----

def cmd_data_ensure_table(args):
    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    lance_path = res.lakefs["paths"]["lance_path"]
    schema = None
    if args.schema_json:
        schema = json.loads(args.schema_json)
    elif args.schema_file:
        schema = json.load(open(args.schema_file))
    try:
        out = lance_ensure(lance_path, schema=schema)
    except Exception as e:
        out = {"ok": False, "error": str(e), "lance_path": lance_path}
    _print(out)

def cmd_data_card(args):
    # Regenerate dataset ABOUT.json/README.md at dataset base
    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    base = res.lakefs["paths"]["dataset_base"]
    # Extract parts for title default
    parts = args.uri.split('://',1)[1].split('/')
    org, domain, env, family, name = parts
    title = args.name or f"{family}/{name}"
    desc = args.desc or args.uri
    from provcat.s3io import put_text
    from jinja2 import Environment, FileSystemLoader
    import pathlib as _p
    _TPL_DIR = (_p.Path(__file__).parent.parent.parent / "templates")
    env_j = Environment(loader=FileSystemLoader(str(_TPL_DIR)))
    put_text(f"{base}/ABOUT.json", env_j.get_template("about_json.j2").render(name=title, description=desc, uri=args.uri))
    put_text(f"{base}/README.md", env_j.get_template("dataset_readme.md.j2").render(name=title, description=desc, uri=args.uri))
    _print({"ok": True, "dataset_base": base})

    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    lance_path = res.lakefs["paths"]["lance_path"]
    schema = None
    if args.schema_json:
        schema = json.loads(args.schema_json)
    elif args.schema_file:
        schema = json.load(open(args.schema_file))
    try:
        out = lance_ensure(lance_path, schema=schema)
    except Exception as e:
        out = {"ok": False, "error": str(e), "lance_path": lance_path}
    _print(out)

# ---- experiment ----
def cmd_experiment_create(args):
    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    base = res.lakefs["paths"]["experiment_base"]
    out = exp_upsert(base, uri=args.uri, name=args.name, description=args.desc)
    _print(out)

def cmd_experiment_meta_set(args):
    return cmd_experiment_create(args)

# ---- runs ----
def _exp_name_from_uri(uri:str)->str:
    parts = uri.split('://',1)[1].split('/')
    return f"{parts[3]}/{parts[4]}"

def cmd_run_start(args):
    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.experiment_uri, context={})
    exp_name = _exp_name_from_uri(args.experiment_uri)
    run_base = res.lakefs["paths"]["experiment_base"].replace("/mlflow/experiments/","/runs/")
    out = run_start(exp_name, run_base_s3=run_base, run_name=args.name, run_desc=args.desc, parent_run_id=args.parent)
    _print(out)

def cmd_run_end(args):
    out = run_end()
    _print(out)

# ---- model publish ----
def cmd_model_publish(args):
    r=_resolver(_pick_cfg(args.cfg))
    # resolve model version base
    model_res = r.resolve(args.to.split("?at=")[0] + f"?at=version:{args.version}", context={})
    version_base = model_res.lakefs["paths"]["model_base"]
    # write lineage + card
    write_lineage(version_base_s3=version_base, produced_by_run=args.from_run, inputs=args.inputs or [], commit_id="TBD")
    write_model_card(version_base_s3=version_base, model_uri=args.to, name="Model Version", description="Published via provc", version=args.version)
    # register in MLflow (source is the version base in lakeFS)
    model_name = args.to.split('?at=')[0].split('/')[-1]
    register_version(model_name=model_name, version_base_s3=version_base, description=f"Published v{args.version}")
    _print({"ok": True, "version_base": version_base})

# ---- artifact ----
def cmd_artifact_prompt_create(args):
    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    base = res.lakefs["paths"]["artifact_base"]
    write_prompt(base, name=args.name or "Prompt", description=args.desc or args.uri, uri=args.uri, prompt_text=args.prompt, meta={"source":"provc"})
    _print({"ok": True, "artifact_base": base})


def cmd_artifact_card_create(args):
    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    base = res.lakefs["paths"]["artifact_base"]
    write_card(base, name=args.name or "Artifact", description=args.desc or args.uri, uri=args.uri, artifact_type=args.type, extra={"source":"provc"})
    _print({"ok": True, "artifact_base": base})

# ---- resolver admin (etcd only) ----
def cmd_resolver_put(args):
    if not _pick_cfg(args.cfg).startswith("etcd://"):
        raise SystemExit("resolver put requires --cfg etcd://HOST:PORT/prefix")
    from provcat.impl_etcd import EtcdResolver
    r=EtcdResolver(_pick_cfg(args.cfg))
    if args.json and args.json_file:
        raise SystemExit("Provide either --json or --json-file, not both")
    payload = None
    if args.json:
        payload = json.loads(args.json)
    elif args.json_file:
        with open(args.json_file, "r") as f:
            payload = json.load(f)
    else:
        raise SystemExit("Missing --json or --json-file")
    r.put_section(args.key, payload)
    _print({"ok": True, "section": args.key})


def cmd_resolver_get(args):
    if not _pick_cfg(args.cfg).startswith("etcd://"):
        raise SystemExit("resolver get requires etcd backend (use --cfg or PROVCAT_RESOLVER=etcd)")
    from provcat.impl_etcd import EtcdResolver
    r=EtcdResolver(_pick_cfg(args.cfg))
    data = r.get_section(args.key)
    _print({"section": args.key, "data": data})

def cmd_resolver_list(args):
    if not _pick_cfg(args.cfg).startswith("etcd://"):
        raise SystemExit("resolver list requires etcd backend (use --cfg or PROVCAT_RESOLVER=etcd)")
    from provcat.impl_etcd import EtcdResolver
    r=EtcdResolver(_pick_cfg(args.cfg))
    out = {
        "repos": r.get_section("repos"),
        "paths": r.get_section("paths"),
        "routing": r.get_section("routing"),
    }
    _print(out)

    if not _pick_cfg(args.cfg).startswith("etcd://"):
        raise SystemExit("resolver get requires --cfg etcd://HOST:PORT/prefix")
    from provcat.impl_etcd import EtcdResolver
    r=EtcdResolver(_pick_cfg(args.cfg))
    data = r.get_section(args.key)
    _print({"section": args.key, "data": data})

    r=_resolver(_pick_cfg(args.cfg)); res=r.resolve(args.uri, context={})
    base = res.lakefs["paths"]["artifact_base"]
    write_card(base, name=args.name or "Artifact", description=args.desc or args.uri, uri=args.uri, artifact_type=args.type, extra={"source":"provc"})
    _print({"ok": True, "artifact_base": base})

def main(argv=None):
    ap=argparse.ArgumentParser("provc")
    ap.add_argument("--cfg", required=False)
    sp=ap.add_subparsers(dest="cmd", required=True)

    # resolve
    rsv=sp.add_parser("resolve"); rsv.add_argument("logical_id")

    # branch
    b=sp.add_parser("branch"); bs=b.add_subparsers(dest="bcmd", required=True)
    c=bs.add_parser("create"); c.add_argument("--uri", required=True); c.add_argument("--name", required=True)
    l=bs.add_parser("list"); l.add_argument("--uri", required=True)
    m=bs.add_parser("merge"); m.add_argument("--uri", required=True); m.add_argument("--from-branch", required=True); m.add_argument("--to", default="main"); m.add_argument("--message")

    # stage
    st=sp.add_parser("stage"); sts=st.add_subparsers(dest="scmd", required=True)
    s1=sts.add_parser("set"); s1.add_argument("--uri", required=True); s1.add_argument("--stage", required=True); s1.add_argument("--commit-id", required=True)
    s2=sts.add_parser("get"); s2.add_argument("--uri", required=True); s2.add_argument("--stage", required=True)

    # dataset
    ds=sp.add_parser("dataset"); dss=ds.add_subparsers(dest="dcmd", required=True)
    d1=dss.add_parser("create"); d1.add_argument("--uri", required=True); d1.add_argument("--name"); d1.add_argument("--desc")
    d2=dss.add_parser("meta-set"); d2.add_argument("--uri", required=True); d2.add_argument("--name"); d2.add_argument("--desc")

    # data
    dt=sp.add_parser("data"); dts=dt.add_subparsers(dest="dcmd2", required=True)
    de=dts.add_parser("ensure-table"); de.add_argument("--uri", required=True); de.add_argument("--schema-json"); de.add_argument("--schema-file")
    dc=dts.add_parser("card"); dc.add_argument("--uri", required=True); dc.add_argument("--name"); dc.add_argument("--desc")

    # experiment
    ex=sp.add_parser("experiment"); exs=ex.add_subparsers(dest="excmd", required=True)
    ex1=exs.add_parser("create"); ex1.add_argument("--uri", required=True); ex1.add_argument("--name"); ex1.add_argument("--desc")
    ex2=exs.add_parser("meta-set"); ex2.add_argument("--uri", required=True); ex2.add_argument("--name"); ex2.add_argument("--desc")

    # runs
    rn=sp.add_parser("run"); rns=rn.add_subparsers(dest="rcmd", required=True)
    rs1=rns.add_parser("start"); rs1.add_argument("--experiment-uri", required=True); rs1.add_argument("--name"); rs1.add_argument("--desc"); rs1.add_argument("--parent")
    rs2=rns.add_parser("end")

    # model
    mp=sp.add_parser("model"); mps=mp.add_subparsers(dest="mmcmd", required=True)
    pub=mps.add_parser("publish"); pub.add_argument("--from-run", required=True); pub.add_argument("--to", required=True); pub.add_argument("--version", required=True); pub.add_argument("--inputs", nargs="*")

    # artifact
    ar=sp.add_parser("artifact"); ars=ar.add_subparsers(dest="acmd", required=True)
    apm=ars.add_parser("create-prompt"); apm.add_argument("--uri", required=True); apm.add_argument("--name"); apm.add_argument("--desc"); apm.add_argument("--prompt", required=True)
    acd=ars.add_parser("create-card"); acd.add_argument("--uri", required=True); acd.add_argument("--type", required=True); acd.add_argument("--name"); acd.add_argument("--desc")

    
# resolver
rv=sp.add_parser("resolver"); rvs=rv.add_subparsers(dest="rvcmd", required=True)
rvp=rvs.add_parser("put"); rvp.add_argument("--key", required=True, choices=["repos","paths","routing"]); rvp.add_argument("--json"); rvp.add_argument("--json-file")
rvg=rvs.add_parser("get"); rvg.add_argument("--key", required=True, choices=["repos","paths","routing"])
        rvl=rvs.add_parser("list")

args=ap.parse_args(argv)

    if args.cmd=="resolve": return cmd_resolve(args)
    if args.cmd=="branch":
        if args.bcmd=="create": return cmd_branch_create(args)
        if args.bcmd=="list": return cmd_branch_list(args)
        if args.bcmd=="merge": return cmd_branch_merge(args)
    if args.cmd=="stage":
        if args.scmd=="set": return cmd_stage_set(args)
        if args.scmd=="get": return cmd_stage_get(args)
    if args.cmd=="dataset":
        if args.dcmd=="create": return cmd_dataset_create(args)
        if args.dcmd=="meta-set": return cmd_dataset_meta_set(args)
    if args.cmd=="data":
        if args.dcmd2=="ensure-table": return cmd_data_ensure_table(args)
        if args.dcmd2=="card": return cmd_data_card(args)
    if args.cmd=="experiment":
        if args.excmd=="create": return cmd_experiment_create(args)
        if args.excmd=="meta-set": return cmd_experiment_meta_set(args)
    if args.cmd=="run":
        if args.rcmd=="start": return cmd_run_start(args)
        if args.rcmd=="end": return cmd_run_end(args)
    if args.cmd=="model":
        if args.mmcmd=="publish": return cmd_model_publish(args)
    if args.cmd=="resolver":
        if args.rvcmd=="put": return cmd_resolver_put(args)
        if args.rvcmd=="get": return cmd_resolver_get(args)
        if args.rvcmd=="list": return cmd_resolver_list(args)
    if args.cmd=="artifact":
        if args.acmd=="create-prompt": return cmd_artifact_prompt_create(args)
        if args.acmd=="create-card": return cmd_artifact_card_create(args)

if __name__=="__main__": main()
