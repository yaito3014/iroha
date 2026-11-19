"""Tests for keys.py. Stdlib only: python3 -m unittest discover -s live/keys"""

import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from keys import (
    App,
    GroupMembers,
    KeyStore,
    authentik_group_fetcher,
    make_auth_server,
    make_keys_server,
    mediamtx_kicker,
)


class FakeMembers:
    def __init__(self, *users: str) -> None:
        self.users = set(users)

    def __call__(self, user: str) -> bool:
        return user in self.users


class FakeKicker:
    def __init__(self) -> None:
        self.kicked: list[tuple[str, str]] = []

    def __call__(self, protocol: str, conn_id: str) -> None:
        self.kicked.append((protocol, conn_id))


@contextmanager
def tmp_store() -> Iterator[KeyStore]:
    with tempfile.TemporaryDirectory() as d:
        yield KeyStore(Path(d) / "keys.json")


class KeyStoreTest(unittest.TestCase):
    def test_unknown_user_has_no_key(self) -> None:
        with tmp_store() as store:
            self.assertIsNone(store.get("alice"))

    def test_reset_issues_a_new_url_safe_key(self) -> None:
        with tmp_store() as store:
            first = store.reset("alice")
            self.assertGreaterEqual(len(first), 32)
            self.assertRegex(first, r"^[A-Za-z0-9_-]+$")
            self.assertEqual(store.get("alice"), first)
            second = store.reset("alice")
            self.assertNotEqual(first, second)
            self.assertEqual(store.get("alice"), second)

    def test_owner(self) -> None:
        with tmp_store() as store:
            alice, bob = store.reset("alice"), store.reset("bob")
            self.assertEqual(store.owner(alice), "alice")
            self.assertEqual(store.owner(bob), "bob")
            for key in ("", "nope", alice[:-1]):
                with self.subTest(key=key):
                    self.assertIsNone(store.owner(key))

    def test_reset_revokes_the_old_key(self) -> None:
        with tmp_store() as store:
            old = store.reset("alice")
            store.reset("alice")
            self.assertIsNone(store.owner(old))

    def test_persists_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "keys.json"
            key = KeyStore(path).reset("alice")
            self.assertEqual(KeyStore(path).owner(key), "alice")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class GroupMembersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.calls = 0
        self.result: set[str] | Exception = {"alice"}

    def fetch(self) -> set[str]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return set(self.result)

    def members(self) -> GroupMembers:
        return GroupMembers(self.fetch, ttl=60, max_stale=3600, clock=self.clock)

    def test_caches_within_ttl(self) -> None:
        m = self.members()
        self.assertTrue(m("alice"))
        self.assertFalse(m("bob"))
        self.clock.now += 59
        self.assertTrue(m("alice"))
        self.assertEqual(self.calls, 1)

    def test_refetches_after_ttl(self) -> None:
        m = self.members()
        m("alice")
        self.result = {"bob"}
        self.clock.now += 61
        self.assertFalse(m("alice"))
        self.assertTrue(m("bob"))
        self.assertEqual(self.calls, 2)

    def test_outage_uses_last_good_answer_until_max_stale(self) -> None:
        m = self.members()
        m("alice")
        self.result = OSError("down")
        self.clock.now += 61
        with self.assertLogs("keys", "WARNING"):
            self.assertTrue(m("alice"))
        self.clock.now += 3600
        with self.assertLogs("keys", "WARNING"):
            self.assertFalse(m("alice"))

    def test_outage_without_any_answer_denies(self) -> None:
        self.result = OSError("down")
        with self.assertLogs("keys", "WARNING"):
            self.assertFalse(self.members()("alice"))


@contextmanager
def serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class FakeAuthentik(BaseHTTPRequestHandler):
    requests: list[tuple[str, str | None]] = []
    status = 200
    body: object = {}

    def do_GET(self) -> None:
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        data = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


class AuthentikGroupFetcherTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeAuthentik.requests = []
        FakeAuthentik.status = 200

    def test_active_members_of_the_named_group(self) -> None:
        users = [
            {"username": "alice", "is_active": True},
            {"username": "bob", "is_active": False},
            {"username": "carol", "is_active": True},
        ]
        FakeAuthentik.body = {"results": [{"name": "streamers", "users_obj": users}]}
        with serve(FakeAuthentik) as url:
            got = authentik_group_fetcher(url, "streamers", "tok")()
        self.assertEqual(got, {"alice", "carol"})
        self.assertEqual(
            FakeAuthentik.requests,
            [
                (
                    "/api/v3/core/groups/?name=streamers&include_users=true",
                    "Bearer tok",
                )
            ],
        )

    def test_missing_group_means_nobody(self) -> None:
        FakeAuthentik.body = {"results": []}
        with serve(FakeAuthentik) as url:
            self.assertEqual(authentik_group_fetcher(url, "streamers", "tok")(), set())

    def test_errors_raise(self) -> None:
        FakeAuthentik.status = 403
        FakeAuthentik.body = {"detail": "nope"}
        with serve(FakeAuthentik) as url, self.assertRaises(OSError):
            authentik_group_fetcher(url, "streamers", "tok")()


class FakeMediaMTX(BaseHTTPRequestHandler):
    posts: list[str] = []
    status = 200

    def do_POST(self) -> None:
        type(self).posts.append(self.path)
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class MediaMTXKickerTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeMediaMTX.posts = []
        FakeMediaMTX.status = 200

    def test_kick_endpoint_per_protocol(self) -> None:
        with serve(FakeMediaMTX) as url:
            kick = mediamtx_kicker(url)
            for proto in ("rtmp", "rtsp", "webrtc", "srt"):
                kick(proto, "id-" + proto)
        self.assertEqual(
            FakeMediaMTX.posts,
            [
                "/v3/rtmpconns/kick/id-rtmp",
                "/v3/rtspsessions/kick/id-rtsp",
                "/v3/webrtcsessions/kick/id-webrtc",
                "/v3/srtconns/kick/id-srt",
            ],
        )

    def test_gone_connection_is_fine(self) -> None:
        FakeMediaMTX.status = 404
        with serve(FakeMediaMTX) as url:
            mediamtx_kicker(url)("rtmp", "gone")  # no exception, no warning

    def test_unknown_protocol_is_skipped(self) -> None:
        with serve(FakeMediaMTX) as url, self.assertLogs("keys", "WARNING"):
            mediamtx_kicker(url)("hls", "x")
        self.assertEqual(FakeMediaMTX.posts, [])


def publish(key: str, **extra: object) -> dict[str, object]:
    req: dict[str, object] = {
        "action": "publish",
        "path": "anything",
        "user": "",
        "password": key,
        "ip": "203.0.113.9",
        "protocol": "rtmp",
        "id": "conn-1",
    }
    req.update(extra)
    return req


class AuthorizeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.store = KeyStore(Path(self.dir.name) / "keys.json")
        self.kicker = FakeKicker()
        self.app = App(self.store, FakeMembers("alice"), self.kicker)
        self.alice = self.store.reset("alice")
        self.bob = self.store.reset("bob")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_streamers_key_publishes_to_any_path(self) -> None:
        for path in ("anything", "alice", "bob", "a/b"):
            with self.subTest(path=path):
                self.assertTrue(self.app.authorize(publish(self.alice, path=path)))

    def test_bearer_token_publishes_too(self) -> None:
        # WHIP (e.g. OBS's WHIP output) sends the key as a Bearer token, which
        # MediaMTX passes as "token" with an empty "password".
        req = publish("", token=self.alice, protocol="webrtc")
        self.assertTrue(self.app.authorize(req))

    def test_user_field_is_ignored(self) -> None:
        self.assertTrue(self.app.authorize(publish(self.alice, user="bob")))

    def test_key_of_a_non_member(self) -> None:
        self.assertFalse(self.app.authorize(publish(self.bob)))

    def test_unknown_or_missing_key(self) -> None:
        for key in ("", "nope", self.alice.upper()):
            with self.subTest(key=key):
                self.assertFalse(self.app.authorize(publish(key)))

    def test_api_from_local_and_docker_network_only(self) -> None:
        for ip in ("127.0.0.1", "::1", "172.18.0.5", "::ffff:172.18.0.5"):
            with self.subTest(ip=ip):
                self.assertTrue(self.app.authorize({"action": "api", "ip": ip}))
        for bad in ("203.0.113.9", "127.0.0.2", "172.32.0.1", "", None):
            with self.subTest(ip=bad):
                self.assertFalse(self.app.authorize({"action": "api", "ip": bad}))

    def test_other_actions_denied(self) -> None:
        # read is excluded in mediamtx.yml and never reaches the hook; if it
        # does, the config is wrong, so fail closed.
        for action in ("read", "playback", "metrics", "pprof", "", None):
            with self.subTest(action=action):
                self.assertFalse(self.app.authorize(publish(self.alice, action=action)))

    def test_reset_kicks_the_users_streams_only(self) -> None:
        self.app.members = FakeMembers("alice", "bob")
        self.app.authorize(publish(self.alice, id="a1", protocol="rtmp"))
        self.app.authorize(publish(self.alice, id="a2", protocol="webrtc"))
        self.app.authorize(publish(self.bob, id="b1", protocol="rtmp"))
        new = self.app.reset_key("alice")
        self.assertEqual(self.kicker.kicked, [("rtmp", "a1"), ("webrtc", "a2")])
        self.assertEqual(self.store.get("alice"), new)
        self.kicker.kicked.clear()
        self.app.reset_key("alice")
        self.assertEqual(self.kicker.kicked, [])  # already kicked, forgotten


class Http:
    @staticmethod
    def request(
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(
            url, data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, dict(res.headers), res.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()


@contextmanager
def running(server: ThreadingHTTPServer) -> Iterator[str]:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class ServersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.store = KeyStore(Path(self.dir.name) / "keys.json")
        self.kicker = FakeKicker()
        self.app = App(self.store, FakeMembers("alice"), self.kicker)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_auth_endpoint(self) -> None:
        key = self.store.reset("alice")
        with running(make_auth_server(("127.0.0.1", 0), self.app)) as url:
            with self.assertLogs("keys", "INFO") as logs:
                ok = Http.request(
                    url + "/auth", "POST", json.dumps(publish(key)).encode()
                )
                bad = Http.request(
                    url + "/auth", "POST", json.dumps(publish("x")).encode()
                )
            self.assertEqual((ok[0], bad[0]), (200, 401))
            self.assertIn("user='alice'", "\n".join(logs.output))
            self.assertNotIn(key, "\n".join(logs.output))
            for body in (b"not json", b"[1]", b""):
                with self.subTest(body=body):
                    self.assertEqual(Http.request(url + "/auth", "POST", body)[0], 400)
            # The panel's key API is not served on the auth listener.
            self.assertEqual(
                Http.request(
                    url + "/keys/me", headers={"X-Authentik-Username": "alice"}
                )[0],
                404,
            )

    def test_keys_endpoint(self) -> None:
        with running(make_keys_server(("127.0.0.1", 0), self.app)) as url:
            me = url + "/keys/me"
            alice = {"X-Authentik-Username": "alice"}
            self.assertEqual(Http.request(me)[0], 401)
            self.assertEqual(
                Http.request(me, headers={"X-Authentik-Username": "bob"})[0], 403
            )

            status, headers, body = Http.request(me, headers=alice)
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            self.assertEqual(json.loads(body), {"user": "alice", "key": None})

            # Reset needs the panel's header (a cross-site form cannot send it).
            self.assertEqual(Http.request(me + "/reset", "POST", b"", alice)[0], 403)
            status, _, body = Http.request(
                me + "/reset", "POST", b"", {**alice, "X-Requested-With": "live-panel"}
            )
            self.assertEqual(status, 200)
            key = json.loads(body)["key"]
            self.assertEqual(self.store.owner(key), "alice")
            self.assertEqual(json.loads(Http.request(me, headers=alice)[2])["key"], key)

            # MediaMTX's auth hook is not served on the keys listener.
            self.assertEqual(Http.request(url + "/auth", "POST", b"{}")[0], 404)


if __name__ == "__main__":
    unittest.main()
