"""Manueller Test fuer /v1/assistant/stream im Text-Modus.

Aufruf:
    uv run python scripts/manual_ws_test.py "Wie spaet ist es?"
    uv run python scripts/manual_ws_test.py "Frage" --url ws://192.168.2.105:8000/v1/assistant/stream
"""

import argparse
import asyncio
import json

import websockets


async def run(url: str, text: str) -> None:
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "mode": "chat"}))
        await ws.send(json.dumps({"type": "text_input", "text": text}))

        async for raw in ws:
            frame = json.loads(raw)
            print(frame)
            if frame.get("type") == "done":
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("text", help="Text-Nachricht an den Orchestrator")
    parser.add_argument("--url", default="ws://localhost:8000/v1/assistant/stream")
    args = parser.parse_args()

    asyncio.run(run(args.url, args.text))
