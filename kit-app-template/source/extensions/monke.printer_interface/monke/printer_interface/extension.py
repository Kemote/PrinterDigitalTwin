import queue
import threading
import omni.ext
import omni.kit.app
import time

from .printer_bridge import PrinterBridge
from .printer_vision import PrinterVision
from .usd_stage_manager import UsdStageManager
from .calibration import Calibrator

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
        self.queue = queue.Queue()
        self.printer_vision = PrinterVision(self.queue)
        self.printer_bridge = PrinterBridge(self.queue)

        # start thread for vision
        self._vision_thread = threading.Thread(target=self.printer_vision.start_websocket, daemon=True)
        self._vision_thread.start()

         # tart thread for printer communication
        self._thread = threading.Thread(target=self.printer_bridge.start_websocket, daemon=True)
        self._thread.start()

        # TODO: do we need to do that in separate thred?
        # start M114 request thread so position telemetry is available during calibration
        self._m114_thread = threading.Thread(target=self.printer_bridge.send_m114, daemon=True)
        self._m114_thread.start()

        calibrator = Calibrator(self.printer_bridge, self.printer_vision, self.queue)
        calibrator.calibrate_printer()

        # set omni app event stream
        self.usd_stage_mgr = UsdStageManager(self.queue)
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


# TODO: czy uzywac thread czy asyncio dla tego wszystkiego?