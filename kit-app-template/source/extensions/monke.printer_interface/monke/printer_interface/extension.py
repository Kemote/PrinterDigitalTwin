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


from pxr import Usd, UsdGeom
from usdrt import Usd as UsdRt, Sdf as SdfRt, UsdShade as UsdShadeRt, Gf as GfRt, Rt


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
            plugins_data = payload.get("plugins", {})
            if plugins_data and is_printing:
                dlp_data = plugins_data.get("DisplayLayerProgress", {}).get("print", {})
                # print(f"/n DLP DATA: {dlp_data}")
                rafined_data |= {
                    "pos_x": dlp_data.get("x", 0.0),
                    "pos_y": dlp_data.get("y", 0.0),
                    "pos_z": dlp_data.get("z", 0.0)
                }

            else:
                # if printer is not activly printing we need to use M114 command inseet DisplayLayerProgress plugin
                logs = payload.get("logs", [])
                position = self._parse_position_from_logs(logs)
                if position:
                    rafined_data["pos_x"], rafined_data["pos_y"], rafined_data["pos_z"] = position

            self._queue.put(rafined_data)

    def _on_error(self, ws, error):
        print(f"[PrinterBridge] WebSocket Error: {error}")

    def _on_close(self, ws, close_status, close_msg):
        print("[PrinterBridge] WebSocket Closed")


