"""End-to-end test: RTSP camera -> bridge -> mock Prusa Connect -> Chromium viewer.

Needs an RTSP(S) test stream (CAMERA_URL) and Playwright's Chromium.
Usage: python tests/e2e.py [webcam|buddy3d]
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright  # noqa: E402

from prusa_rtsp_live import proto as p  # noqa: E402
from prusa_rtsp_live.tokens import obfuscate  # noqa: E402
from tests.mock_prusa import Mock, serve  # noqa: E402

PORT = 18080
TOKEN = "Zz9yX8wQ3vB7nM2kL5pR"
CAMERA_URL = os.environ.get("CAMERA_URL", "rtsps://127.0.0.1:8322/cam?enableSrtp")


async def wait_for(pred, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


async def main(mode: str) -> int:
    video = p.VideoSettings(video_quality=p.QUALITY_HD, video_fps=20,
                            apply_scope=p.SCOPE_ANY, ttl=0)
    mock = Mock(TOKEN, ice=p.IceConfiguration(servers=[], policy=p.POLICY_ALL), video=video)
    runner = await serve(mock, PORT)
    token_value = (f"https://camera-service-webcam.prusa3d.com/#t={obfuscate(TOKEN)}"
                   if mode == "webcam" else TOKEN)
    env = dict(os.environ, MODE=mode, PRUSA_TOKEN=token_value, CAMERA_URL=CAMERA_URL,
               API_URL=f"http://127.0.0.1:{PORT}", SIGNALING_URL=f"http://127.0.0.1:{PORT}",
               STUN_FALLBACK="", SNAPSHOT_INTERVAL="10", LOG_LEVEL="INFO",
               HEALTH_FILE="/tmp/e2e.health", CAMERA_NAME="Test Cam")
    if mode == "buddy3d":
        env["FINGERPRINT"] = "camera0000000001"
    root = pathlib.Path(__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, "-m", "prusa_rtsp_live"], cwd=root, env=env)
    ok = False
    try:
        await wait_for(lambda: any(e[0] == "auth" for e in mock.events), 30, "camera auth")
        await wait_for(lambda: mock.infos, 40, "camera info")
        await wait_for(lambda: mock.snapshots, 40, "first snapshot")
        await wait_for(lambda: any(e[0] == "status" for e in mock.events), 10, "status")
        print("info body:", mock.infos[-1])
        print("snapshot: %d bytes, %s" % (len(mock.snapshots[-1]), mock.snapshots[-1][:3].hex()))
        assert mock.snapshots[-1][:3] == b"\xff\xd8\xff", "snapshot is not a JPEG"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=[
                "--disable-features=WebRtcHideLocalIpsWithMdns",
                "--autoplay-policy=no-user-gesture-required"])
            page = await browser.new_page()
            page.on("console", lambda m: print("[viewer]", m.text))
            t0 = time.monotonic()
            await page.goto(f"http://127.0.0.1:{PORT}/viewer")
            await page.wait_for_function("window.result && window.result.state === 'connected'",
                                         timeout=30000)
            t_connected = time.monotonic() - t0
            await page.wait_for_function("window.collect && true")
            await asyncio.sleep(8)
            r = await page.evaluate("window.collect()")
            print("viewer:", {k: v for k, v in r.items() if k not in ("offer", "answer")})
            print("connected after %.1fs" % t_connected)
            offer = r.get("offer", "")
            assert "a=mid:0" in offer, "video m-line must have mid 0"
            assert "a=msid:" in offer, "track needs an msid so the viewer gets a stream"
            assert r["gotStream"], "viewer got no MediaStream"
            # Playwright's Chromium has no H.264 decoder for WebRTC, so it picks VP8;
            # Chrome/Edge/Safari pick H.264 (tests/e2e_h264.py covers that path)
            assert r.get("codec") in ("video/H264", "video/VP8"), r.get("codec")
            assert (r.get("framesDecoded") or 0) > 60, "too few frames decoded"
            assert r.get("width") == 1280 and r.get("height") == 720, "HD cap not applied"
            await page.evaluate("window.stopStream()")
            await asyncio.sleep(2)
            await browser.close()

        print("events:", sorted({e[0] for e in mock.events}))
        acks = [e for e in mock.events if e[0] == "ack"]
        print("acks:", acks)
        assert all(e[2] == 0 for e in acks), "camera returned a non-OK ack"
        if mode == "webcam":
            assert any(e[0] == "connection_info" for e in mock.events)
            assert any(e[0] == "features" for e in mock.events)
        ok = True
        print(f"E2E {mode}: PASS")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        await runner.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "webcam")))
