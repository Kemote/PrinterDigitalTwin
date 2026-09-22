import re
import os
import json
import asyncio
import httpx
import websockets
from websockets.exceptions import ConnectionClosed


class PrinterBridge:
    """Talks to OctoPrint: authenticates, listens to its status websocket for
    live printer state/telemetry, and polls M114 so position (tele_x/y/z)
    shows up in that feed. Owns its own concurrency - run() fans out into the
    websocket listener and the M114 poller as sibling tasks - so extension.py
    only has to schedule one coroutine via omni.kit.async_engine.
    """

    def __init__(self, extension_queue):
        self._M114_RE = re.compile(r"X:(-?\d+\.?\d*)\s+Y:(-?\d+\.?\d*)\s+Z:(-?\d+\.?\d*)")
        self.ws = None
        self._queue = extension_queue
        self.username = None
        self.session = None
        self.home_pos = True
        self.is_printing = False
        self.is_paused = False
        self.is_ready = False
        self.octo_url = os.environ.get("OCTO_URL")
        self.octo_api_key = os.environ.get("OCTO_API_KEY")
        self.octo_ws_url = os.environ.get("OCTO_WS_URL")
        headers = {"X-Api-Key": self.octo_api_key} if self.octo_api_key else {}
        self._client = httpx.AsyncClient(headers=headers)

    async def run(self):
        if not (self.octo_url and self.octo_api_key and self.octo_ws_url):
            print(
                "[PrinterBridge] OCTO_URL, OCTO_API_KEY and OCTO_WS_URL "
                "must all be set; skipping OctoPrint connection."
            )
            return

        # fetch a session token up front so the websocket can authenticate on open
        await self._authenticate()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._listen_websocket())
            tg.create_task(self._poll_m114())

    async def aclose(self):
        await self._client.aclose()

    async def _authenticate(self):
        while True:
            try:
                self.username, self.session = await self.get_session_token()
                return
            except Exception as error:
                print(f"[PrinterBridge] Failed to fetch OctoPrint session token: {error}, waiting for connection")
                await asyncio.sleep(1)

    def _get_flags(self, payload):
        state_data = payload.get("state", {})
        flags = state_data.get("flags", {})
        self.is_printing = flags.get("printing", False)
        self.is_paused = flags.get("paused", False)
        self.is_ready = flags.get("ready", False)

    async def _listen_websocket(self):
        # `async for ws in websockets.connect(...)` reconnects automatically
        # (with backoff) on drop, re-authenticating each time.
        async for ws in websockets.connect(self.octo_ws_url):
            self.ws = ws
            try:
                print("[PrinterBridge] WebSocket Connected. Authenticating...")
                await ws.send(json.dumps({"auth": f"{self.username}:{self.session}"}))
                async for message in ws:
                    await self._handle_message(message)
            except ConnectionClosed as error:
                print(f"[PrinterBridge] WebSocket Closed: {error}")
            finally:
                self.ws = None

    async def _poll_m114(self):
        url = f"{self.octo_url}/api/printer/command"
        while True:
            try:
                await self._client.post(url, json={"command": "M114"}, timeout=1)
            except Exception as error:
                print(f"[PrinterBridge] Failed to send M114: {error}")
            await asyncio.sleep(0.5)

    async def get_session_token(self):
        url = f"{self.octo_url}/api/login"
        response = await self._client.post(url, json={"passive": True}, timeout=10)

        if response.status_code == 200:
            data = response.json()
            return data.get("name"), data.get("session")
        else:
            raise Exception(f"Failed to fetch session token: {response.status_code} - {response.text}")

    async def send_printer_home(self):
        if not self.is_printing:
            await self._client.post(
                f"{self.octo_url}/api/printer/printhead",
                json={"command": "home", "axes": ["x", "y", "z"]},
                timeout=10,
            )

    async def set_position(self, x, y, z):
        print(f"[PrinterBridge] Setting printer position {x}, {y}, {z}...")
        if self.is_printing:
            return False

        url = f"{self.octo_url}/api/printer/printhead"
        response = await self._client.post(url, json={"command": "jog", "x": x, "y": y, "z": z}, timeout=10)
        if response.status_code in (200, 204):
            return True
        else:
            raise Exception(f"Error occured during moving to position: {response.status_code} - {response.text}")

    def _parse_position_from_logs(self, logs):
        for line in logs:
            match = self._M114_RE.search(line)
            if match:
                x, y, z = match.groups()
                return float(x), float(y), float(z)
        return None, None, None

    async def _handle_message(self, message):
        data = json.loads(message)
        refined_data = {}
        payload = data.get("current")

        if payload:
            self._get_flags(payload)

            # set inital home pos if not printing
            if self.home_pos:
                await self.send_printer_home()
                self.home_pos = False

            # get temps
            temps = payload.get("temps", [{}])
            if len(temps) > 0:
                temps = temps[0]
                refined_data |= {
                    "bed_actual": temps.get("bed", {}).get("actual", 0.0),
                    "bed_target": temps.get("bed", {}).get("target", 0.0),
                }

            # get telemetry position
            logs = payload.get("logs", [])
            refined_data["tele_x"], refined_data["tele_y"], refined_data["tele_z"] = self._parse_position_from_logs(logs)

            self._queue.put_nowait(refined_data)
