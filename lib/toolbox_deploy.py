"""Deploy/update helpers for GitOps apps: where an app's config lives, what a
pushed revision of the deployment repo would change, and whether ArgoCD's
automatic sync has rolled it out.

Read-only towards ArgoCD. Nothing here syncs, refreshes or waits on the
server's behalf - `argocd app wait` is avoided because it requests refreshes.
A change reaches the cluster only by a merged pull request, which ArgoCD picks
up on its own (by default within about three minutes).

Exit codes:

    0  no change / healthy
    1  change found                      (preflight, propose body)
    2  error - the tool failed, nothing is known about the deploy
    3  not there yet, run again          (watch)
    4  deployed, and it failed           (watch)

Any failure - a tool's non-zero exit (argocd: 4 unauthenticated, 5 refused,
20 error), bad output, or a bug here - is reported and becomes 2, so it can
never read as "change found" or "deploy failed".
"""

import argparse
import copy
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import yaml

EXIT_SAME, EXIT_CHANGED, EXIT_ERROR, EXIT_WAITING, EXIT_FAILED = 0, 1, 2, 3, 4

# GitHub rejects PR bodies over 65536 characters.
MAX_BODY_DIFF = 40000

DIFF_DIR = os.path.join(tempfile.gettempdir(), "toolbox-deploy")

# Keys whose values never go into a PR body, even from the values diff.
SECRETISH = re.compile(r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key|credential|auth|dsn)")


class Fail(Exception):
    """Report the message and exit 2."""


def log(msg):
    sys.stdout.flush()   # keep stdout and stderr in the order they were written
    print(f"toolbox: {msg}", file=sys.stderr, flush=True)


def guarded(fn):
    """Every entry point: Fail and anything unexpected exit 2, never 1."""
    @functools.wraps(fn)
    def wrapper(*args, **kw):
        try:
            return fn(*args, **kw)
        except Fail as e:
            log(str(e))
        except Exception as e:  # noqa: BLE001 - a bug must not read as "change found"
            log(f"internal error: {e!r}")
        return EXIT_ERROR
    return wrapper


# ------------------------------------------------------------------- tools --
def _run(argv):
    # Git runs against a read-only mount: no index refresh, no lock files.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", env=env)
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
            raise Fail(f"argocd {' '.join(args[:3])} failed: {last}")
        return out

    def _json(self, *args):
        out = self.argocd(*args)
        try:
            return json.loads(out or "null")
        except ValueError as e:
            raise Fail(f"argocd {' '.join(args[:3])} returned no JSON: {e}")

    def app(self, name):
        a = self._json("app", "get", name, "-o", "json")
        if not isinstance(a, dict):
            raise Fail(f"argocd app get {name} returned nothing")
        return a

    def apps(self):
        return self._json("app", "list", "-o", "json") or []

    def manifests(self, name, repos=None, rev=None):
        args = ["app", "manifests", name]
        if rev:
            if repos and repos[0].position:
                for r in repos:
                    args += ["--revisions", rev, "--source-positions", str(r.position)]
            else:
                args += ["--revision", rev]
        return self.argocd(*args)

    def git(self, root, *args, ok=(0,)):
        rc, out, err = self.run(["git", "-c", "safe.directory=*", "-C", root, *args])
        if rc not in ok:
            raise Fail(f"git {args[0]} failed: {err.strip() or f'exit {rc}'}")
        return rc, out

    def dyff(self, old_path, new_path):
        return self.run(["dyff", "between", "--omit-header", "--ignore-order-changes",
                         old_path, new_path])


# ------------------------------------------------------------------ layout --
@dataclass
class RepoSource:
    position: int    # 1-based index into spec.sources; 0 for a single-source app
    url: str
    revision: str    # the branch/tag/sha the app tracks
    ref: str = ""    # name value files use as $ref; "" when not a ref source
    path: str = ""   # directory the source renders from, if any


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


def _repo_path(p):
    """Normalise a repo-relative path; None if it climbs out of the repo."""
    p = os.path.normpath(p).lstrip("/")
    return None if p == ".." or p.startswith("../") else ("" if p == "." else p)


