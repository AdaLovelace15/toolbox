#!/usr/bin/env bash
# `up` fast-forwards the toolbox clone and runs again with the new version -
# only from a clean main that tracks a remote branch. Real git against a local
# "upstream"; docker is a stub that always fails, so `up` stops right after the
# update and nothing reaches a real daemon.
#
#   bash tests/self-update.sh
set -uo pipefail
TB="$(cd "$(dirname "$0")/.." && pwd -P)/toolbox"
S=$(mktemp -d "${TMPDIR:-/tmp}/toolbox-update.XXXXXX")
S=$(cd "$S" && pwd -P)
trap 'rm -rf "$S"' EXIT
fail=0

mkdir -p "$S/bin"
printf '#!/bin/sh\necho "Cannot connect to the Docker daemon (stub)" >&2\nexit 1\n' >"$S/bin/docker"
chmod +x "$S/bin/docker"
g() { git -c user.email=t@example.com -c user.name=test -c init.defaultBranch=main "$@"; }

# upstream (bare) <- install (the human's clone); dev pushes a new version.
setup() {
    rm -rf "$S/upstream.git" "$S/install" "$S/dev"
    g init -q --bare "$S/upstream.git"
    g clone -q "$S/upstream.git" "$S/dev" 2>/dev/null
    cp "$TB" "$S/dev/toolbox"
    (cd "$S/dev" && g add toolbox && g commit -qm v1 && g push -q origin HEAD:main) || exit 1
    g clone -q "$S/upstream.git" "$S/install" || exit 1
    # The new version announces itself as soon as it runs.
    (cd "$S/dev" && sed -i.bak '2i\
echo "toolbox: NEW VERSION RUNNING" >&2' toolbox && rm -f toolbox.bak \
        && g commit -qam v2 && g push -q origin HEAD:main) || exit 1
}
run() {  # env assignments..., -- toolbox args...
    local envs=()
    while [ "$1" != -- ]; do envs+=("$1"); shift; done; shift
    (cd "$S" && env -u TOOLBOX_NO_UPDATE -u TOOLBOX_UPDATED PATH="$S/bin:$PATH" CLAUDECODE=1 \
        TOOLBOX_CONTAINER=update-test-$$ ${envs[@]+"${envs[@]}"} bash "$S/install/toolbox" "$@" </dev/null 2>&1)
}
expect() {
    local d=$1 out=$2 n missing=""; shift 2
    for n in "$@"; do case "$out" in *"$n"*) ;; *) missing="$missing [$n]" ;; esac; done
    if [ -z "$missing" ]; then echo "ok    $d"; else printf 'FAIL  %s - missing:%s\n%s\n' "$d" "$missing" "$out"; fail=1; fi
}
refute() {
    case "$2" in *"$3"*) printf 'FAIL  %s - unexpected [%s]\n%s\n' "$1" "$3" "$2"; fail=1 ;; *) echo "ok    $1" ;; esac
}
head_of() { git -C "$S/$1" rev-parse HEAD; }

setup
out=$(run -- up a.example.com)
expect "a clean main behind its remote is updated, then runs again" "$out" "updated the toolbox:" "(1 commit); running it again" "NEW VERSION RUNNING"
[ "$(head_of install)" = "$(head_of dev)" ] && echo "ok    ...and the clone is at the new commit" || { echo "FAIL  clone not updated"; fail=1; }
[ "$(printf '%s\n' "$out" | grep -c 'updated the toolbox')" = 1 ] && echo "ok    ...once" || { echo "FAIL  updated more than once"; fail=1; }
out=$(run -- up a.example.com)
refute "an up-to-date clone says nothing" "$out" "updated the toolbox"

setup
out=$(run TOOLBOX_NO_UPDATE=1 -- up a.example.com)
refute "TOOLBOX_NO_UPDATE=1 turns it off" "$out" "NEW VERSION RUNNING"

setup
echo "# local edit" >>"$S/install/toolbox"
out=$(run -- up a.example.com)
expect "local changes are left alone" "$out" "not updating the toolbox: its clone has local changes"
refute "...and the old version runs" "$out" "NEW VERSION RUNNING"

setup
g -C "$S/install" switch -q -c my-branch
out=$(run -- up a.example.com)
expect "another branch is left alone" "$out" "its clone is on my-branch, not main"

setup
(cd "$S/install" && echo x >extra && g add extra && g commit -qm local)
out=$(run -- up a.example.com)
expect "local commits are left alone" "$out" "its main has commits origin/main doesn't"
refute "...and the old version runs" "$out" "NEW VERSION RUNNING"

setup
mv "$S/upstream.git" "$S/gone.git"
out=$(run -- up a.example.com)
expect "unreachable upstream carries on" "$out" "couldn't check for a newer toolbox" "Cannot connect to the Docker daemon"

setup
rm -rf "$S/install/.git"
out=$(run -- up a.example.com)
refute "a copy that isn't a clone is left alone, quietly" "$out" "updating the toolbox"

setup
out=$(run -- status)
refute "only up and reauth update" "$out" "NEW VERSION RUNNING"

echo
if [ "$fail" = 0 ]; then echo "self-update: all checks passed"; else echo "self-update: FAILURES above"; fi
exit "$fail"
