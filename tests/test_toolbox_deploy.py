"""Unit tests for lib/toolbox_deploy.py. No server, no network, no git repo.

    docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint python3 <image> \
        -m unittest discover -s /tests -p 'test_*.py' -v
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("/opt/toolbox/lib", os.path.join(HERE, "..", "lib")):
    if os.path.isdir(p):
        sys.path.insert(0, p)

import toolbox_deploy as td  # noqa: E402

REPO = "https://github.com/acme/deployment-configurations"
CHART = {"repoURL": "https://helm.example.com/project-template", "chart": "app",
         "targetRevision": "0.14.1",
         "helm": {"valueFiles": ["$values/common/common-values.yaml",
                                 "$values/apps/api/base/base-values.yaml",
                                 "$values/apps/api/envs/stage/values.yaml"],
                  "values": "captain_domain: x"}}


def app(sources=None, source=None, name="api-stage", status=None):
    spec = {"destination": {"namespace": "nonprod"}}
    if sources is not None:
        spec["sources"] = sources
    if source is not None:
        spec["source"] = source
    return {"metadata": {"name": name, "namespace": "glueops-core"}, "spec": spec,
            "status": status or {}}


def multi(ref_first=False, url=REPO):
    ref = {"repoURL": url, "targetRevision": "main", "ref": "values"}
    return app([ref, CHART] if ref_first else [CHART, ref])


def deployment(image, replicas=2, name="api-stage"):
    return f"""---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: {name}, namespace: nonprod}}
spec:
  replicas: {replicas}
  template:
    spec:
      initContainers: [{{name: migrate, image: {image}}}]
      containers: [{{name: {name}, image: {image}}}]
"""


SERVICE = """---
apiVersion: v1
kind: Service
metadata: {name: api-stage, namespace: nonprod}
spec: {ports: [{port: 80}]}
"""


def secret(value):
    return f"""---
