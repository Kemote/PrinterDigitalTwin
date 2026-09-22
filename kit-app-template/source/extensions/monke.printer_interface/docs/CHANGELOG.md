# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).


## [0.1.0] - 2026-09-08
- Initial version of basic python extension template

## [0.1.1] - 2026-09-22
- Added camera-based head-position tracking and calibration against `cam_tracker`
- Migrated the extension from threading to asyncio
- Added WebRTC livestreaming support
- Added `fake_app.py` (in `cam_tracker/`) to simulate camera tracking from OctoPrint telemetry, for testing without a camera or physical printer
