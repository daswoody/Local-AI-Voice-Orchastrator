"""Manueller Test des Audio-Pfads von /v1/assistant/stream (Mikro-Phase 1.11).

Sendet hello (talk-Modus) + 0.5s Test-Audio (PCM16/16k) + audio_end und
druckt alle Antwort-Frames (Audio-Payload gekuerzt).

    uv run python scripts/manual_audio_ws_test.py
    uv run python scripts/manual_audio_ws_test.py --url ws://192.168.2.105:8000/v1/assistant/stream
"""

import argparse
import asyncio
import base64
import json
import math
import struct

import websockets

INPUT_RATE = 16000


def _test_audio(seconds: float = 0.5) -> bytes:
    samples = (
        int(20000 * math.sin(2 * math.pi * 440 * i / INPUT_RATE))
        for i in range(int(INPUT_RATE * seconds))
    )
    return b"".join(struct.pack("<h", s) for s in samples)


async def run(url: str) -> None:
    audio_bytes_received = 0
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "mode": "talk"}))
        await ws.send(json.dumps({
            "type": "audio_chunk",
            "audio": base64.b64encode(_test_audio()).decode(),
        }))
        await ws.send(json.dumps({"type": "audio_end"}))

        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("type") == "audio_chunk":
                audio_bytes_received += len(base64.b64decode(frame["audio"]))
                frame["audio"] = f"<{len(frame['audio'])} b64-Zeichen>"
            print(frame)
            if frame.get("type") == "done":
                break

    print(f"\nEmpfangenes Audio gesamt: {audio_bytes_received} Bytes PCM16")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://localhost:8000/v1/assistant/stream")
    args = parser.parse_args()
    asyncio.run(run(args.url))