class UsdStageManager:
    PRINTER_PATH = "monkeDisc://assets/AnycubicI3Mega/AnycubicI3Mega.usda"
    ANYCUBIC_PRIM_PATH_STR = "/World/Printers/AnycubicI3Mega"

    def __init__(self, queue):
        self._queue = queue
        self.rt_stage : UsdRt.Stage = None
        self.pxr_stage : Usd.Stage = None
        self.attached_stage_id = None
        self.x_xfrom = None
        self.y_xfrom = None
        self.z_xfrom = None
        self.thermal_mat_path = None
        self.x_home_pos = None
        self.y_home_pos = None
        self.z_home_pos = None

    def on_update(self, _event : carb.events.IEventStream):
        while True:
            try:
                data = self._queue.get_nowait()

            except queue.Empty:
                break

            self._update_stage(data)

    def _update_stage(self, data):
        if not self._get_stage():        
            return
        
        # update materials
        self._upadte_thermalpad_mat(data)

        # update positions
        self._set_position(data)

    def _get_stage(self):
        omni_ctx = omni.usd.get_context()
        stage_id = omni_ctx.get_stage_id()
        if not stage_id:
            print("[PrinterBridge] No USD stage found")
            return False

        # stage id is changin when user open new stage or stage get recomposed etc...
        if not self.rt_stage or stage_id != self.attached_stage_id:
            stage : Usd.Stage = omni_ctx.get_stage()
            UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
            printer_prim : Usd.Prim = stage.DefinePrim(self.ANYCUBIC_PRIM_PATH_STR, "Xform")
            printer_prim.GetReferences().AddReference(self.PRINTER_PATH)

            self.pxr_stage = stage
            self.rt_stage = UsdRt.Stage.Attach(stage_id)
            self.attached_stage_id = stage_id

            # invalidate cached prim resolutions from the previous stage
            self.x_xfrom = None
            self.y_xfrom = None
            self.z_xfrom = None
            self.thermal_mat_path = None
            self.x_home_pos = None
            self.y_home_pos = None
            self.z_home_pos = None

        if not self.x_xfrom:
            self.thermal_mat_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Looks/thermal_heatMap/PreviewSurface")

            x_prim_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner/extruderHead")
            x_prim = self.rt_stage.GetPrimAtPath(x_prim_path)
            if x_prim.IsValid():
                self.x_xfrom  = Rt.Xformable(x_prim)

            y_prim_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/bed")
            y_prim = self.rt_stage.GetPrimAtPath(y_prim_path)
            if y_prim.IsValid():
                self.y_xfrom= Rt.Xformable(y_prim)

            z_prim_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner")
            z_prim = self.rt_stage.GetPrimAtPath(z_prim_path)
            if z_prim.IsValid():
                self.z_xfrom = Rt.Xformable(z_prim)

        return True

    def _upadte_thermalpad_mat(self, data):
        bed_temp = data.get("bed_actual")
        bed_target = data.get("bed_target", 110)    # max standard firmware bed temp
        if bed_temp:            
            new_col = self._temp_to_heatmap_rgb(bed_temp, bed_target)
            self._set_thermalpad_mat(new_col)

    def _set_thermalpad_mat(self, new_col):
        shd_prim = self.rt_stage.GetPrimAtPath(self.thermal_mat_path)
        thermal_shd  = UsdShadeRt.Shader(shd_prim)
        diffuse_color = thermal_shd.GetInput("diffuseColor")
        diffuse_color.Set(new_col)

    @staticmethod
    def _temp_to_heatmap_rgb(temp, max_temp=100.0):
        """
        Computes the thermalpad material's diffuse color based on the printer's
        current and target temperature. If no target temperature is set, we
        assume it should be 100.0.
        """
        min_temp = 20
        if max_temp <= min_temp:
            ratio = 0.0
        else:
            ratio = min(max((temp - min_temp) / (max_temp - min_temp), 0.0), 1.0)

        if ratio < 0.25:
            segment = ratio / 0.25
            r, g, b = 0.0, segment, 1.0
        elif ratio < 0.5:
            segment = (ratio - 0.25) / 0.25
            r, g, b = 0.0, 1.0, 1.0 - segment
        elif ratio < 0.75:
            segment = (ratio - 0.5) / 0.25
            r, g, b = segment, 1.0, 0.0
        else:
            segment = (ratio - 0.75) / 0.25
            r, g, b = 1.0, 1.0 - segment, 0.0

        return GfRt.Vec3f([r, g, b])

    def _get_home_pos(self, prim_path_str):
        # Read the home position via plain pxr USD instead of usdrt's Fabric-backed
        # world-position attribute: the latter is only populated once Fabric has
        # flattened a transform for this prim, which never happens for a prim with
        # no authored xformOps (confirmed: it stays permanently invalid here). This
        # one-time read isn't performance sensitive, so there's no need for the
        # Fabric fast path.
        prim = self.pxr_stage.GetPrimAtPath(prim_path_str)
        world_transform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        return world_transform.ExtractTranslation()

    def _set_position(self, data):
        print("SET")
        x = data.get("pos_x")
        y = data.get("pos_y")
        z = data.get("pos_z")

        if x is not None and self.x_xfrom:
            if self.x_home_pos is None:
                self.x_home_pos = self._get_home_pos(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner/extruderHead")
            else:
                new_x_pos = (self.x_home_pos[0] - x) / 10
                transform_matrix = GfRt.Matrix4d().SetTranslate(GfRt.Vec3d(new_x_pos, 0, 0))
                self.x_xfrom.CreateLocalMatrixAttr(transform_matrix)

        if y is not None and self.y_xfrom:
            if self.y_home_pos is None:
                self.y_home_pos = self._get_home_pos(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/bed")
            else:
                new_y_pos = (self.y_home_pos[1] + y) / 10
                transform_matrix = GfRt.Matrix4d().SetTranslate(GfRt.Vec3d(0.0, new_y_pos, 0.0))
                self.y_xfrom.CreateLocalMatrixAttr(transform_matrix)

        if z is not None and self.z_xfrom:
            if self.z_home_pos is None:
                self.z_home_pos = self._get_home_pos(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner")
            else:
                new_z_pos = (self.z_home_pos[2] + z) / 10
                transform_matrix = GfRt.Matrix4d().SetTranslate(GfRt.Vec3d(0.0, 0.0, new_z_pos))
                self.z_xfrom.CreateLocalMatrixAttr(transform_matrix)

