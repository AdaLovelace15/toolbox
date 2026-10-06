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
import difflib
import functools
import html
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

# GitHub rejects PR bodies over 65536 characters. Aim below it; past it, drop the diffs.
MAX_BODY, GITHUB_BODY_LIMIT = 60000, 65536
OPEN_DIFF_LINES = 30      # a single app's diff this short is shown open, not collapsed
REMOVED_LINES = 15        # how much of a removed resource to show

DIFF_DIR = os.path.join(tempfile.gettempdir(), "toolbox-deploy")

# High-confidence credential shapes. A rendered diff matching one is not shown
# at all: a PR body travels further than the repo and can't be scrubbed later.
CREDENTIAL = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
    # the same, base64-encoded: "-----BEGIN " then "PRIVATE KEY" at any alignment
    r"|LS0tLS1CRUdJTi[A-Za-z0-9+/]{0,40}?(?:UFJJVkFURSBLRVk|BSSVZBVEUgS0VZ|QUklWQVRFIEtFWQ)"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{30,}|\bglpat-[A-Za-z0-9_-]{20,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}|hooks\.slack\.com/services/T[A-Za-z0-9]+/"
    r"|\bAIza[0-9A-Za-z_-]{35}\b|\b[sr]k_live_[0-9A-Za-z]{16,}|\bsk-ant-[A-Za-z0-9_-]{16,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    # OpenBao/Vault, Grafana, npm, Docker Hub, age, SendGrid, DigitalOcean, Azure storage
    r"|\bhv[sbr]\.[A-Za-z0-9_-]{20,}|\bs\.[A-Za-z0-9]{24}\b|\bglsa_[A-Za-z0-9_]{20,}|\bnpm_[A-Za-z0-9]{36}"
    r"|\bdckr_pat_[A-Za-z0-9_-]{20,}|AGE-SECRET-KEY-1[0-9A-Z]{50,}|\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"|\bdop_v1_[a-f0-9]{64}|AccountKey=[A-Za-z0-9+/=]{40,}"
    # a password in a URL (the user may be empty); not a $VAR, %s, {{ template }} or <placeholder>
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]*:[^/\s@'\"$<{%]+@")

# A key that names a secret, with a literal value: withheld like a credential.
# `secretName: x`, `existingSecret: x` and the like name a Secret; they don't hold one.
SECRET_KEY = re.compile(r"(?i)(?<!existing)(?<!existing[_-])(?:password|passwd|secret|token|api[_-]?key"
                        r"|access[_-]?key|private[_-]?key|client[_-]?secret|credentials?)$")
PLACEHOLDER = re.compile(r"^(?:\$|\{\{|<|%|\*+$|changeme$|none$|null$|true$|false$|\+{8,}$)", re.I)


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

    def server_version(self):
        """ArgoCD server version, for the record; None if it can't be read."""
        rc, out, _ = self.run(["argocd", "version", "-o", "json"])
        try:
            return (json.loads(out).get("server") or {}).get("Version") if rc == 0 else None
        except (ValueError, AttributeError):
            return None


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
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)   # libyaml: much faster on big renders


@functools.lru_cache(maxsize=16)
def _parse(text):
    docs = []
    for d in yaml.load_all(text or "", Loader=_Loader):
        # List, SecretList, ConfigMapList...: the items are the resources.
        if isinstance(d, dict) and str(d.get("kind", "")).endswith("List") and isinstance(d.get("items"), list):
            docs += [i for i in d["items"] if isinstance(i, dict)]
        elif isinstance(d, dict):
            docs.append(d)
    return tuple(docs)


def load_docs(text):
    """The resources in a render. Parsed once per text: treat them as read-only."""
    return list(_parse(text or ""))


def clean_text(s):
    """Rendered text (names, images, keys) is chart-controlled: no control characters,
    so it can't forge lines in what an agent reads."""
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029\u0085]", "?", str(s))


def res_key(d):
    meta = d.get("metadata") or {}
    group = str(d.get("apiVersion", "")).rpartition("/")[0]
    kind = f"{d.get('kind')}.{group}" if group else str(d.get("kind"))
    ns = meta.get("namespace")
    return clean_text(f"{kind}/{ns + '/' if ns else ''}{meta.get('name')}")


def is_secret(d):
    return d.get("kind") == "Secret"


def hide_secret(d):
    """A Secret with its values replaced by <hidden>; its keys stay. Other kinds unchanged.

    ArgoCD already masks them in `app manifests`; this keeps it so if that changes."""
    if not is_secret(d):
        return d
    d = dict(d)
    for k in ("data", "stringData"):
        if isinstance(d.get(k), dict):
            d[k] = {key: "<hidden>" for key in d[k]}
        elif d.get(k) is not None:
            d[k] = "<hidden>"
    meta = d.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("annotations"), dict):
        # e.g. kubectl's last-applied-configuration holds the whole Secret.
        d["metadata"] = dict(meta, annotations={k: "<hidden>" for k in meta["annotations"]})
    return d


def masked_secrets(docs):
    """Secrets whose values ArgoCD masked (`++++++++`), so a change to them can't be seen."""
    out = []
    for d in docs:
        vals = [v for k in ("data", "stringData") if isinstance(d.get(k), dict) for v in d[k].values()]
        if is_secret(d) and vals and all(isinstance(v, str) and v and set(v) == {"+"} for v in vals):
            out.append(res_key(d))
    return sorted(out)


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
                out[(res_key(d), clean_text(c.get("name")))] = clean_text(c["image"])
    return out


