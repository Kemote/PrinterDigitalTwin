# 3D Printer Digital Twin

## Demo

🎥 [Watch the demo video](https://youtu.be/iVV2U_lxSzM)

## Description

This project shows an example of using real printer telemetry inside NVIDIA Omniverse. It's a digital twin of a 3D printer, modeled in Blender and published using my ["Monke Pipeline"](https://github.com/Kemote/MonkePipeline) project.

The head position is tracked by `cam_tracker` instead of relying purely on OctoPrint's telemetry, because OctoPrint doesn't stream position data in real time. For smooth movement, the head position is tracked with a webcam and OpenCV: two colored markers are detected in each frame, and their pixel positions are converted into real coordinates using a homography.

OctoPrint's telemetry is still used for two things: visualizing bed temperature as material color, and providing the printer's real X/Y/Z coordinates during camera calibration. It also visualizes the printing process using an OpenUSD point instancer, which is controlled by extruding printer telemetry data.

The Omniverse side is built on NVIDIA's open-source [kit-app-template](https://github.com/NVIDIA-Omniverse/kit-app-template), which provides the application scaffold, build tooling, and packaging that `monke.printer_interface` (this project's own extension) runs on top of. See `kit-app-template/LICENSE` for its license terms.

## Requirements

- **Hardware**: a 3D printer connected to [OctoPrint](https://octoprint.org/) (a real printer, or its built-in Virtual Printer for testing with no hardware at all), plus a webcam and two colored markers if you want live camera tracking instead of `fake_app.py`'s synthetic positions.
- **Software**: a Linux or Windows machine meeting the [Omniverse Kit SDK prerequisites](https://github.com/NVIDIA-Omniverse/kit-app-template#prerequisites-and-environment-setup) (NVIDIA RTX-capable GPU), a running OctoPrint instance, and Python 3 for `cam_tracker`.

## Setup & Running

1. **Configure environment variables** - copy `sample_printer.env` to `printer.env` and fill in `OCTO_URL`, `OCTO_API_KEY`, and `OCTO_WS_URL` for your OctoPrint instance. The `PROJECTNAME`/`PROJECTSROOT`/`MONKENAME`/`PXR_PLUGINPATH_NAME` variables are only needed if you're using the MonkePipeline asset resolver; `OCTOPRINT_VENV_PATH` is only needed if you use `run_octoprint.sh` to launch OctoPrint from its own virtual environment.

2. **Install `cam_tracker`'s dependencies**:
   ```bash
   pip install -r cam_tracker/requirements.txt
   ```

3. **Build the Omniverse app** (one-time, from the repo root):
   ```bash
   ./kit-app-template/repo.sh build
   ```

4. **Start OctoPrint**, pointed at a real printer or its Virtual Printer (`./run_octoprint.sh` if `OCTOPRINT_VENV_PATH` is set).

5. **Start the head tracker**:
   ```bash
   python3 cam_tracker/app.py       # real webcam + physical printer
   # or, with no camera/printer attached:
   python3 cam_tracker/fake_app.py  # synthetic markers driven by OctoPrint's real/virtual X/Y/Z
   ```

6. **Launch the digital twin**:
   ```bash
   ./run.sh             # desktop
   ./run_streaming.sh   # WebRTC streaming build
   ```
   On first launch, the extension automatically calibrates itself by jogging the print head and correlating it against the tracked marker positions (see `calibration.py`). The result is cached in `calibration.json` so this only happens once - delete that file, or set `RECALIBRATE_VISION=True`, to force it again.

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

## Credits

- [NVIDIA kit-app-template](https://github.com/NVIDIA-Omniverse/kit-app-template) - the Omniverse Kit SDK application template this project's extension is built on.
- [Monke Pipeline](https://github.com/Kemote/MonkePipeline) - used to publish the Blender-modeled printer asset into USD.
