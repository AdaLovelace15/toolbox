#!/usr/bin/env bash
# What ./toolbox tells a human or an agent when the host isn't ready - checked
# offline, with stub docker/uname/id on PATH simulating Linux, macOS (Docker
# Desktop, Colima, OrbStack), WSL, rootless docker, Podman and Windows shells.
# No real docker is ever called; runs anywhere bash does (CI runs it on Linux
# and on macOS's bash 3.2).
#
#   bash tests/wrapper-messages.sh
set -uo pipefail
TB="$(cd "$(dirname "$0")/.." && pwd -P)/toolbox"
S=$(mktemp -d "${TMPDIR:-/tmp}/toolbox-msgs.XXXXXX")
WD=$(mktemp -d "${TMPDIR:-/tmp}/toolbox-wd.XXXXXX")
WD=$(cd "$WD" && pwd -P)
trap 'rm -rf "$S" "$WD"' EXIT

# --- stubs --------------------------------------------------------------------
mkdir -p "$S/bin" "$S/nodocker"
cat >"$S/bin/docker" <<'EOF'
#!/usr/bin/env bash
a="$*"
case "$1" in
  version)
    if [ -n "${STUB_DAEMON_ERR:-}" ]; then printf 'Client: Docker Engine\n'; printf '%s\n' "$STUB_DAEMON_ERR" >&2; exit 1; fi
    printf '%s\n' "${STUB_VERSION:-Server: Docker Engine - Community}"; exit 0 ;;
  context) printf '%s\n' "${STUB_ENDPOINT:-unix:///var/run/docker.sock}" ;;
  info) exit 1 ;;
  container|inspect)
    [ -n "${STUB_RUNNING:-}" ] || exit 1
    case "$a" in
      *State.Running*) echo true ;;
      *Config.Env*) echo "TOOLBOX_CAPTAIN_DOMAIN=$STUB_DOMAIN" ;;
      *Config.Labels*) echo "$STUB_WORKDIR" ;;
      *Mounts*) echo "$STUB_VOLUME" ;;
      *) echo '[{}]' ;;
    esac ;;
  volume)
    case "$2" in
      create) exit 0 ;;
      inspect)
        [ -n "${STUB_RUNNING:-}" ] || exit 1
        case "$a" in *toolbox.cluster*) echo "$STUB_DOMAIN" ;; *toolbox.login*) echo 1 ;; *) echo '[{}]' ;; esac ;;
      *) exit 0 ;;
    esac ;;
  image|rm|pull|ps) exit 0 ;;
  run) if [ -n "${STUB_RUN_ERR:-}" ]; then printf '%s\n' "$STUB_RUN_ERR" >&2; exit 125; fi; echo 0123abcd ;;
  exec) case "$a" in *"test -f"*) exit 0 ;; *) exit 1 ;; esac ;;   # login/probe fail: stop offline
  *) echo "stub docker: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat >"$S/bin/uname" <<'EOF'
#!/bin/sh
case "${1:-}" in -s|"") echo "${STUB_UNAME_S:-Linux}" ;; -r) echo "${STUB_UNAME_R:-6.1.0-generic}" ;; -m) echo x86_64 ;; *) echo "${STUB_UNAME_S:-Linux}" ;; esac
EOF
cat >"$S/bin/id" <<'EOF'
#!/bin/sh
case "${1:-}" in -u) echo 1000 ;; -un) echo dev ;; -Gn) echo "${STUB_GROUPS:-dev}" ;; *) echo "uid=1000(dev)" ;; esac
EOF
chmod +x "$S/bin/"*
# A PATH with the stubs but no docker at all, for "not installed".
for t in bash env sed tr grep dirname head cat seq sleep mktemp; do
    p=$(command -v "$t") && ln -s "$p" "$S/nodocker/$t"
done
ln -s "$S/bin/uname" "$S/nodocker/uname"; ln -s "$S/bin/id" "$S/nodocker/id"

fail=0
# Never let a test reach a real docker: the stub must win.
if [ "$(PATH="$S/bin:$PATH" command -v docker)" != "$S/bin/docker" ]; then echo "FAIL  stub docker not first on PATH"; exit 1; fi
if PATH="$S/nodocker" command -v docker >/dev/null 2>&1; then echo "FAIL  docker reachable without stubs"; exit 1; fi

