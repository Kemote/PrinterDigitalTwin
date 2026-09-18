import cv2
import json
import time
import threading
import numpy as np
from websockets.sync.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed


CAMERA_INDEX = 0      # Adjust if using an external USB webcam
FRAME_WIDTH = 800
FRAME_HEIGHT = 600
TARGET_FPS = 60
FRAME_ROTATION = cv2.ROTATE_90_CLOCKWISE
# WebSocket server that broadcasts tracked X/Y/Z position and accepts calibration
# updates - see TelemetryServer below.
WS_HOST = "0.0.0.0"
WS_PORT = 8765
# Define HSV color ranges for the two markers.
RED_RANGES = [
    (np.array([0, 100, 100]), np.array([10, 255, 255])),
    (np.array([160, 100, 100]), np.array([179, 255, 255])),
]
GREEN_RANGES = [
    (np.array([35, 100, 100]), np.array([85, 255, 255])),
]
MIN_CONTOUR_AREA = 50


class CamTracker:
    def __init__(self, camera_index=CAMERA_INDEX,
                 frame_width=FRAME_WIDTH,
                 frame_height=FRAME_HEIGHT,
                 target_fps=TARGET_FPS,
                 frame_rotation=FRAME_ROTATION,
                 red_ranges=RED_RANGES,
                 green_ranges=GREEN_RANGES,
                 min_contour_area=MIN_CONTOUR_AREA,
                 ws_host=WS_HOST,
                 ws_port=WS_PORT):

        self.camera_index = camera_index
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.target_fps = target_fps
        self.frame_rotation = frame_rotation
        self.red_ranges = red_ranges
        self.green_ranges = green_ranges
        self.min_contour_area = min_contour_area

        self.cap = None
        self.prev_time = None
        self.video_to_real = VideoToRealPosCalc()
        # Broadcasts self.video_to_real's output over WebSocket and lets a
        # connected client (e.g. the Omniverse extension) push new calibration
        # points, which swaps self.video_to_real for a freshly calibrated one.
        self.telemetry_server = TelemetryServer(self, host=ws_host, port=ws_port)

    def init_camera(self):
        self.cap = cv2.VideoCapture(self.camera_index, cv2.CAP_V4L2)  # Use CAP_DSHOW on Windows, CAP_V4L2 on Linux
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open camera index {self.camera_index}. Check CAMERA_INDEX / /dev/video*.")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
        self.cap.set(cv2.CAP_PROP_FPS, self.target_fps)

        # Verify actual frame rate set by hardware driver
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
        print(f"Camera initialized. Target: {self.target_fps} FPS | Actual Driver Rate: {actual_fps} FPS")

    def color_mask(self, hsv, ranges):
        mask = None
        for lower, upper in ranges:
            m = cv2.inRange(hsv, lower, upper)
            mask = m if mask is None else cv2.bitwise_or(mask, m)

        # Morphological operations to clean up small noise blobs
        mask = cv2.erode(mask, None, iterations=2)
        mask = cv2.dilate(mask, None, iterations=2)
        return mask

    def find_marker(self, mask, min_area=None):
        min_area = self.min_contour_area if min_area is None else min_area
        contours, _ = cv2.findContours(mask.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours) == 0:
            return None

        # Find the largest contour (assumed to be the marker)
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) < min_area:
            return None

        # Compute center of mass (Centroid)
        M = cv2.moments(c)
        if M["m00"] == 0:
            return None

        cX = int(M["m10"] / M["m00"])
        cY = int(M["m01"] / M["m00"])
        return c, (cX, cY)

    def main(self):
        self.init_camera()
        self.telemetry_server.start()
        self.prev_time = time.perf_counter()

        while True:
            ret, frame = self.cap.read()
            if not ret:
                print("Failed to grab frame.")
                break

            # Correct the physical 90 degree camera mount before any detection/display
            # work runs, so every downstream pixel coordinate (markers, calibration
            # points, overlays) is already in the upright frame's coordinate space.
            frame = cv2.rotate(frame, self.frame_rotation)

            current_time = time.perf_counter()
            dt = current_time - self.prev_time
            self.prev_time = current_time
            instant_fps = 1.0 / dt if dt > 0 else 0

            # 1. Convert BGR to HSV color space
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

            # 2. Threshold the HSV image and locate each marker
            red_mask = self.color_mask(hsv, self.red_ranges)
            green_mask = self.color_mask(hsv, self.green_ranges)

            red_marker = self.find_marker(red_mask)
            green_marker = self.find_marker(green_mask)

            red_pos, green_pos = None, None

            if red_marker is not None:
                c, (cX, cY) = red_marker
                red_pos = (cX, cY)

                # Draw visual overlays
                cv2.drawContours(frame, [c], -1, (0, 0, 255), 2)
                cv2.circle(frame, (cX, cY), 5, (0, 0, 255), -1)
                cv2.putText(frame, f"X:{cX} Y:{cY}", (cX + 10, cY - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

            if green_marker is not None:
                c, (cX, cY) = green_marker
                green_pos = (cX, cY)

                # Draw visual overlays
                cv2.drawContours(frame, [c], -1, (0, 255, 0), 2)
                cv2.circle(frame, (cX, cY), 5, (0, 255, 0), -1)
                cv2.putText(frame, f"X:{cX} Y:{cY}", (cX + 10, cY - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

            # --- 60 Hz POSITION OUTPUT ---
            # Coordinates in pixel space (0,0 is top-left)
            # if red_pos: print(f"[{current_time:.3f}] Red Marker -> X: {red_pos[0]}, Y: {red_pos[1]}")
            # if green_pos: print(f"[{current_time:.3f}] Green Marker -> X: {green_pos[0]}, Y: {green_pos[1]}")

            red_text = f"Red   X:{red_pos[0]} Y:{red_pos[1]}" if red_pos else "Red   not found"
            green_text = f"Green X:{green_pos[0]} Y:{green_pos[1]}" if green_pos else "Green not found"
            cv2.putText(frame, red_text, (10, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.putText(frame, green_text, (10, 90),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            # Display performance readout
            cv2.putText(frame, f"FPS: {instant_fps:.1f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 0, 0), 2)

            # Display output feed
            cv2.imshow("60 FPS Nozzle Tracker", frame)

            if red_pos is not None and green_pos is not None:
                x, y, z = self.video_to_real.get_printer_head_pos(red_pos[0], red_pos[1], green_pos[0], green_pos[1])
                print(f"X: {x}")
                print(f"Y: {y}")
                print(f"Z: {z}")
                self.telemetry_server.broadcast_position(x, y, z)

            else:
                self.telemetry_server.broadcast_position(None, None, None)

            # Exit on 'q' press
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        self.telemetry_server.stop()
        self.cap.release()
        cv2.destroyAllWindows()


class TelemetryServer:
    """WebSocket server run by CamTracker.

    Broadcasts the tracked printer-head position as {"type": "position", "x", "y",
    "z", "t"} to every connected client (e.g. the Omniverse extension) once per
    processed frame, and accepts {"type": "set_calibration", "points": {...}}
    messages that rebuild the CamTracker's VideoToRealPosCalc with new corner
    points - a full calibration exchange over a single connection.
    """

    REQUIRED_CALIBRATION_POINTS = ("rtl_pos", "rtr_pos", "rbr_pos", "rbl_pos", "gt_pos", "gb_pos")

    def __init__(self, cam_tracker, host=WS_HOST, port=WS_PORT):
        self.cam_tracker = cam_tracker
        self.host = host
        self.port = port
        self._server = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def broadcast_position(self, x, y, z):
        # Server hasn't finished starting yet, or no clients are connected.
        if self._server is None:
            return
        payload = json.dumps({"type": "position", "x": x, "y": y, "z": z, "t": time.time()})
        # Not using websockets.broadcast(): in websockets 17.1 it skips every sync
        # connection, because it checks `send_in_progress is not None` while the
        # sync Connection initializes that field to False rather than None. Sending
        # to each connection directly still goes through Connection.send()'s own
        # lock, so it stays safe to call from this thread while handler threads are
        # blocked receiving on the same connections.
        for connection in list(self._server.connections):
            try:
                connection.send(payload)
            except ConnectionClosed:
                pass

    def _run(self):
        with ws_serve(self._handle_client, self.host, self.port) as server:
            self._server = server
            print(f"[TelemetryServer] Listening on ws://{self.host}:{self.port}")
            server.serve_forever()

    def _handle_client(self, connection):
        # Cache the address up front: reading it again in `finally`, after the
        # socket is already closed, raises OSError instead of returning a value.
        remote_address = connection.remote_address
        print(f"[TelemetryServer] Client connected: {remote_address}")
        try:
            for message in connection:
                self._handle_message(connection, message)
        except ConnectionClosed:
            pass
        finally:
            print(f"[TelemetryServer] Client disconnected: {remote_address}")

    def _handle_message(self, connection, message):
        try:
            data = json.loads(message)
        except (TypeError, ValueError) as error:
            self._reply_error(connection, f"Malformed JSON: {error}")
            return

        if data.get("type") != "set_calibration":
            self._reply_error(connection, f"Unknown message type: {data.get('type')!r}")
            return

        points = data.get("points", {})
        missing = [key for key in self.REQUIRED_CALIBRATION_POINTS if key not in points]
        if missing:
            self._reply_error(connection, f"Missing calibration points: {missing}")
            return

        try:
            new_calc = VideoToRealPosCalc(**{key: points[key] for key in self.REQUIRED_CALIBRATION_POINTS})
        except Exception as error:
            self._reply_error(connection, f"Invalid calibration points: {error}")
            return

        # Rebinding the attribute is a single atomic step under the GIL, so the
        # OpenCV thread reading cam_tracker.video_to_real never sees a half-built
        # object, regardless of when this runs relative to the frame loop.
        self.cam_tracker.video_to_real = new_calc
        connection.send(json.dumps({"type": "calibration_ack", "points": points}))
        print(f"[TelemetryServer] Calibration updated: {points}")

    @staticmethod
    def _reply_error(connection, message):
        print(f"[TelemetryServer] {message}")
        try:
            connection.send(json.dumps({"type": "calibration_error", "error": message}))
        except ConnectionClosed:
            pass


class VideoToRealPosCalc:
    # Those point should be get through calibration
    def __init__(self, rtl_pos=[129, 217], rtr_pos=[444, 222], rbr_pos=[395, 477], rbl_pos=[135, 462], gt_pos=[340, 522], gb_pos=[371,703]):
        # points for Anycubic i3 Mega
        # red marker Y, Z part
        self.r_printer_dst_pts = np.array([
            [0,205],
            [210, 205],
            [210, 0],
            [0, 0]
        ])

        r_camera_src_pts = np.array([
            rtl_pos,
            rtr_pos,
            rbr_pos,
            rbl_pos
        ])
        self.homograpgy_matrix = self._create_homography_matrix(r_camera_src_pts)

        # gree marker Y part
        self.printer_y_max = 210
        self.g_pts_a = np.array(gt_pos, dtype=np.float32)
        self.g_pts_b = np.array(gb_pos, dtype=np.float32)
        self.diff_ab = self.g_pts_a - self.g_pts_b
        self.g_len_sq = np.dot(self.diff_ab, self.diff_ab)

    def _create_homography_matrix(self, src_points):
        matrix, _ = cv2.findHomography(src_points, self.r_printer_dst_pts)
        if matrix is None:
            raise ValueError(
                "Could not compute homography from calibration points "
                "(check for duplicate or collinear points)"
            )
        return matrix

    def get_printer_head_pos(self, rcam_x, rcam_y, gcam_x, gcam_y):
        # calculate red marker
        detected_cam_pos = np.array([[[rcam_x, rcam_y]]], dtype=np.float32)
        transformed_pos = cv2.perspectiveTransform(detected_cam_pos, self.homograpgy_matrix)
        fin_x, fin_z = transformed_pos[0][0]

        # calcualte green marker
        point = np.array([gcam_x, gcam_y], dtype=np.float32)
        point_diff = point - self.g_pts_a
        normalized_pos = np.dot(point_diff, self.diff_ab) / self.g_len_sq
        fin_y = normalized_pos * self.printer_y_max * -1

        # cv2.perspectiveTransform/np.dot return numpy.float32 scalars, which
        # json.dumps (used by TelemetryServer's broadcast) can't serialize -
        # cast to plain Python floats here so every consumer gets native types.
        return float(fin_x), float(fin_y), float(fin_z)


if __name__ == "__main__":
    CamTracker().main()
