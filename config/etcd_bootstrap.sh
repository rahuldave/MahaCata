#!/usr/bin/env bash
set -euo pipefail
PREFIX=${PREFIX:-/resolver}
etcdctl put $PREFIX/resolver/default_branch main
etcdctl put $PREFIX/resolver/repos/dev agreements-bigbank-dev
etcdctl put $PREFIX/resolver/repos/staging agreements-bigbank-staging
etcdctl put $PREFIX/resolver/repos/prod agreements-bigbank-prod
etcdctl put $PREFIX/lance/table_map/acme/bigbank/dev/credit-agreements/qa_outputs qa_outputs.lance
echo "Bootstrapped etcd under $PREFIX"
