import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fieldbridge.core import Bridge
from fieldbridge.fixtures import make_scenario
from fieldbridge.http_api import serve_in_thread


def test_local_http_ingress_and_destination(tmp_path):
    scenario = make_scenario("atlas", "retry_duplicates", 1, 20)
    bridge = Bridge(tmp_path / "bridge.sqlite", scenario["initial"])
    server, thread = serve_in_thread(bridge)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        event = scenario["events"][0]
        request = Request(
            base + "/source/atlas/events",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            assert json.load(response)["outcome"] == "accepted"
        with urlopen(request) as response:
            assert json.load(response)["outcome"] == "duplicate_delivery"
        with urlopen(base + "/destination/state") as response:
            state = json.load(response)["state"]
        assert len(state) == 1
        bad = dict(event, tenant="beacon", event_id="new")
        request = Request(
            base + "/source/atlas/events",
            data=json.dumps(bad).encode(), method="POST",
        )
        try:
            urlopen(request)
            assert False, "source mismatch should be rejected"
        except HTTPError as exc:
            assert exc.code == 403
    finally:
        server.shutdown()
        thread.join()
        bridge.close()
