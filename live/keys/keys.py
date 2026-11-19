"""Per-user stream keys for live/: MediaMTX's auth hook plus the panel's key API.

Each member of the Authentik "streamers" group gets one stream key. OBS sends
it as `<any name>?pass=<key>`; the key alone identifies the streamer, and the
stream name is free. Two listeners keep the halves apart:

- :8000 /auth is MediaMTX's HTTP auth hook (authMethod: http). Only MediaMTX
  calls it; Caddy has no route to it.
- :8001 /keys/me is the panel's key API, reached only through Caddy's forward
  auth, which sets X-Authentik-Username (and strips any client-sent copy).

Stdlib only, so it runs on the stock python image without a build step.
"""

import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol

log = logging.getLogger("keys")

# Same networks the internal-auth config allowed for the API.
API_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("127.0.0.1/32", "::1/128", "172.16.0.0/12")
)

# MediaMTX auth "protocol" -> its API's connection list, for kicking.
KICK_ENDPOINTS = {
    "rtmp": "rtmpconns",
    "rtsp": "rtspsessions",
    "webrtc": "webrtcsessions",
    "srt": "srtconns",
}

MAX_BODY = 64 * 1024
SESSIONS_PER_USER = 32


def _field(req: Mapping[str, object], key: str) -> str:
    value = req.get(key)
    return value if isinstance(value, str) else ""