def parse_layout(app):
    spec = app.get("spec") or {}
    multi = bool(spec.get("sources"))
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    repos, chart, chart_repo = [], None, None
    for i, s in enumerate(sources, 1):
        pos = i if multi else 0
        url = s.get("repoURL") or ""
        rev = s.get("targetRevision") or "HEAD"
        if s.get("chart"):
            chart = chart or s
            continue
        path = _repo_path(s.get("path") or ".") or ""
        if s.get("ref"):
            repos.append(RepoSource(pos, url, rev, ref=s["ref"], path=path if s.get("path") else ""))
        elif s.get("path") is not None:
            r = RepoSource(pos, url, rev, path=path)
            repos.append(r)
            if s.get("helm") and chart is None:
                chart, chart_repo = s, r
    layout = Layout(app_name(app), (spec.get("destination") or {}).get("namespace", ""),
                    chart or {}, repos)

    refs = {r.ref: r for r in repos if r.ref}
    for raw in ((chart or {}).get("helm") or {}).get("valueFiles") or []:
        m = re.match(r"^\$([^/]+)/(.+)$", raw)
        if m and m.group(1) in refs:
            p = _repo_path(m.group(2))
            layout.value_files.append(ValueFile(raw, refs[m.group(1)] if p else None, p or raw))
        elif chart_repo is not None and not raw.startswith("$"):
            p = _repo_path(os.path.join(chart_repo.path, raw))
            layout.value_files.append(ValueFile(raw, chart_repo if p else None, p or raw))
        else:
            layout.value_files.append(ValueFile(raw, None, raw))
    return layout


def normalize_url(url):
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = re.sub(r"^([^/:]+):(?:22|80|443)(/|$)", r"\1\2", u)   # default ports
    u = re.sub(r"^([^/:]+):(?!\d+(/|$))", r"\1/", u)          # scp-style host:org/repo
    u = re.sub(r"/+$", "", u)
    return re.sub(r"\.git$", "", u)


def tracks(revision, branch):
    """Does an app tracking `revision` follow `branch` (the repo's default)?"""
    return revision in (branch, f"refs/heads/{branch}", "HEAD", "")


def pick_repos(layout, origin=None):
    """The git sources to test: those from the clone's origin, or from the only repo."""
    if origin:
        cands = [r for r in layout.repos if normalize_url(r.url) == normalize_url(origin)]
    else:
        urls = {normalize_url(r.url) for r in layout.repos}
        cands = layout.repos if len(urls) == 1 else []
    if cands:
        return cands
    listing = ", ".join(f"position {r.position or 1}: {r.url}" for r in layout.repos) or "none"
    if origin:
        raise Fail(f"{layout.name} reads nothing from {origin} (its sources: {listing}); "
                   "run from the deployment repo clone or pass --repo-root")
    if not layout.repos:
        raise Fail(f"{layout.name} has no git source to test")
    raise Fail(f"{layout.name} reads from several repos ({listing}); run from the deployment "
               "repo clone or pass --repo-root so its origin picks one")


def files_owned(layout, repo):
    """Repo-relative files and directories of `repo` that feed this app."""
    files = [v.path for v in layout.value_files if v.repo is repo]
    dirs = [repo.path] if repo.path else []
    return files, dirs


def uses_file(layout, repo, path):
    files, dirs = files_owned(layout, repo)
    return path in files or any(path == d or path.startswith(d + "/") for d in dirs)


def app_uses(layout, repo, changed):
    return any(uses_file(layout, repo, c) for c in changed)


APP_ENV = re.compile(r"^apps/([^/]+)/envs/([^/]+)/values\.ya?ml$")


def repo_app_env(path):
    m = APP_ENV.match(path or "")
    return (m.group(1), m.group(2)) if m else (None, None)


# ------------------------------------------------------------- yaml lookup --
def find_scalar(text, keys):
    """(line, raw text) of a nested scalar, e.g. ("image", "tag"); None if absent.

    Raw text, so `tag: 1.10` stays "1.10" rather than becoming the float 1.1."""
    try:
        node = yaml.compose(text or "")
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


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def image_tag_locations(root, layout):
    """Every value file in the clone that sets image.tag, in override order."""
    found = []
    for v in layout.value_files:
        if v.repo is None:
            continue
        hit = find_scalar(read_text(os.path.join(root, v.path)), ("image", "tag"))
        if hit:
            found.append((v.path, hit[0], hit[1]))
    return found


