#!/usr/bin/env bash
# Checks the observability gate inside the image, with no network and no login.
# The gate reads the setting from the container's own environment (PID 1), so
# run this twice - in a container created without the opt-in and in one created
# with it:
#
#   docker run --rm --network none -v "$PWD/tests:/tests:ro" --entrypoint bash <image> /tests/observability-gate.sh off
#   docker run --rm --network none -e TOOLBOX_ENABLE_OBSERVABILITY=1 -v "$PWD/tests:/tests:ro" --entrypoint bash <image> /tests/observability-gate.sh on
set -uo pipefail
mode=${1:?usage: observability-gate.sh off|on}

fail=0
expect() {  # want-exit stderr-must-contain command...
    local want=$1 needle=$2 got err
    shift 2
    err=$("$@" 2>&1 >/dev/null)
    got=$?
    if [ "$got" = "$want" ] && { [ -z "$needle" ] || printf '%s' "$err" | grep -q "$needle"; }; then
        printf 'ok    %s  %s\n' "$got" "$*"
    else
        printf 'FAIL  want %s (%s), got %s  %s\n%s\n' "$want" "${needle:-any output}" "$got" "$*" "$err"
        fail=1
    fi
}

off='switched off'
if [ "$mode" = off ]; then
    # Refused, whatever the arguments - and the calling shell can't switch it on.
    expect 5 "$off" promtool --version
    expect 5 "$off" promtool query instant up
    expect 5 "$off" logcli query '{app="x"}'
    expect 5 "$off" logcli --help
    expect 5 "$off" tempo-cli query api search x
    expect 5 "$off" grafana-ds loki
    expect 5 "$off" env TOOLBOX_ENABLE_OBSERVABILITY=1 logcli query x
    expect 5 "$off" env -u TOOLBOX_ENABLE_OBSERVABILITY logcli query x
else
    # Switched on by a human: past the gate, stopping at "not authenticated".
    expect 0 "" promtool --version
    expect 1 "not authenticated" logcli query '{app="x"}'
    expect 1 "not authenticated" tempo-cli query api search x
    expect 1 "" grafana-ds loki
    expect 0 "" env TOOLBOX_ENABLE_OBSERVABILITY=0 promtool --version   # the calling shell can't switch it off either
    # ...but never for a command an agent runs through ./toolbox.
    expect 5 "$off" env TOOLBOX_AGENT=1 logcli query x
    expect 5 "$off" env TOOLBOX_AGENT=1 promtool --version
fi

# The real binaries: not reachable by name, present where the wrappers point, owned by root.
for b in promtool.real logcli.real tempo-cli.real argocd.real; do
    if command -v "$b" >/dev/null 2>&1; then printf 'FAIL  %s is on PATH\n' "$b"; fail=1
    else printf 'ok    %s not on PATH\n' "$b"; fi
done
for w in promtool logcli tempo-cli argocd; do
    r=$(sed -n 's/^REAL=//p' "/usr/local/bin/$w")
    if [ -x "$r" ] && [ "$(stat -c %U "$r")" = root ]; then printf 'ok    %s -> %s (root)\n' "$w" "$r"
    else printf 'FAIL  %s -> %s missing, not executable, or not owned by root\n' "$w" "$r"; fail=1; fi
done

exit "$fail"