class KeyStore:
    """{username: key} in a JSON file, readable by its owner only.

    Keys are stored as-is so the panel can show them again.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._keys = self._load()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise ValueError(f"{self._path}: expected a {{user: key}} object")
        return dict(data)

    def _save(self) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(self._keys, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def get(self, user: str) -> str | None:
        with self._lock:
            return self._keys.get(user)

    def owner(self, key: str) -> str | None:
        if not key:
            return None
        with self._lock:
            items = list(self._keys.items())
        found = None
        # Compare against every key without stopping early, so timing does not
        # reveal how much of a guess matched.
        for user, candidate in items:
            if hmac.compare_digest(candidate.encode(), key.encode()):
                found = user
        return found

    def reset(self, user: str) -> str:
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._keys[user] = key
            self._save()
        return key


class GroupMembers:
    """Cached "is this user in the group" over a fetch of the member list.

    If a refresh fails, the last good list stays in use until it is max_stale
    old, so a short Authentik outage does not stop streams from starting.
    """

    def __init__(
        self,
        fetch: Callable[[], set[str]],
        ttl: float = 60,
        max_stale: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._max_stale = max_stale
        self._clock = clock
        self._lock = threading.Lock()
        self._members: frozenset[str] = frozenset()
        self._fetched_at: float | None = None

    def __call__(self, user: str) -> bool:
        now = self._clock()
        with self._lock:
            if self._fetched_at is None or now - self._fetched_at >= self._ttl:
                try:
                    self._members = frozenset(self._fetch())
                    self._fetched_at = now
                except (OSError, ValueError) as e:
                    log.warning("group lookup failed: %s", e)
                    if (
                        self._fetched_at is None
                        or now - self._fetched_at >= self._max_stale
                    ):
                        return False
            return user in self._members


def authentik_group_fetcher(
    base_url: str, group: str, token: str, timeout: float = 10
) -> Callable[[], set[str]]:
    """Active members of one Authentik group, via a token that may view it."""
    query = urllib.parse.urlencode({"name": group, "include_users": "true"})
    url = f"{base_url}/api/v3/core/groups/?{query}"

    def fetch() -> set[str]:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                data = json.load(res)
        except urllib.error.HTTPError as e:
            e.close()
            raise OSError(f"HTTP {e.code} from {url}") from None
        return {
            u["username"]
            for g in data.get("results", [])
            if g.get("name") == group
            for u in g.get("users_obj") or []
            if u.get("is_active")
        }

    return fetch


class Kicker(Protocol):
    def __call__(self, protocol: str, conn_id: str) -> None: ...


def mediamtx_kicker(api_base: str, timeout: float = 5) -> Kicker:
    def kick(protocol: str, conn_id: str) -> None:
        endpoint = KICK_ENDPOINTS.get(protocol)
        if endpoint is None:
            log.warning("cannot kick %s connection %s", protocol, conn_id)
            return
        url = f"{api_base}/v3/{endpoint}/kick/{urllib.parse.quote(conn_id)}"
        try:
            urllib.request.urlopen(
                urllib.request.Request(url, data=b"", method="POST"), timeout=timeout
            ).close()
        except urllib.error.HTTPError as e:
            e.close()
            if e.code != 404:  # 404: the connection already ended
                log.warning("kick %s %s: HTTP %d", protocol, conn_id, e.code)
        except OSError as e:
            log.warning("kick %s %s: %s", protocol, conn_id, e)

    return kick


class Members(Protocol):
    def __call__(self, user: str) -> bool: ...


class App:
    def __init__(self, store: KeyStore, members: Members, kick: Kicker) -> None:
        self.store = store
        self.members = members
        self.kick = kick
        self._lock = threading.Lock()
        # user -> (protocol, connection id) of streams they started.
        self._sessions: dict[str, list[tuple[str, str]]] = {}

    def authorize(self, req: Mapping[str, object]) -> bool:
        """Decide one MediaMTX auth request (its JSON body, decoded)."""
        action = _field(req, "action")
        user = None
        if action == "publish":
            # The query's user= is ignored: the key alone says who this is. RTMP
            # and RTSP send it as the password, WHIP as a Bearer token.
            key = _field(req, "password") or _field(req, "token")
            user = self.store.owner(key)
            ok = user is not None and self.members(user)
            if ok and user is not None:
                self._remember(user, _field(req, "protocol"), _field(req, "id"))
        elif action == "api":
            ok = _api_ip_allowed(_field(req, "ip"))
        else:
            ok = False
        # Never log the key.
        log.info(
            "%s %s path=%r user=%r ip=%s protocol=%s",
            "allow" if ok else "deny",
            action,
            _field(req, "path"),
            user,
            _field(req, "ip"),
            _field(req, "protocol"),
        )
        return ok

    def _remember(self, user: str, protocol: str, conn_id: str) -> None:
        with self._lock:
            sessions = self._sessions.setdefault(user, [])
            sessions.append((protocol, conn_id))
            del sessions[:-SESSIONS_PER_USER]

    def reset_key(self, user: str) -> str:
        """Issue a new key, then end the streams started with the old one."""
        key = self.store.reset(user)
        with self._lock:
            sessions = self._sessions.pop(user, [])
        for protocol, conn_id in sessions:
            self.kick(protocol, conn_id)
        log.info("reset key user=%r, kicked %d session(s)", user, len(sessions))
        return key


def _api_ip_allowed(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in API_NETWORKS)


class _Handler(BaseHTTPRequestHandler):
    app: App

    def reply(self, status: int, body: object = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        if body is not None:
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.reply(404)

    def do_POST(self) -> None:
        self.reply(404)

    def log_message(self, format: str, *args: object) -> None:
        pass  # decisions are logged by App instead


def make_auth_server(address: tuple[str, int], app: App) -> ThreadingHTTPServer:
    class AuthHandler(_Handler):
        def do_POST(self) -> None:
            if self.path != "/auth":
                self.reply(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(min(length, MAX_BODY)))
            except ValueError:
                req = None
            if not isinstance(req, dict):
                self.reply(400)
                return
            self.reply(200 if app.authorize(req) else 401)

    return ThreadingHTTPServer(address, AuthHandler)


def make_keys_server(address: tuple[str, int], app: App) -> ThreadingHTTPServer:
    class KeysHandler(_Handler):
        def user(self) -> str | None:
            """The caller, or None after replying with an error."""
            user = (self.headers.get("X-Authentik-Username") or "").strip()
            if not user:
                self.reply(401)
                return None
            if not app.members(user):
                self.reply(403)
                return None
            return user

        def do_GET(self) -> None:
            if self.path != "/keys/me":
                self.reply(404)
                return
            if user := self.user():
                self.reply(200, {"user": user, "key": app.store.get(user)})

        def do_POST(self) -> None:
            if self.path != "/keys/me/reset":
                self.reply(404)
                return
            if not (user := self.user()):
                return
            # A cross-site form cannot set this header without a CORS preflight,
            # so a foreign page cannot reset someone's key.
            if self.headers.get("X-Requested-With") != "live-panel":
                self.reply(403)
                return
            self.reply(200, {"user": user, "key": app.reset_key(user)})

    return ThreadingHTTPServer(address, KeysHandler)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    env = os.environ
    members = GroupMembers(
        authentik_group_fetcher(
            env["AUTHENTIK_URL"].rstrip("/"),
            env.get("AUTHENTIK_GROUP", "streamers"),
            env["AUTHENTIK_TOKEN"],
        )
    )
    app = App(
        KeyStore(Path(env.get("KEYS_FILE", "/data/keys.json"))),
        members,
        mediamtx_kicker(env.get("MEDIAMTX_API", "http://mediamtx:9997")),
    )
    auth = make_auth_server(("0.0.0.0", 8000), app)
    keys = make_keys_server(("0.0.0.0", 8001), app)
    threading.Thread(target=auth.serve_forever, daemon=True).start()
    log.info("auth hook on :8000, key API on :8001")
    keys.serve_forever()


if __name__ == "__main__":
    main()
