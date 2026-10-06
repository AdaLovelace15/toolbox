"""Dex device-flow tokens for the GlueOps toolbox.

Everything behind the GlueOps edge (oauth2-proxy) will accept a Dex-issued
id_token in place of a browser session cookie. This module obtains one and keeps
it fresh, so the CLIs in this image can be used normally.

The device flow is deliberate: it needs no loopback listener and no redirect URI,
so it works from inside a container whose browser lives on the host.

stdlib only.
"""

import json
import os
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Refresh early rather than hand out a token that expires mid-request.
EXPIRY_SKEW = 60


def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"toolbox: {name} is not set (see HUMANS.md)")
    return v


def captain_domain():
    return _env("TOOLBOX_CAPTAIN_DOMAIN", required=True)


def dex_url():
    return _env("TOOLBOX_DEX_URL") or f"https://dex.{captain_domain()}"


def client_id():
    return _env("TOOLBOX_CLIENT_ID", "toolbox")


def cache_path():
    return os.path.expanduser(
        _env("TOOLBOX_TOKEN_CACHE", "~/.config/glueops/toolbox-token.json")
    )


def log(msg):
    print(msg, file=sys.stderr)


# A freshly started container can lose its first DNS lookup or two while the
# resolver settles, and an agent driving this runs --begin the instant the
# container is up. Failing on the first miss sent one straight into twenty
# seconds of network debugging; retrying briefly makes that a non-event.
TRANSIENT_RETRIES = int(os.environ.get("TOOLBOX_HTTP_RETRIES") or 6)
TRANSIENT_BACKOFF = 1.0
_SSL = None  # built lazily: ssl_context() logs, and log() is defined below


def _ssl():
    global _SSL
    if _SSL is None:
        _SSL = ssl_context()
    return _SSL


def _post(path, data):
    body = urllib.parse.urlencode(data).encode()
    last = None
    for attempt in range(TRANSIENT_RETRIES):
        req = urllib.request.Request(dex_url() + path, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=10, context=_ssl()) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": raw.decode(errors="replace")[:200]}
        except (urllib.error.URLError, OSError) as e:
            last = getattr(e, "reason", e)
            time.sleep(TRANSIENT_BACKOFF)
    sys.exit(f"toolbox: cannot reach Dex at {dex_url()} after {TRANSIENT_RETRIES} attempts: {last}")


PROBE_OK, PROBE_TLS, PROBE_UNREACHABLE = 0, 2, 1


def probe():
    """Classify this container's egress, fast, against a small public endpoint
    rather than the cluster's own Dex (which may be slow, or private):

      0  reachable
      2  TLS failed - a proxy intercepts TLS and its CA is not trusted here
      1  unreachable - no route, no DNS, or a loopback-bound proxy the bridge
         network cannot see

    Separate from --begin on purpose: --begin returns a cached URL or token
    without touching the network, so it cannot tell a working network from a
    broken one."""
    url = os.environ.get("TOOLBOX_PROBE_URL") or "https://www.google.com/generate_204"
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=5, context=_ssl()) as r:
                if r.status < 400:
                    return PROBE_OK
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except ssl.SSLCertVerificationError as e:
            log(f"toolbox: TLS to {url} failed: {e.reason if hasattr(e, 'reason') else e}")
            return PROBE_TLS
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLCertVerificationError):
                log(f"toolbox: TLS to {url} failed: {reason}")
                return PROBE_TLS
            last = reason
        time.sleep(1)
    log(f"toolbox: cannot reach {url}: {last}")
    return PROBE_UNREACHABLE


# The toolbox serves one cluster at a time. A cached login records which one it
# belongs to - the Dex issuer and client it came from - because a token for one
# cluster is no use on another and must never be sent there. Logins cached before
# this was recorded can't be placed, so they are discarded too: one fresh sign-in.
def cluster_id():
    return {"dex": dex_url(), "client_id": client_id()}


def forget_bao():
    """Remove the OpenBao token, which belongs to whoever logged in last."""
    try:
        os.remove(bao_token_path())
    except OSError:
        pass


