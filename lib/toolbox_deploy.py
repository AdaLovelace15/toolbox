"""Deploy/update helpers for GitOps apps: where an app's config lives, what a
pushed revision of the deployment repo would change, and whether ArgoCD's
automatic sync has rolled it out.

Read-only towards ArgoCD. Nothing here syncs, refreshes or waits on the
server's behalf - `argocd app wait` is avoided because it requests refreshes.
A change reaches the cluster only by a merged pull request, which ArgoCD picks
up on its own (by default within about three minutes).

Exit codes shared by the helpers:

    0  no change / healthy        1  change found
    2  error                      3  still waiting (watch only) - run again

Anything else a tool returns (argocd: 4 unauthenticated, 5 refused, 20 error)
is reported and mapped to 2, so a failure can never read as "change found".
"""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import yaml

EXIT_SAME, EXIT_CHANGED, EXIT_ERROR, EXIT_WAITING = 0, 1, 2, 3

# GitHub rejects PR bodies over 65536 characters.
MAX_BODY_DIFF = 40000

DIFF_DIR = os.path.join(tempfile.gettempdir(), "toolbox-deploy")


class Fail(Exception):
    """Report the message and exit 2."""


def log(msg):
    sys.stdout.flush()   # keep stdout and stderr in the order they were written
    print(f"toolbox: {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------- tools --
def _run(argv):
    # Git runs against a read-only mount: no index refresh, no lock files.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    return p.returncode, p.stdout, p.stderr


class Tools:
    """Every subprocess goes through here, so tests can substitute a fake."""

    def __init__(self, runner=_run):
        self.run = runner

    def argocd(self, *args):
        rc, out, err = self.run(["argocd", *args])
        if rc != 0:
            last = err.strip().splitlines()[-1] if err.strip() else f"exit {rc}"
            raise Fail(f"argocd {' '.join(args[:2])} failed: {last}")
        return out

    def app(self, name):
        return json.loads(self.argocd("app", "get", name, "-o", "json"))

    def apps(self):
        return json.loads(self.argocd("app", "list", "-o", "json") or "[]") or []

    def manifests(self, name, repo=None, rev=None):
        args = ["app", "manifests", name]
        if rev:
            if repo.position:
                args += ["--revisions", rev, "--source-positions", str(repo.position)]
            else:
                args += ["--revision", rev]
        return self.argocd(*args)

    def git(self, root, *args, ok=(0,)):
        rc, out, err = self.run(["git", "-c", "safe.directory=*", "-C", root, *args])
        if rc not in ok:
            raise Fail(f"git {args[0]} failed: {err.strip() or f'exit {rc}'}")
        return rc, out

    def dyff(self, old_path, new_path):
        return self.run(["dyff", "between", "--omit-header", old_path, new_path])


# ------------------------------------------------------------------ layout --
@dataclass
class RepoSource:
    position: int    # 1-based index into spec.sources; 0 for a single-source app
    url: str
    revision: str    # the branch/tag/sha the app tracks
    ref: str = ""    # name value files use as $ref; "" when not a ref source
    path: str = ""   # directory the app renders from, for a git path source


@dataclass
class ValueFile:
    raw: str
    repo: object     # RepoSource, or None when it isn't in a git source we know
    path: str        # repo-relative path, or the raw entry when repo is None


@dataclass
class Layout:
    name: str
    namespace: str
    chart: dict
    repos: list
    value_files: list = field(default_factory=list)


def app_name(app):
    meta = app.get("metadata") or {}
    ns, name = meta.get("namespace"), meta.get("name")
    return f"{ns}/{name}" if ns else name


def parse_layout(app):
    spec = app.get("spec") or {}
    multi = bool(spec.get("sources"))
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    repos, chart, chart_repo = [], None, None
    for i, s in enumerate(sources, 1):
        pos = i if multi else 0
        rev = s.get("targetRevision") or "HEAD"
        if s.get("ref"):
            repos.append(RepoSource(pos, s["repoURL"], rev, ref=s["ref"]))
        elif s.get("chart"):
            chart = chart or s
        elif s.get("path") is not None:
            r = RepoSource(pos, s["repoURL"], rev, path=s["path"].strip("/"))
            repos.append(r)
            if s.get("helm") and chart is None:
                chart, chart_repo = s, r
    layout = Layout(app_name(app), (spec.get("destination") or {}).get("namespace", ""),
                    chart or {}, repos)

    refs = {r.ref: r for r in repos if r.ref}
    for raw in ((chart or {}).get("helm") or {}).get("valueFiles") or []:
        m = re.match(r"^\$([^/]+)/(.+)$", raw)
        if m and m.group(1) in refs:
            layout.value_files.append(ValueFile(raw, refs[m.group(1)], m.group(2)))
        elif chart_repo is not None and not raw.startswith("$"):
            p = os.path.normpath(os.path.join(chart_repo.path, raw))
            layout.value_files.append(ValueFile(raw, chart_repo, p))
        else:
            layout.value_files.append(ValueFile(raw, None, raw))
    return layout


def normalize_url(url):
    u = url.strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", u)   # scp-style host:org/repo
    u = re.sub(r"/+$", "", u)
    return re.sub(r"\.git$", "", u)


def pick_repo(layout, origin=None):
    """The git source to test: the one matching the clone's origin, or the only one."""
    cands = layout.repos
    if origin:
        cands = [r for r in cands if normalize_url(r.url) == normalize_url(origin)]
    if len(cands) == 1:
        return cands[0]
    listing = ", ".join(f"position {r.position or 1}: {r.url}" for r in layout.repos) or "none"
    if not cands:
        raise Fail(f"{layout.name} has no git source matching {origin or 'this repo'} (sources: {listing})")
    raise Fail(f"{layout.name} has several git sources; run from the deployment repo clone "
               f"so its origin picks one (sources: {listing})")


def files_owned(layout, repo):
    """Repo-relative files and directories of `repo` that feed this app."""
    files = [v.path for v in layout.value_files if v.repo is repo]
    dirs = [repo.path] if repo.path and not repo.ref else []
    return files, dirs


def app_uses(layout, repo, changed):
    files, dirs = files_owned(layout, repo)
    for c in changed:
        if c in files or any(c == d or c.startswith(d + "/") for d in dirs):
            return True
    return False


APP_ENV = re.compile(r"^apps/([^/]+)/envs/([^/]+)/values\.ya?ml$")


def repo_app_env(path):
    m = APP_ENV.match(path)
    return (m.group(1), m.group(2)) if m else (None, None)


# ------------------------------------------------------------- yaml lookup --
def find_scalar(text, keys):
    """(line, value) of a nested key, e.g. ("image", "tag"); None if absent."""
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return None
    for k in keys:
        if not isinstance(node, yaml.MappingNode):
            return None
        for kn, vn in node.value:
            if getattr(kn, "value", None) == k:
                node = vn
                break
        else:
            return None
    if not isinstance(node, yaml.ScalarNode):
        return None
    return node.start_mark.line + 1, node.value


def image_tag_locations(root, layout):
    """Every value file in the clone that sets image.tag, in override order."""
    found = []
    for v in layout.value_files:
        if v.repo is None:
            continue
        p = os.path.join(root, v.path)
        try:
            with open(p, encoding="utf-8") as fh:
                hit = find_scalar(fh.read(), ("image", "tag"))
        except OSError:
            continue
        if hit:
            found.append((v.path, hit[0], hit[1]))
    return found


# --------------------------------------------------------------- manifests --
def load_docs(text):
    return [d for d in yaml.safe_load_all(text or "") if isinstance(d, dict)]


def res_key(d):
    meta = d.get("metadata") or {}
    ns = meta.get("namespace")
    return f"{d.get('kind')}/{ns + '/' if ns else ''}{meta.get('name')}"


def redact(docs):
    """Secrets never leave the toolbox, not even in a diff."""
    return [d for d in docs if d.get("kind") != "Secret"]


def _pod_spec(d):
    spec = d.get("spec") or {}
    if d.get("kind") == "Pod":
        return spec
    if d.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
    return ((spec.get("template") or {}).get("spec")) or {}


def images(docs):
    out = {}
    for d in docs:
        ps = _pod_spec(d)
        for c in (ps.get("initContainers") or []) + (ps.get("containers") or []):
            if c.get("image"):
                out[(res_key(d), c.get("name"))] = c["image"]
    return out


@dataclass
class Change:
    added: list
    removed: list
    changed: list
    images: list          # (resource, container, old, new)
    secrets_skipped: bool

    @property
    def any(self):
        return bool(self.added or self.removed or self.changed)


def compare(old_text, new_text):
    old_all, new_all = load_docs(old_text), load_docs(new_text)
    secrets = any(d.get("kind") == "Secret" for d in old_all + new_all)
    old = {res_key(d): d for d in redact(old_all)}
    new = {res_key(d): d for d in redact(new_all)}
    oi, ni = images(old.values()), images(new.values())
    imgs = sorted((r, c, oi.get((r, c)), ni.get((r, c)))
                  for (r, c) in set(oi) | set(ni) if oi.get((r, c)) != ni.get((r, c)))
    return Change(
        added=sorted(set(new) - set(old)),
        removed=sorted(set(old) - set(new)),
        changed=sorted(k for k in set(old) & set(new) if old[k] != new[k]),
        images=imgs,
        secrets_skipped=secrets,
    )


def write_dyff(tools, name, old_text, new_text):
    """Full resource-level diff, kept on disk only (it can be long)."""
    os.makedirs(DIFF_DIR, exist_ok=True)
    stem = os.path.join(DIFF_DIR, name.replace("/", "_"))
    paths = []
    for suffix, text in (("old", old_text), ("new", new_text)):
        p = f"{stem}.{suffix}.yaml"
        with open(p, "w", encoding="utf-8") as fh:
            yaml.safe_dump_all(redact(load_docs(text)), fh, sort_keys=False)
        paths.append(p)
    rc, out, _ = tools.dyff(*paths)
    if rc not in (0, 1):
        return None
    with open(f"{stem}.dyff", "w", encoding="utf-8") as fh:
        fh.write(out)
    return f"{stem}.dyff"


def summary_lines(name, ch, markdown=False):
    b = "`" if markdown else ""
    if not ch.any:
        return [f"{b}{name}{b}: no change"]
    n = len(ch.added) + len(ch.removed) + len(ch.changed)
    lines = [f"{b}{name}{b}: {n} resource{'s' if n != 1 else ''} changed"]
    pre = "- " if markdown else "  "
    for sym, keys in (("+", ch.added), ("-", ch.removed), ("~", ch.changed)):
        lines += [f"{pre}{sym} {b}{k}{b}" for k in keys]
    for r, c, o, n_ in ch.images:
        lines.append(f"{pre}image {b}{c}{b} in {b}{r}{b}: {b}{o or 'none'}{b} -> {b}{n_ or 'none'}{b}")
    if ch.secrets_skipped:
        lines.append(f"{pre}(Secrets are not compared)")
    return lines


# ------------------------------------------------------------------- git ----
def git_origin(tools, root):
    rc, out = tools.git(root, "config", "--get", "remote.origin.url", ok=(0, 1))
    return out.strip() if rc == 0 else None


def git_toplevel(tools, path):
    rc, out = tools.git(path, "rev-parse", "--show-toplevel", ok=(0, 128))
    return out.strip() if rc == 0 else None


def resolve_rev(tools, root, rev):
    for cand in (rev, f"origin/{rev}"):
        rc, out = tools.git(root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", ok=(0, 1, 128))
        if rc == 0:
            return out.strip()
    raise Fail(f"{rev} is not a commit or branch in {root}; push it and git fetch first")


def changed_files(tools, root, base, rev=None):
    """Files that differ from base: committed up to rev, or the working tree."""
    if rev:
        _, out = tools.git(root, "diff", "--name-only", f"{base}...{rev}")
        return sorted(set(out.split()))
    # Working tree against where the branch left base: commits plus edits, but
    # not whatever landed on base since.
    _, out = tools.git(root, "diff", "--name-only", "--merge-base", base)
    _, untracked = tools.git(root, "ls-files", "--others", "--exclude-standard")
    return sorted(set(out.split()) | set(untracked.split()))


def file_at(tools, root, rev, path):
    rc, out = tools.git(root, "show", f"{rev}:{path}", ok=(0, 128))
    return out if rc == 0 else None


def read_worktree(root, path):
    try:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


# -------------------------------------------------------------- toolbox-app --
def main_app(argv, tools=None):
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-app", description=(
        "Where an ArgoCD app's config lives: chart, deployment repo, value files "
        "in override order, which file sets image.tag, and what is running."))
    p.add_argument("app", help="ArgoCD application, e.g. glueops-core/backend-api-stage")
    p.add_argument("--repo-root", help="deployment repo clone (default: the git repo you are in)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        app = tools.app(a.app)
        lay = parse_layout(app)
        root = a.repo_root or git_toplevel(tools, os.getcwd())
        origin = git_origin(tools, root) if root else None
        if origin and not any(normalize_url(r.url) == normalize_url(origin) for r in lay.repos):
            root = None   # a clone of something else; don't read its files as this app's
        tags = image_tag_locations(root, lay) if root else []
        st = app.get("status") or {}
        env_of = next((repo_app_env(v.path) for v in lay.value_files
                       if v.repo and repo_app_env(v.path)[0]), (None, None))
        out = {
            "app": lay.name,
            "sync": (st.get("sync") or {}).get("status"),
            "health": (st.get("health") or {}).get("status"),
            "chart": {k: lay.chart.get(k) for k in ("repoURL", "chart", "targetRevision", "path") if lay.chart.get(k)},
            "repos": [{"position": r.position or 1, "url": r.url, "revision": r.revision,
                       **({"ref": r.ref} if r.ref else {}), **({"path": r.path} if r.path else {})}
                      for r in lay.repos],
            "value_files": [v.path if v.repo else v.raw for v in lay.value_files],
            "inline_values": bool((lay.chart.get("helm") or {}).get("values")
                                  or (lay.chart.get("helm") or {}).get("valuesObject")),
            "repo_app": env_of[0], "repo_env": env_of[1],
            "image_tag": [{"file": f, "line": ln, "value": v} for f, ln, v in tags],
            "images": (st.get("summary") or {}).get("images") or [],
            "repo_root": root,
        }
    except Fail as e:
        log(str(e))
        return EXIT_ERROR
    if a.json:
        print(json.dumps(out, indent=2))
        return EXIT_SAME
    print(f"{out['app']}  sync {out['sync']}  health {out['health']}")
    if out["chart"]:
        print("chart:       " + " ".join(f"{k}={v}" for k, v in out["chart"].items()))
    for r in out["repos"]:
        extra = f" ref={r['ref']}" if r.get("ref") else f" path={r.get('path', '')}"
        print(f"repo:        position {r['position']}: {r['url']} @ {r['revision']}{extra}")
    if out["repo_app"]:
        print(f"repo app:    {out['repo_app']}  env: {out['repo_env']}")
    print("value files (later override earlier):")
    for f in out["value_files"]:
        print(f"  {f}")
    if out["inline_values"]:
        print("  (+ inline values in the app spec, applied after the files)")
    if root:
        for t in out["image_tag"]:
            print(f"image.tag:   {t['file']}:{t['line']}  {t['value']}")
        if not out["image_tag"]:
            print("image.tag:   not set in any value file")
    else:
        print("image.tag:   (run from the deployment repo clone, or pass --repo-root, to locate it)")
    for i in out["images"]:
        print(f"running:     {i}")
    return EXIT_SAME


# -------------------------------------------------------- toolbox-preflight --
def preflight(tools, name, rev, root=None, diff_file=True):
    """Compare ArgoCD's render of `rev` with what it renders from the tracked branch."""
    app = tools.app(name)
    lay = parse_layout(app)
    origin = git_origin(tools, root) if root else None
    repo = pick_repo(lay, origin)
    if rev == repo.revision:
        raise Fail(f"{rev} is the branch {lay.name} already tracks; pass the pushed branch or commit to test")
    sha = resolve_rev(tools, root, rev) if root else rev
    desired = tools.manifests(lay.name)
    proposed = tools.manifests(lay.name, repo, sha)
    ch = compare(desired, proposed)
    path = write_dyff(tools, lay.name, desired, proposed) if (diff_file and ch.any) else None
    return lay, repo, sha, ch, path


def main_preflight(argv, tools=None):
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-preflight", description=(
        "Have ArgoCD render a pushed branch or commit of the deployment repo and "
        "compare it with what it renders today. Read-only. "
        "Exit 0 no change, 1 change, 2 error."))
    p.add_argument("app")
    p.add_argument("--rev", required=True, help="pushed branch or commit")
    p.add_argument("--repo-root", help="deployment repo clone (default: the git repo you are in)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        root = a.repo_root or git_toplevel(tools, os.getcwd())
        lay, repo, sha, ch, path = preflight(tools, a.app, a.rev, root)
    except Fail as e:
        log(str(e))
        return EXIT_ERROR
    code = EXIT_CHANGED if ch.any else EXIT_SAME
    if a.json:
        print(json.dumps({"app": lay.name, "rev": sha, "source_position": repo.position or 1,
                          "added": ch.added, "removed": ch.removed, "changed": ch.changed,
                          "images": [{"resource": r, "container": c, "old": o, "new": n}
                                     for r, c, o, n in ch.images],
                          "secrets_skipped": ch.secrets_skipped, "diff_file": path,
                          "exit": code}, indent=2))
        return code
    print(f"rendered by ArgoCD at {sha[:12]} (source position {repo.position or 1})")
    print("\n".join(summary_lines(lay.name, ch)))
    if path:
        print(f"full diff: {path}")
    if ch.any:
        log("next: open the PR (./toolbox propose -m '<intent>'); never paste rendered manifests into it")
    return code


# ------------------------------------------------------------ toolbox-watch --
def _rev_at(revs, i):
    """Per-source revisions for a multi-source app; a single string otherwise."""
    if not isinstance(revs, list):
        return revs
    return revs[i] if i < len(revs) else None


def watch_state(app, i, is_target):
    """('waiting'|'syncing'|'progressing'|'healthy'|'failed', current revision, detail)."""
    st = app.get("status") or {}
    sync = st.get("sync") or {}
    cur = _rev_at(sync.get("revisions") or sync.get("revision"), i)
    if not is_target(cur):
        return "waiting", cur, None
    ops = st.get("operationState") or {}
    res = ops.get("syncResult") or {}
    op_rev = _rev_at(res.get("revisions") or res.get("revision"), i)
    phase = ops.get("phase")
    if app.get("operation") or phase in ("Running", "Terminating"):
        return "syncing", cur, None
    if not is_target(op_rev):
        return "syncing", cur, None
    if phase in ("Failed", "Error"):
        return "failed", cur, ops.get("message") or phase
    health = (st.get("health") or {}).get("status")
    if health == "Degraded":
        return "failed", cur, "health Degraded"
    if sync.get("status") != "Synced":
        return "syncing", cur, None
    fresh = (st.get("reconciledAt") or "") >= (ops.get("finishedAt") or "")
    if health == "Healthy" and fresh:
        return "healthy", cur, None
    return "progressing", cur, None


def unhealthy(app):
    out = []
    for r in (app.get("status") or {}).get("resources") or []:
        h = (r.get("health") or {})
        if h and h.get("status") not in ("Healthy", None):
            out.append(f"  {r.get('kind')}/{r.get('name')}: {h.get('status')}"
                       + (f" - {h['message']}" if h.get("message") else ""))
    return out


def main_watch(argv, tools=None, clock=time.monotonic, sleep=time.sleep):
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-watch", description=(
        "After a merge, wait for ArgoCD's automatic sync to deploy the commit and "
        "report health. Polls `argocd app get` only: never syncs or refreshes. "
        "Exit 0 healthy, 2 failed or error, 3 not there yet (run again)."))
    p.add_argument("app")
    p.add_argument("--rev", required=True, help="the merge commit")
    p.add_argument("--repo-root", help="deployment repo clone (default: the git repo you are in)")
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--sync-timeout", type=float, default=240,
                   help="seconds to wait for ArgoCD to pick up the commit (default 240)")
    p.add_argument("--health-timeout", type=float, default=180,
                   help="seconds to wait for health once it has (default 180)")
    a = p.parse_args(argv)
    try:
        root = a.repo_root or git_toplevel(tools, os.getcwd())
        app = tools.app(a.app)
        lay = parse_layout(app)
        repo = pick_repo(lay, git_origin(tools, root) if root else None)
        sha = resolve_rev(tools, root, a.rev) if root else a.rev
        i = (repo.position or 1) - 1
        seen = {}

        def is_target(rev):
            if not rev:
                return False
            if rev == sha or (len(sha) >= 7 and rev.startswith(sha)):
                return True
            # A later commit (the bot pushes to main too) that contains ours counts.
            if root and rev not in seen:
                rc, _ = tools.git(root, "merge-base", "--is-ancestor", sha, rev, ok=(0, 1, 128))
                seen[rev] = rc == 0
            return seen.get(rev, False)

        start, synced_at = clock(), None
        print(f"watching {lay.name} for {sha[:12]} (source position {i + 1}); "
              f"ArgoCD polls git about every 3 minutes", flush=True)
        while True:
            state, cur, detail = watch_state(app, i, is_target)
            st = app.get("status") or {}
            now = clock()
            print(f"  {int(now - start):>4}s  rev {(cur or '-')[:12]}  "
                  f"{(st.get('sync') or {}).get('status', '-')}  "
                  f"{(st.get('health') or {}).get('status', '-')}  {state}", flush=True)
            if state == "healthy":
                running = (cur or "")[:12]
                note = "" if running.startswith(sha[:12]) else f" (a later commit that includes {sha[:12]})"
                print(f"{lay.name} is running {running}{note}: Synced and Healthy")
                return EXIT_SAME
            if state == "failed":
                print(f"{lay.name} failed after syncing {sha[:12]}: {detail}")
                print("\n".join(unhealthy(app)) or "  (no unhealthy resources reported)")
                rc, tree, _ = tools.run(["argocd", "app", "get", lay.name, "-o", "tree=detailed"])
                if rc == 0:
                    rows = [ln for ln in tree.splitlines()
                            if re.search(r"\b(Degraded|Progressing|Missing|Unknown|Suspended)\b", ln)]
                    if rows:
                        print("\n".join(rows))
                log(f"next: offer the human a revert PR: ./toolbox propose --revert {sha[:12]}")
                return EXIT_ERROR
            if state == "waiting":
                if now - start >= a.sync_timeout:
                    print(f"not synced after {int(now - start)}s: ArgoCD is still on "
                          f"{(cur or '-')[:12]} (last reconciled {st.get('reconciledAt', '-')})")
                    log("next: run the same command again to keep watching; never force a sync")
                    return EXIT_WAITING
            else:
                synced_at = synced_at if synced_at is not None else now
                if now - synced_at >= a.health_timeout:
                    print(f"{lay.name} synced {sha[:12]} but is still {state}")
                    print("\n".join(unhealthy(app)))
                    log("next: run the same command again to keep watching")
                    return EXIT_WAITING
            sleep(a.interval)
            app = tools.app(a.app)
    except Fail as e:
        log(str(e))
        return EXIT_ERROR


