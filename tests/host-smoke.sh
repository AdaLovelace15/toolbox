#!/usr/bin/env bash
# Exercises the host-side ./toolbox wrapper on whatever machine runs it - Linux,
# macOS, WSL - with no cluster login. It uses throwaway container/volume names,
# so a real `toolbox` container and its login volume are never touched, and an
# unreachable captain domain with every Dex/OpenBao override cleared, so nothing
# is ever sent to a real Dex.
#
#   bash tests/host-smoke.sh                 # builds the image from this checkout first
#   TOOLBOX_IMAGE=ghcr.io/glueops/toolbox:latest bash tests/host-smoke.sh
#
# Needs docker (a reachable daemon that can see this checkout) and outbound
# internet (the wrapper probes egress). About 15 seconds with a prebuilt image.
# Prints one line per check and exits non-zero if any failed.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd -P)

NAME="smoke-$$-${RANDOM}"
export TOOLBOX_CONTAINER=$NAME
# Nothing from the caller's environment may point the test at a real login.
unset TOOLBOX_VOLUME TOOLBOX_WORKDIR TOOLBOX_CAPTAIN_DOMAIN TOOLBOX_ENABLE_OBSERVABILITY \
      TOOLBOX_DEX_URL TOOLBOX_CLIENT_ID TOOLBOX_BAO_UPSTREAM TOOLBOX_BAO_ROLES \
      TOOLBOX_BAO_AUTH_PATH TOOLBOX_WAIT_SECONDS TOOLBOX_IDLE_SECONDS
VOL="glueops-$NAME"
FOREIGN="smoke-foreign-$$-${RANDOM}"
A=smoke-a.invalid
B=smoke-b.invalid
C=smoke-c.invalid
BUILT=""

