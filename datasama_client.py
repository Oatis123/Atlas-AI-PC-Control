import asyncio
import json
import logging
import websockets

logger = logging.getLogger("DataSamaClient")


class DataSamaClient:
    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:2026/ws/integrations",
        integration_name: str = "AI_PC_Control",
        rest_url: str = "http://127.0.0.1:5050",
    ):
        self.ws_url = ws_url
        self.integration_name = integration_name
        self.rest_url = rest_url
        self.websocket = None
        self._running = False
        self._loop_task = None

    async def start(self):
        """Starts background connection loop."""
        self._running = True
        self._loop_task = asyncio.create_task(self._connect_loop())

    async def stop(self):
        """Stops background connection loop."""
        self._running = False
        if self.websocket:
            await self.websocket.close()
        if self._loop_task:
            self._loop_task.cancel()

    async def _connect_loop(self):
        while self._running:
            try:
                logger.info(f"Connecting to Data-Sama WebSocket at {self.ws_url}...")
                async with websockets.connect(self.ws_url) as ws:
                    self.websocket = ws
                    logger.info("Connected to Data-Sama WebSocket.")
                    await self._send_init()

                    # Keep reading incoming messages if any
                    async for message in ws:
                        logger.info(f"Received message from Data-Sama: {message}")
            except (websockets.exceptions.ConnectionClosed, OSError, Exception) as e:
                self.websocket = None
                if not self._running:
                    break
                logger.warning(
                    f"WebSocket connection lost ({e}). Retrying in 5 seconds..."
                )
                await asyncio.sleep(5)

    async def _send_json(self, payload: dict):
        if self.websocket is not None:
            try:
                msg = json.dumps(payload, ensure_ascii=False)
                await self.websocket.send(msg)
                logger.info(f"Sent WS event '{payload.get('event')}': {msg}")
            except Exception as e:
                logger.error(f"Failed to send WS message: {e}")
                raise
            else:
                logger.warning(
                    "Cannot send WS message: WebSocket is disconnected."
                )

    async def _send_init(self):
        init_payload = {
            "event": "init",
            "data": {
                "integration_name": self.integration_name,
                "rest_url": self.rest_url,
                "system_instructions": (
                    "Use this plugin to execute any autonomous tasks on the user's PC "
                    "(launching programs, window management, searching files, running bash console commands, clicking)."
                ),
                "tools": [
                    {
                        "name": "execute_pc_task",
                        "type": "async",
                        "path": "/tools/run-pc-agent",
                        "description": "Instruct the autonomous PC control agent to perform a task on the computer.",
                        "parameters": {
                            "prompt": "string"
                        },
                    }
                ],
            },
        }
        await self._send_json(init_payload)

    async def send_background_update(self, payload_text: str):
        event_data = {
            "event": "state_update",
            "integration_name": self.integration_name,
            "data": {
                "update_type": "background",
                "payload": payload_text,
            },
        }
        await self._send_json(event_data)

    async def send_tool_result(self, tool_name: str, status: str, output: str):
        event_data = {
            "event": "tool_result",
            "integration_name": self.integration_name,
            "tool_name": tool_name,
            "data": {
                "status": status,
                "output": str(output),
            },
        }
        await self._send_json(event_data)