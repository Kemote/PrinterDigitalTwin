import time
import queue
import os
import json


class Calibrator:
    CALIBRATION_FILE = "./calibration.json"

    def __init__(self, printer_bridge, printer_vision, queue):
        self.printer_bridge = printer_bridge
        self.printer_vision = printer_vision
        self.get_corrections = True
        self.queue = queue

        self.calib_pos = None
        recalibrate = os.environ.get("RECALIBRATE_VISION") == "True"
        if os.path.exists(self.CALIBRATION_FILE) and not recalibrate:
            with open(self.CALIBRATION_FILE, "r") as f:
                self.calib_pos = json.load(f)

    def _send_calibration(self):
        self.printer_vision.send_calibration({
            "rtl_pos": self.calib_pos["r_pos_tl"],
            "rtr_pos": self.calib_pos["r_pos_tr"],
            "rbr_pos": self.calib_pos["r_pos_br"],
            "rbl_pos": self.calib_pos["r_pos_bl"],
            "gt_pos": self.calib_pos["g_pos_max"],
            "gb_pos": self.calib_pos["g_pos_min"]
        })
        time.sleep(2)

    def calibrate_printer(self):
        # calibrate printer with webcam
        if not self.calib_pos:
            print("[monke.printer_interface] Calibrating printer....")
            if self.printer_bridge.is_printing:
                print("[monke.printer_interface] Printer currently printing, omit calibration.")
                return

            self.calib_pos = {
                "g_pos_min": None,
                "g_pos_max": None,
                "r_pos_tl": None,
                "r_pos_tr": None,
                "r_pos_br": None,
                "r_pos_bl": None
            }

            calibration_steps = [
                {"position": (0.0, 0.0, 0.0), "g_key": "g_pos_max", "r_key": "r_pos_bl"},
                {"position": (0.0, 210.0, 205.0), "g_key": "g_pos_min", "r_key": "r_pos_tl"},
                {"position": (210.0, 210.0, 205.0), "g_key": None, "r_key": "r_pos_tr"},
                {"position": (210.0, 210.0, 0.0), "g_key": None, "r_key": "r_pos_br"},
            ]

            for step in calibration_steps:
                x = step["position"][0]
                y = step["position"][1]
                z = step["position"][2]

                self.printer_bridge.set_position(x, y, z)
                print(f"[monke.printer_interface] Get visual position of {step['position']}")
                time.sleep(150)

                queue_semaphor = False
                while not queue_semaphor:
                    while True:
                        try:
                            queue_data = self.queue.get_nowait()
                        except queue.Empty:
                            break

                    if "marker_gx" in queue_data:
                        if None not in [queue_data["marker_gx"], queue_data["marker_gy"], queue_data["marker_rx"], queue_data["marker_ry"]]:
                            if step["g_key"]:
                                self.calib_pos[step["g_key"]] = [
                                    queue_data["marker_gx"],
                                    queue_data["marker_gy"]
                                ]
                            self.calib_pos[step["r_key"]] = [
                                queue_data["marker_rx"],
                                queue_data["marker_ry"]
                            ]
                            queue_semaphor = True
    
            if not None in self.calib_pos.values():
                with open(self.CALIBRATION_FILE, "w") as f:
                    json.dump(self.calib_pos, f)
                print("[monke.printer_interface] Printer calibrated with vision data")

        self._send_calibration()