def spec_image_tag(layout):
    """image.tag set in the app spec itself, which overrides every value file."""
    helm = layout.chart.get("helm") or {}
    for p in helm.get("parameters") or []:
        if p.get("name") == "image.tag":
            return "helm.parameters", str(p.get("value"))
    vo = helm.get("valuesObject")
    if isinstance(vo, dict) and isinstance(vo.get("image"), dict) and "tag" in vo["image"]:
        return "helm.valuesObject", str(vo["image"]["tag"])
    hit = find_scalar(helm.get("values") or "", ("image", "tag"))
    return ("helm.values", hit[1]) if hit else None


# --------------------------------------------------------------- manifests --
def load_docs(text):
    docs = []
    for d in yaml.safe_load_all(text or ""):
        if isinstance(d, dict) and d.get("kind") == "List" and isinstance(d.get("items"), list):
            docs += [i for i in d["items"] if isinstance(i, dict)]
        elif isinstance(d, dict):
            docs.append(d)
    return docs


def res_key(d):
    meta = d.get("metadata") or {}
    group = d.get("apiVersion", "").rpartition("/")[0]
    kind = f"{d.get('kind')}.{group}" if group else str(d.get("kind"))
    ns = meta.get("namespace")
    return f"{kind}/{ns + '/' if ns else ''}{meta.get('name')}"


def is_secret(d):
    return d.get("kind") == "Secret"


def redact(docs):
    """Secrets never leave the toolbox, not even in a diff."""
    return [d for d in docs if not is_secret(d)]


def _secret_digest(d):
    body = {k: d.get(k) for k in ("type", "data", "stringData", "immutable")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


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
            if isinstance(c, dict) and c.get("image"):
                out[(res_key(d), c.get("name"))] = c["image"]
    return out


def diff_paths(a, b, prefix="", out=None, limit=8):
    """Paths of the fields that differ - names only, never values."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if a.get(k) != b.get(k):
                diff_paths(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else str(k), out, limit)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                diff_paths(x, y, f"{prefix}[{i}]", out, limit)
    elif a != b:
        out.append(prefix or "(whole resource)")
    return out


@dataclass
class Change:
    added: list
    removed: list
    changed: list
    paths: dict           # changed resource -> differing field paths (no values)
    images: list          # (resource, container, old, new)
    secrets: list         # (symbol, resource) - contents never shown

    @property
    def any(self):
        return bool(self.added or self.removed or self.changed or self.secrets)


def compare(old_text, new_text):
    old_all, new_all = load_docs(old_text), load_docs(new_text)
    old = {res_key(d): d for d in redact(old_all)}
    new = {res_key(d): d for d in redact(new_all)}
    so = {res_key(d): _secret_digest(d) for d in old_all if is_secret(d)}
    sn = {res_key(d): _secret_digest(d) for d in new_all if is_secret(d)}
    secrets = ([("+", k) for k in sorted(set(sn) - set(so))]
               + [("-", k) for k in sorted(set(so) - set(sn))]
               + [("~", k) for k in sorted(set(so) & set(sn)) if so[k] != sn[k]])
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    oi, ni = images(old.values()), images(new.values())
    imgs = sorted((r, c, oi.get((r, c)), ni.get((r, c)))
                  for (r, c) in set(oi) | set(ni) if oi.get((r, c)) != ni.get((r, c)))
    return Change(
        added=sorted(set(new) - set(old)),
        removed=sorted(set(old) - set(new)),
        changed=changed,
        paths={k: diff_paths(old[k], new[k]) for k in changed},
        images=imgs,
        secrets=secrets,
    )


def write_dyff(tools, name, old_text, new_text):
    """Full resource-level diff, kept in the container only. Secrets excluded."""
    try:
        os.makedirs(DIFF_DIR, exist_ok=True)
        work = tempfile.mkdtemp(dir=DIFF_DIR)
        try:
            paths = []
            for suffix, text in (("old", old_text), ("new", new_text)):
                p = os.path.join(work, f"{suffix}.yaml")
                with open(p, "w", encoding="utf-8") as fh:
                    yaml.safe_dump_all(redact(load_docs(text)), fh, sort_keys=False)
                paths.append(p)
            rc, out, _ = tools.dyff(*paths)
        finally:
            shutil.rmtree(work, ignore_errors=True)   # rendered values stay on disk no longer
        if rc not in (0, 1):
            return None
        dest = os.path.join(DIFF_DIR, name.replace("/", "_") + ".dyff")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(out)
        return dest
    except OSError:
        return None


def summary_lines(name, ch, markdown=False):
    b = "`" if markdown else ""
    if not ch.any:
        return [f"{b}{name}{b}: no change"]
    n = len(ch.added) + len(ch.removed) + len(ch.changed) + len(ch.secrets)
    lines = [f"{b}{name}{b}: {n} resource{'s' if n != 1 else ''} changed"]
    pre = "- " if markdown else "  "
    for sym, keys in (("+", ch.added), ("-", ch.removed)):
        lines += [f"{pre}{sym} {b}{k}{b}" for k in keys]
    for k in ch.changed:
        fields = ", ".join(ch.paths.get(k) or [])
        lines.append(f"{pre}~ {b}{k}{b}" + (f" ({fields})" if fields else ""))
    for sym, k in ch.secrets:
        lines.append(f"{pre}{sym} {b}{k}{b} (contents not shown)")
    for r, c, o, n_ in ch.images:
        lines.append(f"{pre}image {b}{c}{b} in {b}{r}{b}: {b}{o or 'none'}{b} -> {b}{n_ or 'none'}{b}")
    return lines


# ------------------------------------------------------------------- git ----
def git_origin(tools, root):
    rc, out = tools.git(root, "config", "--get", "remote.origin.url", ok=(0, 1))
    return out.strip() if rc == 0 else None


def git_toplevel(tools, path):
    rc, out = tools.git(path, "rev-parse", "--show-toplevel", ok=(0, 128))
    return out.strip() if rc == 0 else None


def usable_root(tools, layout, root):
    """(root, origin) when `root` is a clone of one of the app's repos, else (None, None)."""
    if not root:
        return None, None
    origin = git_origin(tools, root)
    if origin and any(normalize_url(r.url) == normalize_url(origin) for r in layout.repos):
        return root, origin
    log(f"{root} is a clone of {origin or 'no remote'}, not of a repo {layout.name} reads; "
        "ignoring it (cd into the deployment repo clone, or pass --repo-root)")
    return None, None


def resolve_rev(tools, root, rev):
    for cand in (rev, f"origin/{rev}"):
        rc, out = tools.git(root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", ok=(0, 1, 128))
        if rc == 0:
            return out.strip()
    raise Fail(f"{rev} is not a commit or branch in {root}; git fetch on the host and run again")


def merge_base(tools, root, a, b):
    rc, out = tools.git(root, "merge-base", a, b, ok=(0, 1, 128))
    return out.strip() if rc == 0 else None


def _paths(out):
    return sorted({p for p in out.split("\0") if p})


def changed_files(tools, root, base, rev=None):
    """Files that differ from base: committed up to rev, or the working tree."""
    if rev:
        _, out = tools.git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}...{rev}")
        return _paths(out)
    # Working tree against where the branch left base: commits plus edits, but
    # not whatever landed on base since.
    _, out = tools.git(root, "diff", "--name-only", "--no-renames", "-z", "--merge-base", base)
    _, untracked = tools.git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(_paths(out)) | set(_paths(untracked)))


