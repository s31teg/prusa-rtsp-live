"""Camera input: decodes the RTSP stream on demand and shares frames.

One background thread reads and decodes the camera while at least one consumer
holds it (live viewers, or the snapshot loop briefly). Every decoded frame gets
a shared presentation timestamp so several WebRTC sessions can encode the same
frame object without touching it.
"""
from __future__ import annotations

import asyncio
import fractions
import io
import logging
import threading
import time
from typing import Optional

import av

log = logging.getLogger("prusa_rtsp_live.source")

VIDEO_TIME_BASE = fractions.Fraction(1, 90000)


def _redact_url(url: str) -> str:
    # hide credentials and the UniFi stream key in logs
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        scheme, rest = url.split("//", 1)
        url = scheme + "//***@" + rest.split("@", 1)[1]
    head, sep, tail = url.rpartition("/")
    if sep and len(tail) > 8:
        return head + "/" + tail[:4] + "…"
    return url


class FrameSource:
    def __init__(self, url: str, rtsp_transport: str = "tcp", idle_seconds: int = 15) -> None:
        self.url = url
        self.rtsp_transport = rtsp_transport
        self.idle_seconds = idle_seconds
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._users = 0
        self._live_users = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._latest: Optional[av.VideoFrame] = None
        self._latest_time = 0.0
        self._seq = 0
        self._new_frame: Optional[asyncio.Event] = None
        self._idle_handle: Optional[asyncio.TimerHandle] = None
        self._epoch = time.monotonic()
        self.width: Optional[int] = None
        self.height: Optional[int] = None
        self.last_error: Optional[str] = None

    # -- lifecycle -----------------------------------------------------------

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._new_frame = asyncio.Event()

    def acquire(self, live: bool = False) -> None:
        self._users += 1
        if live:
            self._live_users += 1
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="rtsp-reader", daemon=True)
            self._thread.start()
            log.info("Opening camera stream %s", _redact_url(self.url))

    def release(self, live: bool = False) -> None:
        self._users = max(0, self._users - 1)
        if live:
            self._live_users = max(0, self._live_users - 1)
        if self._users == 0 and self._loop is not None:
            if self._idle_handle:
                self._idle_handle.cancel()
            self._idle_handle = self._loop.call_later(self.idle_seconds, self._stop_if_idle)

    def _stop_if_idle(self) -> None:
        self._idle_handle = None
        if self._users == 0 and self._thread is not None:
            log.info("No viewers or snapshots pending, closing camera stream")
            self._stop.set()
            self._thread = None
            self._latest = None

    def close(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- frames --------------------------------------------------------------

    def latest(self, max_age: float = 2.0) -> Optional[av.VideoFrame]:
        if self._latest is not None and time.monotonic() - self._latest_time <= max_age:
            return self._latest
        return None

    async def next_frame(self, after_seq: int, timeout: float = 10.0) -> tuple[int, av.VideoFrame]:
        assert self._new_frame is not None, "bind() first"
        deadline = time.monotonic() + timeout
        while self._seq == after_seq or self._latest is None:
            event = self._new_frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(event.wait(), remaining)
        return self._seq, self._latest

    def _publish(self, frame: av.VideoFrame) -> None:
        # runs on the event loop thread
        self._latest = frame
        self._latest_time = time.monotonic()
        self._seq += 1
        event, self._new_frame = self._new_frame, asyncio.Event()
        if event is not None:
            event.set()

    # -- reader thread -------------------------------------------------------

    def _open(self):
        options = {"fflags": "nobuffer", "flags": "low_delay"}
        if self.url.lower().startswith(("rtsp://", "rtsps://")):
            options["rtsp_transport"] = self.rtsp_transport
            options["timeout"] = "10000000"  # microseconds, socket I/O timeout
        return av.open(self.url, options=options, timeout=(15.0, 10.0))

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            container = None
            try:
                container = self._open()
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                backoff = 1.0
                self.last_error = None
                skip = None
                wait_key = False
                for frame in container.decode(stream):
                    if self._stop.is_set():
                        break
                    # Without live viewers only keyframes are decoded: enough for
                    # snapshots at a fraction of the CPU.
                    wanted = "DEFAULT" if self._live_users > 0 else "NONKEY"
                    if wanted != skip:
                        try:
                            stream.codec_context.skip_frame = wanted
                        except Exception:
                            pass
                        # frames after a switch may reference undecoded ones:
                        # publish again from the next keyframe
                        wait_key = skip is not None
                        skip = wanted
                    if wait_key:
                        if not frame.key_frame:
                            continue
                        wait_key = False
                    if self.width != frame.width or self.height != frame.height:
                        self.width, self.height = frame.width, frame.height
                        log.info("Camera stream is %dx%d (%s)", frame.width, frame.height,
                                 stream.codec_context.name)
                    frame.pts = int((time.monotonic() - self._epoch) * 90000)
                    frame.time_base = VIDEO_TIME_BASE
                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(self._publish, frame)
                if not self._stop.is_set():
                    log.warning("Camera stream ended, reconnecting")
            except Exception as exc:  # network errors, decoder errors, timeouts
                self.last_error = str(exc)
                log.warning("Camera stream error: %s (retrying in %.0fs)", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        pass


def frame_to_jpeg(frame: av.VideoFrame, max_width: int, quality: int) -> bytes:
    image = frame.to_image()
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, height))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
