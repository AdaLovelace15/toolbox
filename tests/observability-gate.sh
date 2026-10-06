#!/usr/bin/env bash
# Checks the observability gate inside the image, with no network and no login.
# Switched off (the default), each tool must refuse with exit 5 before touching a
# token; with TOOLBOX_ENABLE_OBSERVABILITY=1 it must get past the gate (promtool
# --version is local and succeeds; the rest stop at "not authenticated", exit 1).
#
#   docker run --rm --network none -v "$PWD/tests:/tests:ro" --entrypoint bash <image> /tests/observability-gate.sh
set -uo pipefail

fail=0
expect() {
    local want=$1 env=$2 got err
    shift 2
    err=$(env $env "$@" 2>&1 >/dev/null)
    got=$?
    if [ "$got" = "$want" ] && { [ "$want" != 5 ] || printf '%s' "$err" | grep -q 'switched off'; }; then
        printf 'ok    %s  %s %s\n' "$got" "${env:-(off)}" "$*"
    else
        printf 'FAIL  want %s, got %s  %s %s\n%s\n' "$want" "$got" "${env:-(off)}" "$*" "$err"
        fail=1
    fi
}

off=TOOLBOX_ENABLE_OBSERVABILITY=
on=TOOLBOX_ENABLE_OBSERVABILITY=1

# Off: refused, whatever the arguments.
expect 5 "$off" promtool --version
expect 5 "$off" promtool query instant up
expect 5 "$off" logcli query '{app="x"}'
expect 5 "$off" logcli --help
expect 5 "$off" tempo-cli query api search x
expect 5 "$off" grafana-ds loki
expect 5 TOOLBOX_ENABLE_OBSERVABILITY=true logcli query x   # exactly 1, nothing else

# On: past the gate.
expect 0 "$on" promtool --version
expect 1 "$on" logcli query '{app="x"}'
expect 1 "$on" tempo-cli query api search x
expect 1 "$on" grafana-ds loki

# The real binaries are not reachable by name.
for b in promtool.real logcli.real tempo-cli.real argocd.real; do
    if command -v "$b" >/dev/null 2>&1; then
        printf 'FAIL  %s is on PATH\n' "$b"; fail=1
    else
        printf 'ok    %s not on PATH\n' "$b"
    fi
done

exit "$fail"
