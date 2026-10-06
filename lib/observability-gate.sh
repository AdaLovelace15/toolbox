# Sourced, not executed, by promtool, logcli, tempo-cli and grafana-ds.
#
# The toolbox is beta, and the observability CLIs have known issues, so they are
# switched off: only argocd and bao are in scope. A human can switch them back on,
# unsupported and at their own risk, with TOOLBOX_ENABLE_OBSERVABILITY=1 on
# `./toolbox up` from a terminal (see HUMANS.md#beta-and-risk-acceptance).
#
# The setting is read from the container's own environment - PID 1's, fixed when
# `up` created it - not from the calling shell, so `env TOOLBOX_ENABLE_OBSERVABILITY=1
# logcli ...` changes nothing. Commands an agent runs through ./toolbox carry
# TOOLBOX_AGENT=1 and are refused even when a human has switched the CLIs on.
#
# It is a guardrail, not a security boundary: the token and the binaries under
# /usr/local/libexec/toolbox are still reachable from inside the container. It
# stops accidents and tells an agent plainly that the answer is no.
#
# Exit 5, like the argocd guard: refused, before any token or network use.
toolbox_observability_gate() {
    local on
    on=$(tr '\0' '\n' </proc/1/environ 2>/dev/null | sed -n 's/^TOOLBOX_ENABLE_OBSERVABILITY=//p' || true)
    if [ "$on" = 1 ] && [ -z "${TOOLBOX_AGENT:-}" ]; then return 0; fi
    {
        echo "toolbox: $1 is switched off: the toolbox is beta and promtool/logcli/tempo-cli/grafana-ds have known issues. Only argocd and bao are in scope."
        echo "toolbox: AI agents: this is final - don't retry or work around it. Tell the human: \"That's outside the toolbox's beta scope (argocd and bao only), so I won't run it.\""
        echo "toolbox: humans: to opt in at your own risk, see https://github.com/GlueOps/toolbox/blob/main/HUMANS.md#beta-and-risk-acceptance"
    } >&2
    exit 5
}
