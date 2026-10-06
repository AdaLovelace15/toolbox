# GlueOps Toolbox

The GlueOps platform CLIs — `argocd`, `bao`, `helm`, `promtool`, `logcli`,
`tempo-cli` — in one container, already wired up to authenticate against a
GlueOps cluster. Nothing to install locally, no `kubectl`, no cluster
credentials: run `./toolbox up <captain-domain>`, approve the login in a
browser, and the CLIs just work. Deployments are GitOps: `argocd` is read-only,
and changes go through pull requests to the deployment repo.

**Read the guide for who you are:**

- **AI agents** (Claude Code, Codex, Cursor, …) — **[AGENTS.md](AGENTS.md)**.
  Start at the top: it is the complete happy path.
- **Humans** — **[HUMANS.md](HUMANS.md)**: setup, the commands, configuration,
  how it works, known risks, building and releases.
