"""End-to-end H.264 check with an aiortc viewer (Playwright's Chromium lacks H.264).

The viewer forces H.264 like Chrome/Edge/Safari would pick it, decodes frames
and checks the negotiated profile is Constrained Baseline (42e01f).
Usage: python tests/e2e_h264.py [webcam|buddy3d]
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import time

import aiohttp
from aiortc import RTCPeerConnection, RTCRtpReceiver, RTCSessionDescription

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from prusa_rtsp_live import proto as p  # noqa: E402
from tests.mock_prusa import Mock, serve  # noqa: E402

PORT = 18081
TOKEN = "Aa1Bb2Cc3Dd4Ee5Ff6Gg"
CAMERA_URL = os.environ.get("CAMERA_URL", "rtsp://127.0.0.1:8554/cam")


async def viewer(seconds: float) -> dict:
    result = {"frames": 0, "sizes": set(), "keyframes": 0}
    pc = RTCPeerConnection()
    done = asyncio.Event()

    @pc.on("track")
    def on_track(track):
        async def pull():
            t0 = time.monotonic()
            while time.monotonic() - t0 < seconds:
                frame = await track.recv()
                result["frames"] += 1
                result["sizes"].add((frame.width, frame.height))
            done.set()
        asyncio.ensure_future(pull())

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"http://127.0.0.1:{PORT}/viewer-ws") as ws:
            await ws.send_json({"type": "request", "id": "aiortc-viewer-1"})
            async for msg in ws:
                data = msg.json()
                if data["type"] == "offer":
                    result["offer"] = data["sdp"]
                    await pc.setRemoteDescription(RTCSessionDescription(data["sdp"], "offer"))
                    h264 = [c for c in RTCRtpReceiver.getCapabilities("video").codecs
                            if c.mimeType == "video/H264"]
                    for t in pc.getTransceivers():
                        if t.kind == "video":
                            t.setCodecPreferences(h264)
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    result["answer"] = pc.localDescription.sdp
                    await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})
                    break
            await asyncio.wait_for(done.wait(), seconds + 20)
            await ws.send_json({"type": "stop"})
            await asyncio.sleep(1)
    await pc.close()
    return result


async def main(mode: str) -> int:
    mock = Mock(TOKEN, ice=p.IceConfiguration(servers=[], policy=p.POLICY_ALL))
    runner = await serve(mock, PORT)
    env = dict(os.environ, MODE=mode, PRUSA_TOKEN=TOKEN, CAMERA_URL=CAMERA_URL,
               API_URL=f"http://127.0.0.1:{PORT}", SIGNALING_URL=f"http://127.0.0.1:{PORT}",
               STUN_FALLBACK="", LOG_LEVEL="INFO", HEALTH_FILE="/tmp/e2e2.health",
               VIDEO_MAX_FPS="20")
    root = pathlib.Path(__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, "-m", "prusa_rtsp_live"], cwd=root, env=env)
    try:
        deadline = time.monotonic() + 30
        while not mock.camera_sid and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert mock.camera_sid, "camera never authenticated"
        seconds = 8.0
        r = await viewer(seconds)
        answer_codecs = [l for l in r["answer"].splitlines() if l.startswith("a=rtpmap")]
        fmtp = [l for l in r["answer"].splitlines() if l.startswith("a=fmtp")]
        fps = r["frames"] / seconds
        print("answer codecs:", answer_codecs)
        print("answer fmtp:", fmtp)
        print(f"frames: {r['frames']} (~{fps:.1f} fps), sizes: {r['sizes']}")
        assert any("H264" in l for l in answer_codecs), "H.264 not negotiated"
        assert any("42e01f" in l for l in fmtp), "expected Constrained Baseline 42e01f"
        assert (1920, 1080) in r["sizes"], "expected full 1080p (no cap requested)"
        assert 15 <= fps <= 21, f"fps cap of 20 not respected ({fps:.1f})"
        print(f"E2E H.264 {mode}: PASS")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        await runner.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "webcam")))
