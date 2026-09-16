import cv2
import time
import numpy as np


# --- CONFIGURATION ---
CAMERA_INDEX = 0      # Adjust if using an external USB webcam
FRAME_WIDTH = 800     # Lower resolution helps guarantee 60 FPS processing
FRAME_HEIGHT = 600
TARGET_FPS = 60
# The camera is physically mounted rotated 90 degrees; correct it in software.
# Try ROTATE_90_CLOCKWISE first - if the feed still looks sideways/upside down,
# switch to ROTATE_90_COUNTERCLOCKWISE or ROTATE_180.
FRAME_ROTATION = cv2.ROTATE_90_CLOCKWISE

# Define HSV color ranges for the two markers.
# OpenCV HSV ranges: H 0-179, S 0-255, V 0-255 (not 0-360/0-100/0-100).
# Use a calibration tool or HSV picker script to fine-tune these.
# Red marker. Red wraps around the hue wheel, so it needs two ranges OR'd together.
RED_RANGES = [
    (np.array([0, 100, 100]), np.array([10, 255, 255])),
    (np.array([160, 100, 100]), np.array([179, 255, 255])),
]
# Green marker.
GREEN_RANGES = [
    (np.array([35, 100, 100]), np.array([85, 255, 255])),
]

MIN_CONTOUR_AREA = 50

def init_camera():
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2) # Use CAP_DSHOW on Windows, CAP_V4L2 on Linux
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera index {CAMERA_INDEX}. Check CAMERA_INDEX / /dev/video*.")
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)

    # Verify actual frame rate set by hardware driver
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"Camera initialized. Target: {TARGET_FPS} FPS | Actual Driver Rate: {actual_fps} FPS")
    return cap

def color_mask(hsv, ranges):
    mask = None
    for lower, upper in ranges:
        m = cv2.inRange(hsv, lower, upper)
        mask = m if mask is None else cv2.bitwise_or(mask, m)

    # Morphological operations to clean up small noise blobs
    mask = cv2.erode(mask, None, iterations=2)
    mask = cv2.dilate(mask, None, iterations=2)
    return mask

def find_marker(mask, min_area=MIN_CONTOUR_AREA):
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

def main():
    cap = init_camera()
    prev_time = time.perf_counter()
    fps_history = []

    # TODO move it to init during rebuilding it to class
    video_to_real = VideoToRealPosCalc() # it should get data from extension after calibration is done

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to grab frame.")
            break

        # Correct the physical 90 degree camera mount before any detection/display
        # work runs, so every downstream pixel coordinate (markers, calibration
        # points, overlays) is already in the upright frame's coordinate space.
        frame = cv2.rotate(frame, FRAME_ROTATION)

        current_time = time.perf_counter()
        dt = current_time - prev_time
        prev_time = current_time
        instant_fps = 1.0 / dt if dt > 0 else 0

        # 1. Convert BGR to HSV color space
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # 2. Threshold the HSV image and locate each marker
        red_mask = color_mask(hsv, RED_RANGES)
        green_mask = color_mask(hsv, GREEN_RANGES)

        red_marker = find_marker(red_mask)
        green_marker = find_marker(green_mask)

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

        # TEST
        if red_pos is not None and green_pos is not None:
            x, y, z = video_to_real.get_printer_head_pos(red_pos[0], red_pos[1], green_pos[0], green_pos[1])
            print(f"X: {x}")
            print(f"Y: {y}")
            print(f"Z: {z}")

        # Exit on 'q' press
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()


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
        return fin_x, fin_y, fin_z

    
if __name__ == "__main__":
    main()