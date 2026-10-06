"""The login cache belongs to one cluster. No network, no Dex.

    docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint python3 <image> \
        -m unittest discover -s /tests -p 'test_*.py' -v
"""
import base64
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("/opt/toolbox/lib", os.path.join(HERE, "..", "lib")):
    if os.path.isdir(p):
        sys.path.insert(0, p)

import toolbox_auth as ta  # noqa: E402


def jwt(exp):
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


class ClusterScopedLogin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {k: os.environ.get(k) for k in ("TOOLBOX_CAPTAIN_DOMAIN", "TOOLBOX_TOKEN_CACHE",
                                                   "TOOLBOX_DEX_URL", "TOOLBOX_CLIENT_ID")}
        os.environ["TOOLBOX_TOKEN_CACHE"] = os.path.join(self.dir, "toolbox-token.json")
        os.environ.pop("TOOLBOX_DEX_URL", None)
        os.environ.pop("TOOLBOX_CLIENT_ID", None)
        self.cluster("a.example.com")

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def cluster(self, domain):
        os.environ["TOOLBOX_CAPTAIN_DOMAIN"] = domain

    def quiet(self, fn, *a):
        err = io.StringIO()
        with redirect_stderr(err):
            return fn(*a), err.getvalue()

    def test_same_cluster_keeps_the_login(self):
        ta._write_cache({"id_token": jwt(time.time() + 3600), "refresh_token": "r"})
        cache, _ = self.quiet(ta._read_cache)
        self.assertEqual(cache["refresh_token"], "r")
        self.assertEqual(cache["cluster"]["dex"], "https://dex.a.example.com")

    def test_another_cluster_discards_it(self):
        ta._write_cache({"id_token": jwt(time.time() + 3600), "refresh_token": "r"})
        ta._write_private_json(ta.pending_path(), {"device_code": "d", "cluster": ta.cluster_id(),
                                                   "expires_at": time.time() + 300})
        self.cluster("b.example.com")
        cache, err = self.quiet(ta._read_cache)
        self.assertEqual(cache, {})
        self.assertIn("not for https://dex.b.example.com", err)
        self.assertFalse(os.path.exists(ta.cache_path()))
        self.assertFalse(os.path.exists(ta.pending_path()))

    def test_a_valid_token_for_another_cluster_is_never_returned(self):
        ta._write_cache({"id_token": jwt(time.time() + 3600)})
        self.cluster("b.example.com")
        tok, _ = self.quiet(ta.get_token, False, False)
        self.assertIsNone(tok)

    def test_login_from_before_clusters_were_recorded_is_discarded(self):
        ta._write_private_json(ta.cache_path(), {"id_token": jwt(time.time() + 3600)})
        cache, err = self.quiet(ta._read_cache)
        self.assertEqual(cache, {})
        self.assertIn("discarded", err)

    def test_pending_code_from_another_cluster_is_dropped(self):
        ta._write_private_json(ta.pending_path(), {"device_code": "d", "cluster": ta.cluster_id(),
                                                   "expires_at": time.time() + 300})
        self.assertIsNotNone(ta._read_pending())
        self.cluster("b.example.com")
        self.assertIsNone(ta._read_pending())
        self.assertFalse(os.path.exists(ta.pending_path()))

    def test_dex_override_or_client_counts_as_another_cluster(self):
        ta._write_cache({"id_token": jwt(time.time() + 3600)})
        os.environ["TOOLBOX_CLIENT_ID"] = "other"
        cache, _ = self.quiet(ta._read_cache)
        self.assertEqual(cache, {})

    def test_forced_logout_writes_an_empty_cache_silently(self):
        ta._write_cache({})
        cache, err = self.quiet(ta._read_cache)
        self.assertEqual(cache, {})
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
