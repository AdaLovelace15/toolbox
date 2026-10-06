#!/usr/bin/env bash
# Exercises the host-side ./toolbox wrapper on whatever machine runs it - Linux,
# macOS, WSL - with no cluster login. It uses throwaway container/volume names,
# so a real `toolbox` container and its login volume are never touched, and an
# unreachable captain domain, so nothing is ever sent to a real Dex.
#
#   bash tests/host-smoke.sh                 # builds the image from this checkout first
#   TOOLBOX_IMAGE=ghcr.io/glueops/toolbox:latest bash tests/host-smoke.sh
#
# Needs docker (a reachable daemon) and outbound internet (the wrapper probes
# egress). Prints one line per check and exits non-zero if any failed.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd -P)

NAME="smoke-$$"
export TOOLBOX_CONTAINER=$NAME
export TOOLBOX_HTTP_RETRIES=1
unset TOOLBOX_VOLUME TOOLBOX_WORKDIR TOOLBOX_CAPTAIN_DOMAIN TOOLBOX_ENABLE_OBSERVABILITY
VOL="glueops-$NAME"
A=smoke-a.invalid
B=smoke-b.invalid

fail=0
ok()   { printf 'ok    %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; fail=1; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "# host: $(uname -srm) | bash ${BASH_VERSION} | $(docker version -f 'docker {{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})' 2>/dev/null || echo 'docker: unreachable')"
grep -qi microsoft /proc/version 2>/dev/null && echo "# WSL: $(grep -o 'WSL[0-9]*\|microsoft[^ ]*' /proc/version | head -1)"

# Windows git with core.autocrlf=true turns LF into CRLF, and bash then can't
# even start the script ("/usr/bin/env: 'bash\r'").
crlf=$(grep -lI $'\r' toolbox bin/* lib/*.sh tests/*.sh 2>/dev/null | tr '\n' ' ')
check "scripts have LF line endings${crlf:+ (CRLF in: $crlf)}" '[ -z "$crlf" ]'

if [ -z "${TOOLBOX_IMAGE:-}" ]; then
    echo "# building toolbox:smoke from this checkout (set TOOLBOX_IMAGE to skip)"
    if docker build -q -t toolbox:smoke . >/dev/null; then ok "image builds"; else bad "image builds"; exit 1; fi
    export TOOLBOX_IMAGE=toolbox:smoke
fi

out=$(./toolbox help 2>&1); rc=$?
check "help exits 0 and shows usage" '[ $rc = 0 ] && printf "%s" "$out" | grep -q "toolbox up <captain-domain>"'
out=$(./toolbox rules 2>&1)
check "rules prints the agent rules" 'printf "%s" "$out" | grep -q "## Rules (beta)"'

out=$(./toolbox up 'not a domain!' 2>&1); rc=$?
check "up refuses an invalid domain" '[ $rc != 0 ] && printf "%s" "$out" | grep -q "isn.t a captain domain"'
check "...and creates nothing" '! docker container inspect "$NAME" >/dev/null 2>&1'

echo "# up $A (unreachable on purpose; takes a minute)"
out=$(./toolbox up "$A" 2>&1); rc=$?
check "up prints the beta notice and agent rules" 'printf "%s" "$out" | grep -q "AGENT RULES"'
check "up stops at Dex for an unreachable domain" '[ $rc != 0 ] && printf "%s" "$out" | grep -q "dex.$A"'
check "container is running" '[ "$(docker container inspect -f "{{.State.Running}}" "$NAME" 2>/dev/null)" = true ]'
check "login volume is labelled with the cluster" '[ "$(docker volume inspect -f "{{index .Labels \"dev.glueops.toolbox.cluster\"}}" "$VOL" 2>/dev/null)" = "$A" ]'
check "the workdir is mounted read-only at the same path" '[ "$(docker container inspect -f "{{range .Mounts}}{{if eq .Destination \"$REPO\"}}{{.RW}}{{end}}{{end}}" "$NAME")" = false ]'

out=$(./toolbox pwd 2>/dev/null)
check "commands start in the caller's directory" '[ "$out" = "$REPO" ]'
./toolbox argocd app sync x >/dev/null 2>&1; rc=$?
check "argocd write refused (exit 5)" '[ $rc = 5 ]'
./toolbox logcli query x >/dev/null 2>&1; rc=$?
check "observability CLI refused (exit 5)" '[ $rc = 5 ]'
out=$(./toolbox status 2>&1)
check "status names the login volume and its cluster" 'printf "%s" "$out" | grep -q "login volume: $VOL ($A)"'

out=$(./toolbox up "https://SMOKE-A.invalid/" 2>&1)
check "a pasted variant of the same domain is not a cluster switch" '! printf "%s" "$out" | grep -q "switching cluster"'

echo "# up $B (cluster switch)"
created=$(docker volume inspect -f '{{.CreatedAt}}' "$VOL" 2>/dev/null)
sleep 1
out=$(./toolbox up "$B" 2>&1)
check "switching cluster removes the old login" 'printf "%s" "$out" | grep -q "switching cluster: $A -> $B" && printf "%s" "$out" | grep -q "removed:"'
check "...and the new volume is for $B" '[ "$(docker volume inspect -f "{{index .Labels \"dev.glueops.toolbox.cluster\"}}" "$VOL" 2>/dev/null)" = "$B" ] && [ "$(docker volume inspect -f "{{.CreatedAt}}" "$VOL")" != "$created" ]'

id=$(docker container inspect -f '{{.Id}}' "$NAME" 2>/dev/null)
out=$(./toolbox reauth 'bad domain!' 2>&1); rc=$?
check "reauth refuses a bad domain before deleting anything" '[ $rc != 0 ] && [ "$(docker container inspect -f "{{.Id}}" "$NAME" 2>/dev/null)" = "$id" ]'

./toolbox down >/dev/null 2>&1
check "down removes the container" '! docker container inspect "$NAME" >/dev/null 2>&1'
check "...and keeps the login volume" 'docker volume inspect "$VOL" >/dev/null 2>&1'
out=$(./toolbox status 2>&1)
check "status after down: absent, volume and cluster shown" 'printf "%s" "$out" | grep -q "container: absent" && printf "%s" "$out" | grep -q "login volume: $VOL ($B)"'

echo
if [ "$fail" = 0 ]; then echo "host smoke test: all checks passed"; else echo "host smoke test: FAILURES above"; fi
exit "$fail"
