import asyncio
import omni.ext
import omni.kit.app
import omni.kit.async_engine as omni_async

from .printer_bridge import PrinterBridge
from .printer_vision import PrinterVision
from .usd_stage_manager import UsdStageManager
from .calibration import Calibrator


# Any class derived from `omni.ext.IExt` in the top level module (defined in
# `python.modules` of `extension.toml`) will be instantiated when the extension
# gets enabled, and `on_startup(ext_id)` will be called. Later when the
# extension gets disabled on_shutdown() is called.
class MyExtension(omni.ext.IExt):
    def on_startup(self, _ext_id):
        print("[monke.printer_interface] Extension startup")
        self._vision_task = None
        self._bridge_task = None
        self._calibration_task = None
        self.queue = asyncio.Queue()
        self.printer_vision = PrinterVision(self.queue)
        self.printer_bridge = PrinterBridge(self.queue)

        # vision and the OctoPrint bridge each run as a coroutine on Kit's own
        # asyncio loop instead of a dedicated OS thread - see
        # PrinterVision.run() / PrinterBridge.run()
        self._vision_task = omni_async.run_coroutine(self.printer_vision.run())
        self._bridge_task = omni_async.run_coroutine(self.printer_bridge.run())

        # calibration also runs as a coroutine (it awaits printer_vision's and
        # printer_bridge's coroutine-based calls) - this also means
        # calibration no longer blocks the whole extension/viewport for its
        # duration.
        calibrator = Calibrator(self.printer_bridge, self.printer_vision, self.queue)
        self._calibration_task = omni_async.run_coroutine(self._run_calibration(calibrator))

    async def _run_calibration(self, calibrator):
        await calibrator.calibrate_printer()

        # set omni app event stream
        self.usd_stage_mgr = UsdStageManager(self.queue)
        app = omni.kit.app.get_app()
        update_stream = app.get_update_event_stream()
        self._update_sub = update_stream.create_subscription_to_pop(self.usd_stage_mgr.on_update)

    def on_shutdown(self):
        print("[monke.printer_interface] Extension shutdown")
        if self._vision_task:
            self._vision_task.cancel()
        if self._bridge_task:
            self._bridge_task.cancel()
        if self._calibration_task:
            self._calibration_task.cancel()
        omni_async.run_coroutine(self.printer_bridge.aclose())