# ---------------------------------------------------------- toolbox-propose --
def slug(text, limit=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].rstrip("-") or "change"


def tag_bump(old_text, new_text):
    """The new tag, if the only difference between two values files is image.tag."""
    try:
        old, new = yaml.safe_load(old_text or ""), yaml.safe_load(new_text or "")
    except yaml.YAMLError:
        return None
    if not isinstance(old, dict) or not isinstance(new, dict):
        return None
    ot = (old.get("image") or {}).get("tag") if isinstance(old.get("image"), dict) else None
    nt = (new.get("image") or {}).get("tag") if isinstance(new.get("image"), dict) else None
    if nt is None or ot == nt:
        return None
    probe = copy.deepcopy(new)
    probe["image"]["tag"] = ot
    if ot is None:
        del probe["image"]["tag"]
    return str(nt) if probe == old else None


def describe(changed, read_old, read_new, intent):
    """Branch, title and deploy marker for a change, following the bump action's conventions."""
    for c in changed:
        if c.startswith("apps/") and "/envs/previews/" in c:
            raise Fail(f"{c} is a preview environment; those are managed by the app repo's pull requests")
    if len(changed) == 1:
        app, env = repo_app_env(changed[0])
        if app:
            tag = tag_bump(read_old(changed[0]), read_new(changed[0]))
            if tag:
                return {"branch": f"{app}/update-{env}-image-tag-{slug(tag, 60)}",
                        "title": f"chore(deploy): {app} [{env}] -> {tag}",
                        "marker": json.dumps({"app": app, "env": env, "tag": tag}, separators=(",", ":"))}
    apps = {m.group(1) for c in changed for m in [re.match(r"^apps/([^/]+)/", c)] if m}
    envs = {m.group(2) for c in changed for m in [re.match(r"^apps/([^/]+)/envs/([^/]+)/", c)] if m}
    summary = (intent or "update config").strip().splitlines()[0][:72]
    if len(apps) == 1 and len(envs) == 1 and all(c.startswith("apps/") for c in changed):
        app, env = next(iter(apps)), next(iter(envs))
        return {"branch": f"{app}/update-{env}-{slug(intent)}",
                "title": f"chore(deploy): {app} [{env}] {summary}", "marker": ""}
    scope = next(iter(apps)) if len(apps) == 1 else "deploy"
    return {"branch": f"{scope}/update-{slug(intent)}",
            "title": f"chore(deploy): {summary}", "marker": ""}


