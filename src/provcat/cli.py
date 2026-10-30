"""The provc command."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .catalog import Catalog
from .config import EtcdConfig, open_config
from .errors import ProvcatError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("provc", description="Resolve, fetch and write catalog URIs.")
    ap.add_argument("--config", help="a YAML path or etcd://host:port/prefix (default: PROVCAT_CONFIG, ./provcat.yaml)")
    ap.add_argument("--cache", help="the cache folder (default: PROVCAT_CACHE, ~/.cache/provcat)")
    ap.add_argument("--offline", action="store_true", help="use only the cache")
    ap.add_argument("--tenant", help="see only this tenant's part of the configuration (default: PROVCAT_TENANT)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("resolve", "show the physical URI"), ("pin", "show the URI with a fixed version"),
                       ("stat", "show one entry"), ("ls", "list a folder"),
                       ("fetch", "print a local path that holds the object"), ("cat", "write a file to stdout")]:
        sp.add_parser(name, help=text).add_argument("uri")
    put = sp.add_parser("put", help="write a local file or folder, or - for stdin")
    put.add_argument("uri")
    put.add_argument("src")
    sp.add_parser("rm", help="delete one object").add_argument("uri")
    com = sp.add_parser("commit", help="commit a lakeFS branch or a bucket folder; prints the pinned URI")
    com.add_argument("uri")
    com.add_argument("-m", "--message", required=True)
    com.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE", help="commit metadata")
    br = sp.add_parser("branch", help="make a lakeFS branch, or copy a bucket branch")
    br.add_argument("uri", help="the new branch: repository/branch or bucket/branch")
    br.add_argument("--from", dest="source", required=True, help="a branch, a tag or a commit")
    cfg = sp.add_parser("config", help="show or copy the configuration")
    cs = cfg.add_subparsers(dest="ccmd", required=True)
    cs.add_parser("show", help="print the configuration as JSON")
    push = cs.add_parser("push", help="copy the configuration into etcd")
    push.add_argument("--to", required=True, help="etcd://host:port/prefix")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "config":
            source = open_config(args.config)
            if args.ccmd == "show":
                tenant = args.tenant if args.tenant is not None else os.environ.get("PROVCAT_TENANT")
                tenant = tenant or None
                _print({"source": source.location, "tenant": tenant, "config": source.load(tenant)})
            else:
                data = source.raw()  # keep ${...} so each reader fills in its own environment
                EtcdConfig(args.to).save(data)
                _print({"ok": True, "from": source.location, "to": args.to, "sections": sorted(data)})
            return 0
        cat = Catalog.open(args.config, cache_root=args.cache, offline=args.offline, tenant=args.tenant)
        if args.cmd == "resolve":
            r = cat.resolve(args.uri)
            _print({"logical": str(r.logical), "physical": str(r.physical), "via": list(r.via)})
        elif args.cmd == "pin":
            print(cat.pin(args.uri))
        elif args.cmd == "stat":
            _print(cat.stat(args.uri).as_dict())
        elif args.cmd == "ls":
            _print([e.as_dict() for e in cat.list(args.uri)])
        elif args.cmd == "fetch":
            print(cat.fetch(args.uri))
        elif args.cmd == "cat":
            sys.stdout.buffer.write(cat.read(args.uri))
        elif args.cmd == "put":
            src = sys.stdin.buffer.read() if args.src == "-" else args.src
            _print([e.as_dict() for e in cat.put(args.uri, src)])
        elif args.cmd == "rm":
            cat.delete(args.uri)
            _print({"deleted": args.uri})
        elif args.cmd == "commit":
            meta = {}
            for item in args.meta:
                key, sep, value = item.partition("=")
                if not sep or not key:
                    raise ProvcatError(f"--meta {item!r}: write KEY=VALUE")
                meta[key] = value
            print(cat.commit(args.uri, args.message, meta))
        elif args.cmd == "branch":
            print(cat.branch(args.uri, args.source))
        sys.stdout.flush()
        return 0
    except BrokenPipeError:
        # The reader closed the pipe, for example "provc ls x | head".
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (ProvcatError, OSError) as e:
        print(f"provc: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