def _read_cache():
    try:
        with open(cache_path()) as f:
            ino = os.fstat(f.fileno()).st_ino
            obj = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict) or not obj:
        return {}
    had = obj.get("cluster")
    if had == cluster_id():
        return obj
    # Not ours: never use it. Delete it only if it is still the file just read -
    # the proxy, toolbox-token and --wait share this cache, and another of them
    # may have written a fresh login since. A pending code has its own check.
    try:
        if os.stat(cache_path()).st_ino == ino:
            os.remove(cache_path())
    except OSError:
        pass
    if not isinstance(had, dict):
        log("toolbox: the cached login is from an older toolbox that didn't record its "
            "cluster; discarded it once - approve the new URL to sign in")
    elif had.get("dex") != dex_url():
        log(f"toolbox: the cached login was for {had.get('dex')}, not {dex_url()}; "
            "discarded it - approve the new URL to sign in")
    else:
        log(f"toolbox: the cached login was for client {had.get('client_id')}, not "
            f"{client_id()}; discarded it - approve the new URL to sign in")
    return {}


def _write_private_json(path, obj):
    """Atomic, 0600 from the first byte, and safe with concurrent writers - the
    proxy, toolbox-token and --wait can all refresh at once. A fixed temp name
    would let one writer's os.replace move another's half-written file into
    place; a mode set after close would leave a world-readable window."""
    import tempfile

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".toolbox-")
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_cache(obj):
    _write_private_json(cache_path(), dict(obj, cluster=cluster_id()) if obj else {})


def ssl_context():
    """The trust store every HTTPS call here should use. Honours TOOLBOX_EXTRA_CA
    directly rather than relying on SSL_CERT_FILE from /etc/toolbox-env.sh, which
    only login shells source - `docker exec toolbox toolbox-login` is not one."""
    ctx = ssl.create_default_context()
    # Colon-separated, so the driver can hand over the host's bundle and a
    # user-supplied CA together.
    for extra in (os.environ.get("TOOLBOX_EXTRA_CA") or "").split(":"):
        if not extra or not os.path.isfile(extra):
            continue
        try:
            ctx.load_verify_locations(cafile=extra)
        except (ssl.SSLError, OSError) as e:
            log(f"toolbox: ignoring TOOLBOX_EXTRA_CA entry {extra}: {e}")
    return ctx


def _expiry(token):
    """`exp` from a JWT, unverified - verification is the server's job. We only
    need to know whether it is still worth sending."""
    try:
        import base64

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except Exception:
        return 0


def _valid(token):
    return bool(token) and _expiry(token) - EXPIRY_SKEW > time.time()


def _refresh(refresh_token, quiet=False):
    status, body = _post(
        "/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id(),
        },
    )
    if status == 200 and body.get("id_token"):
        return body
    if not quiet:
        log(f"toolbox: refresh failed ({status}); falling back to a browser login")
    return None


def pending_path():
    return cache_path() + ".pending"


# A pending code with less time than this left is not worth handing out again.
PENDING_REUSE_MIN = 30


def begin_device_flow(force=False):
    """Ask Dex for a device code and persist it. Returns the pending record.

    Split from the wait so a caller can print the URL and return immediately -
    an agent driving this over docker exec needs the URL in hand within one
    command, not after a sleep-and-hope.

    Idempotent: if a code is already pending and still has time on it, hand back
    the same one. Minting a fresh code on every call would orphan the URL the
    human already has open - they approve it, and --wait polls a different code
    forever.
    """
    if not force:
        live = _read_pending()
        if live and live.get("expires_at", 0) - time.time() > PENDING_REUSE_MIN:
            return live
    status, body = _post(
        "/device/code",
        {
            "client_id": client_id(),
            "scope": "openid profile email groups offline_access",
        },
    )
    if status != 200:
        sys.exit(f"toolbox: could not start device flow: {status} {body}")

    pending = {
        "device_code": body["device_code"],
        "user_code": body["user_code"],
        "url": body["verification_uri_complete"],
        "interval": body.get("interval", 5),
        "expires_at": time.time() + body.get("expires_in", 300),
        "force": force,
        "cluster": cluster_id(),
    }
    _write_private_json(pending_path(), pending)
    return pending


def _read_pending():
    try:
        with open(pending_path()) as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("cluster") != cluster_id():
        _clear_pending()   # a code from another cluster's Dex can never be approved here
        return None
    return obj


def _clear_pending():
    try:
        os.remove(pending_path())
    except OSError:
        pass


