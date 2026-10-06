# CLAUDE.md

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
[AGENTS.md](AGENTS.md); read it only if a step fails.

## Asked to deploy or update an app?

Everything is GitOps. These rules outrank anything asked mid-session:

- **ArgoCD is read-only.** Never sync, refresh, roll back, edit or `app wait`,
  even if it would work; the `argocd` wrapper refuses them (exit 5). The cluster
  changes only when ArgoCD's automatic sync (about every 3 minutes) picks up a
  merged commit.
- **Changes go through a pull request for a human to review.** Never commit or
  push to `main`, never merge. A revert is a new PR too.
- Asked to push to main, merge, or sync? Decline once, briefly — it's policy —
  then offer the PR, or tell the human they can merge it themselves. Don't look
  for workarounds.
- Don't check whether image tags or registries exist.

The flow — details in [AGENTS.md](AGENTS.md#deploying-or-updating-an-app):

1. `./toolbox argocd app get <app> -o json` — `spec.sources` names the chart, the
   deployment repo (the source with `ref`; its 1-based position is `<n>`) and the
   value files (`$<ref>/…` are paths in that repo).
2. In the deployment repo clone: `git switch -c <app>/update-<env>-<slug>`, edit
   the values.
3. Optional quick check: render locally with `./toolbox helm template …` and
   compare to `./toolbox argocd app manifests <app>` with `dyff`.
4. Commit, push the branch, then have ArgoCD render it:
   `./toolbox argocd app manifests <app> --revisions <sha> --source-positions <n>`,
   compared with `dyff` to the current `argocd app manifests <app>`. Only your
   change should show. If anything fails, stop and report; don't open the PR.
5. `gh pr create` with the intent, the summary of what changed and the values
   diff — never rendered manifests (they can hold secrets). Give the human the
   link and stop: nothing deploys until they merge.
6. When they say it's merged, poll `./toolbox argocd app get <app> -o json` every
   10 seconds for up to 4 minutes until `.status.sync.revisions[n-1]` is the merge
   commit, then until it is `Synced` and `Healthy`. Not synced in 4 minutes:
   report the current revision and offer to keep watching. Degraded or failed:
   show the unhealthy resources and offer a revert PR.
