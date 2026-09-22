"""
Stand-in for cam_tracker/app.py that needs no camera and no physical printer.

Gets real X/Y/Z from OctoPrint the same way PrinterBridge does (M114 polling
+ log regex), fabricates matching red/green marker pixels, and broadcasts
through app.py's own TelemetryServer - so the extension connects to this
exactly like the real cam_tracker, calibration included.

OctoPrint can point at a real printer or its built-in Virtual Printer
(Settings > Serial Connection > Serial Port: VIRTUAL).

Usage: reads OCTO_URL/OCTO_API_KEY/OCTO_WS_URL from printer.env automatically.
    python3 fake_app.py
"""
import os
import re
import json
import math
import time
import threading
import cv2
import numpy as np
import requests
from websockets.sync.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from app import TelemetryServer, VideoToRealPosCalc, WS_HOST, WS_PORT

M114_RE = re.compile(r"X:(-?\d+\.?\d*)\s+Y:(-?\d+\.?\d*)\s+Z:(-?\d+\.?\d*)")
BROADCAST_HZ = 30  # fake "frame rate" - matches a real camera tracker's ~30fps
MAX_STEP_PER_TICK = 1.0  # mm - how far the simulated position may move per broadcast tick
PRINTER_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "printer.env")


def _load_printer_env(path=PRINTER_ENV_PATH):
    """Load `export NAME=value` lines from printer.env into os.environ.
    Already-set env vars are not overridden.
    """
    if not os.path.exists(path):
        return

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            name, sep, value = line.partition("=")
            if not sep:
                continue
            os.environ.setdefault(name.strip(), value.strip().strip("'\""))

    print(f"[fake_app] Loaded env from {path}")


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"[fake_app] {name} must be set (see printer.env) - this tool needs OctoPrint to get real X/Y/Z from.")
    return value


class FakeCamTracker:
    """Duck-types the one thing TelemetryServer actually touches on a real
    CamTracker: a `.video_to_real` attribute it can rebind on calibration."""

    def __init__(self):
        self.video_to_real = VideoToRealPosCalc()


class OctoPrintBridge:
    """Mirrors PrinterBridge's OctoPrint access (login, M114 polling, log
    regex) but synchronous/threaded - there's no Kit event loop here.
    """

    def __init__(self):
        self.octo_url = _require_env("OCTO_URL")
        self.octo_api_key = _require_env("OCTO_API_KEY")
        self.octo_ws_url = _require_env("OCTO_WS_URL")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": self.octo_api_key})
        self.username = None
        self.session_token = None
        self._lock = threading.Lock()
        self._position = (0.0, 0.0, 0.0)

    def get_position(self):
        with self._lock:
            return self._position

    def start(self):
        self._authenticate()
        threading.Thread(target=self._listen_websocket, daemon=True).start()
        threading.Thread(target=self._poll_m114, daemon=True).start()

    def _authenticate(self):
        while True:
            try:
                response = self.session.post(f"{self.octo_url}/api/login", json={"passive": True}, timeout=10)
                response.raise_for_status()
                data = response.json()
                self.username, self.session_token = data.get("name"), data.get("session")
                print(f"[fake_app] Authenticated with OctoPrint as {self.username!r}")
                return
            except Exception as error:
                print(f"[fake_app] Failed to fetch OctoPrint session token: {error}, retrying...")
                time.sleep(1)

    def _poll_m114(self):
        url = f"{self.octo_url}/api/printer/command"
        while True:
            try:
                self.session.post(url, json={"command": "M114"}, timeout=1)
            except Exception as error:
                print(f"[fake_app] Failed to send M114: {error}")
            time.sleep(0.5)

    def _listen_websocket(self):
        while True:
            try:
                with ws_connect(self.octo_ws_url) as ws:
                    print("[fake_app] OctoPrint WebSocket connected. Authenticating...")
                    ws.send(json.dumps({"auth": f"{self.username}:{self.session_token}"}))
                    for message in ws:
                        self._handle_message(message)
            except ConnectionClosed as error:
                print(f"[fake_app] OctoPrint WebSocket closed: {error}, reconnecting...")
            except Exception as error:
                print(f"[fake_app] OctoPrint WebSocket error: {error}, reconnecting...")
            time.sleep(1)

    def _handle_message(self, message):
        data = json.loads(message)
        payload = data.get("current")
        if not payload:
            return

        for line in payload.get("logs", []):
            match = M114_RE.search(line)
            if match:
                x, y, z = (float(v) for v in match.groups())
                with self._lock:
                    self._position = (x, y, z)
                break


def _step_toward(current, target, max_step):
    """Move `current` at most `max_step` toward `target`, so simulated
    movement ramps smoothly instead of jumping straight to the latest
    (much less frequent) OctoPrint reading.
    """
    delta = target - current
    if abs(delta) <= max_step:
        return target
    return current + math.copysign(max_step, delta)


def real_to_marker_pixels(video_to_real, x, y, z):
    """Inverse of VideoToRealPosCalc.get_printer_head_pos(): fabricates the
    marker pixels that would produce this position under the current
    calibration.
    """
    # get_printer_head_pos maps pixel -> (X, Z) via homography; invert it.
    inv_homography = np.linalg.inv(video_to_real.homography_matrix)
    src = np.array([[[x, z]]], dtype=np.float32)
    red_x, red_y = cv2.perspectiveTransform(src, inv_homography)[0][0]

    # get_printer_head_pos projects onto g_pts_a -> g_pts_b and rescales to
    # Y; walk back along that same segment by the matching fraction.
    t = -y / video_to_real.printer_y_max
    green_x, green_y = video_to_real.g_pts_a + t * video_to_real.diff_ab

    return (float(red_x), float(red_y)), (float(green_x), float(green_y))


def main():
    _load_printer_env()

    cam_tracker = FakeCamTracker()
    telemetry_server = TelemetryServer(cam_tracker, host=WS_HOST, port=WS_PORT)
    telemetry_server.start()
    print(f"[fake_app] Serving fake telemetry on ws://{WS_HOST}:{WS_PORT}")

    bridge = OctoPrintBridge()
    bridge.start()

    period = 1.0 / BROADCAST_HZ
    current = bridge.get_position()
    try:
        while True:
            target = bridge.get_position()
            current = tuple(_step_toward(c, t, MAX_STEP_PER_TICK) for c, t in zip(current, target))
            x, y, z = current
            (red_x, red_y), (green_x, green_y) = real_to_marker_pixels(cam_tracker.video_to_real, x, y, z)
            print(f"POS: {x}, {y}, {z}")
            telemetry_server.broadcast_position(x, y, z, red_x, red_y, green_x, green_y)
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        telemetry_server.stop()


if __name__ == "__main__":
    main()
