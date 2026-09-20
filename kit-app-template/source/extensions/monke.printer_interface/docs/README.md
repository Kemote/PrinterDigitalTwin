# Printer Interface [monke.printer_interface]

This is an example of pure python Kit extension. It is intended to be copied and to serve as a template to create new ones.

## Setup

This extension's Python dependencies (`websocket-client`, `requests`, `websockets`, `httpx`, ...) are vendored into
`pip_prebundle/`, which is **not** committed to git (see `tools/deps/pip.toml`). Before running the extension on a fresh
clone, fetch them once:

```
./repo.sh build --fetch-only
```

Re-run this whenever `tools/deps/pip.toml` changes (e.g. a new package is added).
