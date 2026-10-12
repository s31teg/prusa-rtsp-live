"""Wires the camera source, Prusa Connect API, snapshots and live view together."""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from . import config as config_mod
from . import encoder
from .connect_api import ConnectApi
from .signaling import Signaling
from .source import FrameSource, frame_to_jpeg
from .tokens import mask
from . import webrtc_session
from .webrtc_session import SessionManager

log = logging.getLogger("prusa_rtsp_live")


@dataclass
class State:
    width: Optional[int] = None
    height: Optional[int] = None
    registered: bool = False
    last_snapshot_ok: float = 0.0


class App:
    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.state = State()
        self.source = FrameSource(cfg.camera_url, cfg.rtsp_transport, cfg.source_idle_seconds)
        self.http: Optional[aiohttp.ClientSession] = None
        self.api: Optional[ConnectApi] = None
        self.signaling: Optional[Signaling] = None
        self._snapshot_now = asyncio.Event()
        self._stop = asyncio.Event()

    # -- snapshots ------------------------------------------------------------

    async def grab_jpeg(self) -> Optional[bytes]:
        loop = asyncio.get_running_loop()
        # holding the source briefly also restarts its idle timer, so regular
        # snapshots keep the stream open instead of reconnecting every time
        self.source.acquire()
        try:
            frame = self.source.latest(max_age=3.0)
            if frame is None:
                _, frame = await self.source.next_frame(0, timeout=25.0)
        except asyncio.TimeoutError:
            log.warning("No picture from the camera within 25s (%s)",
                        self.source.last_error or "no frames")
            return None
        finally:
            self.source.release()
        self._update_resolution(frame.width, frame.height)
        return await loop.run_in_executor(None, frame_to_jpeg, frame,
                                          self.cfg.snapshot_max_width, self.cfg.snapshot_quality)

    def _update_resolution(self, width: int, height: int) -> None:
        w, h = width, height
        # report what viewers get, not the camera's native size
        scale = min(1.0, self.cfg.max_width / w, self.cfg.max_height / h)
        w, h = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
        if (w, h) != (self.state.width, self.state.height):
            changed = self.state.width is not None
            self.state.width, self.state.height = w, h
            if changed and self.state.registered:
                asyncio.ensure_future(self.api.put_info(w, h))

    async def snapshot_loop(self) -> None:
        interval = self.cfg.snapshot_interval
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                jpeg = await self.grab_jpeg()
                if jpeg:
                    status = await self.api.put_snapshot(jpeg)
                    if status < 300:
                        if not self.state.last_snapshot_ok:
                            log.info("Snapshots are reaching Prusa Connect (%d KB)", len(jpeg) // 1024)
                        self.state.last_snapshot_ok = time.time()
                        self.touch_health()
                    elif status in (401, 403):
                        log.error("Prusa Connect refused the snapshot (HTTP %d): wrong token or "
                                  "fingerprint", status)
            except aiohttp.ClientError as exc:
                log.warning("Snapshot upload failed: %s", exc)
            except Exception as exc:
                log.exception("Snapshot failed: %s", exc)
            wait = max(1.0, interval - (time.monotonic() - started))
            self._snapshot_now.clear()
            try:
                await asyncio.wait_for(self._snapshot_now.wait(), wait)
            except asyncio.TimeoutError:
                pass

    def request_snapshot(self) -> None:
        self._snapshot_now.set()

    # -- registration -----------------------------------------------------------

    async def register(self) -> None:
        for attempt in range(1, 1000):
            try:
                status = await self.api.put_info(self.state.width, self.state.height)
                if status < 300:
                    self.state.registered = True
                    return
                if status in (401, 403):
                    log.error("Prusa Connect refused the camera info (HTTP %d). The token may "
                              "belong to another camera type, or be bound to another FINGERPRINT.",
                              status)
            except aiohttp.ClientError as exc:
                log.warning("Camera info upload failed: %s", exc)
            await asyncio.sleep(min(300, 10 * attempt))

    async def startup(self) -> None:
        # learn the resolution first so the camera registers with it
        try:
            self.source.acquire()
            try:
                _, frame = await self.source.next_frame(0, timeout=20.0)
                self._update_resolution(frame.width, frame.height)
            finally:
                self.source.release()
        except asyncio.TimeoutError:
            log.warning("Camera not reachable yet (%s); registering without a resolution",
                        self.source.last_error or "no frames")
        await self.register()

    async def info_refresh_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(3600)
            try:
                await self.api.put_info(self.state.width, self.state.height)
            except Exception as exc:
                log.debug("info refresh failed: %s", exc)

    # -- health -------------------------------------------------------------------

    def touch_health(self) -> None:
        try:
            with open(self.cfg.health_file, "w") as fh:
                fh.write(str(int(time.time())))
        except OSError:
            pass

    async def health_loop(self) -> None:
        # with snapshots off, a working signaling link counts as healthy
        while not self._stop.is_set():
            if not self.cfg.snapshots_enabled and self.signaling and self.signaling.authenticated:
                self.touch_health()
            await asyncio.sleep(30)

    # -- main ---------------------------------------------------------------------

    async def run(self) -> None:
        cfg = self.cfg
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass
        self.source.bind(loop)
        encoder.configure(cfg.x264_preset, cfg.max_bitrate, cfg.start_bitrate, cfg.max_fps)
        webrtc_session.DEFAULT_STUN[:] = [cfg.stun_fallback] if cfg.stun_fallback else []
        log.info("prusa-rtsp-live starting: mode=%s token=%s camera=%s snapshots=%s live=%s",
                 cfg.mode, mask(cfg.token), cfg.camera_name,
                 f"every {cfg.snapshot_interval}s" if cfg.snapshots_enabled else "off",
                 f"up to {cfg.max_width}x{cfg.max_height}@{cfg.max_fps}" if cfg.live_enabled else "off")

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            self.http = http
            self.api = ConnectApi(cfg, http)
            self.signaling = Signaling(
                cfg, self.state,
                manager_factory=lambda sig: SessionManager(cfg, self.source, sig),
                on_snapshot_request=self.request_snapshot)
            tasks = [asyncio.create_task(self.startup(), name="startup"),
                     asyncio.create_task(self.info_refresh_loop(), name="info"),
                     asyncio.create_task(self.signaling.run(), name="signaling"),
                     asyncio.create_task(self.health_loop(), name="health")]
            if cfg.snapshots_enabled:
                tasks.append(asyncio.create_task(self.snapshot_loop(), name="snapshots"))
            await self._stop.wait()
            log.info("Shutting down")
            await self.signaling.stop()
            await self.signaling.manager.stop_all()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.source.close()
