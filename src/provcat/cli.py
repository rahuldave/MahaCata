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
    ap = argparse.ArgumentParser("provc", description="Resolve and fetch catalog URIs.")
    ap.add_argument("--config", help="a YAML path or etcd://host:port/prefix (default: PROVCAT_CONFIG, ./provcat.yaml)")
    ap.add_argument("--cache", help="the cache folder (default: PROVCAT_CACHE, ~/.cache/provcat)")
    ap.add_argument("--offline", action="store_true", help="use only the cache")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, text in [("resolve", "show the physical URI"), ("pin", "show the URI with a fixed version"),
                       ("stat", "show one entry"), ("ls", "list a folder"),
                       ("fetch", "print a local path that holds the object"), ("cat", "write a file to stdout")]:
        sp.add_parser(name, help=text).add_argument("uri")
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
                _print({"source": source.location, "config": source.load()})
            else:
                data = source.raw()  # keep ${...} so each reader fills in its own environment
                EtcdConfig(args.to).save(data)
                _print({"ok": True, "from": source.location, "to": args.to, "sections": sorted(data)})
            return 0
        cat = Catalog.open(args.config, cache_root=args.cache, offline=args.offline)
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
