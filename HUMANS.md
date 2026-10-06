# GlueOps Toolbox — for humans

> **Beta.** Everything here is beta and may change or break. Only `argocd` and
> `bao` (and the GitOps deploy flow built on them) are in scope. `promtool`,
> `logcli`, `tempo-cli` and `grafana-ds` (Grafana) have known issues and are
> switched off. Using the toolbox, yourself or through an AI agent, is at your
> own risk, and by doing so you accept that risk:
> [Beta and risk acceptance](#beta-and-risk-acceptance).

> AI agent? Read [AGENTS.md](AGENTS.md) instead; this file is for people.

The platform CLIs in one container, already wired up to authenticate. Developers
don't install `argocd`, `bao`, `helm`, or anything else locally — and they don't
need `kubectl` or cluster access.

```bash
git clone https://github.com/GlueOps/toolbox && cd toolbox
./toolbox up <your-captain-domain>     # prints a login URL
./toolbox shell                        # once approved
```

Or without the wrapper:

```bash
docker run -it --rm \
  -e TOOLBOX_CAPTAIN_DOMAIN=<your-captain-domain> \
  -v glueops-toolbox:/home/toolbox/.config/glueops \
  ghcr.io/glueops/toolbox:latest
```

You'll be given a URL to open and approve with GitHub. After that:

```bash
argocd app list
bao kv get secret/my-app
```

No flags, no `argocd login`, no `bao login`. Both CLIs behave normally —
except that `argocd` is read-only: deployments are GitOps, so changes go
through a pull request to the deployment repo (see
[GitOps](#gitops-read-only-argocd-changes-by-pull-request)).

> Mount the named volume. Without it the login is thrown away when the container
> exits and you re-authenticate every run.

## Beta and risk acceptance

**Everything in the toolbox is beta.** Commands, behaviour and defaults may
change, be switched off or be removed without notice. It is provided "as is",
without warranty, under the Apache 2.0 [LICENSE](LICENSE). This notice doesn't
change the LICENSE or any agreement you have with GlueOps.

**In scope:** `argocd` (read-only, including `argocd app logs`), `bao`, and the
GitOps deploy flow built on them (`helm`, `dyff`, `toolbox-app`,
`toolbox-preflight`, `toolbox-watch`, `./toolbox propose`). **Out of scope:**
`promtool`, `logcli`, `tempo-cli` and `grafana-ds`. They have known issues and
refuse to run (exit 5). A human at a terminal can switch them back on, unsupported
and at their own risk, with `TOOLBOX_ENABLE_OBSERVABILITY=1 ./toolbox up <domain>`.
`up` ignores the variable when an AI agent drives it (or anything without a
terminal), and any later `up` without it - including every `up` an agent runs -
recreates the container with the CLIs off, dropping open `./toolbox shell`
sessions. Commands an agent runs through `./toolbox` are refused even while the
CLIs are on, and agents decline these requests regardless.

**AI agents act with your credentials:** the toolbox token (ArgoCD, OpenBao -
`editor` by default - and the observability datasources behind Grafana) and, on the host,
your `git`/`gh` login. The guardrails — read-only `argocd`, deploys only by pull
request, agents declining out-of-scope requests — reduce that risk but don't
remove it. They are guardrails, not security boundaries. You are responsible for
what an agent does with the toolbox, including reviewing every pull request before
you merge it.

**By using the toolbox, or letting an AI agent use it for you, you accept these
risks**, along with those under [Known risks](#known-risks) and
[Known issues](#known-issues).

### Using an agent from your deployment repo

Agents load instructions from the repository they are working in, and they
usually work in your deployment-configurations clone — not here — so they don't
see this repo's [CLAUDE.md](CLAUDE.md) or [AGENTS.md](AGENTS.md) on their own.
`./toolbox up` prints the agent rules in brief every time and `./toolbox rules`
prints them in full, but to be sure, add them to the deployment repo. They live
on their own in [AGENT-RULES.md](AGENT-RULES.md):

- **Claude Code:** in that repo's `CLAUDE.md`, add `@../toolbox/AGENT-RULES.md`.
  Relative imports resolve from the file that contains them, so this assumes the
  two clones are siblings; from `~/.claude/CLAUDE.md`, use an absolute path. Claude
  Code asks you to approve an import from outside the project the first time; if
  you decline, the rules aren't loaded.
- **Codex, Cursor and others:** copy [AGENT-RULES.md](AGENT-RULES.md) into that
  repo's `AGENTS.md`.

## Why a container

Everything on a GlueOps cluster sits behind oauth2-proxy, which expects a browser
session cookie. CLIs don't have one, so out of the box every request is answered
with a login redirect. Getting past that needs a token, and — for OpenBao — a
header on every single invocation that no shell wrapper can place reliably.

The container handles all of it, so the CLIs are just the CLIs.

## The `toolbox` wrapper

`./toolbox` is a small host-side script that drives the container and deals with
the environment so you don't have to. `up` starts dockerd if it's installed but
not running, pulls the image if missing, passes proxy settings through by name,
mounts the host's own CA bundle so the container trusts whatever the host trusts
— honouring `CURL_CA_BUNDLE`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` or
`NODE_EXTRA_CA_CERTS` when the environment sets one, before the system paths —
and uses host networking when the proxy is
bound to the host's loopback. It then checks egress from inside the container
against a small public endpoint — not your cluster, which may be slow or private
— and if the bridge network has none, recreates the container with host
networking; a TLS failure there means an interception CA the host doesn't have,
and it says so. It prints each decision.

**One cluster at a time.** The login lives in a volume (`glueops-toolbox`, or
`glueops-<container>` with `TOOLBOX_CONTAINER`) that `up` labels with its cluster;
the login inside also records its Dex issuer and client. `up` for a different
captain domain — even after `down`, with no container left — removes the
container and that volume before starting, so no token from the old cluster
survives. `https://`, a trailing `/` or `.`, and capitals don't count as a
different domain. `./toolbox reauth` does the same removal on demand. The tokens
are removed from this machine, not revoked; they expire on their own. Switching
back costs another sign-in - to keep two clusters signed in, give each its own
container: `TOOLBOX_CONTAINER=toolbox-prod ./toolbox up prod…`.

Only volumes the toolbox created are ever removed. `TOOLBOX_VOLUME` may name your
own volume or a host directory; those are never removed (a cluster switch leaves
them with a warning, and `reauth` refuses), and inside the container a cached
login for another cluster is discarded rather than used, as a backstop. A login
volume another container still uses stops both, before anything is removed.

**Upgrading to this version:** everyone signs in once more, because logins cached
before didn't record their cluster - once, if the script and the image are
upgraded together (`git pull` and `docker pull ghcr.io/glueops/toolbox:latest`).
A container started with `TOOLBOX_CONTAINER` set now gets its own
`glueops-<name>` volume instead of sharing `glueops-toolbox`; if an old container
still uses the shared volume, remove it (`docker rm -f <name>`).

`up` also mounts the directory it was run from — or `TOOLBOX_WORKDIR` —
read-only at the same path inside the container, and every `./toolbox <command>`
starts in your current directory. So a deployment-configurations clone under it
can be rendered with `./toolbox helm template …` using relative paths. Running
`up` from somewhere outside the mounted directory recreates the container with
the new mount; the login is kept.

It also notices when it's being driven by an agent (Claude Code sets
`CLAUDECODE`; otherwise, no terminal): then `up` prints the next step after the
URL and `wait` returns after ~90s so it fits under a tool's command timeout. At a
terminal, `up` opens the browser and `wait` blocks until you've approved.

| | |
|---|---|
| `./toolbox up <domain>` | start (or reuse) the container, print the login URL |
| `./toolbox wait` | wait for approval; exit 2 means call again |
| `./toolbox <command…>` | run it in the container: `./toolbox bao kv list secret/` |
| `./toolbox shell` | interactive shell |
| `./toolbox status` | running? logged in? |
| `./toolbox down` | remove the container; the login volume is kept |
| `./toolbox reauth [<domain>]` | forget the login entirely — remove the container and its login volume — and run `up` from scratch. The same cluster unless you name another. To sign in as someone else, open the new URL in a private window or sign out of GitHub first |
| `./toolbox rules` | the agent rules for the beta, in full ([AGENT-RULES.md](AGENT-RULES.md)) |
| `./toolbox propose -m <why>` | from a deployment repo clone: commit the change on a branch, have ArgoCD render it, open (or update) a pull request — never on a branch ArgoCD deploys from, never merged. `--revert <sha>` reverts a merged change the same way. Needs `git` and `gh` on the host. Exit 0 PR opened, 3 no change, 2 failed |

Overrides: `TOOLBOX_IMAGE`, `TOOLBOX_CONTAINER`, `TOOLBOX_VOLUME`,
`TOOLBOX_WORKDIR` (default: the current directory),
`TOOLBOX_PROBE_URL` (default `https://www.google.com/generate_204`), plus every
container variable below is passed through if set.

## Commands inside the container

| | |
|---|---|
| `argocd …` | ArgoCD CLI, **read-only**. Authenticated per-invocation, so a long shell never goes stale. Anything that changes state — sync, rollback, edits, `--refresh`, `app wait` — is refused (exit 5) before it reaches the server. |
| `helm …` | Helm 3, the version ArgoCD's server renders with. For `helm template` of the deployment repo before pushing a change. |
| `dyff …` | Kubernetes-aware YAML diff: `dyff between old.yaml new.yaml`. |
| `toolbox-app <app>` | Where an app's config lives: value files in override order, the `file:line` that sets `image.tag`, running images. |
| `toolbox-preflight <app> --rev <rev>` | ArgoCD's render of a pushed branch or commit vs. its render of where that branch started. Exit 0 no change, 1 change, 2 error. Secret contents are never printed. |
| `toolbox-watch <app> --rev <sha>` | After a merge, poll every 10 s until ArgoCD's automatic sync deploys it, then report health. Never syncs. Exit 0 healthy, 3 not yet, 4 deployed and failing, 2 tool error. |
| `bao …` | OpenBao CLI, pointed at a local proxy that attaches your token. |
| `toolbox-login` | Authenticate. Runs automatically on an interactive start. |
| `toolbox-login --begin` / `--wait` | The same login in two halves: print the URL and return (idempotent), then wait for approval — about 90 s per call, exit 2 means call again. For callers that can't sit on a blocking command. |
| `toolbox-login --force` | Start a fresh login inside the container. To wipe everything (tokens, the volume) and switch accounts or clusters, use `./toolbox reauth` from the host. |
| `toolbox-token` | Print the raw token, for scripting. |
| `promtool query instant\|range …` | **Switched off during the beta.** Prometheus CLI, pointed at Thanos. Metrics, plus alert state. The server argument is filled in for you. `query series`/`labels` and `debug` take no `--header`, so they cannot reach the cluster. |
| `logcli …` | **Switched off during the beta.** Loki CLI. Log queries and `--tail`. |
| `tempo-cli query api …` | **Switched off during the beta.** Tempo CLI. TraceQL search and trace lookup. |
| `grafana-ds <type>` | **Switched off during the beta.** Print a datasource UID (`prometheus`/`loki`/`tempo`); used by the wrappers. |


## Configuration

| Variable | Default | |
|---|---|---|
| `TOOLBOX_CAPTAIN_DOMAIN` | — | **Required.** e.g. `prod.foobar.onglueops.com` |
| `TOOLBOX_CLIENT_ID` | `toolbox` | Dex client used to mint the token |
| `TOOLBOX_DEX_URL` | `https://dex.$DOMAIN` | |
| `TOOLBOX_BAO_UPSTREAM` | `https://vault.$DOMAIN` | |
| `ARGOCD_SERVER` | `argocd.$DOMAIN` | |
| `TOOLBOX_PROXY_PORT` | `8200` | Loopback port the OpenBao proxy listens on |
| `TOOLBOX_TOKEN_CACHE` | `~/.config/glueops/toolbox-token.json` | |
| `TOOLBOX_EXTRA_CA` | — | Path to a mounted CA certificate to trust, for networks that terminate TLS at an egress proxy. Appended to the system store, so public CAs keep working. |
| `TOOLBOX_WAIT_SECONDS` | `90` | How long one `toolbox-login --wait` call waits before returning exit 2 |
| `TOOLBOX_IDLE_SECONDS` | `14400` | How long a bare `docker run -d` container stays up |
| `TOOLBOX_BAO_ROLES` | `editor,reader` | OpenBao roles tried at login, in order |
| `TOOLBOX_BAO_AUTH_PATH` | `jwt` | OpenBao auth mount the CLI logs in through |
| `TOOLBOX_ENABLE_OBSERVABILITY` | — | `1` switches `promtool`/`logcli`/`tempo-cli`/`grafana-ds` back on. Beta, unsupported, at your own risk. Set it on `up` from a terminal: `up` ignores it when an agent drives it (or there is no terminal, e.g. CI), any later `up` without it switches the CLIs off again, and commands an agent runs are refused regardless. See [Beta and risk acceptance](#beta-and-risk-acceptance). |

## How it works

**Getting a token.** `toolbox-login` runs the OIDC **device flow** against Dex.
That matters: there's no loopback listener and no redirect URI, so it works from
inside a container whose browser is on the host — a `localhost:8085` callback
would not. Dex issues a refresh token alongside, so the browser step happens once
rather than daily.

**ArgoCD** accepts that token directly (it's configured with the toolbox audience
in `allowedAudiences`), so one token satisfies both the edge and ArgoCD itself.

It needs to go in **two** headers, because each side reads only its own:

| | header | read by |
|---|---|---|
| `ARGOCD_AUTH_TOKEN` | `Token: <jwt>` | ArgoCD |
| `-H "Authorization: …"` | `Authorization: Bearer <jwt>` | oauth2-proxy |

Send only the env var and the edge sees no credential, redirects to a login page,
and the CLI reports `rpc error: unexpected EOF`. Send only `-H` and you get past
the edge with `Token:` empty, so ArgoCD answers `Unauthenticated: no session
information`. The wrapper sets both, fresh on every call.

**OpenBao** can't work that way. Its own credential travels in `X-Vault-Token`,
and the edge needs an `Authorization` bearer as well. `bao` has a `-header` flag,
but it must sit after the subcommand and before any positional argument —

```
bao kv get -header="…" secret/foo     ✓
bao kv get secret/foo -header="…"     ✗   flags must precede positional arguments
bao -header="…" kv get secret/foo     ✗   no global flag position
```

— and since `bao kv list secret` is indistinguishable from a subcommand plus a
path, no wrapper can place it correctly in general. So instead the container runs
a small loopback proxy that adds the header and forwards upstream, and points
`BAO_ADDR` at it. `bao` then needs no flags at all and scripts work unmodified.

`toolbox-login` also exchanges your Dex token for an OpenBao token, so `bao` is
usable immediately. It posts to the login endpoint directly rather than running
`bao login -method=jwt`, because the OpenBao CLI registers no `jwt` method — only
`oidc`, which is the browser redirect flow. Roles are tried most-privileged first
(`TOOLBOX_BAO_ROLES`, default `editor,reader`); which one you actually get is
decided by the role's `bound_claims`.

Set `TOOLBOX_BAO_ROLES=reader` to deliberately hold only read access for a
session. The CLI roles live on their own `auth/jwt` mount, separate from the web
UI's `auth/oidc`, which is why they can share the names of the policies they
grant.

The proxy binds to `127.0.0.1` only — it attaches your credential to whatever it
forwards, so it must never be exposed.

*The rest of this section describes `promtool`, `logcli` and `tempo-cli`, which
are switched off during the beta (see [Beta and risk acceptance](#beta-and-risk-acceptance)):
this is how they work when a human opts in.*

**Grafana, Loki, Thanos and Tempo** need no proxy. `promtool`, `logcli` and
`tempo-cli` all accept arbitrary headers, so each wrapper simply sends two:

| header | read by |
|---|---|
| `Authorization: Bearer <jwt>` | oauth2-proxy at the edge |
| `X-JWT-Assertion: <jwt>` | Grafana's `[auth.jwt]` |

Two headers are needed because the edge consumes `Authorization` for itself and
Traefik's forwardauth deletes it before Grafana sees it. `X-JWT-Assertion` is in
no `authResponseHeaders` list, so it passes through untouched — which is why this
needs no bearer-preserving middleware, unlike `argocd`.

Queries go through Grafana's datasource proxy rather than to Loki/Thanos/Tempo
directly, so there is one edge host and one credential for everything. The
wrappers resolve the datasource UID at run time (`grafana-ds`), since Grafana
generates UIDs per cluster.

`promtool` differs in one way the wrapper hides: only `query instant` and
`query range` accept `--header`. Those two get the `<server>` positional filled in
with the proxy URL. `query series`, `query labels` and every `debug` subcommand
take no `--header`, so a request from them reaches the edge with no credential and
is redirected to the login page; the wrapper refuses those against this cluster
rather than letting them fail as a confusing 302, and passes an explicit
`http(s)` server through untouched.

`tempo-cli` differs in three ways the wrapper hides: headers are `Key=Value` not
`Key: Value`; `search` takes a bare host plus `--path-prefix` while `trace-id`
takes a full URL; and `--use-grpc` is refused, because headers would then travel
as gRPC metadata and never reach the edge.

## GitOps: read-only ArgoCD, changes by pull request

The cluster changes only when ArgoCD's automatic sync picks up a commit merged to
the deployment repo. The `argocd` wrapper therefore allows only read commands,
and the agent instructions require every change to be a pull request for a human
to review. Both are conventions the toolbox enforces for itself; two platform
settings make them hold for everyone:

- **Branch protection on the deployment repo's `main`** — require pull requests
  and block direct pushes (with an exception for the deploy bot, if it commits
  directly). This is the only control that stops a push to `main` from a host.
- **Optional: read-only ArgoCD RBAC** for the toolbox's group — drop `sync` and
  `exec`. The wrapper's guard is a guardrail, not a boundary: the token is still
  reachable inside the container. Note that RBAC cannot stop a refresh, since any
  `get` permission can ask for one.

## Known risks

Everything below is **known**, not a bug report, and by using the toolbox you
accept it (see [Beta and risk acceptance](#beta-and-risk-acceptance)). It is
written down because the capabilities are wider than the commands imply, and
nothing in the platform currently constrains them.

**Your token can write to the observability datasources, not just read them.**
Grafana's datasource proxy is a full pass-through: it forwards `POST`, `PUT` and
`DELETE` to the datasource exactly as it forwards `GET`, and Grafana has no
method-level control over it. So the same credential that runs a PromQL query can
also reach Loki's ingestion endpoint:

    POST /api/datasources/proxy/uid/<loki>/loki/api/v1/push

Verified: that request reaches Loki's push handler (it answers with Loki's own
validation error, not a Grafana block).

**Writes to Loki, Thanos and Tempo are durable.** All three are backed by S3
object storage, so anything written survives pod restarts and full cluster
rebuilds. Deleting pods does not undo it — the objects have to be removed from the
bucket. Log lines in particular carry no provenance: Loki records what was pushed,
not who pushed it, so an injected line is indistinguishable from an ingested one.
Ingestion limits bound this (`reject_old_samples_max_age: 168h`, rate limits,
`max_streams_per_user`) but do not prevent it.

**No audit trail.** Grafana OSS does not log datasource-proxy requests per user,
so there is no record of who queried or wrote what.

**This is not privilege escalation.** Anyone holding a toolbox token already has
ArgoCD and OpenBao access; they are trusted operators. The point is that the
datasource write path is a side effect of enabling CLI authentication, not
something anyone deliberately granted — so treat these tools as read-only by
convention, because nothing enforces it.

If that convention is ever not enough, the enforcement point is the edge: a
Traefik router rule matching `Method(`GET`)` on `/api/datasources/proxy/` would
make the read-only intent real. It is deliberately not done today. Switching the
observability CLIs off during the beta doesn't close this either: the token
still reaches Grafana's proxy directly.

## Known issues

**The observability CLIs are switched off during the beta.** `promtool`,
`logcli`, `tempo-cli` and `grafana-ds` have known issues and exit 5 with a
refusal. See [Beta and risk acceptance](#beta-and-risk-acceptance). It is a
guardrail, not a security boundary: the binaries remain in the image.

**An ArgoCD permission error can look like a login problem.** The `argocd` CLI
speaks gRPC-web over root paths (`/application.ApplicationService/List` and
similar). On the platform those paths are matched by the ingress that carries the
edge's `errors-redirect` plugin, which rewrites any 401-403 response into a
redirect to the login page. That plugin sits outside ArgoCD, so it catches
ArgoCD's own responses too: an ordinary RBAC denial ("you don't have permission
for this app") is rewritten into a login redirect, and the CLI reports

    rpc error: code = Unknown desc = unexpected EOF

Re-authenticating will not help, because you were never unauthenticated. If
`toolbox-login` succeeds and the same command still fails this way, suspect
permissions rather than your session, and check the same operation in the ArgoCD
web UI, which reports the real error. The `/api/v1` REST paths are unaffected and
return a normal status code.

**Your token is visible to other processes in the container.** The `argocd`
wrapper passes the edge credential on the command line (`-H "Authorization:
Bearer …"`) and in the environment (`ARGOCD_AUTH_TOKEN`), because the ArgoCD CLI
has no way to take a header from anywhere else — `ARGOCD_OPTS` rejects any value
containing spaces. Anything else running inside the container can therefore read
your token from `/proc/<pid>/cmdline` or `/proc/<pid>/environ`, and `cmdline` is
world-readable. That token opens the edge, ArgoCD, and OpenBao.

Treat the container as trusted: do not run untrusted code, unvetted argocd
plugins, or third-party scripts inside it while you are logged in. `bao` is not
affected — it reaches OpenBao through the loopback proxy, which adds the header
server-side and keeps it off the command line.


## Platforms

Built for `linux/amd64` and `linux/arm64`, so Apple Silicon is native — no
emulation, no Rosetta.

The host side, `./toolbox`, needs bash, the docker CLI and a docker daemon on
the same machine: `up` bind-mounts your working directory and CA bundle, so a
remote `DOCKER_HOST` or SSH docker context can't work. `propose` also needs
`git` and `gh`. `tests/host-smoke.sh` checks a machine in a few minutes, with
no login and throwaway names: `bash tests/host-smoke.sh`.

| Host | Status |
|---|---|
| Linux, Docker Engine | Tested; the smoke test runs in CI |
| A codespace or devcontainer using the host's docker | Tested — works when the workspace is at the same path on the docker host |
| Windows: WSL2 (Ubuntu 24.04) with Docker Engine inside it | Tested: the smoke test, from the Linux filesystem and from `/mnt/c`, and a real login |
| macOS: Docker Desktop, Colima, OrbStack | Expected to work (bash 3.2, BSD tools); not yet tested |
| Linux, rootless Docker | Expected to work; not yet tested |
| Windows: WSL2 with Docker Desktop | Expected to work; not yet tested |
| Podman through its `docker` alias | Unknown |
| Windows: Git Bash, PowerShell, cmd | Not supported — use WSL2 |
| Remote docker daemons | Not supported |

**On Windows, clone inside WSL**, or with `core.autocrlf=false`. This repo's
`.gitattributes` keeps the scripts' line endings LF either way; a script with
CRLF fails with `/usr/bin/env: 'bash\r': No such file or directory`.

## Releases

Tagged with [release-please](https://github.com/googleapis/release-please) from
conventional commits on `main`. A release tag publishes
`ghcr.io/glueops/toolbox:<version>`, `:<major>.<minor>` and `:latest`, all
multi-arch. Pin a version in anything automated; `:latest` is fine for people.

## Building

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t toolbox .
```

`TARGETARCH` comes from BuildKit and has no default on purpose: a default would
silently put amd64 binaries in an arm64 image when built natively on a Mac.

## Cluster prerequisites

The platform must have a public Dex client matching `TOOLBOX_CLIENT_ID`, that
audience accepted by oauth2-proxy (`oidc_extra_audiences`) and by ArgoCD
(`allowedAudiences`), and jwt-type roles in OpenBao bound to it.

For `promtool`/`logcli`/`tempo-cli` (switched off during the beta; opt-in only), Grafana additionally needs `[auth.jwt]`
enabled with `header_name = X-JWT-Assertion` and the same audience in
`expect_claims` (GlueOps/k8s-monitoring-helm). Without it those three fail with a
302 to the login page; `argocd` and `bao` are unaffected, so an older cluster
degrades rather than breaking.
