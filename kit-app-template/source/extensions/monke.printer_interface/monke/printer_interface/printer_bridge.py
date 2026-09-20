import re
import os
import time
import json
import threading
import requests
import websocket


class PrinterBridge:
    _POSITION_TOLERANCE = 0.5  # mm

    def __init__(self, queue):
        self._M114_RE = re.compile(r"X:(-?\d+\.?\d*)\s+Y:(-?\d+\.?\d*)\s+Z:(-?\d+\.?\d*)")
        self.ws = None
        self._queue = queue
        self.username = None
        self.session = None
        self.home_pos = True
        self.is_printing = False
        self.is_paused = False
        self.is_ready = False
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

    def _get_flags(self, payload):
        state_data = payload.get("state", {})
        flags = state_data.get("flags", {})
        self.is_printing = flags.get("printing", False)
        self.is_paused = flags.get("paused", False)
        self.is_ready = flags.get("ready", False)
    
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

    def send_printer_home(self):
        if not self.is_printing:
            headers = {"X-Api-Key": self.octo_api_key, "Content-Type": "application/json"}
            requests.post( f"{self.octo_url}/api/printer/printhead", json={"command": "home", "axes": ["x", "y", "z"]},
            headers=headers
            )

    def set_position(self, x, y, z):
        print(f"[PrinterBridge] Seting printer position {x}, {y}, {z}...")
        if not self.is_printing:
            url = f"{self.octo_url}/api/printer/printhead"
            headers = {"X-Api-Key": self.octo_api_key, "Content-Type": "application/json"}
            response = requests.post(url, json={"command": "jog", "x": x, "y": y, "z": z}, headers=headers)
            if response.status_code in (200, 204):
                return True
            else:
                raise Exception(f"Error occured during moving to position: {response.status_code} - {response.text}")
        else:
            return False

    def _parse_position_from_logs(self, logs):
        for line in logs:
            match = self._M114_RE.search(line)
            if match:
                x, y, z = match.groups()
                return float(x), float(y), float(z)
        return None, None, None

    def _on_open(self, ws):
        print("[PrinterBridge] WebSocket Connected. Authenticating...")
        auth_payload = {"auth": f"{self.username}:{self.session}"}
        ws.send(json.dumps(auth_payload))

    def _on_message(self, ws, message):
        data = json.loads(message)
        rafined_data = {}
        payload = data.get("current")

        if payload:
            self._get_flags(payload)
            rafined_data["is_printing"] = self.is_printing
            rafined_data["is_paused"] = self.is_paused
            rafined_data["is_ready"] = self.is_ready

            # set inital home pos if not printing
            if self.home_pos:
                self.send_printer_home()
                self.home_pos = False
            
            # get temps 
            temps = payload.get("temps", [{}])
            if len(temps) > 0:
                temps = temps[0]
                rafined_data |= {
                    "hotend_actual": temps.get("tool0", {}).get("actual", 0.0),
                    "hotend_target": temps.get("tool0", {}).get("target", 0.0),
                    "bed_actual": temps.get("bed", {}).get("actual", 0.0),
                    "bed_target": temps.get("bed", {}).get("target", 0.0),
                }

            # get telemetry position
            logs = payload.get("logs", [])
            rafined_data["tele_x"], rafined_data["tele_y"], rafined_data["tele_z"] = self._parse_position_from_logs(logs)
        
            self._queue.put(rafined_data)

    def _on_error(self, ws, error):
        print(f"[PrinterBridge] WebSocket Error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        print("[PrinterBridge] WebSocket Closed")