def file_at(tools, root, rev, path):
    rc, out = tools.git(root, "show", f"{rev}:{path}", ok=(0, 128))
    return out if rc == 0 else None


# -------------------------------------------------------------- toolbox-app --
@guarded
def main_app(argv, tools=None):
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-app", description=(
        "Where an ArgoCD app's config lives: chart, deployment repo, value files "
        "in override order, which file sets image.tag, and what is running."))
    p.add_argument("app", help="ArgoCD application, e.g. glueops-core/backend-api-stage")
    p.add_argument("--repo-root", help="deployment repo clone (default: the git repo you are in)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    app = tools.app(a.app)
    lay = parse_layout(app)
    root, _ = usable_root(tools, lay, a.repo_root or git_toplevel(tools, os.getcwd()))
    tags = image_tag_locations(root, lay) if root else []
    in_spec = spec_image_tag(lay)
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
        "image_tag": [{"file": f, "line": ln, "value": v, "effective": i == len(tags) - 1 and not in_spec}
                      for i, (f, ln, v) in enumerate(tags)],
        "image_tag_in_spec": {"where": in_spec[0], "value": in_spec[1]} if in_spec else None,
        "images": (st.get("summary") or {}).get("images") or [],
        "repo_root": root,
    }
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
            print(f"image.tag:   {t['file']}:{t['line']}  {t['value']}"
                  + ("   <- effective" if t["effective"] else ""))
        if not out["image_tag"]:
            print("image.tag:   not set in any value file")
    else:
        print("image.tag:   (run from the deployment repo clone, or pass --repo-root, to locate it)")
    if in_spec:
        print(f"image.tag:   {in_spec[1]} in the app spec ({in_spec[0]}) - overrides the value files; "
              "it is not in the deployment repo")
    for i in out["images"]:
        print(f"running:     {i}")
    return EXIT_SAME


