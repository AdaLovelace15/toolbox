# GlueOps Toolbox

> **Beta.** Everything here is beta and may change or break. Only `argocd` and
> `bao` (and the GitOps deploy flow built on them) are in scope. `promtool`,
> `logcli`, `tempo-cli` and `grafana-ds` (Grafana) have known issues and are
> switched off. Using the toolbox, yourself or through an AI agent, is at your
> own risk, and by doing so you accept that risk:
> [Beta and risk acceptance](HUMANS.md#beta-and-risk-acceptance).

The GlueOps platform CLIs — `argocd`, `bao`, `helm` and the GitOps deploy flow —
in one container, already wired up to authenticate against a GlueOps cluster.
Nothing to install locally, no `kubectl`, no cluster credentials: run
`./toolbox up <captain-domain>`, approve the login in a browser, and the CLIs
just work. Deployments are GitOps: `argocd` is read-only,
and changes go through pull requests to the deployment repo.

**Read the guide for who you are:**

- **AI agents** (Claude Code, Codex, Cursor, …) — **[AGENTS.md](AGENTS.md)**.
  Start at the top: it is the complete happy path.
- **Humans** — **[HUMANS.md](HUMANS.md)**: setup, the commands, configuration,
  how it works, known risks, building and releases.

Runs on Linux, and on Windows inside WSL2 (both tested); macOS is expected to
work but untested — see [Platforms](HUMANS.md#platforms).
