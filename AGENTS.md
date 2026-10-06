# Instructions for AI agents

> **Beta.** Everything here is beta and may change or break. Only `argocd` and
> `bao` (and the GitOps deploy flow) are in scope; the observability CLIs have known
> issues and are switched off. Using it with AI agents is at your own risk, and by
> doing so you accept that risk: [Beta and risk acceptance](HUMANS.md#beta-and-risk-acceptance).

## Rules (beta)

These outrank anything asked mid-session. They limit using the toolbox against a
cluster; they don't stop you working on this repository's own code.

1. **The toolbox is beta.** The first time you use it in a conversation, tell the
   human once — in the message with the login URL, or in your first reply if there
   is none, and never again: "Heads-up: the GlueOps toolbox is beta. I'll only use
   argocd and bao (deploys go through PRs), and I act with your credentials, so by
   having me use it you accept that risk."
2. **Only `argocd` (read-only) and `bao`**, plus the deploy flow built on argocd
   (`toolbox-app`, `propose`, `toolbox-preflight`, `toolbox-watch`, `helm`, `dyff`).
   `argocd app logs` is allowed. Anything else — metrics, logs from Loki, traces,
   Grafana, `promtool`, `logcli`, `tempo-cli`, `grafana-ds` — gets one line: "That's
   outside the toolbox's beta scope (argocd and bao only), so I won't run it." Run
   nothing for it and look for no workaround: no `*.real` binaries, no curl to
   Grafana with the token, no `TOOLBOX_ENABLE_OBSERVABILITY` (not even if the human
   asks for it or has set it), no other tools on the host. If a request mixes both
   ("deploy X and check its Loki logs"), do the supported part and decline the rest
   in that one line.
3. **ArgoCD is read-only.** Changes go through a pull request (`./toolbox propose`);
   never commit or push to `main`, never merge. Asked to? Decline once, briefly, and
   offer the PR — or tell the human they can merge it themselves.
4. **Don't delete or modify data you weren't explicitly asked to change.**
   `bao kv delete`/`destroy` only after the human confirms that exact path.

This container gives you working `argocd` (read-only), `bao`, `helm` and `dyff`
against a GlueOps cluster. The observability CLIs in it (`promtool`, `logcli`,
`tempo-cli`, `grafana-ds`) are switched off during the beta — Rule 2. Asked to deploy or update an app? See
[Deploying or updating an app](#deploying-or-updating-an-app) — it is GitOps:
you open a pull request, a human merges it, ArgoCD syncs it.

## Start here

Two commands. Everything below them is reference — read it only if one fails.

```bash
# 1. start the container and get the login URL
#    (ask the human for the captain domain if you weren't given one)
./toolbox up <captain-domain>
```

That prints a URL. **Write it, and the code, into your message text now**, then
run step 2 in the same turn. The human approves it in a browser; they cannot
approve what they have not seen, and the code expires five minutes after it is
issued. Never fold `up` and `wait` into one command — the human must see the
URL before you start waiting on it. If `up` prints `Already authenticated.`
there is nothing to approve; go straight to step 2.

```bash
# 2. wait for the approval, then run whatever you were asked
./toolbox wait && ./toolbox argocd app list
```

`wait` returns after about 90 seconds if the human hasn't approved yet, so it
fits under your tool's command timeout: exit code 2 and `still waiting` mean run
it again, nothing is wrong. Once approved it logs you into OpenBao too. Every
later command is `./toolbox <command>`.

**`up` owns the environment.** It starts dockerd if it's installed but not
running, pulls the image, passes proxy variables through, mounts the host's CA
bundle so the container trusts what the host trusts, uses host networking when
the proxy is on the host's loopback, checks egress from inside the container and
retries with host networking if the bridge has none, and retries again if Dex
turns out to be reachable only from the host. It prints each decision on stderr,
and it knows it's talking to you (it sees `CLAUDECODE`, or no terminal) so it
tells you the next step right after the URL. Don't check docker, read proxy
documentation, look for CA files or test connectivity before running it — every
one of those is a wasted command; `up` already does the right thing or tells you
exactly what it couldn't do.

## If `up` fails

Its last lines say what happened. The cases:

- **`docker is not installed`** / **`cannot connect to the docker daemon`** and
  you are not root — you need docker, or a user that can reach it. Nothing in
  this repo can fix that; tell the human.
- **`dockerd did not come up`** — `cat /tmp/toolbox-dockerd.log`.
- **`no network egress at all`** after both networks — this host can't reach the
  internet from a container. If the host needs a proxy, `export HTTPS_PROXY`
  (and `HTTP_PROXY`) in your shell and run `up` again; it passes them through.
- **`a proxy intercepts TLS and the container does not trust its CA`** — the
  host's CA bundle didn't include the interception CA. Find the CA file your
  environment installs (its own docs will say), then
  `TOOLBOX_EXTRA_CA=/path/to/ca.crt ./toolbox up <domain>`. This is the one case
  where reading your environment's proxy docs is worth it.
- **`cannot reach https://dex.<domain>`** after both networks, with egress
  working — the domain is wrong, or the cluster is on a network this host can't
  see. Check `curl https://dex.<domain>/healthz` from the host; if that fails
  too, no container flag will change it.
- **`the code expired`** or **`login access_denied`** from `wait` — run
  `./toolbox up <domain>` again and show the new URL.
- **Told to log in as someone else** — `./toolbox toolbox-login --begin --force`
  discards the cached identity and mints a fresh URL, then `./toolbox wait`.

## Do not

- **Don't read the source to work out how it functions.** The proxy, the wrappers
  and the login helper are implementation detail. Nothing in them changes what you
  type, and reading them is minutes of work for no answer.
- **Don't probe the environment first** — docker state, proxy variables, CA
  files, network egress, Python libraries. `up` does all of that and prints what
  it found. If it fails, the error tells you what's wrong.
- **Don't run the container by hand** with `docker run -it`. You have no TTY, and
  `up` already made the decisions a bare `docker run` would get wrong.
- **Don't delete or modify data.** These credentials can write well beyond what
  the commands suggest — including into Loki, Thanos and Tempo through Grafana's
  datasource proxy, durably and with no audit trail (see
  [HUMANS.md](HUMANS.md#known-risks)). Query, inspect and report: no
  `bao kv delete`/`destroy`, no Grafana `DELETE` calls, no pushes to any
  datasource. If a task seems to need a destructive action, stop and ask.

Get the login URL in front of the human as fast as you can — the code expires five
minutes after it is issued, and every command you run first eats into that. Step 1
is one command for exactly that reason.

---

Everything below is reference.

## The tools

**`argocd`** — [argoproj/argo-cd](https://github.com/argoproj/argo-cd), GitOps
continuous delivery for Kubernetes. It manages `Application` resources that sync a
cluster to git. The CLI talks to a central API server, not to the Kubernetes API,
so it does not need kubeconfig. Currently `v3.3.12` in this image. **Read-only
here**: the wrapper refuses anything that changes state, including `--refresh`
and `app wait` (both force a reconcile) — see
[Argo CD is read-only](#argo-cd-is-read-only).

**`helm`** — renders charts locally (`helm template`), for checking a change to
the deployment repo before pushing it. `3.19.4`, the version ArgoCD's server
renders with; `argocd version` shows the server's. **`dyff`** — diffs
multi-document Kubernetes YAML by resource rather than by line. `1.12.0`.

**`bao`** — [openbao/openbao](https://github.com/openbao/openbao), a secrets
manager. It is an open-source fork of HashiCorp Vault, so almost everything you
know about Vault applies: same API shape, same path layout (`secret/`, `sys/`,
`auth/`), same policy model. Two differences that will trip you up:

- The binary is `bao`, not `vault`, and the environment variables are `BAO_*`
  (`BAO_ADDR`, `BAO_TOKEN`). The `VAULT_*` names still work, and `BAO_*` wins if
  both are set — this container sets both, so either will do.
- It has diverged from Vault in places. Don't assume a Vault feature exists; check
  first. For example the CLI registers no `jwt` auth method, so
  `bao login -method=jwt` fails even though the `jwt` auth backend is mounted and
  works over the API.

Currently `2.4.4` in this image. Its docs are at
[openbao.org/docs](https://openbao.org/docs/), and where they are thin the Vault
documentation is usually still correct.

The one thing you can't do is authenticate. Login is a device flow: a human opens
a URL and approves with GitHub. Start it, **give the URL to the person you're
working for**, wait for them, then run whatever you were asked.

If a step in **Start here** fails, this is what each one is doing and why.

**The captain domain** (e.g. `prod.foobar.onglueops.com`) is not in this repo and
cannot be guessed. Ask for it.

**`docker run -it` cannot work** — you have no TTY, so there is nothing to type
into and no way to read the device URL back out. `up` runs the container
detached and drives it with `docker exec`, which is why it exists.

**`--begin` and `--wait` are two halves of one login.** `--begin` asks Dex for a
device code, saves it, prints the URL and returns; run it twice and you get the
same URL back, not a second one. `--wait` polls Dex with that code, for about 90
seconds per call (`TOOLBOX_WAIT_SECONDS`), and exits 2 if the human hasn't
approved yet — just call it again. Both are safe to rerun when already logged in.

**`./toolbox <command>` runs it in a login shell** inside the container, which is
what sources `/etc/toolbox-env.sh` and configures the CLIs. If you ever bypass
the wrapper, it has to be `docker exec toolbox bash -lc '...'` — a bare
`docker exec toolbox argocd app list` will not work.

**`Already authenticated.`** with no URL means the cached volume still holds a
valid token. Skip to the command. Codes expire after five minutes; if one lapses,
rerun `toolbox-login --begin`, show the new URL, then `--wait` again.

## Commands

Everything runs as `./toolbox <command>`; the rest of this section shows just
the command. Arguments are passed through intact, so quote as you normally would.
For a pipeline or a script, wrap it: `./toolbox bash -c 'argocd app list -o json | ...'`
so the container's environment applies throughout.

```bash
./toolbox argocd app list
./toolbox bao kv get -format=json secret/my-app
```

### Argo CD — reading

| | |
|---|---|
| `argocd app list` | every application, with sync and health |
| `argocd app list -o json` | same, machine-readable — use this to filter or sort |
| `argocd app get <app>` | one application in detail, including its resources |
| `argocd app history <app>` | deployment history, newest first |
| `argocd app get <app> -o tree=detailed` | resources down to pods, with health and messages |
| `argocd app diff <app>` | live state vs. desired — exits `1` if there is a diff, `0` if none, `2` on error |
| `argocd app manifests <app>` | rendered manifests (desired state from git) |
| `argocd app manifests <app> --revisions <sha> --source-positions <n>` | rendered manifests for another commit of source `<n>` — how you test a pushed branch |
| `argocd app logs <app>` | logs from the app's pods |
| `argocd cluster list` | connected clusters (may be empty: needs cluster permissions) |
| `argocd proj list` | projects |
| `argocd repo list` | configured repositories |

### Argo CD is read-only

Everything is GitOps: the cluster changes only when ArgoCD's automatic sync
(about every 3 minutes) picks up a commit merged to the deployment repo. So the
`argocd` wrapper allows only reads — `app list|get|diff|manifests|history|
resources|logs|get-resource`, `proj`, `cluster`, `repo` and `appset` `list|get`,
`account get-user-info|can-i`, `version` — and refuses everything else with exit
`5` before contacting the server. That includes `--refresh`/`--hard-refresh`
anywhere and `app wait`, which ask the server to reconcile, and flags that point
it elsewhere (`--server`, `--core`, `--port-forward`, `-H`, …). Global flags go
after the command. Exit `4` means not authenticated.

Don't work around it, even where your RBAC would allow a sync. If something
needs to change, it goes through a PR.

## Deploying or updating an app

Rules 3 and 4 at the top apply. Also: don't check that image tags or registries
exist, and `TOOLBOX_BAO_ROLES=reader` on `up` is enough for this work.

Run `up` from a directory that contains both this repo and the deployment repo
clone (their common parent, say): it is mounted read-only at the same path, and
`./toolbox` commands start in your current directory, so relative paths work
inside the container. Work from inside the clone. `git` and `gh` stay on the
host. Below, `<toolbox>` is the path to the wrapper from the clone, e.g.
`../toolbox/toolbox`.

| | |
|---|---|
| `<toolbox> toolbox-app <app>` | where the config lives: chart, deployment repo and its source position, value files in override order, the `file:line` setting `image.tag` (`<- effective` marks the winner; a tag set in the app spec itself is called out), running images. `--json` |
| `<toolbox> propose -m "<why>"` | **host side.** Branch, commit, push the branch, have ArgoCD render it for every affected app, open (or update) the PR — then stop. Exit `0` PR opened/updated, `3` no change (no PR), `2` failed (no PR), `1` usage |
| `<toolbox> propose --revert <merge-sha>` | the same for reverting a merged change |
| `<toolbox> toolbox-preflight <app> --rev <branch\|sha>` | ArgoCD's render of a pushed revision vs. its render of where that branch left the tracked branch — `0` no change, `1` change, `2` error. `propose` runs it for you |
| `<toolbox> toolbox-watch <app> --rev <merge-sha>` | after a merge: polls every 10 s until ArgoCD's automatic sync deploys it, then for health — `0` healthy, `3` not there yet, `4` deployed and failing, `2` the tool failed |

**1. Find the config.** `<toolbox> toolbox-app <app>`. Later value files override
earlier ones; edit the most specific that fits — usually
`apps/<app>/envs/<env>/values.yaml`. Base, env-overlay and common files affect
several apps (`propose` finds and checks them all). Preview environments
(`apps/*/envs/previews/`) belong to the app repos' pull requests; `propose`
refuses them. A brand-new app or environment that no ArgoCD app reads yet isn't
supported by `propose` — ask the human how they want it proposed.

**2. Edit.** On `main`, or on a branch of your own. Don't commit — `propose`
does — and don't leave anything else in the clone: `propose` refuses changes no
ArgoCD app reads (scratch files, renders, `.env`) rather than commit them.

**3. Propose.** `<toolbox> propose -m "<why, in a sentence>"`. It:

- refuses to touch any branch an ArgoCD app tracks. On one of those (`main`,
  say) it starts a new branch from `origin/main`, carrying your edits; on your own
  branch it uses that. Names and titles follow the deploy bot:
  `<app>/update-<env>-image-tag-<tag>` and `chore(deploy): <app> [<env>] -> <tag>`
  for a tag bump, `<app>/update-<env>-<slug>` otherwise;
- commits only the changed files that ArgoCD apps read, and pushes the branch
  to its own name and nothing else (an explicit refspec, so no git setting can
  redirect it), checking afterwards that no tracked branch moved;
- runs `toolbox-preflight` for every ArgoCD app visible to you that reads a
  changed file from the tracked branch. No change: exit `3`, no PR. A failed
  render: exit `2`, no PR. Either way the branch stays pushed, and you are on it;
  say so — the human can delete it;
- opens the PR with your intent, a summary per app (resources and the fields
  that changed, image old → new; Secrets as "contents not shown") and the values
  diff with secret-looking values masked. **Never rendered manifests.** A pure
  image-tag bump also gets the `glueops-deploy` marker, so the repo's cleanup
  workflow treats it like the bot's deploy PRs — it closes older open PRs for
  the same app and env, and a newer one closes this. `propose` lists any it
  would supersede;
- run again on the same branch (after review feedback), it pushes and updates
  the open PR instead of opening another;
- prints the URL. **Give the human the link and stop.** Nothing deploys until
  they merge.

The full resource-level diff stays in the container:
`<toolbox> cat /tmp/toolbox-deploy/<namespace>_<app>.dyff` (Secrets excluded).

**4. After they merge, watch — don't sync.** `git fetch`, get the merge commit
(`gh pr view <pr> --json mergeCommit --jq .mergeCommit.oid`), then
`<toolbox> toolbox-watch <app> --rev <sha>` for each affected app. Give the
command at least 8 minutes (Bash `timeout: 480000`), or lower `--sync-timeout`
and `--health-timeout` to fit. It reads `argocd app get` every 10 seconds: up
to 4 minutes for ArgoCD to pick up the commit (it polls git about every 3
minutes; a later commit that includes yours counts), then up to 3 minutes for
health. It is done when the app is `Synced` and `Healthy` at that revision with
no operation running — including when the merge changed nothing for that app.

- Exit `3`: not there yet, or ArgoCD is on a commit your clone doesn't have
  (it says to `git fetch`). Report it and offer to keep watching (run it
  again). Never force it.
- Exit `4`: deployed and failing — the sync failed, or the app is `Degraded`
  after it. It prints the unhealthy resources and pods. Show them, and offer
  `<toolbox> propose --revert <sha>`, which opens a revert PR — for the human to
  merge, not you.
- Exit `2`: the tool failed (not logged in, network, …). That says nothing about
  the deploy; don't offer a revert on it.

Without the helpers, the same checks are `argocd app get <app> -o json` (the
deployment repo is the source with a `ref`; its 1-based position in `spec.sources`
is `<n>`), `argocd app manifests <app> --revisions <sha> --source-positions <n>`
compared with plain `argocd app manifests <app>` using `dyff`, and polling
`.status.sync.revisions[n-1]`, `.status.operationState.phase` and
`.status.health.status`.

### OpenBao — reading

| | |
|---|---|
| `bao kv list secret/` | keys at a path — trailing slash matters |
| `bao kv get secret/<path>` | one secret |
| `bao kv get -format=json secret/<path>` | machine-readable |
| `bao kv get -field=<key> secret/<path>` | one value, unquoted, no trailing newline |
| `bao kv metadata get secret/<path>` | versions and timestamps, no values |
| `bao token lookup` | who you are, which policies you hold |
| `bao token capabilities secret/<path>` | what you may do at a path — check before assuming |
| `bao secrets list` | mounted secrets engines |
| `bao policy read <name>` | a policy's rules |

### OpenBao — changing (only when asked; deletes only after the human confirms the exact path)

| | |
|---|---|
| `bao kv put secret/<path> k=v` | write, creating a new version |
| `bao kv patch secret/<path> k=v` | update one key, leaving others |
| `bao kv delete secret/<path>` | soft-delete the latest version |
| `bao kv destroy -versions=<n> secret/<path>` | permanently remove a version |

`bao kv put` replaces the whole secret — keys you don't pass are dropped from the
new version. Use `patch` to change one field, or read the secret first.

### Switched off during the beta

`promtool`, `logcli`, `tempo-cli` and `grafana-ds` refuse with exit `5` — known
issues. Per Rule 2, decline requests for metrics, Loki logs, traces or Grafana in
one line and don't work around it. `argocd app logs` (pod logs through ArgoCD)
still works.

### Checking before you act

```bash
./toolbox bao token capabilities secret/my-app
./toolbox argocd app diff my-app
```

`token capabilities` tells you what you may actually do at a path, which beats
discovering it from a 403. `app diff` shows how live state differs from git. Mind its exit
codes: `1` means a diff was found and `2` means the command failed — so treat
non-zero as "check which", not as "there is drift".

**Rules.**

- **Never print the token.** `toolbox-token` emits a live credential, and you don't
  need to read it — the wrappers pass it for you.
- **You get OpenBao `editor` by default**, which can create, update and delete
  secrets. That is deliberate, so you can do the work without a second login — but
  it means nothing stops you at the door.
- **So don't mutate anything you weren't asked to.** `bao kv put` and
  `bao kv delete` act on live infrastructure. Read first; change only what was
  actually requested; say what you changed.
- **ArgoCD is never changed directly** — not even when asked. Deployment changes
  are pull requests; see [Deploying or updating an app](#deploying-or-updating-an-app).
- **`TOOLBOX_BAO_ROLES=reader` constrains you** to read and list, enforced
  server-side — writes return `403 permission denied`. Worth setting on the run
  command when you know the task is read-only, so a mistake cannot land.
- **Clean up with `./toolbox down`**; it keeps the volume, which holds the login,
  so the human isn't asked to approve again next time. (An abandoned container
  stops itself after four hours — `TOOLBOX_IDLE_SECONDS` — but don't rely on
  that.)