def diff_paths(a, b, prefix="", out=None, limit=8):
    """Paths of the fields that differ - names only, never values."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if a.get(k) != b.get(k):
                diff_paths(a.get(k), b.get(k), f"{prefix}.{clean_text(k)}" if prefix else clean_text(k), out, limit)
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

    @property
    def any(self):
        return bool(self.added or self.removed or self.changed)


def compare(old_text, new_text):
    old, new = _index(old_text), _index(new_text)
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
    )


# The dumper the rendered diff reads well with: multi-line strings as `|`
# blocks (a one-line edit to a config file is a one-line diff), unicode as is,
# nothing folded, no anchors, keys sorted so ordering noise disappears.
class _DiffDumper(getattr(yaml, "CSafeDumper", yaml.SafeDumper)):
    def ignore_aliases(self, data):
        return True


_DiffDumper.add_representer(str, lambda d, s: d.represent_scalar(
    "tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))


def _blockable(s):
    """Can YAML show this multi-line string as a `|` block? Not with tabs, CRs or trailing spaces."""
    return not re.search(r"[\t\r]| \n| $", s)


def _lines_for_diff(v):
    """Multi-line strings YAML would quote onto one line become a list of lines, so they diff by line."""
    if isinstance(v, dict):
        return {k: _lines_for_diff(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_lines_for_diff(x) for x in v]
    if isinstance(v, str) and "\n" in v and not _blockable(v):
        return v.split("\n")
    return v

# Set by ArgoCD on every resource; they differ between renders without meaning anything.
ARGOCD_NOISE = {"labels": ("argocd.argoproj.io/instance",),
                "annotations": ("argocd.argoproj.io/tracking-id", "argocd.argoproj.io/installation-id")}


def _clean(d):
    d = copy.deepcopy(hide_secret(d))
    meta = d.get("metadata")
    if isinstance(meta, dict):
        for k, noise in ARGOCD_NOISE.items():
            if isinstance(meta.get(k), dict):
                for n in noise:
                    meta[k].pop(n, None)
                if not meta[k]:
                    del meta[k]
    return _lines_for_diff(d)


def _dump(d):
    text = yaml.dump(_clean(d), Dumper=_DiffDumper, sort_keys=True, default_flow_style=False,
                     allow_unicode=True, width=1 << 30)
    return [ln + "\n" for ln in text.split("\n")[:-1]]   # \n only: not U+2028 and friends


def _index(text):
    out = {}
    for d in load_docs(text):
        k, n = res_key(d), 2
        while k in out:
            k, n = f"{res_key(d)}#{n}", n + 1
        out[k] = d
    return out


def rendered_diff(old_text, new_text, context=3):
    """[(resource, unified diff)] for every resource whose render differs."""
    old, new = _index(old_text), _index(new_text)
    out = []
    for k in sorted(set(old) | set(new)):
        a = _dump(old[k]) if k in old else []
        b = _dump(new[k]) if k in new else []
        if a == b:
            if k in old and k in new and is_secret(new[k]) and old[k] != new[k]:
                out.append((k, f"--- a/{k}\n+++ b/{k}\n@@ Secret values changed (not shown) @@\n"))
            continue
        lines = list(difflib.unified_diff(a, b, f"a/{k}" if a else "/dev/null",
                                          f"b/{k}" if b else "/dev/null", n=context))
        if lines:
            out.append((k, "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)))
    return out


def write_dyff(tools, name, old_text, new_text):
    """Full resource-level diff, kept in the container. Secret values hidden."""
    try:
        os.makedirs(DIFF_DIR, exist_ok=True)
        work = tempfile.mkdtemp(dir=DIFF_DIR)
        try:
            paths = []
            for suffix, text in (("old", old_text), ("new", new_text)):
                p = os.path.join(work, f"{suffix}.yaml")
                with open(p, "w", encoding="utf-8") as fh:
                    yaml.safe_dump_all([hide_secret(d) for d in load_docs(text)], fh, sort_keys=False)
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
    n = len(ch.added) + len(ch.removed) + len(ch.changed)
    lines = [f"{b}{name}{b}: {n} resource{'s' if n != 1 else ''} changed"]
    pre = "- " if markdown else "  "
    for sym, keys in (("+", ch.added), ("-", ch.removed)):
        lines += [f"{pre}{sym} {b}{k}{b}" for k in keys]
    for k in ch.changed:
        fields = ", ".join(ch.paths.get(k) or [])
        lines.append(f"{pre}~ {b}{k}{b}" + (f" ({fields})" if fields else ""))
    for r, c, o, n_ in ch.images:
        lines.append(f"{pre}image {b}{c}{b} in {b}{r}{b}: {b}{o or 'none'}{b} -> {b}{n_ or 'none'}{b}")
    return lines


# ----------------------------------------------------------------- risks ----
DATA_KINDS = {"PersistentVolumeClaim", "StatefulSet", "Namespace", "CustomResourceDefinition", "ExternalSecret"}
IMMUTABLE = {"Deployment": (("spec", "selector"),),
             "DaemonSet": (("spec", "selector"),),
             "StatefulSet": (("spec", "selector"), ("spec", "volumeClaimTemplates"), ("spec", "serviceName"),
                             ("spec", "podManagementPolicy")),
             "Job": (("spec", "selector"), ("spec", "template"))}
SCALABLE = {"Deployment", "StatefulSet", "ReplicaSet"}
RISKS_CHECKED = ("removals, immutable fields, scale-downs, PodDisruptionBudgets, ingress hosts/TLS, "
                 "image tags, prod with non-prod")
# Warning severities, most serious first; the body sorts by them.
SEV_DATA, SEV_DELETE, SEV_CHANGE, SEV_IMAGE, SEV_SPREAD = range(5)


def _get(d, path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def sync_mode(sync_policy):
    """(automated, prune, self_heal) from an app's spec.syncPolicy."""
    auto = (sync_policy or {}).get("automated")
    if not isinstance(auto, dict) or auto.get("enabled") is False:
        return False, False, False
    return True, bool(auto.get("prune")), bool(auto.get("selfHeal"))