def affected(tools, root, base_branch, changed):
    """ArgoCD apps that read any changed file from this repo's tracked branch."""
    origin = git_origin(tools, root)
    if not origin:
        raise Fail(f"{root} has no origin remote")
    hits = []
    for app in tools.apps():
        lay = parse_layout(app)
        for r in lay.repos:
            if (normalize_url(r.url) == normalize_url(origin) and r.revision == base_branch
                    and app_uses(lay, r, changed)):
                hits.append(lay.name)
                break
    return sorted(hits)


def main_propose(argv, tools=None):
    """Container half of `./toolbox propose`; git and gh run on the host."""
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-propose")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="branch, title and affected apps for the working tree")
    bd = sub.add_parser("body", help="run preflight for every affected app and print the PR body")
    for s in (pl, bd):
        s.add_argument("--repo-root", required=True)
        s.add_argument("--base", required=True, help="e.g. origin/main")
        s.add_argument("-m", "--message", default="")
    pl.add_argument("--branch", help="the branch currently checked out")
    bd.add_argument("--rev", required=True)
    bd.add_argument("--revert-of")
    a = p.parse_args(argv)
    base_branch = a.base.split("/", 1)[1] if a.base.startswith("origin/") else a.base
    try:
        if a.cmd == "plan":
            changed = changed_files(tools, a.repo_root, a.base)
            if not changed:
                raise Fail(f"nothing to propose: no changes against {a.base}")
            d = describe(changed, lambda f: file_at(tools, a.repo_root, a.base, f),
                         lambda f: read_worktree(a.repo_root, f), a.message)
            apps = affected(tools, a.repo_root, base_branch, changed)
            if not apps:
                raise Fail("no ArgoCD app reads these files from " + base_branch +
                           " (a brand-new app?) - ask the human how they want it proposed")
            if a.branch and a.branch != base_branch:
                d["branch"] = a.branch
            print(f"branch={d['branch']}")
            print(f"title={d['title']}")
            print(f"apps={' '.join(apps)}")
            print(f"files={' '.join(changed)}")
            return EXIT_SAME

        changed = changed_files(tools, a.repo_root, a.base, a.rev)
        if not changed:
            raise Fail(f"{a.rev[:12]} changes nothing against {a.base}")
        apps = affected(tools, a.repo_root, base_branch, changed)
        if not apps:
            raise Fail("no ArgoCD app reads these files from " + base_branch)
        sections, any_change = [], False
        for name in apps:
            lay, repo, sha, ch, path = preflight(tools, name, a.rev, a.repo_root)
            any_change = any_change or ch.any
            sections.append("\n".join(summary_lines(lay.name, ch, markdown=True)))
            log("\n".join(summary_lines(lay.name, ch)) + (f"\n  full diff: {path}" if path else ""))
        if not any_change:
            log("ArgoCD renders no change for any affected app; nothing to propose")
            return EXIT_SAME
        d = describe(changed, lambda f: file_at(tools, a.repo_root, a.base, f),
                     lambda f: file_at(tools, a.repo_root, a.rev, f), a.message)
        _, diff = tools.git(a.repo_root, "diff", f"{a.base}...{a.rev}")
        if len(diff) > MAX_BODY_DIFF:
            diff = diff[:MAX_BODY_DIFF] + "\n... (truncated; see the Files tab)\n"
        intro = a.message.strip() or ("Reverts " + a.revert_of if a.revert_of else "")
        body = [intro, "", "### What changes",
                f"Rendered by ArgoCD from `{a.rev[:12]}` (`toolbox-preflight`), compared with what "
                f"it renders from `{base_branch}` today. Rendered manifests are deliberately not "
                "included.", "", *sections, "", "### Values diff", "```diff", diff.rstrip(), "```",
                "", "---",
                f"Opened with `./toolbox propose`. Nothing deploys until this is merged; ArgoCD "
                f"then syncs `{base_branch}` automatically."]
        if d["marker"] and not a.revert_of:
            body += ["", f"<!-- glueops-deploy:{d['marker']} -->"]
        print("\n".join(body))
        return EXIT_CHANGED
    except Fail as e:
        log(str(e))
        return EXIT_ERROR
