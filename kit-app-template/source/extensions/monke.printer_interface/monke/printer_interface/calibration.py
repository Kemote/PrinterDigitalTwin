import time
import queue


class Calibrator:
    def __init__(self, printer_bridge, printer_vision, queue):
        self.printer_bridge = printer_bridge
        self.printer_bridge.send_printer_home()
        time.sleep(5)    # lets be sure that its get home
        self.printer_vision = printer_vision
        self.get_corrections = True
        self.queue = queue
        self.correction_x = 0
        self.correction_y = 0
        self.correction_z = 0

    def calibrate_printer(self):
    # calibrate printer with webcam
        print("[monke.printer_interface] Calibrating printer....")
        if self.printer_bridge.is_printing:
            print("[monke.printer_interface] Printer currently printing, omit calibration.")
            return

        calib_pos = {
            "g_pos_min": None,
            "g_pos_max": None,
            "r_pos_tl": None,
            "r_pos_tr": None,
            "r_pos_br": None,
            "r_pos_bl": None
        }

        calibration_steps = [
            {"position": (0.0, 0.0, 0.0), "g_key": "g_pos_min", "r_key": "r_pos_bl"},
            {"position": (0.0, 210.0, 205.0), "g_key": "g_pos_max", "r_key": "r_pos_tl"},
            {"position": (210.0, 210.0, 205.0), "g_key": None, "r_key": "r_pos_tr"},
            {"position": (210.0, 210.0, 0.0), "g_key": None, "r_key": "r_pos_br"},
        ]

        for step in calibration_steps:
            x = step["position"][0]
            y = step["position"][1]
            z = step["position"][2]

            if self.printer_bridge.set_position(x, y, z):
                print(f"[monke.printer_interface] Get visual position of {step['position']}")
                queue_semaphor = False
                while not queue_semaphor:
                    while True:
                        try:
                            queue_data = self.queue.get_nowait()
                        except queue.Empty:
                            break

                    # obtain telemetry data
                    tele_x = queue_data.get("tele_x")
                    tele_y = queue_data.get("tele_y")
                    tele_z = queue_data.get("tele_z")
                    if None in (tele_x, tele_y, tele_z):
                        continue

                    tele_x -= self.correction_x
                    tele_y -= self.correction_y
                    tele_z -= self.correction_z

                    print(f"    [monke.printer_interface] Waiting for printer to get at posision {x}, {y}, {z}, current position {tele_x}, {tele_y}, {tele_z}")
                    if tele_x == x and tele_y == y and tele_z == z:
                        if "marker_gx" in queue_data:
                            if step["g_key"]:
                                calib_pos[step["g_key"]] = [
                                    queue_data["marker_gx"],
                                    queue_data["marker_gy"]
                                ]
                            calib_pos[step["r_key"]] = [
                                queue_data["marker_rx"],
                                queue_data["marker_ry"]
                            ]
                        queue_semaphor = True
                        
                    # get corrections if need
                    if self.get_corrections :
                        self.correction_x = tele_x
                        self.correction_y = tele_y
                        self.correction_z = tele_z
                        self.get_corrections = False

                    time.sleep(1)
                    

        # now send it to home position after calibration data is gathered
        # self.printer_bridge.send_printer_home()

        if not None in calib_pos.values():
            self.printer_vision.send_calibration(
                calib_pos["r_pos_tl"],
                calib_pos["r_pos_tr"],
                calib_pos["r_pos_br"],
                calib_pos["r_pos_bl"],
                calib_pos["g_pos_min"],
                calib_pos["g_pos_max"]
            )
            time.sleep(2)
            print("[monke.printer_interface] Printer calibrated with vision data")