def _image_tag(image):
    """('digest'|'tag'|'none', tag) of an image reference."""
    if "@" in image:
        return "digest", image.rsplit("@", 1)[1]
    last = image.rsplit("/", 1)[-1]
    return ("tag", last.rsplit(":", 1)[1]) if ":" in last else ("none", "")


def _containers(d):
    ps = _pod_spec(d)
    return [c for c in (ps.get("initContainers") or []) + (ps.get("containers") or []) if isinstance(c, dict)]


def _ingress(d):
    rules = (d.get("spec") or {}).get("rules") or []
    tls = (d.get("spec") or {}).get("tls") or []
    hosts = sorted({clean_text(r.get("host") or "*") for r in rules if isinstance(r, dict)})
    return hosts, sorted(json.dumps(t, sort_keys=True) for t in tls)


def _annotations(d):
    return (d.get("metadata") or {}).get("annotations") or {}


def _prune_option(d):
    """'false' or 'confirm' from a Prune= sync option; '' otherwise. (Delete=false only
    matters when the Application itself is deleted, not to prune.)"""
    m = re.search(r"\bPrune=(false|confirm)\b", str(_annotations(d).get("argocd.argoproj.io/sync-options", "")))
    return m.group(1) if m else ""


def _recreated_by_argocd(d):
    """Hooks, and resources synced with Replace or Force, are recreated, so an immutable
    field changing is fine."""
    a = _annotations(d)
    return bool(a.get("argocd.argoproj.io/hook") or a.get("helm.sh/hook")
                or re.search(r"\b(Replace|Force)=true\b", str(a.get("argocd.argoproj.io/sync-options", ""))))


def _autoscaled(docs):
    """(kind, name) of every workload an HorizontalPodAutoscaler scales."""
    out = set()
    for d in docs:
        ref = _get(d, ("spec", "scaleTargetRef"))
        if d.get("kind") == "HorizontalPodAutoscaler" and isinstance(ref, dict):
            out.add((ref.get("kind"), ref.get("name")))
    return out


def _hosts(hosts, limit=5):
    shown = ", ".join(_md(h) for h in hosts[:limit]) or "none"
    return shown + (f" and {len(hosts) - limit} more" if len(hosts) > limit else "")


def risk_flags(old_text, new_text, sync_policy):
    """[(severity, warning)] a reviewer should look at twice, from the two renders and the sync policy."""
    old, new = _index(old_text), _index(new_text)
    auto, prune, _ = sync_mode(sync_policy)
    hpa = _autoscaled(new.values())
    flags = []
    for k in sorted(set(old) - set(new)):
        kind = old[k].get("kind")
        sev, opt = SEV_DELETE, _prune_option(old[k])
        if opt == "false":
            what = "is removed from git but stays in the cluster (its sync-options say Prune=false)"
        elif opt == "confirm":
            what = "is removed from git; ArgoCD deletes it once someone confirms the prune in ArgoCD"
        elif auto and prune:
            what = "will be DELETED on merge"
        elif auto:
            what = "is removed from git but stays in the cluster, orphaned (prune is off)"
        else:
            what = "is removed from git; the next manual sync with prune deletes it"
        if kind in DATA_KINDS and "stays" not in what:
            what, sev = what + " - possible data loss", SEV_DATA
        elif kind == "PodDisruptionBudget":
            what += " - its pods lose their disruption protection"
        flags.append((sev, f"{_md(k)} {what}"))
    for k in sorted(set(old) & set(new)):
        o, n = old[k], new[k]
        if o == n:
            continue
        kind = n.get("kind")
        for path in () if _recreated_by_argocd(n) else IMMUTABLE.get(kind, ()):
            if _get(o, path) != _get(n, path):
                flags.append((SEV_DATA, f"{_md(k)}: `{'.'.join(path)}` is immutable - the sync will fail "
                                        "unless the resource is deleted and recreated"))
        ro, rn = _get(o, ("spec", "replicas")), _get(n, ("spec", "replicas"))
        if kind in SCALABLE and (kind, _get(n, ("metadata", "name"))) not in hpa and isinstance(ro, int):
            if isinstance(rn, int) and rn < ro:
                flags.append((SEV_CHANGE, f"{_md(k)}: " + ("scaled to 0 replicas" if rn == 0 else f"replicas {ro} → {rn}")))
            elif rn is None and ro > 1:
                flags.append((SEV_CHANGE, f"{_md(k)}: `spec.replicas` removed - it falls back to 1 (was {ro})"))
        if kind == "PodDisruptionBudget":
            flags.append((SEV_CHANGE, f"{_md(k)}: PodDisruptionBudget changed "
                                      f"({', '.join(map(_md, diff_paths(o, n)))})"))
        if kind == "Ingress":
            (ho, to), (hn, tn) = _ingress(o), _ingress(n)
            if ho != hn:
                flags.append((SEV_CHANGE, f"{_md(k)}: ingress hosts {_hosts(ho)} → {_hosts(hn)}"))
            if to != tn:
                flags.append((SEV_CHANGE, f"{_md(k)}: ingress TLS changed"))
    for k in sorted(new):
        before = {c.get("name"): c.get("image") for c in _containers(old.get(k) or {})}
        for c in _containers(new[k]):
            img = c.get("image")
            if not isinstance(img, str) or before.get(c.get("name")) == img:
                continue
            form, tag = _image_tag(img)
            was = _image_tag(before[c.get("name")])[0] if isinstance(before.get(c.get("name")), str) else None
            # No resource name: the same image in several apps becomes one warning.
            if form == "none" or tag == "latest":
                always = "; with `imagePullPolicy: Always`, every restart may pull a different image" \
                    if c.get("imagePullPolicy") == "Always" else ""
                flags.append((SEV_IMAGE, f"floating image {_md(img)} "
                                         f"({'no tag' if form == 'none' else 'tag `latest`'}{always})"))
            elif was == "digest" and form == "tag":
                flags.append((SEV_IMAGE, f"pinned digest replaced by the tag {_md(img)}"))
    return sorted(set(flags), key=lambda f: (f[0], f[1]))


