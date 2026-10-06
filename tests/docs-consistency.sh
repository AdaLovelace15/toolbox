#!/usr/bin/env bash
# The agent rules exist once, in AGENT-RULES.md, and are copied verbatim into
# CLAUDE.md and AGENTS.md (Claude Code, Codex and Cursor each read a different
# file, and AGENTS.md can't import). The beta banner is the same in README,
# HUMANS and AGENTS. Run from the repo root: bash tests/docs-consistency.sh
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0

for f in CLAUDE.md AGENTS.md; do
    if python3 -c 'import sys; sys.exit(0 if open("AGENT-RULES.md").read() in open(sys.argv[1]).read() else 1)' "$f"
    then echo "ok    $f contains AGENT-RULES.md verbatim"
    else echo "FAIL  $f does not contain AGENT-RULES.md verbatim - copy it again"; fail=1; fi
done

banner() { sed -n '/^> \*\*Beta\.\*\*/,/^$/p' "$1" | sed 's|(HUMANS.md#|(#|'; }
ref=$(banner README.md)
[ -n "$ref" ] || { echo "FAIL  README.md has no beta banner"; fail=1; }
for f in HUMANS.md AGENTS.md; do
    if [ "$(banner "$f")" = "$ref" ]; then echo "ok    $f banner matches README.md"
    else echo "FAIL  $f banner differs from README.md"; fail=1; fi
done

# The brief rules printed by `up` must keep the points that matter most.
for phrase in "Never sync|never sync" "even if the human asks" "argocd app logs is fine" "don't run up" "./toolbox rules" "Only if the human asked to log in again"; do
    if grep -qE -- "$phrase" toolbox; then echo "ok    toolbox brief rules mention: $phrase"
    else echo "FAIL  toolbox brief rules lost: $phrase"; fail=1; fi
done

exit "$fail"