# -------------------------------------------------------- toolbox-preflight --
@dataclass
class Preflight:
    layout: Layout
    repos: list
    sha: str
    baseline: str      # what the change is compared against, for humans
    change: Change
    diff_file: str


def preflight(tools, name, rev, root=None, diff_file=True):
    """Compare ArgoCD's render of `rev` with its render of where `rev` left the tracked branch."""
    app = tools.app(name)
    lay = parse_layout(app)
    root, origin = usable_root(tools, lay, root)
    repos = pick_repos(lay, origin)
    tracked = repos[0].revision
    if rev in (tracked, f"origin/{tracked}") or (tracked == "HEAD" and rev in ("main", "master")):
        raise Fail(f"{rev} is the branch {lay.name} already tracks; pass the pushed branch or commit to test")
    old_text, baseline = None, f"{tracked} today"
    sha = rev
    if root:
        sha = resolve_rev(tools, root, rev)
        mb = merge_base(tools, root, f"origin/{tracked}" if tracked != "HEAD" else "origin/HEAD", sha)
        if mb and mb != sha:
            # Compare with where the branch left the tracked branch, so whatever
            # merged since doesn't show up reversed.
            old_text = tools.manifests(lay.name, repos, mb)
            baseline = f"{mb[:12]}, where it left {tracked}"
    if old_text is None:
        old_text = tools.manifests(lay.name)
    new_text = tools.manifests(lay.name, repos, sha)
    ch = compare(old_text, new_text)
    path = write_dyff(tools, lay.name, old_text, new_text) if (diff_file and ch.any) else None
    return Preflight(lay, repos, sha, baseline, ch, path)