def is_prod(env):
    """A production environment name: prod, prd, production, prod-eu, ... - not preprod or non-prod."""
    e = (env or "").lower()
    if re.search(r"(pre|non)[-_.]?(prod|prd)", e):
        return False
    return bool(re.search(r"(^|[-_.])(prod|prd|production)\d*($|[-_.])", e))


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
    old_text: str = ""
    new_text: str = ""
    base_sha: str = ""            # the merge base compared with; "" when it is the live desired state
    app: dict = field(default_factory=dict)
    masked: list = field(default_factory=list)    # Secrets ArgoCD masked: a change to them can't be seen
    withheld: str = ""            # why the rendered diff must not be shown, or ""
    spec_values: bool = False     # the app spec sets Helm values itself, outside this repo

    @property
    def unseen(self):
        """Nothing visible changed, but a masked Secret might have: the changed file feeds
        this app, and ArgoCD can't show whether its Secret values moved."""
        return not self.change.any and bool(self.masked)

    def diff(self):
        if self.withheld:
            return []
        chunks = rendered_diff(self.old_text, self.new_text)
        return [] if credential_in_resources(self, [k for k, _ in chunks]) else chunks

    def credential(self):
        return not self.withheld and credential_in_resources(self, [k for k, _ in rendered_diff(
            self.old_text, self.new_text)])


def secretish(v, key=""):
    """A literal value under a key that names a secret - env `{name: DB_PASSWORD, value: x}`
    included. Secrets (values hidden anyway) and placeholders don't count."""
    if isinstance(v, dict):
        if v.get("kind") == "Secret":
            return False
        if isinstance(v.get("name"), str) and isinstance(v.get("value"), str) and SECRET_KEY.search(v["name"]):
            if len(v["value"]) >= 4 and not PLACEHOLDER.search(v["value"]):
                return True
        return any(secretish(x, str(k)) for k, x in v.items())
    if isinstance(v, list):
        return any(secretish(x, key) for x in v)
    if isinstance(v, str) and key and SECRET_KEY.search(key):
        return len(v) >= 4 and not PLACEHOLDER.search(v) and "\n" not in v
    if isinstance(v, str) and "\n" in v:   # a config file in a ConfigMap: key: value lines
        return any(SECRET_KEY.search(m.group(1)) and len(m.group(2)) >= 4 and not PLACEHOLDER.search(m.group(2))
                   for m in re.finditer(r"(?m)^[ \t]*[\"']?([A-Za-z0-9_.-]+)[\"']?[ \t]*[:=][ \t]*[\"']?([^\s\"'#]+)", v))
    return False


def credential_in_resources(pf, keys):
    """Does any resource that changed look like it holds a credential - anywhere in it,
    not only in the lines that changed (a PEM body can change without its header)?"""
    old, new = _index(pf.old_text), _index(pf.new_text)
    return any(CREDENTIAL.search("".join(_dump(side[k]))) or secretish(side[k])
               for k in keys for side in (old, new) if k in side)


def withheld_reason(app, layout, repos):
    """Why this app's render may hold things that aren't in the deployment repo, or ""."""
    spec = app.get("spec") or {}
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    st = app.get("status") or {}
    if (any(isinstance(s, dict) and s.get("plugin") is not None for s in sources)
            or st.get("sourceType") == "Plugin" or "Plugin" in (st.get("sourceTypes") or [])):
        return "it is rendered by a config-management plugin, which can inject secrets"
    ours = {(r.position, normalize_url(r.url)) for r in repos}
    elsewhere = sorted({v.raw if v.repo is None else v.repo.url for v in layout.value_files
                        if v.repo is None or re.search(r"[a-z][a-z0-9+.-]*://", v.raw)
                        or (v.repo.position, normalize_url(v.repo.url)) not in ours})
    if elsewhere:
        return f"it reads values from outside this repo ({', '.join(elsewhere)})"
    templates = sorted({r.url for r in layout.repos if not r.ref
                        and normalize_url(r.url) not in {u for _, u in ours}})
    if templates:
        return f"it renders templates from another repo ({', '.join(templates)})"
    if spec_values_secret(layout):
        return "its Application spec sets a Helm value that looks like a credential (that isn't in this repo)"
    return ""


SPEC_VALUE_KEYS = ("values", "valuesObject", "parameters", "fileParameters")


def has_spec_values(layout):
    helm = layout.chart.get("helm") or {}
    return any(helm.get(k) for k in SPEC_VALUE_KEYS)


