import os
import json
import asyncio
import websockets
from websockets.exceptions import ConnectionClosed


class PrinterVision:
    """Fetches the tracked printer-head position from cam_tracker's
    TelemetryServer (see cam_tracker/app.py) over a WebSocket connection, and
    lets calibration corner points be pushed back to it.

    Runs as a coroutine on Kit's own asyncio loop (scheduled by extension.py
    via omni.kit.async_engine) instead of a dedicated OS thread.
    """

    def __init__(self, extension_queue):
        self.ws = None
        self._queue = extension_queue
        self.cam_tracker_ws_url = os.environ.get("CAM_TRACKER_WS_URL", "ws://localhost:8765")
        self._connected = asyncio.Event()

    async def run(self):
        # `async for ws in websockets.connect(...)` reconnects automatically
        # (with backoff) whenever the connection drops, so cam_tracker
        # restarting no longer requires reloading the extension.
        async for ws in websockets.connect(self.cam_tracker_ws_url):
            self.ws = ws
            self._connected.set()
            print("[PrinterVision] WebSocket Connected to cam_tracker.")
            try:
                async for message in ws:
                    self._handle_message(message)
            except ConnectionClosed as error:
                print(f"[PrinterVision] WebSocket Closed: {error}")
            finally:
                self._connected.clear()
                self.ws = None

    def _handle_message(self, message):
        try:
            data = json.loads(message)
        except (TypeError, ValueError) as error:
            print(f"[PrinterVision] Malformed message from cam_tracker: {error}")
            return

        msg_type = data.get("type")
        if msg_type == "position":
            self._queue.put_nowait({
                "pos_x": data.get("x"),
                "pos_y": data.get("y"),
                "pos_z": data.get("z"),
                "marker_rx": data.get("marker_rx"),
                "marker_ry": data.get("marker_ry"),
                "marker_gx": data.get("marker_gx"),
                "marker_gy": data.get("marker_gy")
            })
        elif msg_type == "calibration_ack":
            print(f"[PrinterVision] Calibration acknowledged: {data.get('points')}")
        elif msg_type == "calibration_error":
            print(f"[PrinterVision] Calibration error: {data.get('error')}")
        else:
            print(f"[PrinterVision] Unknown message type: {msg_type!r}")

    async def send_calibration(self, points, timeout=30):
        """
        Push new calibration corner points to cam_tracker.
        `points` must supply TelemetryServer.REQUIRED_CALIBRATION_POINTS'
        keys - "rtl_pos", "rtr_pos", "rbr_pos", "rbl_pos", "gt_pos", "gb_pos" -
        each a [x, y] pixel coordinate.
        """
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            print("[PrinterVision] Cannot send calibration: not connected to cam_tracker")
            return
        await self.ws.send(json.dumps({"type": "set_calibration", "points": points}))