NAME=msgs-$$
GROUPS_FILE="$S/group"; : >"$GROUPS_FILE"
run() {  # PATH-dir, env assignments..., -- toolbox args...
    local pathdir=$1; shift
    local envs=() unsets=()
    while [ "$1" != -- ]; do
        if [ "$1" = -u ]; then unsets+=(-u "$2"); shift 2; else envs+=("$1"); shift; fi
    done; shift
    (cd "$WD" && env -u DOCKER_HOST -u DOCKER_DEFAULT_PLATFORM -u TOOLBOX_WORKDIR -u TOOLBOX_VOLUME \
        -u TOOLBOX_DEX_URL -u TOOLBOX_CLIENT_ID -u TOOLBOX_CAPTAIN_DOMAIN -u TOOLBOX_ENABLE_OBSERVABILITY \
        ${unsets[@]+"${unsets[@]}"} PATH="$pathdir" CLAUDECODE=1 TOOLBOX_CONTAINER="$NAME" TOOLBOX_GROUP_FILE="$GROUPS_FILE" \
        ${envs[@]+"${envs[@]}"} bash "$TB" "$@" </dev/null 2>&1)
}
expect() {  # description, output, needle... (all literal substrings)
    local d=$1 out=$2 n missing=""
    shift 2
    for n in "$@"; do case "$out" in *"$n"*) ;; *) missing="$missing [$n]" ;; esac; done
    if [ -z "$missing" ]; then printf 'ok    %s\n' "$d"; else printf 'FAIL  %s - missing:%s\n%s\n' "$d" "$missing" "$out"; fail=1; fi
}
refute() {  # description, output, needle
    case "$2" in *"$3"*) printf 'FAIL  %s - unexpected [%s]\n%s\n' "$1" "$3" "$2"; fail=1 ;; *) printf 'ok    %s\n' "$1" ;; esac
}
STOP="agents: give the human that line and stop"
DOWN="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
WSLR=5.15.167.4-microsoft-standard-WSL2
P="$S/bin:$PATH"

# --- docker missing ---------------------------------------------------------------
out=$(run "$S/nodocker" -- up a.example.com)
expect "not installed (Linux)" "$out" "docker is not installed" "ask the human to: install Docker Engine" "$STOP"
out=$(run "$S/nodocker" STUB_UNAME_S=Darwin -- up a.example.com)
expect "not installed (macOS)" "$out" "ask the human to: install Docker Desktop, OrbStack or Colima"
out=$(run "$S/nodocker" STUB_UNAME_R=$WSLR -- up a.example.com)
expect "not installed (WSL)" "$out" "docker is not installed in this WSL distro" "WSL integration"

