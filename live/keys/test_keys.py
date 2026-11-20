"""Tests for keys.py. Stdlib only: python3 -m unittest discover -s live/keys"""

import copy
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
    MediaMTXClient,
    UpstreamError,
    authentik_group_fetcher,
    make_auth_server,
    make_keys_server,
)


class FakeMembers:
    def __init__(self, *users: str) -> None:
        self.users = set(users)

    def __call__(self, user: str) -> bool:
        return user in self.users


class FakeApi:
    """In-memory MediaMTX API: GETs answer from `data`, POSTs are recorded."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.posts: list[str] = []
        self.gone: set[str] = set()
        self.down = False

    def get(self, path: str) -> object:
        if self.down:
            raise UpstreamError(502)
        if path not in self.data:
            raise UpstreamError(404)
        return copy.deepcopy(self.data[path])

    def post(self, path: str) -> None:
        self.posts.append(path)
        if path in self.gone:
            raise UpstreamError(404)


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

    def do_GET(self) -> None:
        data = json.dumps({"path": self.path}).encode()
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        type(self).posts.append(self.path)
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class MediaMTXClientTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeMediaMTX.posts = []
        FakeMediaMTX.status = 200

    def test_get_and_post(self) -> None:
        with serve(FakeMediaMTX) as url:
            api = MediaMTXClient(url)
            self.assertEqual(api.get("/v3/paths/list"), {"path": "/v3/paths/list"})
            api.post("/v3/rtmpconns/kick/x")
        self.assertEqual(FakeMediaMTX.posts, ["/v3/rtmpconns/kick/x"])

    def test_http_errors_carry_the_status(self) -> None:
        FakeMediaMTX.status = 404
        with serve(FakeMediaMTX) as url:
            api = MediaMTXClient(url)
            with self.assertRaises(UpstreamError) as got:
                api.get("/v3/rtmpconns/get/x")
            self.assertEqual(got.exception.status, 404)
            with self.assertRaises(UpstreamError) as got:
                api.post("/v3/rtmpconns/kick/x")
            self.assertEqual(got.exception.status, 404)

    def test_unreachable_is_bad_gateway(self) -> None:
        with serve(FakeMediaMTX) as url:
            pass  # shut down: the port now refuses connections
        with self.assertRaises(UpstreamError) as got:
            MediaMTXClient(url, timeout=2).get("/v3/paths/list")
        self.assertEqual(got.exception.status, 502)


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
        self.api = FakeApi()
        self.app = App(
            self.store,
            FakeMembers("alice"),
            FakeMembers(),
            self.api,
            Path(self.dir.name) / "conns.json",
        )
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

    def test_reset_during_the_membership_check_wins(self) -> None:
        # The key is reset while a publish with it waits on Authentik: the
        # publish must not be allowed (and recorded) after the reset kicked.
        def members(user: str) -> bool:
            self.app.reset_key(user)
            return True

        self.app.members = members
        self.assertFalse(self.app.authorize(publish(self.alice, id="late")))
        self.app.members = FakeMembers("alice")
        self.app.reset_key("alice")
        self.assertEqual(self.api.posts, [])

    def test_reset_kicks_the_users_streams_only(self) -> None:
        self.app.members = FakeMembers("alice", "bob")
        self.app.authorize(publish(self.alice, id="a1", protocol="rtmp"))
        self.app.authorize(publish(self.alice, id="a2", protocol="webrtc"))
        self.app.authorize(publish(self.bob, id="b1", protocol="rtmp"))
        new = self.app.reset_key("alice")
        self.assertEqual(
            self.api.posts,
            ["/v3/rtmpconns/kick/a1", "/v3/webrtcsessions/kick/a2"],
        )
        self.assertEqual(self.store.get("alice"), new)
        self.assertIsNone(self.app.owner_of("a1"))
        self.assertEqual(self.app.owner_of("b1"), "bob")
        self.api.posts.clear()
        self.app.reset_key("alice")
        self.assertEqual(self.api.posts, [])  # already kicked, forgotten

    def test_reset_kicks_rtmps_streams(self) -> None:
        # MediaMTX reports RTMPS publishes as protocol "rtmp" too, but lists
        # (and kicks) them under rtmpsconns.
        self.app.authorize(publish(self.alice, id="s1", protocol="rtmp"))
        self.api.gone.add("/v3/rtmpconns/kick/s1")
        with self.assertNoLogs("keys", "WARNING"):
            self.app.reset_key("alice")
        self.assertEqual(
            self.api.posts, ["/v3/rtmpconns/kick/s1", "/v3/rtmpsconns/kick/s1"]
        )

    def test_reset_tolerates_ended_and_unkickable_streams(self) -> None:
        self.app.authorize(publish(self.alice, id="gone", protocol="rtmp"))
        self.app.authorize(publish(self.alice, id="h", protocol="hls"))
        self.api.gone.add("/v3/rtmpconns/kick/gone")
        with self.assertLogs("keys", "WARNING") as logs:
            self.app.reset_key("alice")
        self.assertEqual(len([m for m in logs.output if "WARNING" in m]), 1)  # hls

    def test_owner_of(self) -> None:
        self.app.authorize(publish(self.alice, id="a1"))
        self.app.authorize(publish(self.bob, id="b1"))  # bob is no streamer: denied
        self.assertEqual(self.app.owner_of("a1"), "alice")
        self.assertIsNone(self.app.owner_of("b1"))
        self.assertIsNone(self.app.owner_of("unknown"))


A = "00000000-0000-4000-8000-00000000000a"  # alice publishes "show"
B = "00000000-0000-4000-8000-00000000000b"  # bob publishes "bobshow"
R = "00000000-0000-4000-8000-00000000000c"  # viewer of "show"
S = "00000000-0000-4000-8000-00000000000d"  # viewer of "bobshow"


class ApiFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.store = KeyStore(Path(self.dir.name) / "keys.json")
        self.api = FakeApi()
        self.app = App(
            self.store,
            FakeMembers("alice", "bob"),
            FakeMembers(),
            self.api,
            Path(self.dir.name) / "conns.json",
        )
        alice, bob = self.store.reset("alice"), self.store.reset("bob")
        self.app.authorize(publish(alice, id=A, path="show"))
        self.app.authorize(publish(bob, id=B, path="bobshow"))
        self.api.data.update(
            {
                f"/v3/rtmpconns/get/{A}": {"id": A, "path": "show"},
                f"/v3/rtmpconns/get/{B}": {"id": B, "path": "bobshow"},
                f"/v3/rtspsessions/get/{R}": {"id": R, "path": "show"},
                f"/v3/rtspsessions/get/{S}": {"id": S, "path": "bobshow"},
                "/v3/paths/get/show": {"source": {"type": "rtmpConn", "id": A}},
                "/v3/paths/get/bobshow": {"source": {"type": "rtmpConn", "id": B}},
            }
        )

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_lists_hide_queries_and_name_the_streamer(self) -> None:
        self.api.data["/v3/rtmpconns/list"] = {
            "itemCount": 3,
            "items": [
                {"id": A, "state": "publish", "query": "pass=SECRET", "user": ""},
                {"id": B, "state": "publish", "query": "pass=SECRET2", "user": "x"},
                {"id": R, "state": "read", "query": "user=alice", "user": "alice"},
            ],
        }
        status, body = self.app.api_list("/v3/rtmpconns/list")
        self.assertEqual(status, 200)
        self.assertNotIn("SECRET", json.dumps(body))
        assert isinstance(body, dict)
        self.assertEqual(
            [(i["query"], i["user"]) for i in body["items"]],
            [("", "alice"), ("", "bob"), ("", "")],  # client-sent user= not trusted
        )
        self.assertEqual(body["itemCount"], 3)

    def test_rtmps_list_hides_queries_and_names_the_streamer(self) -> None:
        self.api.data["/v3/rtmpsconns/list"] = {
            "items": [
                {"id": A, "state": "publish", "query": "pass=SECRET", "user": ""}
            ],
        }
        status, body = self.app.api_list("/v3/rtmpsconns/list")
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {"items": [{"id": A, "state": "publish", "query": "", "user": "alice"}]},
        )

    def test_paths_list_passes_through(self) -> None:
        self.api.data["/v3/paths/list"] = {"items": [{"name": "show", "ready": True}]}
        self.assertEqual(
            self.app.api_list("/v3/paths/list"),
            (200, {"items": [{"name": "show", "ready": True}]}),
        )

    def test_only_the_panels_lists(self) -> None:
        self.api.data["/v3/config/global/get"] = {"authHTTPAddress": "x"}
        for path in ("/v3/config/global/get", f"/v3/rtmpconns/get/{A}", "/v3/x"):
            with self.subTest(path=path):
                self.assertEqual(self.app.api_list(path), (404, None))

    def test_upstream_failure(self) -> None:
        self.assertEqual(self.app.api_list("/v3/rtspsessions/list")[0], 404)
        self.api.data["/v3/webrtcsessions/list"] = "not a list response"
        self.assertEqual(self.app.api_list("/v3/webrtcsessions/list")[0], 502)

    def test_kick_own_stream(self) -> None:
        self.assertEqual(self.app.api_kick("alice", "rtmpconns", A), 200)
        self.assertEqual(self.api.posts, [f"/v3/rtmpconns/kick/{A}"])

    def test_kick_own_rtmps_stream(self) -> None:
        self.assertEqual(self.app.api_kick("alice", "rtmpsconns", A), 200)
        self.assertEqual(self.api.posts, [f"/v3/rtmpsconns/kick/{A}"])

    def test_kick_viewer_of_own_stream(self) -> None:
        self.assertEqual(self.app.api_kick("alice", "rtspsessions", R), 200)
        self.assertEqual(self.api.posts, [f"/v3/rtspsessions/kick/{R}"])

    def test_admins_kick_any_stream_or_viewer(self) -> None:
        self.app.admins = FakeMembers("alice")
        self.assertEqual(self.app.api_kick("alice", "rtmpconns", B), 200)
        self.assertEqual(self.app.api_kick("alice", "rtspsessions", S), 200)
        self.assertEqual(
            self.api.posts, [f"/v3/rtmpconns/kick/{B}", f"/v3/rtspsessions/kick/{S}"]
        )
        self.assertEqual(self.app.api_kick("alice", "config", B), 404)

    def test_cannot_kick_others(self) -> None:
        for kind, conn in (("rtmpconns", B), ("rtspsessions", S)):
            with self.subTest(conn=conn):
                self.assertEqual(self.app.api_kick("alice", kind, conn), 403)
        self.assertEqual(self.api.posts, [])

    def test_kick_unknown(self) -> None:
        unknown = "00000000-0000-4000-8000-0000000000ff"
        self.assertEqual(self.app.api_kick("alice", "rtmpconns", unknown), 404)
        for kind, conn in (("srtconns", A), ("config", A), ("rtmpconns", "../x")):
            with self.subTest(kind=kind, conn=conn):
                self.assertEqual(self.app.api_kick("alice", kind, conn), 404)
        self.assertEqual(self.api.posts, [])

    def test_kick_takes_the_canonical_id_only(self) -> None:
        self.assertEqual(self.app.api_kick("alice", "rtmpconns", A.upper()), 200)
        self.assertEqual(self.app.api_kick("alice", "rtmpconns", "urn:uuid:" + A), 200)
        self.assertEqual(self.api.posts, [f"/v3/rtmpconns/kick/{A}"] * 2)

    def test_kick_already_gone(self) -> None:
        self.api.gone.add(f"/v3/rtmpconns/kick/{A}")
        self.assertEqual(self.app.api_kick("alice", "rtmpconns", A), 404)


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


def live(*ids: str) -> dict[str, object]:
    return {"items": [{"id": i} for i in ids]}


class ConnRecordTest(unittest.TestCase):
    """Who published on which connection: kept across restarts, pruned by
    asking MediaMTX which connections are still open."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.store = KeyStore(Path(self.dir.name) / "keys.json")
        self.api = FakeApi()
        self.app = self.new_app()
        self.alice = self.store.reset("alice")
        self.bob = self.store.reset("bob")
        self.api.data.update(
            {
                "/v3/rtmpconns/list": live(),
                "/v3/rtspsessions/list": live(),
                "/v3/webrtcsessions/list": live(),
            }
        )

    def tearDown(self) -> None:
        self.dir.cleanup()

    def new_app(self) -> App:
        conns = Path(self.dir.name) / "conns.json"
        return App(
            self.store, FakeMembers("alice", "bob"), FakeMembers(), self.api, conns
        )

    def test_a_restart_keeps_the_record(self) -> None:
        self.app.authorize(publish(self.alice, id="a1", protocol="rtmp"))
        restarted = self.new_app()
        self.assertEqual(restarted.owner_of("a1"), "alice")
        restarted.reset_key("alice")
        self.assertEqual(self.api.posts, ["/v3/rtmpconns/kick/a1"])

    def test_a_bad_record_file_starts_empty(self) -> None:
        (Path(self.dir.name) / "conns.json").write_text("[1]")
        with self.assertLogs("keys", "WARNING"):
            app = self.new_app()
        self.assertIsNone(app.owner_of("a1"))

    def test_many_other_publishes_do_not_push_a_stream_out(self) -> None:
        self.app.authorize(publish(self.alice, id="a1"))
        for i in range(1500):
            self.app.authorize(publish(self.bob, id=f"b{i}"))
        self.assertEqual(self.app.owner_of("a1"), "alice")

    def test_prune_drops_ended_connections_only(self) -> None:
        for conn_id, protocol in (("a1", "rtmp"), ("a2", "rtsp"), ("a3", "webrtc")):
            self.app.authorize(publish(self.alice, id=conn_id, protocol=protocol))
        self.app.authorize(publish(self.bob, id="b1"))
        self.api.data["/v3/rtmpconns/list"] = live("a1")
        self.api.data["/v3/webrtcsessions/list"] = live("a3", "viewer")
        self.app.prune()
        for app in (self.app, self.new_app()):  # pruned on disk too
            with self.subTest(app=app):
                owners = [app.owner_of(i) for i in ("a1", "a2", "a3", "b1")]
                self.assertEqual(owners, ["alice", None, "alice", None])

    def test_prune_keeps_open_rtmps_connections(self) -> None:
        # Recorded as protocol "rtmp", but listed under rtmpsconns.
        self.app.authorize(publish(self.alice, id="s1", protocol="rtmp"))
        self.api.data["/v3/rtmpsconns/list"] = live("s1")
        self.app.prune()
        self.assertEqual(self.app.owner_of("s1"), "alice")

    def test_a_disabled_server_has_no_connections(self) -> None:
        # srt is off in mediamtx.yml, so MediaMTX answers 404 for its list.
        self.app.authorize(publish(self.alice, id="s1", protocol="srt"))
        self.app.prune()
        self.assertIsNone(self.app.owner_of("s1"))

    def test_prune_keeps_everything_if_mediamtx_does_not_answer(self) -> None:
        self.app.authorize(publish(self.alice, id="a1"))
        for broken in ("down", "not a list"):
            with self.subTest(broken=broken):
                self.api.down = broken == "down"
                if broken == "not a list":
                    self.api.data["/v3/rtspsessions/list"] = {"items": None}
                with self.assertLogs("keys", "WARNING"):
                    self.app.prune()
                self.assertEqual(self.app.owner_of("a1"), "alice")

    def test_prune_keeps_connections_recorded_while_it_runs(self) -> None:
        # The lists are read before a1 is recorded, so they cannot show it.
        get = self.api.get

        def get_then_publish(path: str) -> object:
            data = get(path)
            if self.app.owner_of("a1") is None:
                self.app.authorize(publish(self.alice, id="a1"))
            return data

        self.api.get = get_then_publish  # type: ignore[method-assign]
        self.app.prune()
        self.assertEqual(self.app.owner_of("a1"), "alice")


class ServersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.store = KeyStore(Path(self.dir.name) / "keys.json")
        self.api = FakeApi()
        self.app = App(
            self.store,
            FakeMembers("alice"),
            FakeMembers(),
            self.api,
            Path(self.dir.name) / "conns.json",
        )

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
            self.assertEqual(
                json.loads(body), {"user": "alice", "key": None, "admin": False}
            )
            self.app.admins = FakeMembers("alice")
            self.assertTrue(json.loads(Http.request(me, headers=alice)[2])["admin"])

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

    def test_api_endpoint(self) -> None:
        key = self.store.reset("alice")
        self.app.authorize(publish(key, id=A, path="show"))
        self.api.data["/v3/rtmpconns/list"] = {
            "items": [{"id": A, "query": "pass=" + key, "user": ""}]
        }
        alice = {"X-Authentik-Username": "alice"}
        panel = {**alice, "X-Requested-With": "live-panel"}
        with running(make_keys_server(("127.0.0.1", 0), self.app)) as url:
            lst = url + "/api/v3/rtmpconns/list"
            self.assertEqual(Http.request(lst)[0], 401)
            status, headers, body = Http.request(lst + "?itemsPerPage=5", headers=alice)
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            self.assertNotIn(key, body.decode())
            self.assertEqual(json.loads(body)["items"][0]["user"], "alice")
            self.assertEqual(
                Http.request(url + "/api/v3/config/global/get", headers=alice)[0], 404
            )

            kick = f"{url}/api/v3/rtmpconns/kick/{A}"
            self.assertEqual(Http.request(kick, "POST", b"", alice)[0], 403)
            self.assertEqual(self.api.posts, [])
            self.assertEqual(Http.request(kick, "POST", b"", panel)[0], 200)
            self.assertEqual(self.api.posts, [f"/v3/rtmpconns/kick/{A}"])


if __name__ == "__main__":
    unittest.main()
