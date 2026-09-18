import queue
import threading
import omni.ext
import omni.kit.app

from .printer_bridge import PrinterBridge
from .printer_vision import PrinterVision
from .usd_stage_manager import UsdStageManager

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

        # configure and start thread for vision
        self.printer_vision = PrinterVision(self._queue)
        self._vision_thread = threading.Thread(target=self.printer_vision.start_websocket, daemon=True)
        self._vision_thread.start()

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
