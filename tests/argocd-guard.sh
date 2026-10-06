#!/usr/bin/env bash
# Checks bin/argocd's read-only guard inside the image, with no server and no
# login: a refused command must exit 5 (refused before any token is fetched), an
# allowed one must get as far as the token and exit 4 (not authenticated).
#
#   docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint bash <image> /tests/argocd-guard.sh
set -uo pipefail

fail=0
expect() {
    local want=$1 got
    shift
    argocd "$@" >/dev/null 2>&1
    got=$?
    if [ "$got" = "$want" ]; then
        printf 'ok    %s  argocd %s\n' "$got" "$*"
    else
        printf 'FAIL  want %s, got %s  argocd %s\n' "$want" "$got" "$*"
        fail=1
    fi
}

# Refused: writes, refreshes, other servers and modes, unknown commands.
expect 5 app sync x
expect 5 app rollback x 1
expect 5 app delete x
expect 5 app set x --parameter a=b
expect 5 app patch x --patch '{}'
expect 5 app terminate-op x
expect 5 app actions run x restart
expect 5 app wait x
expect 5 app get x --refresh
expect 5 app get x --hard-refresh
expect 5 app get x --refresh=hard
expect 5 app diff x --hard-refresh
expect 5 --grpc-web app sync x
expect 5 --server=evil app list
expect 5 app list --server evil
expect 5 app list --core
expect 5 app list --port-forward
expect 5 app list -H 'X: y'
expect 5 app list --auth-token t
expect 5 app list --insecure
expect 5 context foo
expect 5 login argocd.example.com
expect 5 proj windows add x
expect 5 apps list

# Allowed: read-only, so they reach the token lookup and stop there.
expect 4 app list
expect 4 app list -o json
expect 4 app get x -o json
expect 4 app get x -o tree=detailed
expect 4 app diff x --revisions abc --source-positions 2
expect 4 app diff x --server-side-diff
expect 4 app manifests x --revisions abc --source-positions 2
expect 4 app history x
expect 4 app resources x
expect 4 app logs x --tail 5
expect 4 proj windows list x
expect 4 account can-i sync applications '*'
expect 4 cluster list

# Help never needs a server or a token, even for refused commands.
expect 0 app sync --help
expect 0 version --client --help

exit "$fail"
