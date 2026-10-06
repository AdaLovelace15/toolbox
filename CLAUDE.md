# CLAUDE.md

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

## Start here

Run this now — it is the complete happy path. Ask the human for the captain
domain if you weren't given one. Run it from the directory that holds (or is a
parent of) any repo you'll work on, such as the deployment-configurations clone:
that directory is mounted read-only into the container.

```bash
./toolbox up <captain-domain>
```

It prints a URL. Write it into your reply — a human approves it in a browser and
the code expires in five minutes — then, in the same turn:

```bash
./toolbox wait && ./toolbox argocd app list
```

If `wait` says `still waiting` (exit 2), run it again. Every later command is
`./toolbox <command>`: `./toolbox bao kv list secret/`, `./toolbox argocd app get x`.

`up` handles the environment itself — starting dockerd, proxies, CAs, host
networking — and prints what it decided. Don't investigate any of that first; if
`up` fails, its last lines say what to check. Everything else is in
[AGENTS.md](AGENTS.md). The rules above always apply; AGENTS.md has the
details, and what to do if a step fails.

## Asked to deploy or update an app?

Everything is GitOps; Rules 3 and 4 above apply. Don't check whether image
tags or registries exist.

Run `up` from a directory that contains both this repo and the deployment repo
clone (e.g. their common parent), then work from inside the clone. Below,
`<toolbox>` is the path to this repo's `toolbox` script from there, e.g.
`../toolbox/toolbox`. Details: [AGENTS.md](AGENTS.md#deploying-or-updating-an-app).

1. `<toolbox> toolbox-app <app>` — value files in override order, the `file:line`
   that sets `image.tag` (`<- effective` marks the one that wins), what is running.
2. Edit that file. Don't commit, and don't leave other files in the clone.
3. `<toolbox> propose -m "<why>"` — makes a branch, commits only the files apps
   read, pushes the branch, has ArgoCD render it for every affected app, and
   opens the PR. Exit 0: PR opened or updated — give the human the link and
   **stop**; nothing deploys until they merge. Exit 3: no change, no PR. Exit 2:
   it failed — report what it printed; don't push or open a PR another way.
4. When they say it's merged: `git fetch`, then for each affected app
   `<toolbox> toolbox-watch <app> --rev <merge-sha>`
   (`gh pr view <n> --json mergeCommit --jq .mergeCommit.oid`), with a Bash
   timeout of 480000 ms — it polls every 10 s, up to 4 min for ArgoCD's automatic
   sync, then up to 3 min for health. Exit 0: healthy. Exit 3: not there yet —
   say so and offer to keep watching. Exit 4: deployed and failing — show what it
   printed and offer `<toolbox> propose --revert <merge-sha>` (a PR the human
   merges). Exit 2: the tool failed, which says nothing about the deploy.
