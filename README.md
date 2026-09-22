# 3D Printer Digital Twin

## Description

This project shows an example of using real printer telemetry inside NVIDIA Omniverse. It's a digital twin of a 3D printer, modeled in Blender and published using my ["Monke Pipeline"](https://github.com/Kemote/MonkePipeline) project.

The head position is tracked by `cam_tracker` instead of relying purely on OctoPrint's telemetry, because OctoPrint doesn't stream position data in real time. For smooth movement, the head position is tracked with a webcam and OpenCV: two colored markers are detected in each frame, and their pixel positions are converted into real coordinates using a homography.

OctoPrint's telemetry is still used for two things: visualizing bed temperature as material color, and providing the printer's real X/Y/Z during camera calibration.

## Components

### `cam_tracker/`

Tracks the printer head's X/Y/Z position from a webcam and broadcasts it over a websocket.

- `app.py` - the real camera tracker.
- `fake_app.py` - sends synthetic position data instead of using a webcam, so the Omniverse extension can be tested against OctoPrint's virtual printer with no camera or physical printer attached.

### Omniverse extension - `kit-app-template/source/extensions/monke.printer_interface/monke/printer_interface/`

- `extension.py` - the extension's entry point.
- `printer_bridge.py` - handles communication with the OctoPrint server.
- `printer_vision.py` - handles communication with `cam_tracker`.
- `usd_stage_manager.py` - the class with all the methods used to modify the stage through UsdRt.
- `calibration.py` - runs calibration using OctoPrint and webcam data, then saves it to `calibration.json` so it doesn't need to run again on the next launch. Delete that file, or set `RECALIBRATE_VISION=True`, to force a fresh calibration on the next extension start.

  Calibration takes a while because there's no way to tell whether the camera has finished settling on a position - the camera used is too low quality to make that possible. On top of that, the firmware's M114 response always reports the current *target* position, not the physical one, so we can't assume the moment we receive it is the moment the head has actually arrived.

### Other files

- `calibration.json` - created during calibration; lets the extension skip recalibrating on every launch.
- `run.sh` - runs the Omniverse extension.
- `run_streaming.sh` - runs Omniverse with the WebRTC streaming layer.
- `run_octoprint.sh` - runs OctoPrint from the terminal.