@guarded
def main_preflight(argv, tools=None):
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-preflight", description=(
        "Have ArgoCD render a pushed branch or commit of the deployment repo and "
        "compare it with its render of where that branch started. Read-only. "
        "Exit 0 no change, 1 change, 2 error."))
    p.add_argument("app")
    p.add_argument("--rev", required=True, help="pushed branch or commit")
    p.add_argument("--repo-root", help="deployment repo clone (default: the git repo you are in)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    pf = preflight(tools, a.app, a.rev, a.repo_root or git_toplevel(tools, os.getcwd()))
    ch = pf.change
    code = EXIT_CHANGED if ch.any else EXIT_SAME
    positions = [r.position or 1 for r in pf.repos]
    if a.json:
        print(json.dumps({"app": pf.layout.name, "rev": pf.sha, "baseline": pf.baseline,
                          "source_positions": positions,
                          "added": ch.added, "removed": ch.removed, "changed": ch.changed,
                          "changed_fields": ch.paths,
                          "secrets": [{"change": s, "resource": k} for s, k in ch.secrets],
                          "images": [{"resource": r, "container": c, "old": o, "new": n}
                                     for r, c, o, n in ch.images],
                          "diff_file": pf.diff_file, "exit": code}, indent=2))
        return code
    print(f"rendered by ArgoCD at {pf.sha[:12]} (source position {', '.join(map(str, positions))}), "
          f"compared with {pf.baseline}")
    print("\n".join(summary_lines(pf.layout.name, ch)))
    if pf.diff_file:
        print(f"full diff (Secrets excluded): ./toolbox cat {pf.diff_file}")
    if ch.any:
        log("next: open the PR with ./toolbox propose; never paste rendered manifests into it")
    return code


# ------------------------------------------------------------ toolbox-watch --
def _rev_at(revs, i):
    """Per-source revisions for a multi-source app; a single string otherwise."""
    if not isinstance(revs, list):
        return revs
    return revs[i] if 0 <= i < len(revs) else None


def watch_state(app, i, is_target):
    """('waiting'|'syncing'|'progressing'|'healthy'|'failed', current revision, detail)."""
    st = app.get("status") or {}
    sync = st.get("sync") or {}
    cur = _rev_at(sync.get("revisions") or sync.get("revision"), i)
    if not is_target(cur):
        return "waiting", cur, None
    ops = st.get("operationState") or {}
    phase = ops.get("phase")
    if app.get("operation") or phase in ("Running", "Terminating"):
        return "syncing", cur, None
    res = ops.get("syncResult") or {}
    op_sync = ((ops.get("operation") or {}).get("sync") or {})
    op_rev = _rev_at(res.get("revisions") or res.get("revision")
                     or op_sync.get("revisions") or op_sync.get("revision"), i)
    if is_target(op_rev) and phase in ("Failed", "Error"):
        return "failed", cur, ops.get("message") or f"sync {phase}"
    # When op_rev is older, either the automatic sync hasn't run yet (OutOfSync)
    # or the commit changed nothing for this app, so there was nothing to sync.
    if sync.get("status") != "Synced":
        return "syncing", cur, None
    health = (st.get("health") or {}).get("status")
    # Health computed before the last sync is stale: don't call it either way.
    fresh = (st.get("reconciledAt") or "") >= (ops.get("finishedAt") or "")
    if not fresh:
        return "progressing", cur, None
    if health == "Degraded":
        return "failed", cur, "health Degraded"
    if health == "Healthy":
        return "healthy", cur, None
    return "progressing", cur, None


def unhealthy(app):
    out = []
    for r in (app.get("status") or {}).get("resources") or []:
        h = r.get("health") or {}
        if h and h.get("status") not in ("Healthy", None):
            out.append(f"  {r.get('kind')}/{r.get('name')}: {h.get('status')}"
                       + (f" - {h['message']}" if h.get("message") else ""))
    return out


@guarded
def main_watch(argv, tools=None, clock=time.monotonic, sleep=time.sleep):
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-watch", description=(
        "After a merge, wait for ArgoCD's automatic sync to deploy the commit and "
        "report health. Polls `argocd app get` only: never syncs or refreshes. "
        "Exit 0 healthy, 2 tool error, 3 not there yet (run again), 4 deployed and failed."))
    p.add_argument("app")
    p.add_argument("--rev", required=True, help="the merge commit")
    p.add_argument("--repo-root", help="deployment repo clone (default: the git repo you are in)")
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--sync-timeout", type=float, default=240,
                   help="seconds to wait for ArgoCD to pick up the commit (default 240)")
    p.add_argument("--health-timeout", type=float, default=180,
                   help="seconds to wait for health once it has (default 180)")
    a = p.parse_args(argv)
    app = tools.app(a.app)
    lay = parse_layout(app)
    root, origin = usable_root(tools, lay, a.repo_root or git_toplevel(tools, os.getcwd()))
    repos = pick_repos(lay, origin)
    sha = resolve_rev(tools, root, a.rev) if root else a.rev
    i = (repos[0].position or 1) - 1
    seen, unknown = {}, {}

    def is_target(rev):
        if not rev:
            return False
        if rev == sha or (len(sha) >= 7 and rev.startswith(sha)):
            return True
        # A later commit (the bot pushes to main too) that contains ours counts.
        if root and rev not in seen:
            rc, _ = tools.git(root, "merge-base", "--is-ancestor", sha, rev, ok=(0, 1, 128))
            seen[rev] = rc == 0
            if rc == 128:
                unknown[rev] = True
        return seen.get(rev, False)

    start, synced_at, errors = clock(), None, 0
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
            log(f"next: show the human the above and offer a revert PR: ./toolbox propose --revert {sha[:12]}")
            return EXIT_FAILED
        if state == "waiting":
            if cur in unknown:
                print(f"ArgoCD is on {cur[:12]}, which this clone doesn't have, so it can't tell "
                      f"whether that includes {sha[:12]}")
                log("next: git fetch on the host, then run the same command again")
                return EXIT_WAITING
            if now - start >= a.sync_timeout:
                print(f"not synced after {int(now - start)}s: ArgoCD is still on "
                      f"{(cur or '-')[:12]} (last reconciled {st.get('reconciledAt', '-')})")
                log("next: run the same command again to keep watching; never force a sync")
                return EXIT_WAITING
        else:
            synced_at = synced_at if synced_at is not None else now
            if now - synced_at >= a.health_timeout:
                print(f"{lay.name} has picked up {sha[:12]} but is not healthy yet ({state})")
                print("\n".join(unhealthy(app)))
                log("next: run the same command again to keep watching")
                return EXIT_WAITING
        sleep(a.interval)
        try:
            app = tools.app(a.app)
            errors = 0
        except Fail:
            errors += 1   # a blip is not a verdict; three in a row is
            if errors >= 3:
                raise


