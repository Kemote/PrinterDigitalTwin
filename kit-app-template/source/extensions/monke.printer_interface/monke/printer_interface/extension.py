import os
import re
import json
import queue
import time
import carb
import requests
import threading
import websocket
import omni.ext
import omni.kit.app


from pxr import Usd, Sdf, UsdShade, Gf


# Functions and vars are available to other extensions as usual in python:
# `monke.printer_interface.some_public_function(x)`
def some_public_function(x: int):
    """This is a public function that can be called from other extensions."""
    print(f"[monke.printer_interface] some_public_function was called with {x}")
    return x**x


# Any class derived from `omni.ext.IExt` in the top level module (defined in
# `python.modules` of `extension.toml`) will be instantiated when the extension
# gets enabled, and `on_startup(ext_id)` will be called. Later when the
# extension gets disabled on_shutdown() is called.
class MyExtension(omni.ext.IExt):
    def on_startup(self, _ext_id):
        print("[monke.printer_interface] Extension startup")
        self._thread = None
        self._queue = queue.Queue()

         # configure and start thread for telemetry
        self.printer_bridge = PrinterBridge(self._queue)
        self._thread = threading.Thread(target=self.printer_bridge.start_websocket, daemon=True)
        self._thread.start()

        # start M114 request thread
        self._m114_thread = threading.Thread(target=self.printer_bridge.send_m114, daemon=True)
        self._m114_thread.start()

        # set
        self.usd_stage_mgr = UsdStageManager(self._queue)
        app = omni.kit.app.get_app()
        update_stream = app.get_update_event_stream()
        self._update_sub = update_stream.create_subscription_to_pop(self.usd_stage_mgr.on_update)

    def on_shutdown(self):
        print("[monke.printer_interface] Extension shutdown")
        if self.printer_bridge.ws:
            self.printer_bridge.ws.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._m114_thread and self._m114_thread.is_alive():
            self._m114_thread.join(timeout=1.0)


class PrinterBridge:
    def __init__(self, queue):
        self._M114_RE = re.compile(r"X:(-?\d+\.?\d*)\s+Y:(-?\d+\.?\d*)\s+Z:(-?\d+\.?\d*)")
        self.ws = None
        self._queue = queue
        self.username = None
        self.session = None
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

    def _parse_position_from_logs(self, logs):
        for line in logs:
            match = self._M114_RE.search(line)
            if match:
                x, y, z = match.groups()
                return float(x), float(y), float(z)
        return None

    def get_session_token(self):
        url = f"{self.octo_url}/api/login"
        headers = {"X-Api-Key": self.octo_api_key, "Content-Type": "application/json"}
        response = requests.post(url, json={"passive": True}, headers=headers)

        if response.status_code == 200:
            data = response.json()
            return data.get("name"), data.get("session")
        else:
            raise Exception(f"Failed to fetch session token: {response.status_code} - {response.text}")


    def _on_open(self, ws):
        print("[PrinterBridge] WebSocket Connected. Authenticating...")
        auth_payload = {"auth": f"{self.username}:{self.session}"}
        ws.send(json.dumps(auth_payload))

    def _on_message(self, ws, message):
        data = json.loads(message)
        payload = data.get("current")
        if payload:
            is_printing, is_paused, is_ready = self._get_flags(payload)
            # get temps 
            temps = payload.get("temps", [{}])
            if len(temps) > 0:
                temps = temps[0]
                plugins_data = payload.get("plugins", {})
                dlp_data = plugins_data.get("DisplayLayerProgress", {}).get("print", {})

                rafined_data = {
                    "hotend_actual": temps.get("tool0", {}).get("actual", 0.0),
                    "hotend_target": temps.get("tool0", {}).get("target", 0.0),
                    "bed_actual": temps.get("bed", {}).get("actual", 0.0),
                    "bed_target": temps.get("bed", {}).get("target", 0.0),
                }

                if is_printing and ("x" in dlp_data):
                    rafined_data["pos_x"] = dlp_data.get("x", 0.0)
                    rafined_data["pos_y"] = dlp_data.get("y", 0.0)
                    rafined_data["pos_z"] = dlp_data.get("z", 0.0)
                else:
                    # if printer is not activly printing we need to use M114 command inseet DisplayLayerProgress plugin
                    logs = payload.get("logs", [])
                    position = self._parse_position_from_logs(logs)
                    if position:
                        rafined_data["pos_x"], rafined_data["pos_y"], rafined_data["pos_z"] = position

                rafined_data["is_printing"] = is_printing
                rafined_data["is_paused"] = is_paused
                rafined_data["is_ready"] = is_ready

                self._queue.put(rafined_data)

    def _on_error(self, ws, error):
        print(f"[PrinterBridge] WebSocket Error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        print("[PrinterBridge] WebSocket Closed")


class UsdStageManager:
    def __init__(self, queue):
        self._queue = queue
        self.stage : Usd.Stage = None

    def on_update(self, _event : carb.events.IEventStream):
        while True:
            try:
                data = self._queue.get_nowait()

            except queue.Empty:
                break

            self._update_stage(data)

    def _update_stage(self, data):
        print(f"DATA: {data}")
        if not self._get_stage():        
            return
        self._upadte_thermalpad_mat(data)

    def _get_stage(self):
        if not self.stage:
            stage = omni.usd.get_context().get_stage()
            if not stage:
                print("[PrinterBridge] No USD stage found")
                return False
            self.stage = stage
        return True

    def _upadte_thermalpad_mat(self, data):
        bed_temp = data.get("bed_actual")
        bed_target = data.get("bed_target", 110)    # max standard firmware bed temp
        if bed_temp:

            
            new_col = self._temp_to_heatmap_rgb(bed_temp, bed_target)
            thermalpad_mat = self._get_thermalpad_mat(new_col)

    def _get_thermalpad_mat(self, new_col):
        # TODO to zrobic madrzej
        thermal_shd  = UsdShade.Shader.Get(self.stage, "/World/AnycubicI3Mega/Looks/adhesive_thermalView/PreviewSurface")
        diffuse_color = thermal_shd.GetInput("diffuseColor")
        diffuse_color.Set(new_col)

    @staticmethod
    def _temp_to_heatmap_rgb(temp, max_temp=100.0):
        min_temp = 20
        # 1. Clamp and normalize ratio between 0.0 and 1.0
        if max_temp <= min_temp:
            ratio = 0.0
        else:
            ratio = min(max((temp - min_temp) / (max_temp - min_temp), 0.0), 1.0)

        # 2. Map ratio across 4 color band segments (Blue -> Cyan -> Green -> Yellow -> Red)
        # Segment boundaries: 0.0 (Blue), 0.25 (Cyan), 0.5 (Green), 0.75 (Yellow), 1.0 (Red)
        if ratio < 0.25:
            # Blue to Cyan (R: 0, G: 0->1, B: 1)
            segment = ratio / 0.25
            r, g, b = 0.0, segment, 1.0
        elif ratio < 0.5:
            # Cyan to Green (R: 0, G: 1, B: 1->0)
            segment = (ratio - 0.25) / 0.25
            r, g, b = 0.0, 1.0, 1.0 - segment
        elif ratio < 0.75:
            # Green to Yellow (R: 0->1, G: 1, B: 0)
            segment = (ratio - 0.5) / 0.25
            r, g, b = segment, 1.0, 0.0
        else:
            # Yellow to Red (R: 1, G: 1->0, B: 0)
            segment = (ratio - 0.75) / 0.25
            r, g, b = 1.0, 1.0 - segment, 0.0

        return Gf.Vec3f([r, g, b])