apiVersion: v1
kind: Secret
metadata: {{name: creds, namespace: nonprod}}
stringData: {{password: {value}}}
"""


class FakeTools(td.Tools):
    """Answers subprocess calls from a list of (predicate, (rc, out, err)) rules."""

    def __init__(self, rules):
        self.calls = []
        self.rules = rules
        super().__init__(self._fake)

    def _fake(self, argv):
        self.calls.append(argv)
        for pred, resp in self.rules:
            if pred(argv):
                return resp(argv) if callable(resp) else resp
        raise AssertionError(f"unexpected call: {argv}")


def has(*words):
    return lambda argv: all(w in argv for w in words)


def run_quiet(fn, *args, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args, **kw)
    return rc, out.getvalue(), err.getvalue()


class Layout(unittest.TestCase):
    def test_ref_at_position_2(self):
        lay = td.parse_layout(multi())
        self.assertEqual(lay.name, "glueops-core/api-stage")
        self.assertEqual([(r.position, r.ref) for r in lay.repos], [(2, "values")])
        self.assertEqual([v.path for v in lay.value_files],
                         ["common/common-values.yaml", "apps/api/base/base-values.yaml",
                          "apps/api/envs/stage/values.yaml"])

    def test_ref_at_position_1(self):
        lay = td.parse_layout(multi(ref_first=True))
        self.assertEqual(lay.repos[0].position, 1)

    def test_two_refs_need_origin(self):
        a = app([CHART, {"repoURL": REPO, "targetRevision": "main", "ref": "values"},
                 {"repoURL": "https://github.com/acme/other", "targetRevision": "main", "ref": "other"}])
        lay = td.parse_layout(a)
        with self.assertRaises(td.Fail):
            td.pick_repo(lay)
        self.assertEqual(td.pick_repo(lay, "git@github.com:acme/other.git").position, 3)
        with self.assertRaises(td.Fail):
            td.pick_repo(lay, "https://github.com/acme/nope")

    def test_single_source_git_path(self):
        a = app(source={"repoURL": REPO, "targetRevision": "main", "path": "charts/api",
                        "helm": {"valueFiles": ["values.yaml", "../shared/v.yaml"]}})
        lay = td.parse_layout(a)
        self.assertEqual(lay.repos[0].position, 0)
        self.assertEqual([v.path for v in lay.value_files], ["charts/api/values.yaml", "charts/shared/v.yaml"])
        self.assertTrue(td.app_uses(lay, lay.repos[0], ["charts/api/templates/x.yaml"]))

    def test_normalize_url(self):
        want = "github.com/acme/deployment-configurations"
        for u in (REPO, REPO + ".git", REPO + "/", "git@github.com:Acme/deployment-configurations.git",
                  "ssh://git@github.com/acme/deployment-configurations"):
            self.assertEqual(td.normalize_url(u), want, u)

    def test_app_uses(self):
        lay = td.parse_layout(multi())
        r = lay.repos[0]
        self.assertTrue(td.app_uses(lay, r, ["common/common-values.yaml"]))
        self.assertFalse(td.app_uses(lay, r, ["apps/api/envs/prod/values.yaml"]))

    def test_repo_app_env(self):
        self.assertEqual(td.repo_app_env("apps/api/envs/stage/values.yaml"), ("api", "stage"))
        self.assertEqual(td.repo_app_env("common/common-values.yaml"), (None, None))


class Yaml(unittest.TestCase):
    def test_find_image_tag(self):
        text = "service:\n  enabled: true\nimage:\n  tag: v0.0.2\n  registry: ghcr.io\n"
        self.assertEqual(td.find_scalar(text, ("image", "tag")), (4, "v0.0.2"))
        self.assertIsNone(td.find_scalar("image: {}\n", ("image", "tag")))
        self.assertIsNone(td.find_scalar("", ("image", "tag")))
        self.assertIsNone(td.find_scalar("a: [\n", ("image", "tag")))


class Compare(unittest.TestCase):
    def test_image_change(self):
        ch = td.compare(SERVICE + deployment("r/api:v1"), SERVICE + deployment("r/api:v2"))
        self.assertTrue(ch.any)
        self.assertEqual(ch.changed, ["Deployment/nonprod/api-stage"])
        self.assertEqual(len(ch.images), 2)
        self.assertEqual({(c, o, n) for _, c, o, n in ch.images},
                         {("migrate", "r/api:v1", "r/api:v2"), ("api-stage", "r/api:v1", "r/api:v2")})

    def test_same(self):
        self.assertFalse(td.compare(SERVICE, SERVICE).any)

    def test_secrets_never_compared(self):
        ch = td.compare(SERVICE + secret("old"), SERVICE + secret("new"))
        self.assertFalse(ch.any)
        self.assertTrue(ch.secrets_skipped)
        text = "\n".join(td.summary_lines("x", td.compare(secret("a") + deployment("i:1"),
                                                          secret("b") + deployment("i:2"))))
        self.assertNotIn("old", text)
        self.assertNotIn("password", text)

    def test_added_removed(self):
        ch = td.compare(SERVICE, deployment("r/api:v1"))
        self.assertEqual(ch.added, ["Deployment/nonprod/api-stage"])
        self.assertEqual(ch.removed, ["Service/nonprod/api-stage"])


class Describe(unittest.TestCase):
    OLD = "image:\n  tag: v1\n  port: 3000\ndeployment:\n  replicas: 2\n"

    def test_tag_bump(self):
        self.assertEqual(td.tag_bump(self.OLD, self.OLD.replace("v1", "v2")), "v2")
        self.assertIsNone(td.tag_bump(self.OLD, self.OLD.replace("v1", "v2").replace("2\n", "3\n")))
        self.assertIsNone(td.tag_bump(self.OLD, self.OLD))
        self.assertEqual(td.tag_bump("image:\n  port: 1\n", "image:\n  port: 1\n  tag: v9\n"), "v9")

    def test_describe_tag_bump(self):
        f = "apps/api/envs/stage/values.yaml"
        d = td.describe([f], lambda _: self.OLD, lambda _: self.OLD.replace("v1", "v2.0-rc1"), "bump")
        self.assertEqual(d["branch"], "api/update-stage-image-tag-v2-0-rc1")
        self.assertEqual(d["title"], "chore(deploy): api [stage] -> v2.0-rc1")
        self.assertEqual(json.loads(d["marker"]), {"app": "api", "env": "stage", "tag": "v2.0-rc1"})

    def test_describe_config_change_has_no_marker(self):
        f = "apps/api/envs/stage/values.yaml"
        d = td.describe([f], lambda _: self.OLD, lambda _: self.OLD.replace("2\n", "3\n"),
                        "Scale stage to 3 replicas")
        self.assertEqual(d["branch"], "api/update-stage-scale-stage-to-3-replicas")
        self.assertEqual(d["title"], "chore(deploy): api [stage] Scale stage to 3 replicas")
        self.assertEqual(d["marker"], "")

    def test_describe_shared_file(self):
        d = td.describe(["common/common-values.yaml"], lambda _: "", lambda _: "", "raise limits")
        self.assertEqual(d["branch"], "deploy/update-raise-limits")
        self.assertEqual(d["marker"], "")

    def test_previews_refused(self):
        with self.assertRaises(td.Fail):
            td.describe(["apps/api/envs/previews/common/values.yaml"], str, str, "x")


ST = {"sync": {"status": "Synced", "revisions": ["0.14.1", "new"]},
      "health": {"status": "Healthy"},
      "operationState": {"phase": "Succeeded", "finishedAt": "2026-10-06T06:24:01Z",
                         "syncResult": {"revisions": ["0.14.1", "new"]}},
      "reconciledAt": "2026-10-06T07:03:28Z"}


def status(**changes):
    s = json.loads(json.dumps(ST))
    for path, value in changes.items():
        node = s
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return s


class WatchState(unittest.TestCase):
    def state(self, st, operation=None):
        a = app(status=st)
        if operation:
            a["operation"] = operation
        return td.watch_state(a, 1, lambda r: r == "new")[0]

    def test_states(self):
        self.assertEqual(self.state(status(sync__revisions=["0.14.1", "old"])), "waiting")
        self.assertEqual(self.state(ST, operation={"sync": {}}), "syncing")
        self.assertEqual(self.state(status(operationState__phase="Running")), "syncing")
        self.assertEqual(self.state(status(operationState__syncResult={"revisions": ["0.14.1", "old"]})), "syncing")
        self.assertEqual(self.state(status(operationState__phase="Failed")), "failed")
        self.assertEqual(self.state(status(health={"status": "Degraded"})), "failed")
        self.assertEqual(self.state(status(health={"status": "Progressing"})), "progressing")
        self.assertEqual(self.state(status(reconciledAt="2026-10-06T06:00:00Z")), "progressing")
        self.assertEqual(self.state(status(sync__status="OutOfSync")), "syncing")
        self.assertEqual(self.state(ST), "healthy")

    def test_single_source_revision(self):
        st = {"sync": {"status": "Synced", "revision": "new"}, "health": {"status": "Healthy"},
              "operationState": {"phase": "Succeeded", "finishedAt": "a",
                                 "syncResult": {"revision": "new"}}, "reconciledAt": "b"}
        self.assertEqual(td.watch_state(app(status=st), 0, lambda r: r == "new")[0], "healthy")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def watch_tools(statuses):
    seq = iter(statuses)

    def get(argv):
        if "tree=detailed" in argv:
            return 0, "Deployment/api  Synced  Degraded  ImagePullBackOff\n", ""
        a = multi()
        a["status"] = next(seq)
        return 0, json.dumps(a), ""

    return FakeTools([(has("rev-parse", "--show-toplevel"), (128, "", "")),
                      (has("app", "get"), get)])


class Watch(unittest.TestCase):
    def run_watch(self, statuses, *extra):
        clock = Clock()
        tools = watch_tools(statuses)
        rc, out, err = run_quiet(td.main_watch, ["glueops-core/api-stage", "--rev", "new", *extra],
                                 tools=tools, clock=clock, sleep=clock.sleep)
        return rc, out, err, tools, clock

    def test_healthy_after_waiting(self):
        old = status(sync__revisions=["0.14.1", "old"])
        rc, out, _, tools, clock = self.run_watch([old, old, ST])
        self.assertEqual(rc, td.EXIT_SAME)
        self.assertEqual(clock.t, 20)
        self.assertIn("Synced and Healthy", out)
        # Read-only: only `app get`, never sync/refresh/wait.
        for argv in tools.calls:
            if argv[0] == "argocd":
                self.assertEqual(argv[1:3], ["app", "get"])
                self.assertNotIn("--refresh", argv)

    def test_not_synced_in_time(self):
        old = status(sync__revisions=["0.14.1", "old"])
        rc, out, err, _, clock = self.run_watch([old] * 30)
        self.assertEqual(rc, td.EXIT_WAITING)
        self.assertEqual(clock.t, 240)
        self.assertIn("never force a sync", err)

    def test_failed(self):
        rc, out, err, _, _ = self.run_watch([status(health={"status": "Degraded"})])
        self.assertEqual(rc, td.EXIT_ERROR)
        self.assertIn("ImagePullBackOff", out)
        self.assertIn("propose --revert", err)

    def test_still_progressing(self):
        prog = status(health={"status": "Progressing"})
        rc, out, _, _, _ = self.run_watch([prog] * 30, "--health-timeout", "30")
        self.assertEqual(rc, td.EXIT_WAITING)
        self.assertIn("still progressing", out)


class Preflight(unittest.TestCase):
    def tools(self, desired, proposed, get_rc=0):
        return FakeTools([
            (has("rev-parse", "--show-toplevel"), (128, "", "")),
            (has("app", "get"), (get_rc, json.dumps(multi()), "boom" if get_rc else "")),
            (has("app", "manifests", "--revisions"), (0, proposed, "")),
            (has("app", "manifests"), (0, desired, "")),
            (has("dyff"), (1, "diff text", "")),
        ])

    def test_change(self):
        t = self.tools(deployment("r/api:v1"), deployment("r/api:v2"))
        rc, out, _ = run_quiet(td.main_preflight, ["glueops-core/api-stage", "--rev", "abc1234"], tools=t)
        self.assertEqual(rc, td.EXIT_CHANGED)
        self.assertIn("r/api:v1", out)
        call = next(c for c in t.calls if "--revisions" in c)
        self.assertEqual(call[call.index("--source-positions") + 1], "2")

    def test_no_change(self):
        t = self.tools(deployment("r/api:v1"), deployment("r/api:v1"))
        rc, _, _ = run_quiet(td.main_preflight, ["glueops-core/api-stage", "--rev", "abc1234"], tools=t)
        self.assertEqual(rc, td.EXIT_SAME)

    def test_refuses_tracked_branch(self):
        t = self.tools("", "")
        rc, _, err = run_quiet(td.main_preflight, ["glueops-core/api-stage", "--rev", "main"], tools=t)
        self.assertEqual(rc, td.EXIT_ERROR)
        self.assertIn("already tracks", err)

    def test_argocd_error_is_exit_2(self):
        # e.g. the wrapper's "not authenticated" (4) or "refused" (5): never "change found".
        t = self.tools("", "", get_rc=4)
        rc, _, _ = run_quiet(td.main_preflight, ["glueops-core/api-stage", "--rev", "abc"], tools=t)
        self.assertEqual(rc, td.EXIT_ERROR)


class Affected(unittest.TestCase):
    def test_affected_filters_repo_and_branch(self):
        stage, prod = multi(), multi()
        prod["metadata"]["name"] = "api-prod"
        prod["spec"]["sources"][0] = dict(CHART, helm={"valueFiles": ["$values/apps/api/envs/prod/values.yaml"]})
        other_branch = multi()
        other_branch["metadata"]["name"] = "api-pr-1"
        other_branch["spec"]["sources"][1] = dict(other_branch["spec"]["sources"][1], targetRevision="pr-1")
        t = FakeTools([
            (has("remote.origin.url"), (0, "git@github.com:acme/deployment-configurations.git\n", "")),
            (has("app", "list"), (0, json.dumps([stage, prod, other_branch]), "")),
        ])
        self.assertEqual(td.affected(t, "/r", "main", ["apps/api/envs/stage/values.yaml"]),
                         ["glueops-core/api-stage"])
        self.assertEqual(td.affected(t, "/r", "main", ["common/common-values.yaml"]),
                         ["glueops-core/api-stage"])


if __name__ == "__main__":
    unittest.main()