def spec_values_secret(layout):
    """Helm values the Application spec sets (outside this repo) that look like a credential."""
    helm = layout.chart.get("helm") or {}
    spec = {k: helm.get(k) for k in SPEC_VALUE_KEYS if helm.get(k)}
    params = [{"name": p.get("name"), "value": p.get("value")} for p in helm.get("parameters") or []
              if isinstance(p, dict)]
    try:
        inline = yaml.safe_load(helm.get("values") or "") if isinstance(helm.get("values"), str) else None
    except yaml.YAMLError:
        inline = None
    return bool(CREDENTIAL.search(json.dumps(spec, default=str)) or secretish(params) or secretish(inline)
                or secretish(helm.get("valuesObject")))


def preflight(tools, name, rev, root=None, diff_file=True):
    """Compare ArgoCD's render of `rev` with its render of where `rev` left the tracked branch."""
    app = tools.app(name)
    lay = parse_layout(app)
    root, origin = usable_root(tools, lay, root)
    repos = pick_repos(lay, origin)
    tracked = repos[0].revision
    if rev in (tracked, f"origin/{tracked}") or (tracked == "HEAD" and rev in ("main", "master")):
        raise Fail(f"{rev} is the branch {lay.name} already tracks; pass the pushed branch or commit to test")
    old_text, baseline, base_sha = None, f"{tracked} today", ""
    sha = rev
    if root:
        sha = resolve_rev(tools, root, rev)
        mb = merge_base(tools, root, f"origin/{tracked}" if tracked != "HEAD" else "origin/HEAD", sha)
        if mb and mb != sha:
            # Compare with where the branch left the tracked branch, so whatever
            # merged since doesn't show up reversed.
            old_text = tools.manifests(lay.name, repos, mb)
            baseline, base_sha = f"{mb[:12]}, where it left {tracked}", mb
    if old_text is None:
        old_text = tools.manifests(lay.name)
    new_text = tools.manifests(lay.name, repos, sha)
    ch = compare(old_text, new_text)
    path = write_dyff(tools, lay.name, old_text, new_text) if (diff_file and ch.any) else None
    return Preflight(lay, repos, sha, baseline, ch, path, old_text, new_text, base_sha, app,
                     masked_secrets(load_docs(new_text)), withheld_reason(app, lay, repos),
                     has_spec_values(lay))


def secret_note(keys):
    names = ", ".join(_md(k) for k in keys)
    return (f"ArgoCD shows no change, but it masks Secret values, so it can't tell whether {names} "
            "changed. Check in the Files tab that the change is meant to reach it")


CREDENTIAL_MSG = ("possible credential in a changed resource - not shown. Check the Files tab; "
                  "if it is one, it belongs in OpenBao, not in git")


@guarded
def main_preflight(argv, tools=None):
    tools = tools or Tools()
    p = argparse.ArgumentParser(prog="toolbox-preflight", description=(
        "Have ArgoCD render a pushed branch or commit of the deployment repo and "
        "compare it with its render of where that branch started. Read-only. "
        "Exit 0 no change, 1 change (or a Secret ArgoCD masks, so it can't tell), 2 error."))
    p.add_argument("app")
    p.add_argument("--rev", required=True, help="pushed branch or commit")
    p.add_argument("--repo-root", help="deployment repo clone (default: the git repo you are in)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--diff", action="store_true",
                   help="also print the rendered manifest diff (what the PR body shows)")
    a = p.parse_args(argv)
    pf = preflight(tools, a.app, a.rev, a.repo_root or git_toplevel(tools, os.getcwd()))
    ch = pf.change
    code = EXIT_CHANGED if (ch.any or pf.unseen) else EXIT_SAME
    positions = [r.position or 1 for r in pf.repos]
    chunks = pf.diff() if a.diff else []
    withheld = pf.withheld or (CREDENTIAL_MSG if a.diff and pf.credential() else "")
    if a.json:
        out = {"app": pf.layout.name, "rev": pf.sha, "baseline": pf.baseline,
               "source_positions": positions,
               "added": ch.added, "removed": ch.removed, "changed": ch.changed,
               "changed_fields": ch.paths, "masked_secrets": pf.masked, "secret_change_unknown": pf.unseen,
               "images": [{"resource": r, "container": c, "old": o, "new": n}
                          for r, c, o, n in ch.images],
               "diff_file": pf.diff_file, "exit": code}
        if a.diff:
            out["diff"] = None if withheld else "".join(t for _, t in chunks)
            out["diff_withheld"] = withheld or None
        print(json.dumps(out, indent=2))
        return code
    print(f"rendered by ArgoCD at {pf.sha[:12]} (source position {', '.join(map(str, positions))}), "
          f"compared with {pf.baseline}")
    print("\n".join(summary_lines(pf.layout.name, ch)))
    if pf.unseen:
        print("  " + secret_note(pf.masked).replace("`", ""))
    if a.diff and withheld:
        print(f"rendered diff not shown: {withheld}")
    elif chunks:
        print("".join(t for _, t in chunks), end="")
    if pf.diff_file:
        print(f"full diff: <toolbox> cat {pf.diff_file}")
    if ch.any or pf.unseen:
        log("next: open the PR with ./toolbox propose - it puts this rendered diff in the PR body")
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


# --------------------------------------------------------------- PR body ----
def _fence(text):
    """A code fence longer than any run of backticks in `text`."""
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, run + 1)