def wait_device_flow(pending, max_seconds=None):
    """Poll Dex until the human approves.

    Returns the token response, or None if max_seconds elapsed first with the
    code still valid - the caller can simply call again. Terminal failures
    (expired, denied) exit, and drop the pending record so the next --begin
    mints a fresh code instead of re-polling a dead one.

    Polls before sleeping: by the time --wait runs, --begin was at least one
    agent turn ago and the human has often already approved, so a leading
    sleep is a guaranteed five seconds of nothing.
    """
    interval = pending.get("interval", 5)
    stop = None if max_seconds is None else time.time() + max_seconds
    while time.time() < pending["expires_at"]:
        status, tok = _post(
            "/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": pending["device_code"],
                "client_id": client_id(),
            },
        )
        if status == 200 and tok.get("id_token"):
            _clear_pending()
            return tok
        err = tok.get("error")
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            _clear_pending()
            sys.exit(f"toolbox: login {err or 'failed'}: {tok.get('error_description') or tok} - run toolbox-login --begin again")
        if stop is not None and time.time() + interval >= stop:
            return None
        time.sleep(interval)

    _clear_pending()
    sys.exit("toolbox: the code expired before it was approved - run toolbox-login --begin again")


def _device_flow():
    pending = begin_device_flow()
    log("")
    log("  Open this URL in your browser and approve with GitHub:")
    log(f"    {pending['url']}")
    log("")
    log(f"  code {pending['user_code']} - expires in 5 minutes")
    log("")
    tok = wait_device_flow(pending)
    log("  Authenticated.")
    return tok


def save_token(tok, cache):
    # Dex does not always return a new refresh token on refresh; keep the old one.
    if not tok.get("refresh_token") and cache.get("refresh_token"):
        tok["refresh_token"] = cache["refresh_token"]
    _write_cache(tok)
    return tok["id_token"]


def get_token(force_login=False, interactive=True):
    """A valid Dex id_token: from cache, by silent refresh, or by browser login.

    With interactive=False, returns None rather than starting a browser login.
    The proxy uses that: a device-flow prompt printed from a background process
    while the CLI hangs is bewildering, and concurrent requests would each start
    their own flow. Interactive login belongs in `toolbox-login`.
    """
    cache = {} if force_login else _read_cache()

    if _valid(cache.get("id_token")):
        return cache["id_token"]

    tok = None
    if not force_login and cache.get("refresh_token"):
        tok = _refresh(cache["refresh_token"], quiet=not interactive)
    if tok is None:
        if not interactive:
            return None
        tok = _device_flow()

    return save_token(tok, cache)


# ---------------------------------------------------------------------------
# OpenBao
# ---------------------------------------------------------------------------
# OpenBao needs its own token; the Dex token only gets us past the edge. Exchange
# one for the other so a single `toolbox-login` leaves both CLIs usable.
#
# This posts to the login endpoint directly rather than using `bao login`: the
# OpenBao CLI registers no `jwt` auth method (only `oidc`, which is the browser
# redirect flow), so `-method=jwt` fails with "Unknown auth method".
#
# The CLI has its own mount (auth/jwt) separate from the web UI's (auth/oidc), so
# its roles can carry the same names as the policies they grant.


def bao_roles():
    return [
        r.strip()
        for r in _env("TOOLBOX_BAO_ROLES", "editor,reader").split(",")
        if r.strip()
    ]


def bao_auth_path():
    return _env("TOOLBOX_BAO_AUTH_PATH", "jwt").strip("/")


def bao_addr():
    return _env("BAO_ADDR", "http://127.0.0.1:8200")


def bao_token_path():
    return os.path.expanduser(_env("BAO_TOKEN_PATH", "~/.vault-token"))


def bao_login(id_token):
    """Exchange the Dex token for an OpenBao token. Returns the role used, or None.

    Roles are tried most-privileged first; the role's bound_claims decide which
    one a given user is actually entitled to, so a rejection here is expected and
    not an error.
    """
    last = None
    for role in bao_roles():
        body = json.dumps({"role": role, "jwt": id_token}).encode()
        req = urllib.request.Request(
            f"{bao_addr()}/v1/auth/{bao_auth_path()}/login", data=body, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            last = e.read().decode(errors="replace")[:200]
            continue
        except urllib.error.URLError as e:
            log(f"toolbox: cannot reach OpenBao at {bao_addr()}: {e.reason}")
            return None

        tok = (data.get("auth") or {}).get("client_token")
        if tok:
            p = bao_token_path()
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w") as f:
                f.write(tok)
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            return role

    if last:
        log(f"toolbox: OpenBao login failed for {bao_roles()}: {last}")
    return None
