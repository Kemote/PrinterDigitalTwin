import re
import os
import time
import json
import requests
import websocket


class PrinterBridge:
    def __init__(self, queue):
        self._M114_RE = re.compile(r"X:(-?\d+\.?\d*)\s+Y:(-?\d+\.?\d*)\s+Z:(-?\d+\.?\d*)")
        self.ws = None
        self._queue = queue
        self.username = None
        self.session = None
        self.home_pos = True
        self.octo_url = os.environ.get("OCTO_URL")
        self.octo_api_key = os.environ.get("OCTO_API_KEY")
        self.octo_ws_url = os.environ.get("OCTO_WS_URL")
        if not (self.octo_url and self.octo_api_key and self.octo_ws_url):
            print(
                "[PrinterBridge] OCTO_URL, OCTO_API_KEY and OCTO_WS_URL "
                "must all be set; skipping OctoPrint connection."
            )
            return

        # fetch a session token up front so the websocket can authenticate on open
        is_octo_connected = False
        while not is_octo_connected:
            try:
                self.username, self.session = self.get_session_token()
                is_octo_connected = True
            except Exception as error:
                print(f"[PrinterBridge] Failed to fetch OctoPrint session token: {error}, waiting for connection")
                time.sleep(1)

    @staticmethod
    def _get_flags(payload):
        state_data = payload.get("state", {})
        flags = state_data.get("flags", {})
        is_printing = flags.get("printing", False)
        is_paused = flags.get("paused", False)
        is_ready = flags.get("ready", False)
        return is_printing, is_paused, is_ready
    
    def start_websocket(self):
        self.ws = websocket.WebSocketApp(
            self.octo_ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self.ws.run_forever()

    def send_m114(self):
        url = f"{self.octo_url}/api/printer/command"
        headers = {"X-Api-Key": self.octo_api_key, "Content-Type": "application/json"}
        while True:
            try:
                requests.post(url, json={"command": "M114"}, headers=headers, timeout=1)
            except Exception as error:
                print(f"[PrinterBridge] Failed to send M114: {error}")
            time.sleep(0.5)
    
    def get_session_token(self):
        url = f"{self.octo_url}/api/login"
        headers = {"X-Api-Key": self.octo_api_key, "Content-Type": "application/json"}
        response = requests.post(url, json={"passive": True}, headers=headers)

        if response.status_code == 200:
            data = response.json()
            return data.get("name"), data.get("session")
        else:
            raise Exception(f"Failed to fetch session token: {response.status_code} - {response.text}")

    def _send_printer_home(self):
        headers = {"X-Api-Key": self.octo_api_key, "Content-Type": "application/json"}
        requests.post( f"{self.octo_url}/api/printer/printhead", json={"command": "home", "axes": ["x", "y"]},
        headers=headers
        )

    def _parse_position_from_logs(self, logs):
        for line in logs:
            match = self._M114_RE.search(line)
            if match:
                x, y, z = match.groups()
                return float(x), float(y), float(z)
        return None

    def _on_open(self, ws):
        print("[PrinterBridge] WebSocket Connected. Authenticating...")
        auth_payload = {"auth": f"{self.username}:{self.session}"}
        ws.send(json.dumps(auth_payload))

    def _on_message(self, ws, message):
        data = json.loads(message)
        rafined_data = {}
        print(f"DATA: {data}")
        payload = data.get("current")

        if payload:
            is_printing, is_paused, is_ready = self._get_flags(payload)
            rafined_data["is_printing"] = is_printing
            rafined_data["is_paused"] = is_paused
            rafined_data["is_ready"] = is_ready

            # set inital home pos if not printing
            if not is_printing and self.home_pos:
                self._send_printer_home()
                self.home_pos = False
            
            # get temps 
            # print(f"/n PAYLOAD {payload}")
            temps = payload.get("temps", [{}])
            # print(f"TEMPS: {temps}")
            if len(temps) > 0:
                temps = temps[0]
                rafined_data |= {
                    "hotend_actual": temps.get("tool0", {}).get("actual", 0.0),
                    "hotend_target": temps.get("tool0", {}).get("target", 0.0),
                    "bed_actual": temps.get("bed", {}).get("actual", 0.0),
                    "bed_target": temps.get("bed", {}).get("target", 0.0),
                }

            # set position
            print(f"PAYLOAD: {payload}")
            plugins_data = payload.get("plugins", {})
            dlp_data = plugins_data.get("DisplayLayerProgress", {}).get("print", {}) if plugins_data else {}
            has_dlp_data = (
                is_printing
                and dlp_data.get("x") is not None
                and dlp_data.get("y") is not None
                and dlp_data.get("z") is not None
            )

            if has_dlp_data:
                position = (dlp_data["x"], dlp_data["y"], dlp_data["z"])
            else:
                # DisplayLayerProgress isn't reporting real data (plugin missing/disabled,
                # or unsupported for this file) - previously this silently defaulted to
                # (0, 0, 0) whenever "plugins" had other entries but no DisplayLayerProgress
                # key, which snapped the model toward the home corner mid-print. Always fall
                # back to parsing the real position out of the M114 logs instead.
                logs = payload.get("logs", [])
                position = self._parse_position_from_logs(logs)

            if position:
                rafined_data["pos_x"], rafined_data["pos_y"], rafined_data["pos_z"] = position

            print(f"PRINTER DATA: {rafined_data}")
            self._queue.put(rafined_data)

    def _on_error(self, ws, error):
        print(f"[PrinterBridge] WebSocket Error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        print("[PrinterBridge] WebSocket Closed")