def _shares(sizes, budget):
    """Split `budget` so small sizes are met in full and big ones share the rest equally."""
    out, left = [0] * len(sizes), max(0, budget)
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for n, i in enumerate(order):
        out[i] = min(sizes[i], left // (len(sizes) - n))
        left -= out[i]
    return out


def _app_env(layout):
    return next((repo_app_env(v.path)[1] for v in layout.value_files
                 if v.repo and repo_app_env(v.path)[0]), None)


def _md(text):
    """Inline code, safe in a table cell or list item."""
    t = str(text).replace("|", "\\|").replace("\n", " ").replace("`", "'")
    return f"`{t}`"


def _defuse(text):
    """Only the footer may carry the deploy marker: the cleanup workflow reads the first
    `glueops-deploy:` it finds, wherever it is, so an echo of one elsewhere is broken."""
    return re.sub(r"(?i)glueops-deploy(\s*):", "glueops\u200b-deploy\\1:", text)


@dataclass
class AppReport:
    pf: Preflight
    env: str
    flags: list             # [(severity, warning)]
    chunks: list            # [(resource, diff text)], ordered and trimmed; [] when withheld
    withheld: str           # why the diff isn't shown, or ""
    fold_key: str = ""      # the untrimmed diff with the app's name taken out
    same_as: str = ""       # another app whose diff this one repeats

    @property
    def name(self):
        return self.pf.layout.name


def _order(pf, flags, chunks):
    """Removed first (trimmed), then flagged changes, other changes, added."""
    ch = pf.change
    flagged = {k for k in ch.changed if any(_md(k) in f for _, f in flags)}

    def rank(k):
        if k in ch.removed:
            return 0
        if k in flagged:
            return 1
        return 3 if k in ch.added else 2

    out = []
    for k, text in sorted(chunks, key=lambda c: (rank(c[0]), c[0])):
        if rank(k) == 0:
            lines = text.splitlines(keepends=True)
            if len(lines) > REMOVED_LINES + 3:
                text = "".join(lines[:REMOVED_LINES + 3]) + f"  ... ({len(lines) - REMOVED_LINES - 3} more lines)\n"
        out.append((k, text))
    return out


def build_reports(pfs):
    reps = []
    for pf in pfs:
        flags = risk_flags(pf.old_text, pf.new_text, (pf.app.get("spec") or {}).get("syncPolicy"))
        chunks, withheld = [], pf.withheld
        if not withheld:
            chunks = rendered_diff(pf.old_text, pf.new_text)
            if credential_in_resources(pf, [k for k, _ in chunks]):
                chunks, withheld = [], CREDENTIAL_MSG
        short = pf.layout.name.rsplit("/", 1)[-1]
        key = json.dumps([(k.replace(short, "<app>"), t.replace(short, "<app>")) for k, t in chunks])
        reps.append(AppReport(pf, _app_env(pf.layout), flags, _order(pf, flags, chunks), withheld, key))
    seen = {}
    for r in reps:
        if r.chunks and r.fold_key in seen:
            r.same_as = seen[r.fold_key]
        else:
            seen.setdefault(r.fold_key, r.name)
    return reps


def _counts(ch, bold=True):
    parts = [f"{len(ch.changed)} changed" if ch.changed else "", f"{len(ch.added)} added" if ch.added else "",
             (f"**{len(ch.removed)} removed**" if bold else f"{len(ch.removed)} removed") if ch.removed else ""]
    return ", ".join(p for p in parts if p)


def _warnings(reps):
    """[(severity, line)] across every app: identical warnings are merged, the most serious first."""
    merged = {}
    for r in reps:
        for sev, text in r.flags:
            merged.setdefault((sev, text), []).append(r.name)
        if r.pf.unseen:
            merged.setdefault((SEV_CHANGE, secret_note(r.pf.masked)), []).append(r.name)
    out = []
    for (sev, text), apps in sorted(merged.items()):
        if len(reps) > 1 and len(apps) == len(reps):
            out.append((sev, f"every app: {text}"))
        elif len(apps) > 1:
            out.append((sev, f"{text} - in {', '.join(f'`{a}`' for a in apps)}"))
        else:
            out.append((sev, f"`{apps[0]}`: {text}"))
    envs = {r.env for r in reps if r.env and (r.pf.change.any or r.pf.unseen)}
    prod = sorted(e for e in envs if is_prod(e))
    other = sorted(e for e in envs if not is_prod(e))
    if prod and other:
        out.append((SEV_SPREAD, f"prod ({', '.join(f'`{e}`' for e in prod)}) changes together with non-prod "
                                f"({', '.join(f'`{e}`' for e in other)}): consider one PR per environment"))
    return out


def _on_merge(reps):
    groups = {}
    for r in reps:
        auto, prune, heal = sync_mode((r.pf.app.get("spec") or {}).get("syncPolicy"))
        if auto:
            what = (f"ArgoCD syncs automatically, within about 3 min; "
                    f"prune {'on' if prune else 'off'}, self-heal {'on' if heal else 'off'}")
        else:
            what = "manual sync: nothing changes in the cluster until someone syncs it in ArgoCD"
        groups.setdefault(what, []).append(r)
    if len(groups) == 1 and len(reps) > 1:
        return [f"- all {len(reps)} apps: {next(iter(groups))}"]
    if len(reps) > 20:   # too many to name: say how many follow each policy
        return [f"- {len(rs)} apps: {what}" for what, rs in groups.items()]
    return [f"- {', '.join(f'`{r.name}`' for r in rs)}: {what}" for what, rs in groups.items()]


def _summary(reps, n_warnings):
    changing = [r for r in reps if r.pf.change.any or r.pf.unseen]
    envs = sorted({r.env for r in changing if r.env})
    tot = Change(*([sum((getattr(r.pf.change, f) for r in changing), []) for f in ("added", "removed", "changed")]
                   + [{}, []]))
    imgs = sorted({(o or "none", n or "none") for r in changing for _, _, o, n in r.pf.change.images})
    what = _counts(tot) or "no visible change"
    if imgs and len(imgs) <= 2:
        what += "; image " + ", ".join(f"{_md(o)} → {_md(n)}" for o, n in imgs)
    apps = f"{len(changing)} app{'s' if len(changing) != 1 else ''}"
    warn = f"{n_warnings} warning{'s' if n_warnings != 1 else ''}" if n_warnings else "no warnings"
    where = f" ({', '.join(envs[:8])}{f' and {len(envs) - 8} more' if len(envs) > 8 else ''})" if envs else ""
    return f"**{apps}{where}: {what}. {warn}.**"


def _table(reps):
    server = re.sub(r"^[a-z]+://", "", os.environ.get("ARGOCD_SERVER", "")).strip("/")
    rows = ["| app | env | resources | image |", "|---|---|---|---|"]
    for r in reps:
        ch = r.pf.change
        imgs = sorted({(o or "none", n or "none") for _, _, o, n in ch.images})
        img = "<br>".join(f"{_md(o)} → {_md(n)}" for o, n in imgs) or "-"
        meta = r.pf.app.get("metadata") or {}
        path = "/".join(x for x in (meta.get("namespace"), meta.get("name")) if x)
        app = f"[{_md(r.name)}](https://{server}/applications/{path})" if server and path else _md(r.name)
        res = _counts(ch) or ("no visible change (Secret values masked)" if r.pf.unseen else "no visible change")
        rows.append(f"| {app} | {_md(r.env) if r.env else '-'} | {res} | {img} |")
    return rows


def _diff_block(rep, shown, omitted, hint, open_):
    text = "".join(t for _, t in shown)
    fence = _fence(text)
    counts = _counts(rep.pf.change, bold=False) or "no visible change"
    title = f"<code>{html.escape(rep.name)}</code>: {counts}"
    out = ([f"**{title}**", ""] if open_ else [f"<details><summary>{title}</summary>", ""])
    if shown:
        out += [fence + "diff", text.rstrip("\n"), fence, ""]
    if omitted:
        names = ", ".join(_md(k) for k in omitted[:20]) + (f" and {len(omitted) - 20} more" if len(omitted) > 20 else "")
        out += [f"Not shown, to keep this description under GitHub's size limit: {names}. {hint}", ""]
    if not open_:
        out += ["</details>", ""]
    return out


def _fit(chunks, budget):
    """The whole resources that fit in `budget` characters, in order, skipping any that
    don't; the names of those skipped; and the characters used."""
    shown, omitted, used = [], [], 0
    for k, t in chunks:
        if used + len(t) > budget:
            omitted.append(k)
            continue
        shown.append((k, t))
        used += len(t)
    return shown, omitted, used


TOOLBOX = "<path-to-toolbox>/toolbox"


def render_body(reps, *, intent, rev, default_branch, version, marker, level=0, intent_cut=False):
    """The PR description: what merging does, what to look at, then the evidence.

    level 0 has the diffs; 1 drops them; 2 is a last resort for very many apps."""
    warnings = _warnings(reps)
    width = 600 if level < 2 else 200
    lines = [f"- {w}" if len(w) <= width else f"- {w[:width]}…" for _, w in warnings]
    cap = 60 if level < 2 else 15
    if len(lines) > cap:
        lines = lines[:cap] + [f"- ... and {len(lines) - cap} more, less serious"]
    top = [_summary(reps, len(warnings)), "", "**On merge**", *_on_merge(reps), "", "**Warnings**"]
    top += lines or [f"- None of the automatic checks fired ({RISKS_CHECKED}). "
                     "They don't judge config values: read the diff."]
    top += [""]
    if intent:
        top += ["**Intent**, as written by the proposer (not verified)"
                + (" - cut short here" if intent_cut else "") + ":", "",
                _fence(intent) + "text", intent, _fence(intent), ""]
    top += ["### What changes",
            f"ArgoCD's render of `{rev[:12]}` compared with its render of the merge base with "
            f"`{default_branch}`, for every ArgoCD app visible to the proposer that reads a changed "
            "file. This is what the change does; the values diff is in the Files tab.", ""]
    if level < 2:
        top += _table(reps) + [""]
        notes = []
        for r in reps:
            if r.withheld:
                notes.append(f"- `{r.name}`: rendered diff not shown: {r.withheld}")
            if r.same_as:
                notes.append(f"- `{r.name}`: same diff as `{r.same_as}`, apart from its name")
        spec = [r.name for r in reps if r.pf.spec_values]
        if spec:
            who = "every app's" if len(spec) == len(reps) > 1 else ", ".join(f"`{a}`" for a in spec) + (
                "'s" if len(spec) == 1 else "")
            notes.append(f"- {who} Application spec also sets Helm values: they are in the render, "
                         "but not in this repo")
        top += notes + ([""] if notes else [])
    else:
        top += [f"{len(reps)} apps: too many to list here. Run the commands below to see each one.", ""]

    charts = sorted({f"{_md(c.get('repoURL', '?'))} {_md(c.get('chart') or c.get('path') or '?')}"
                     f"@{_md(c.get('targetRevision', '?'))}" for r in reps for c in [r.pf.layout.chart] if c})
    if len(charts) > 5:
        charts = charts[:5] + [f"and {len(charts) - 5} more"]
    bases = sorted({r.pf.base_sha for r in reps if r.pf.base_sha})
    positions = sorted({" ".join(str(x.position) for x in r.pf.repos if x.position) for r in reps})

    def raw(sha):   # single-source apps take --revision; multi-source ones a revision per source
        if positions == [""]:
            return f"--revision {sha}"
        if len(positions) == 1 and " " not in positions[0]:
            return f"--revisions {sha} --source-positions {positions[0]}"
        return f"--revisions {sha} --source-positions <n>` (per app, see `toolbox-app`)`"
    base_txt = (f"the merge base with `{default_branch}`, `{bases[0]}`" if len(bases) == 1 else
                f"the merge base with `{default_branch}`" if bases else
                f"`{default_branch}` as ArgoCD had it at render time")
    how = ["<details><summary>How this was rendered</summary>", "",
           f"- head `{rev}`, compared with {base_txt}",
           f"- ArgoCD server {f'`{version}`' if version else 'version unknown'}; chart {', '.join(charts) or 'n/a'}",
           f"- to reproduce, from your deployment-repo clone after `git fetch`: "
           f"`{TOOLBOX} toolbox-preflight <app> --rev {rev} --diff` for each app above",
           f"- or raw: `argocd app manifests <app> {raw(rev)}` and `argocd app manifests <app> "
           f"{raw(bases[0] if len(bases) == 1 else '<merge-base>')}`, then diff the two",
           f"- if this PR's head isn't `{rev[:12]}`, this description is stale: run `toolbox propose` again",
           "", "</details>", ""]
    foot = ["---",
            f"Opened with `toolbox propose`. After the merge, from your deployment-repo clone: "
            f"`{TOOLBOX} toolbox-watch <app> --rev <merge-sha>` for each app above; if it fails, "
            f"`{TOOLBOX} propose --revert <merge-sha>` opens a revert PR."]

    hint = f"`{TOOLBOX} toolbox-preflight <app> --rev {rev[:12]} --diff` shows everything."
    blocks = []
    shown = [r for r in reps if r.chunks and not r.same_as] if level == 0 else []
    if shown:
        skeleton = len("\n".join(top + how + foot))
        overhead = sum(400 + 120 * min(len(r.chunks), 20) for r in shown)   # headers, fences, omitted names
        sizes = [sum(len(t) for _, t in r.chunks) for r in shown]
        shares = _shares(sizes, MAX_BODY - skeleton - overhead)
        fits = [_fit(r.chunks, s) for r, s in zip(shown, shares)]
        short = [i for i, (_, om, _) in enumerate(fits) if om]
        spare = sum(s - used for s, (_, om, used) in zip(shares, fits) if not om)
        for i in short:   # what the apps shown whole left unused goes to the cut ones
            fits[i] = _fit(shown[i].chunks, shares[i] + spare // len(short))
        single = len(reps) == 1
        for r, (vis, omitted, _) in zip(shown, fits):
            lines_n = sum(t.count("\n") for _, t in vis)
            blocks += _diff_block(r, vis, omitted, hint, open_=single and not omitted and lines_n <= OPEN_DIFF_LINES)
    elif level >= 1 and any(r.chunks for r in reps):
        blocks += [f"Rendered diffs left out: they don't fit GitHub's size limit. {hint}", ""]
    body = _defuse("\n".join(top + blocks + how + foot))
    if marker:
        body += f"\n\n<!-- glueops-deploy:{marker} -->"
    if len(body) > GITHUB_BODY_LIMIT and level < 2:
        return render_body(reps, intent=intent, rev=rev, default_branch=default_branch, version=version,
                           marker=marker, level=level + 1, intent_cut=intent_cut)
    return body


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
    if CREDENTIAL.search(a.message):
        raise Fail("the -m intent looks like it contains a credential; it would go into the PR description. "
                   "Run again without it")
    apps, _, _ = scan(tools, root, default_branch, changed)
    if not apps:
        raise Fail("no ArgoCD app reads these files from " + default_branch)
    pfs = []
    for name in apps:
        pf = preflight(tools, name, a.rev, root)
        pfs.append(pf)
        # Summaries only: agents read stderr, and the diff belongs in the PR.
        log("\n".join(summary_lines(pf.layout.name, pf.change)
                      + ([f"  {secret_note(pf.masked)}".replace("`", "")] if pf.unseen else []))
            + (f"\n  full diff: <toolbox> cat {pf.diff_file}" if pf.diff_file else ""))
    if not any(pf.change.any or pf.unseen for pf in pfs):
        log("ArgoCD renders no change for any affected app; nothing to propose")
        return EXIT_SAME
    mb = merge_base(tools, root, a.base, a.rev) or a.base
    d = describe(changed, lambda f: file_at(tools, root, mb, f),
                 lambda f: file_at(tools, root, a.rev, f), a.message)
    intent = a.message.strip() or (f"Reverts {a.revert_of}." if a.revert_of else "")
    marker = d["marker"] if d["marker"] and not a.revert_of else ""
    reps = build_reports(pfs)
    warnings = _warnings(reps)
    if warnings:
        # The agent hands the human the link: it has to know what the PR warns about.
        log("warnings in the PR - tell the human, or fix the change if you didn't intend one:\n"
            + "\n".join(f"  - {w}".replace("`", "") for _, w in warnings))
    print(render_body(reps, intent=intent[:4000], rev=pfs[0].sha, default_branch=default_branch,
                      version=tools.server_version(), marker=marker, intent_cut=len(intent) > 4000))
    return EXIT_CHANGED
