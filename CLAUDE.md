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

The flow, from inside the deployment repo clone — details in
[AGENTS.md](AGENTS.md#deploying-or-updating-an-app):

1. `../toolbox/toolbox toolbox-app <app>` (adjust the path to `toolbox`) — the
   value files in override order, the `file:line` that sets `image.tag`, and
   what is running.
2. Edit the values file it points at. Don't commit.
3. `./toolbox propose -m "<why>"` — branches, commits, pushes the branch, has
   ArgoCD render it for every app the change affects, and opens the PR. It
   refuses `main`, and opens nothing if ArgoCD renders no change or fails.
4. Give the human the PR link and stop: nothing deploys until they merge.
5. When they say it's merged:
   `./toolbox toolbox-watch <app> --rev <merge-sha>` (`gh pr view <n> --json mergeCommit`),
   with a command timeout of at least 8 minutes. It polls every 10 seconds, up to 4
   minutes for ArgoCD's automatic sync, then for health. Exit 3: not there yet —
   report it and offer to keep watching. Exit 2: failed — show what it printed and
   offer `./toolbox propose --revert <merge-sha>` (a PR, never merged by you).
