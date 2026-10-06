#!/usr/bin/env bash
# ./toolbox propose hands the PR body to gh on stdin (--body-file -), never as an
# argument: a rendered diff can be too big for argv, and arguments show in ps.
# Runs propose_publish from the wrapper with stub git, gh and container calls.
#
#   bash tests/propose-body.sh
set -uo pipefail
TB="$(cd "$(dirname "$0")/.." && pwd -P)/toolbox"
S=$(mktemp -d "${TMPDIR:-/tmp}/toolbox-body.XXXXXX")
trap 'rm -rf "$S"' EXIT
fail=0

mkdir -p "$S/bin"
cat >"$S/bin/gh" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "pr list") [ -n "${STUB_OPEN_PR:-}" ] && echo "$STUB_OPEN_PR"; exit 0 ;;
  "pr create"|"pr edit")
    [ -z "${STUB_GH_FAIL:-}" ] || { cat >/dev/null; echo "HTTP 422" >&2; exit 1; }
    printf '%s\n' "$@" >"$STUB_DIR/argv"
    cat >"$STUB_DIR/stdin"
    echo "https://github.com/acme/repo/pull/7" ;;
  *) echo "stub gh: unexpected: $*" >&2; exit 99 ;;
esac
EOF
cat >"$S/bin/git" <<'EOF'
#!/bin/sh
echo 0123456789abcdef0123456789abcdef01234567
EOF
chmod +x "$S/bin/"*

# A body bigger than one argument may be (MAX_ARG_STRLEN is 128 KiB), with
# characters a shell would mangle.
{ printf '%s\n' '### What changes' "\`\$(touch $S/pwned)\` \"quotes\" 'single' -- --title"
  head -c 200000 /dev/zero | tr '\0' 'x'; echo; } >"$S/body"

run() {
    PATH="$S/bin:$PATH" STUB_DIR="$S" bash -c '
        set -euo pipefail
        log() { :; }; die() { echo "die: $*" >&2; exit 1; }
        push_branch() { :; }
        cmd_exec() { cat "$STUB_DIR/body"; return 1; }
        eval "$(sed -n "/^propose_publish() {/,/^}/p" "$1")"
        propose_publish "$STUB_DIR" /r main my-branch "the title" "" "" "" ""' _ "$TB"
}

check() {  # name
    if ! grep -qx -- "--body-file" "$S/argv" || ! grep -qx -- "-" "$S/argv"; then
        echo "FAIL $1: gh wasn't given --body-file -"; fail=1
    elif grep -q -- "^--body$" "$S/argv" || grep -q xxxxxxxx "$S/argv"; then
        echo "FAIL $1: the body went on the command line"; fail=1
    elif ! cmp -s "$S/stdin" "$S/body"; then
        echo "FAIL $1: the body on stdin isn't the one the container wrote"; fail=1
    elif [ -e "$S/pwned" ]; then
        echo "FAIL $1: the body was evaluated"; fail=1
    else
        echo "ok   $1"
    fi
    rm -f "$S/argv" "$S/stdin"
}

run >/dev/null 2>"$S/err" || { echo "FAIL create: $(cat "$S/err")"; fail=1; }
check "gh pr create reads the body from stdin"
STUB_OPEN_PR=https://github.com/acme/repo/pull/7 run >/dev/null 2>"$S/err" || { echo "FAIL edit: $(cat "$S/err")"; fail=1; }
check "gh pr edit reads the body from stdin"
if STUB_GH_FAIL=1 run >/dev/null 2>"$S/err"; then
    echo "FAIL a gh failure must fail propose"; fail=1
elif ! grep -q "gh pr create failed; branch my-branch is pushed" "$S/err"; then
    echo "FAIL gh failure message: $(cat "$S/err")"; fail=1
else
    echo "ok   a gh failure fails propose and says the branch is pushed"
fi
exit $fail
