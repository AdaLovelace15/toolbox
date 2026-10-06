# Sourced, not executed, by promtool, logcli, tempo-cli and grafana-ds.
#
# The toolbox is beta, and the observability CLIs have known issues, so they are
# switched off: only argocd and bao are in scope. A human can switch them back on,
# unsupported and at their own risk, with TOOLBOX_ENABLE_OBSERVABILITY=1 on
# `./toolbox up` from a terminal; `up` ignores the variable when an agent drives
# it (see HUMANS.md#beta-and-risk-acceptance).
#
# It is a guardrail, not a security boundary: the token and the binaries under
# /usr/local/libexec/toolbox are still reachable from inside the container. It
# stops accidents and tells an agent plainly that the answer is no.
#
# Exit 5, like the argocd guard: refused, before any token or network use.
toolbox_observability_gate() {
    [ "${TOOLBOX_ENABLE_OBSERVABILITY:-}" = 1 ] && return 0
    {
        echo "toolbox: $1 is switched off: the toolbox is beta and promtool/logcli/tempo-cli/grafana-ds have known issues. Only argocd and bao are in scope."
        echo "toolbox: AI agents: this is final - don't retry or work around it; tell the human in one line that it is unsupported during the beta."
        echo "toolbox: humans: see HUMANS.md#beta-and-risk-acceptance to opt in at your own risk."
    } >&2
    exit 5
}