# --- daemon not running, by where docker points -----------------------------------
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" -- up a.example.com)
expect "not running (Linux)" "$out" "cannot connect to the docker daemon at unix:///var/run/docker.sock" "ask the human to: sudo service docker start" "(docker: Cannot connect" "$STOP"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" STUB_UNAME_R=$WSLR -- up a.example.com)
expect "not running (WSL)" "$out" "sudo service docker start (Docker Engine in WSL)" "Docker Desktop on Windows"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" STUB_UNAME_S=Darwin STUB_ENDPOINT=unix:///Users/dev/.docker/run/docker.sock -- up a.example.com)
expect "not running (Docker Desktop)" "$out" "ask the human to: start Docker Desktop"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" STUB_UNAME_S=Darwin STUB_ENDPOINT=unix:///Users/dev/.colima/default/docker.sock -- up a.example.com)
expect "not running (Colima)" "$out" "ask the human to: colima start"
refute "...default profile isn't named" "$out" "--profile"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" STUB_UNAME_S=Darwin STUB_ENDPOINT=unix:///Users/dev/.colima/work/docker.sock -- up a.example.com)
expect "not running (Colima, other profile)" "$out" "colima start --profile work"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" STUB_UNAME_S=Darwin STUB_ENDPOINT=unix:///Users/dev/.orbstack/run/docker.sock -- up a.example.com)
expect "not running (OrbStack)" "$out" "ask the human to: start OrbStack"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" STUB_UNAME_S=Darwin -- up a.example.com)
expect "not running (macOS, unknown app)" "$out" "start your docker app"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" DOCKER_HOST=unix:///run/user/1000/docker.sock -- up a.example.com)
expect "not running (rootless)" "$out" "systemctl --user start docker (rootless docker - no sudo)"
out=$(run "$P" "STUB_DAEMON_ERR=$DOWN" DOCKER_HOST=ssh://me@build-box -- up a.example.com)
expect "remote daemon" "$out" "is on another machine, which can't work"

# --- daemon running, this user can't reach it ----------------------------------------
PD="permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
out=$(run "$P" "STUB_DAEMON_ERR=$PD" -- up a.example.com)
expect "permission denied, not in the group" "$out" 'sudo usermod -aG docker $USER' "restart the terminal and the AI agent"
echo "docker:x:999:alice,dev" >"$GROUPS_FILE"
out=$(run "$P" "STUB_DAEMON_ERR=$PD" -- up a.example.com)
expect "permission denied, group added after the session started" "$out" "added to the docker group, but this session started before that"
out=$(run "$P" "STUB_DAEMON_ERR=$PD" "STUB_GROUPS=dev docker" -- up a.example.com)
expect "permission denied, already in the group" "$out" "docker refused this user's connection"
: >"$GROUPS_FILE"
out=$(run "$P" "STUB_DAEMON_ERR=$PD" -u USER -- up a.example.com 2>&1 || true)
expect "permission denied with USER unset doesn't crash" "$out" 'sudo usermod -aG docker $USER'
refute "...no unbound variable" "$out" "unbound variable"
out=$(run "$P" "STUB_DAEMON_ERR=connect: operation not permitted" -- up a.example.com)
expect "agent sandbox blocks the socket" "$out" "usually the AI agent's sandbox" "outside the agent's sandbox"

# --- mounts the daemon can't see --------------------------------------------------------
out=$(run "$P" "STUB_RUN_ERR=docker: Error response from daemon: invalid mount config for type \"bind\": bind source path does not exist: $WD" -- up a.example.com)
expect "workdir not visible to docker" "$out" "the docker daemon can't see $WD (the working directory)" "Colima shares only the home directory" "$STOP"
out=$(run "$P" "STUB_RUN_ERR=docker: Error response from daemon: invalid mount config for type \"bind\": bind source path does not exist: /opt/corp/ca.pem" -- up a.example.com)
expect "CA file not visible to docker" "$out" "can't see /opt/corp/ca.pem (a file up mounts"
out=$(run "$P" "STUB_RUN_ERR=docker: Error response from daemon: Mounts denied:
The path /Volumes/work/x is not shared from the host and is not known to Docker." -- up a.example.com)
expect "Docker Desktop file sharing" "$out" "can't see /Volumes/work/x" "File sharing"

# --- other platform notes ------------------------------------------------------------------
out=$(run "$P" "STUB_VERSION=Server: Podman Engine 5.2" -- up a.example.com)
expect "Podman is flagged" "$out" "this docker is Podman"
out=$(run "$P" DOCKER_DEFAULT_PLATFORM=linux/amd64 -- up a.example.com)
expect "DOCKER_DEFAULT_PLATFORM is ignored" "$out" "ignoring DOCKER_DEFAULT_PLATFORM=linux/amd64"
out=$(run "$P" STUB_UNAME_S=MINGW64_NT-10.0-26100 -- up a.example.com)
expect "Windows shells refused" "$out" "Windows shells (Git Bash, MSYS, Cygwin) aren't supported"
out=$(run "$P" STUB_UNAME_S=MINGW64_NT-10.0-26100 -- help)
refute "...but help still works" "$out" "aren't supported"

# --- a re-run after a cut-off up recovers ----------------------------------------------------
out=$(run "$P" STUB_RUNNING=1 STUB_DOMAIN=a.example.com "STUB_WORKDIR=$WD" "STUB_VOLUME=glueops-$NAME" -- up a.example.com)
expect "a running container that can't log in is recreated" "$out" "reusing the running container" "the running container can't log in; recreating it" "network: bridge"

# --- the stop line is for agents only ----------------------------------------------------------
if command -v script >/dev/null 2>&1 && script -qec true /dev/null >/dev/null 2>&1; then
    out=$(cd "$WD" && env -u CLAUDECODE PATH="$P" TOOLBOX_CONTAINER="$NAME" "STUB_DAEMON_ERR=$DOWN" \
          script -qec "bash '$TB' up a.example.com" /dev/null 2>&1 | tr -d '\r')
    expect "a human at a terminal gets the remedy" "$out" "ask the human to: sudo service docker start"
    refute "...without the agents' stop line" "$out" "$STOP"
else
    echo "skip  human-at-a-terminal case (no util-linux script here)"
fi

echo
if [ "$fail" = 0 ]; then echo "wrapper messages: all checks passed"; else echo "wrapper messages: FAILURES above"; fi
exit "$fail"
