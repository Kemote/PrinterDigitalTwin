import os
import json
import threading
import websocket


class PrinterVision:
    """Fetches the tracked printer-head position from cam_tracker's
    TelemetryServer (see cam_tracker/app.py) over a WebSocket connection, and
    lets calibration corner points be pushed back to it.
    """

    def __init__(self, queue):
        self.ws = None
        self._queue = queue
        self.cam_tracker_ws_url = os.environ.get("CAM_TRACKER_WS_URL", "ws://localhost:8765")
        self._connected = threading.Event()

    def start_websocket(self):
        self.ws = websocket.WebSocketApp(
            self.cam_tracker_ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self.ws.run_forever()


    def send_calibration(self, points, timeout=30):
        """Push new calibration corner points to cam_tracker.

        `points` must supply TelemetryServer.REQUIRED_CALIBRATION_POINTS'
        keys - "rtl_pos", "rtr_pos", "rbr_pos", "rbl_pos", "gt_pos", "gb_pos" -
        each a [x, y] pixel coordinate. Working out those points is not
        implemented yet; this just sends them once a caller has them.

        The websocket thread connects asynchronously, so this waits (up to
        `timeout` seconds) for that connection instead of failing immediately
        when called right after startup.
        """
        if not self._connected.wait(timeout):
            print("[PrinterVision] Cannot send calibration: not connected to cam_tracker")
            return
        self.ws.send(json.dumps({"type": "set_calibration", "points": points}))

    def _on_open(self, ws):
        print("[PrinterVision] WebSocket Connected to cam_tracker.")
        self._connected.set()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except (TypeError, ValueError) as error:
            print(f"[PrinterVision] Malformed message from cam_tracker: {error}")
            return

        msg_type = data.get("type")
        if msg_type == "position":
            self._queue.put({
                "pos_x": data.get("x"),
                "pos_y": data.get("y"),
                "pos_z": data.get("z"),
                "marker_rx": data.get("marker_rx"),
                "marker_ry": data.get("marker_ry"),
                "marker_gx": data.get("marker_gx"),
                "marker_gy": data.get("marker_gy"),
                "t": data.get("t")
            })

        elif msg_type == "calibration_ack":
            print(f"[PrinterVision] Calibration acknowledged: {data.get('points')}")
        elif msg_type == "calibration_error":
            print(f"[PrinterVision] Calibration error: {data.get('error')}")
        else:
            print(f"[PrinterVision] Unknown message type: {msg_type!r}")

    def _on_error(self, ws, error):
        print(f"[PrinterVision] WebSocket Error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        print("[PrinterVision] WebSocket Closed")
        self._connected.clear()
