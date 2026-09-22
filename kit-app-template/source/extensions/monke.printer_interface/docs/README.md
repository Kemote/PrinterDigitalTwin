# Printer Interface [monke.printer_interface]

This extension gathers telemetry from the webcam-based `cam_tracker` and from OctoPrint, and uses it to drive a visual representation of the printer on a USD stage via usdrt.

## Setup

This extension's Python dependencies (`websocket-client`, `requests`, `websockets`, `httpx`, ...) are vendored into
`pip_prebundle/`, which is **not** committed to git (see `tools/deps/pip.toml`). Before running the extension on a fresh
clone, fetch them once:

```
./repo.sh build --fetch-only
```

Re-run this whenever `tools/deps/pip.toml` changes (e.g. a new package is added).
