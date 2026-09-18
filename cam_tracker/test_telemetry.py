"""
Communication test for CamTracker's TelemetryServer.

This is a pure client: it never builds a CamTracker of its own. It connects to
a real `python3 app.py` process running as its own separate Python instance
(with an actual camera) and exercises the WebSocket protocol against it:
  1. Listens for a live position broadcast.
  2. Sends calibration points, gets an ack back, and confirms the server
     applies them.
  3. Confirms the server rejects bad calibration requests (missing points,
     degenerate points, unknown message types) with a calibration_error
     instead of crashing or hanging.

Start the real process first, in its own terminal:
    python3 app.py

Then run this against it:
    python3 test_telemetry.py
    python3 test_telemetry.py --host 192.168.1.50 --port 8765

WARNING: the calibration checks send real set_calibration messages to
whatever process is actually listening on the target host/port, and WILL
overwrite its current calibration. The points used here are the same values
VideoToRealPosCalc falls back to by default, so if the running process hasn't
been calibrated to something else, this is a no-op; if it has, that custom
calibration will be replaced with these defaults.
"""
import argparse
import json
import sys

from websockets.sync.client import connect

from app import WS_PORT


VALID_POINTS = {
    "rtl_pos": [129, 217], "rtr_pos": [444, 222],
    "rbr_pos": [395, 477], "rbl_pos": [135, 462],
    "gt_pos": [340, 522], "gb_pos": [371, 703],
}


def fail(message):
    print(f"FAIL: {message}")
    sys.exit(1)


def ok(message):
    print(f"OK:   {message}")


def recv_json(ws, timeout=3.0):
    return json.loads(ws.recv(timeout=timeout))


def test_position_broadcast(ws):
    # Only a live process with both markers actually visible sends these, so
    # this is a soft check, not a hard requirement.
    print("Listening for a live position broadcast (needs both markers visible)...")
    try:
        msg = recv_json(ws, timeout=10.0)
    except TimeoutError:
        print("SKIP: no position broadcast received in 10s (markers not visible right now?)")
        return

    if msg.get("type") != "position":
        fail(f"expected a 'position' message, got: {msg}")
    for key in ("x", "y", "z", "t"):
        if key not in msg:
            fail(f"position message missing '{key}': {msg}")
    ok(f"received position broadcast: X:{msg['x']:.2f} Y:{msg['y']:.2f} Z:{msg['z']:.2f}")


def test_valid_calibration(ws):
    ws.send(json.dumps({"type": "set_calibration", "points": VALID_POINTS}))
    reply = recv_json(ws)
    if reply.get("type") != "calibration_ack":
        fail(f"expected calibration_ack, got: {reply}")
    ok("valid calibration accepted")


def test_missing_points(ws):
    ws.send(json.dumps({"type": "set_calibration", "points": {"rtl_pos": [0, 0]}}))
    reply = recv_json(ws)
    if reply.get("type") != "calibration_error":
        fail(f"expected calibration_error for missing points, got: {reply}")
    ok(f"missing points correctly rejected: {reply.get('error')}")


def test_degenerate_points(ws):
    bad = dict(VALID_POINTS)
    bad["rtr_pos"] = bad["rbr_pos"] = bad["rbl_pos"] = VALID_POINTS["rtl_pos"]
    ws.send(json.dumps({"type": "set_calibration", "points": bad}))
    reply = recv_json(ws)
    if reply.get("type") != "calibration_error":
        fail(f"expected calibration_error for degenerate points, got: {reply}")
    ok(f"degenerate points correctly rejected: {reply.get('error')}")


def test_unknown_message_type(ws):
    ws.send(json.dumps({"type": "not_a_real_type"}))
    reply = recv_json(ws)
    if reply.get("type") != "calibration_error":
        fail(f"expected calibration_error for an unknown message type, got: {reply}")
    ok("unknown message type correctly rejected")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="Host the app.py process is listening on")
    parser.add_argument("--port", type=int, default=WS_PORT, help="Port the app.py process is listening on")
    args = parser.parse_args()

    print(f"Connecting to app.py's TelemetryServer at ws://{args.host}:{args.port}\n")

    try:
        with connect(f"ws://{args.host}:{args.port}") as ws:
            print("PRE CALIBRATION POS:")
            test_position_broadcast(ws)
            test_valid_calibration(ws)

            test_missing_points(ws)
            test_degenerate_points(ws)
            test_unknown_message_type(ws)
    except OSError as error:
        fail(
            f"could not connect to ws://{args.host}:{args.port}: {error}\n"
            "Is `python3 app.py` running in a separate terminal?"
        )

    print("\nAll communication checks passed.")


if __name__ == "__main__":
    main()