# ---------------------------------------------------------- toolbox-propose --
def ref_safe(text, limit=60):
    """Usable inside a git branch name, keeping dots like the deploy bot does."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "")
    s = re.sub(r"\.{2,}", "-", s).strip("-.")
    s = re.sub(r"\.lock$", "", s)[:limit].strip("-.")
    return s or "change"


def slug(text, limit=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].rstrip("-") or "change"


def one_line(text, limit=72):
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return re.sub(r"[\x00-\x1f\x7f]", "", first)[:limit]


def tag_bump(old_text, new_text):
    """The new tag, if the only difference between two values files is image.tag."""
    try:
        old, new = yaml.safe_load(old_text or ""), yaml.safe_load(new_text or "")
    except yaml.YAMLError:
        return None
    if not isinstance(old, dict) or not isinstance(new, dict) or not isinstance(new.get("image"), dict):
        return None
    ot, nt = find_scalar(old_text, ("image", "tag")), find_scalar(new_text, ("image", "tag"))
    if nt is None or (ot and ot[1] == nt[1]) or re.search(r"[\x00-\x1f\x7f]", nt[1]):
        return None
    probe = copy.deepcopy(new)
    if ot is None:
        del probe["image"]["tag"]
    elif isinstance(old.get("image"), dict):
        probe["image"]["tag"] = old["image"].get("tag")
    return nt[1] if probe == old else None


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
                return {"branch": f"{ref_safe(app)}/update-{ref_safe(env)}-image-tag-{ref_safe(tag)}",
                        "title": f"chore(deploy): {app} [{env}] -> {tag}",
                        "marker": json.dumps({"app": app, "env": env, "tag": tag}, separators=(",", ":")),
                        "marker_app": app, "marker_env": env}
    apps = {m.group(1) for c in changed for m in [re.match(r"^apps/([^/]+)/", c)] if m}
    envs = {m.group(2) for c in changed for m in [re.match(r"^apps/([^/]+)/envs/([^/]+)/", c)] if m}
    summary = one_line(intent) or "update config"
    if len(apps) == 1 and len(envs) == 1 and all(c.startswith("apps/") for c in changed):
        app, env = next(iter(apps)), next(iter(envs))
        return {"branch": f"{ref_safe(app)}/update-{ref_safe(env)}-{slug(intent)}",
                "title": f"chore(deploy): {app} [{env}] {summary}", "marker": ""}
    scope = ref_safe(next(iter(apps))) if len(apps) == 1 else "deploy"
    return {"branch": f"{scope}/update-{slug(intent)}",
            "title": f"chore(deploy): {summary}", "marker": ""}


def scan(tools, root, default_branch, changed):
    """Affected apps, the files they read, and every revision any app tracks in this repo."""
    origin = git_origin(tools, root)
    if not origin:
        raise Fail(f"{root} has no origin remote")
    hits, used, tracked = [], set(), set()
    for app in tools.apps():
        lay = parse_layout(app)
        for r in lay.repos:
            if normalize_url(r.url) != normalize_url(origin):
                continue
            tracked.add(default_branch if tracks(r.revision, default_branch) else
                        r.revision.removeprefix("refs/heads/"))
            if not tracks(r.revision, default_branch):
                continue
            mine = [c for c in changed if uses_file(lay, r, c)]
            if mine:
                used.update(mine)
                if lay.name not in hits:
                    hits.append(lay.name)
    return sorted(hits), sorted(used), sorted(tracked)


def redact_diff(diff):
    """Mask the values of secret-looking keys on added/removed lines."""
    out = []
    for line in diff.splitlines():
        if line[:1] in "+-" and not line.startswith(("+++", "---")):
            m = re.match(r"^([+-]\s*-?\s*[\"']?([\w.-]+)[\"']?\s*:\s*)(\S.*)$", line)
            if m and SECRETISH.search(m.group(2)):
                line = m.group(1) + "<redacted>"
            m = re.match(r"^([+-]\s*value\s*:\s*)(\S.*)$", line)
            if m and out and SECRETISH.search(out[-1]):
                line = m.group(1) + "<redacted>"
        out.append(line)
    return "\n".join(out)


@guarded
def main_propose(argv, tools=None):
    """Container half of `./toolbox propose`; git and gh run on the host."""
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-propose")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="branch, title, files and affected apps for the working tree")
    bd = sub.add_parser("body", help="run preflight for every affected app and print the PR body")
    tr = sub.add_parser("tracked", help="every branch an ArgoCD app tracks in this repo")
    for s in (pl, bd, tr):
        s.add_argument("--repo-root", required=True)
        s.add_argument("--base", required=True, help="e.g. origin/main")
        s.add_argument("-m", "--message", default="")
    bd.add_argument("--rev", required=True)
    bd.add_argument("--revert-of")
    a = p.parse_args(argv)
    default_branch = a.base.split("/", 1)[1] if a.base.startswith("origin/") else a.base
    root = a.repo_root

    if a.cmd == "tracked":
        _, _, tracked = scan(tools, root, default_branch, [])
        print("\n".join(f"tracked={x}" for x in tracked))
        return EXIT_SAME

    if a.cmd == "plan":
        changed = changed_files(tools, root, a.base)
        if not changed:
            raise Fail(f"nothing to propose: no changes against {a.base}")
        if any("\n" in c or "\r" in c for c in changed):
            raise Fail("a changed file name contains a newline; rename it")
        apps, used, tracked = scan(tools, root, default_branch, changed)
        if not apps:
            raise Fail(f"no ArgoCD app reads these files from {default_branch} (a brand-new app?): "
                       f"{', '.join(changed)} - ask the human how they want it proposed")
        unused = [c for c in changed if c not in used]
        if unused:
            raise Fail("these changes aren't read by any ArgoCD app, so propose won't commit them: "
                       f"{', '.join(unused)}. Remove them or move them out of the clone, then run again")
        mb = merge_base(tools, root, a.base, "HEAD") or a.base
        d = describe(changed, lambda f: file_at(tools, root, mb, f),
                     lambda f: read_text(os.path.join(root, f)), a.message)
        lines = [f"branch={d['branch']}", f"title={d['title']}"]
        lines += [f"app={x}" for x in apps] + [f"file={x}" for x in used]
        lines += [f"tracked={x}" for x in tracked]
        if d.get("marker"):
            lines += [f"marker_app={d['marker_app']}", f"marker_env={d['marker_env']}"]
        print("\n".join(lines))
        return EXIT_SAME

    changed = changed_files(tools, root, a.base, a.rev)
    if not changed:
        raise Fail(f"{a.rev[:12]} changes nothing against {a.base}")
    apps, _, _ = scan(tools, root, default_branch, changed)
    if not apps:
        raise Fail("no ArgoCD app reads these files from " + default_branch)
    sections, any_change = [], False
    for name in apps:
        pf = preflight(tools, name, a.rev, root)
        any_change = any_change or pf.change.any
        sections.append("\n".join(summary_lines(pf.layout.name, pf.change, markdown=True)))
        log("\n".join(summary_lines(pf.layout.name, pf.change))
            + (f"\n  full diff: ./toolbox cat {pf.diff_file}" if pf.diff_file else ""))
    if not any_change:
        log("ArgoCD renders no change for any affected app; nothing to propose")
        return EXIT_SAME
    mb = merge_base(tools, root, a.base, a.rev) or a.base
    d = describe(changed, lambda f: file_at(tools, root, mb, f),
                 lambda f: file_at(tools, root, a.rev, f), a.message)
    _, diff = tools.git(root, "diff", "--no-renames", f"{a.base}...{a.rev}")
    diff = redact_diff(diff)
    if len(diff) > MAX_BODY_DIFF:
        diff = diff[:MAX_BODY_DIFF] + "\n... (truncated; see the Files tab)"
    intro = a.message.strip() or (f"Reverts {a.revert_of}." if a.revert_of else "")
    body = [intro, "", "### What changes",
            f"Rendered by ArgoCD from `{a.rev[:12]}` (`toolbox-preflight`) and compared with its "
            f"render of where this branch left `{default_branch}`, for every ArgoCD app visible to "
            "the proposer that reads a changed file. Secrets are compared but never shown; "
            "rendered manifests are deliberately not included.", "",
            *sections, "", "### Values diff",
            "Values of secret-looking keys are masked here; the Files tab has the real diff.", "",
            "```diff", diff.rstrip(), "```", "", "---",
            f"Opened with `./toolbox propose`. Nothing deploys until this is merged; ArgoCD "
            f"then syncs `{default_branch}` automatically."]
    if d["marker"] and not a.revert_of:
        body += ["", f"<!-- glueops-deploy:{d['marker']} -->"]
    print("\n".join(body))
    return EXIT_CHANGED
