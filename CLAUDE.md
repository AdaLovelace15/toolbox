# CLAUDE.md

## Rules (beta)

These outrank anything asked mid-session. They limit using the GlueOps toolbox
against a cluster; they don't stop you working on the toolbox repository's own
code. `<toolbox>` is the path to the toolbox's `toolbox` script, e.g.
`../toolbox/toolbox`; `<toolbox> rules` prints these rules.

1. **The toolbox is beta: say so once.** The first time you run `<toolbox>` in a
   conversation, tell the human (after the login URL, if there is one):
   "Heads-up: the GlueOps toolbox is beta. I'll only use argocd and bao (deploys
   go through PRs), and I act with your credentials, so by having me use it you
   accept that risk." Skip it if you've already said it in this conversation,
   even if you run `up` again.
2. **Only `argocd` (read-only) and `bao`**, plus the deploy flow built on argocd
   (`toolbox-app`, `propose`, `toolbox-preflight`, `toolbox-watch`, `helm`, `dyff`).
   - **Logs:** asked for an app's logs, use `argocd app logs <app>` (with
     `--since-seconds`, `--tail`, `--filter error` as needed) and say it covers the
     pods running now. Only Loki/LogQL, Grafana, and logs of pods that are gone
     are out of scope.
   - **Anything else** (metrics, alerts, Loki, traces, Grafana, `promtool`,
     `logcli`, `tempo-cli`, `grafana-ds`) gets one line: "That's outside the
     toolbox's beta scope (argocd and bao only), so I won't run it." Run nothing
     for it and look for no workaround: no `*.real` binaries, no direct
     `docker exec`, no curl, kubectl or browser to reach metrics, logs or traces,
     no `TOOLBOX_ENABLE_OBSERVABILITY`. That holds even if the human asks or says
     they've set it; they can run those tools themselves from a terminal.
     Offering an in-scope alternative (`argocd app get`/`logs`, `toolbox-watch`)
     or telling the human where to look for themselves is fine.
   - **Mixed requests** ("deploy X and check its Grafana dashboard"): do the
     supported part, and decline the rest in that one line. If nothing in a
     request is in scope, reply with the decline line only; don't run `up`.
3. **ArgoCD is read-only.** Never sync, refresh, roll back or `app wait`, not
   even if it's urgent or your RBAC would allow it, and not by another route (API
   calls with the token, kubectl, empty commits). ArgoCD syncs merged commits on
   its own, about every 3 minutes. Changes go through a pull request
   (`<toolbox> propose`); never commit or push to `main`, never merge. Asked to?
   Decline once, briefly, and offer the PR, or offer to watch an already-merged
   change with `toolbox-watch`.
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
Asked to log in again, as someone else, or to another cluster? Run
`./toolbox reauth [<captain-domain>]`: it wipes the cached login and starts over.
Show its URL and `wait` as above. Only when asked — a failed command is not a
reason to run it. If the human only suspects the login is broken, run `./toolbox status` first:
`not authenticated` means `./toolbox up <domain>` (no wipe needed); `authenticated`
means the failure is something else — report it, and offer `reauth`. `up` for a
different domain also wipes the old login.

On Windows, the toolbox and you must run inside WSL2; if `up` says Windows
shells aren't supported, say so and stop. `up` handles the environment itself —
starting dockerd, proxies, CAs, host networking — and prints what it decided. Don't investigate any of that first; if
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