fail=0
ok()   { printf 'ok    %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; fail=1; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
has()  { printf '%s' "$1" | grep -qF -- "$2"; }   # literal substring
label() { docker volume inspect -f "{{index .Labels \"dev.glueops.toolbox.cluster\"}}" "$1" 2>/dev/null; }
cid()  { docker container inspect -f '{{.Id}}' "$NAME" 2>/dev/null; }
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOL" "$FOREIGN" >/dev/null 2>&1 || true
    if [ -n "$BUILT" ]; then docker image rm "$BUILT" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
cleanup

docker_desc=$(docker version -f 'docker {{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})' 2>/dev/null) || docker_desc="docker: unreachable"
echo "# host: $(uname -srm) | bash ${BASH_VERSION} | $docker_desc"
if grep -qi microsoft /proc/version 2>/dev/null; then echo "# WSL: $(uname -r)"; fi

# Windows git with core.autocrlf=true turns LF into CRLF, and bash then can't
# even start the script ("/usr/bin/env: 'bash\r'").
crlf=$(grep -lI $'\r' toolbox entrypoint.sh bin/* lib/*.sh tests/*.sh 2>/dev/null | tr '\n' ' ')
check "scripts have LF line endings${crlf:+ (CRLF in: $crlf- fix: git rm -r -q --cached . && git reset --hard)}" '[ -z "$crlf" ]'

# Preflight: environment problems are reported as such, not as a cascade of FAILs.
if ! docker info >/dev/null 2>&1; then bad "docker daemon reachable"; exit 1; fi
ok "docker daemon reachable"

if [ -z "${TOOLBOX_IMAGE:-}" ]; then
    BUILT="toolbox:smoke-$$"
    echo "# building $BUILT from this checkout (set TOOLBOX_IMAGE to skip)"
    if docker build -q -t "$BUILT" . >/dev/null; then ok "image builds"; else bad "image builds"; BUILT=""; exit 1; fi
    export TOOLBOX_IMAGE=$BUILT
fi
if ! docker run --rm --mount "type=bind,src=$REPO,dst=/smoke,readonly" --entrypoint true "$TOOLBOX_IMAGE" >/dev/null 2>&1; then
    bad "the docker daemon can see $REPO (a daemon elsewhere, or a devcontainer whose workspace is at another path on the docker host, can't)"
    exit 1
fi
ok "the docker daemon can see this checkout"

out=$(./toolbox help 2>&1); rc=$?
check "help exits 0 and shows usage" '[ $rc = 0 ] && has "$out" "toolbox up <captain-domain>"'
out=$(./toolbox rules 2>&1)
check "rules prints the agent rules" 'has "$out" "## Rules (beta)" && has "$out" "ArgoCD is read-only"'

out=$(./toolbox up 'not a domain!' 2>&1); rc=$?
check "up refuses an invalid domain" '[ $rc != 0 ] && has "$out" "isn'"'"'t a captain domain"'
check "...and creates nothing" '[ -z "$(cid)" ]'

echo "# up $A (unreachable on purpose)"
out=$(./toolbox up "$A" 2>&1); rc=$?
if has "$out" "no network egress"; then bad "outbound internet from a container (the wrapper's egress probe failed)"; printf '%s\n' "$out" | tail -3; exit 1; fi
check "up prints the beta notice and agent rules" 'has "$out" "BETA - only argocd and bao" && has "$out" "AGENT RULES"'
check "up stops at Dex for an unreachable domain" '[ $rc != 0 ] && has "$out" "cannot reach Dex at https://dex.$A"'
if [ "$(docker container inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]; then
    bad "container is running"; printf '%s\n' "$out" | tail -5; exit 1
fi
ok "container is running"
check "login volume is labelled with the cluster" '[ "$(label "$VOL")" = "$A" ]'
check "the workdir is mounted read-only at the same path" '[ "$(docker container inspect -f "{{range .Mounts}}{{if eq .Destination \"$REPO\"}}{{.RW}}{{end}}{{end}}" "$NAME" 2>/dev/null)" = false ]'

out=$(./toolbox pwd 2>/dev/null)
check "commands start in the caller's directory" '[ "$out" = "$REPO" ]'
./toolbox argocd app sync x >/dev/null 2>&1; rc=$?
check "argocd write refused (exit 5)" '[ $rc = 5 ]'
./toolbox logcli query x >/dev/null 2>&1; rc=$?
check "observability CLI refused (exit 5)" '[ $rc = 5 ]'
out=$(./toolbox status 2>&1); rc=$?
check "status exits 0 and names the login volume and its cluster" '[ $rc = 0 ] && has "$out" "login volume: $VOL ($A)"'

id=$(cid)
out=$(./toolbox up " https://SMOKE-A.invalid./x" 2>&1)
check "a pasted variant of the same domain is the same cluster" 'has "$out" "captain domain: $A (from" && has "$out" "reusing the running container" && ! has "$out" "switching cluster" && [ -n "$id" ] && [ "$(cid)" = "$id" ]'

echo "# up $B (cluster switch, container present)"
out=$(./toolbox up "$B" 2>&1)
check "switching cluster removes the old container and login" 'has "$out" "switching cluster: $A -> $B; removing the old container and its login" && has "$out" "removed: container $NAME, volume $VOL"'
check "...and the new login volume is for $B" '[ "$(label "$VOL")" = "$B" ]'

id=$(cid)
out=$(./toolbox reauth 'bad domain!' 2>&1); rc=$?
check "reauth refuses a bad domain before deleting anything" '[ -n "$id" ] && [ $rc != 0 ] && has "$out" "isn'"'"'t a captain domain" && [ "$(cid)" = "$id" ] && [ "$(label "$VOL")" = "$B" ]'

out=$(./toolbox down 2>&1); rc=$?
check "down exits 0 and removes the container" '[ -n "$id" ] && [ $rc = 0 ] && [ -z "$(cid)" ] && has "$out" "removed $NAME"'
check "...and keeps the login volume" '[ "$(label "$VOL")" = "$B" ]'
out=$(./toolbox status 2>&1)
check "status after down: absent, volume and cluster shown" 'has "$out" "container: absent" && has "$out" "login volume: $VOL ($B)"'

echo "# up $C (cluster switch after down: only the volume says which cluster)"
out=$(./toolbox up "$C" 2>&1)
check "switching cluster after down removes the old login volume" 'has "$out" "switching cluster: $B -> $C; removing the old login" && has "$out" "removed: volume $VOL"'
check "...and the new login volume is for $C" '[ "$(label "$VOL")" = "$C" ]'

echo "# a volume the toolbox didn't create is never removed"
docker volume create "$FOREIGN" >/dev/null
./toolbox down >/dev/null 2>&1
out=$(TOOLBOX_VOLUME=$FOREIGN ./toolbox reauth "$A" 2>&1); rc=$?
check "reauth refuses to remove it" '[ $rc != 0 ] && has "$out" "refusing to remove $FOREIGN" && docker volume inspect "$FOREIGN" >/dev/null 2>&1'
out=$(TOOLBOX_VOLUME=$FOREIGN ./toolbox up "$B" 2>&1)
check "up leaves it in place" 'has "$out" "leaving the login volume $FOREIGN" && docker volume inspect "$FOREIGN" >/dev/null 2>&1'

echo
if [ "$fail" = 0 ]; then echo "host smoke test: all checks passed"; else echo "host smoke test: FAILURES above"; fi
exit "$fail"
