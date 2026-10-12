"""Socket.IO connection to camera-signaling.prusa3d.com (camera side)."""
from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
from typing import Callable, Optional

import socketio

from . import proto as p
from .connect_api import features_json
from .tokens import mask

log = logging.getLogger("prusa_rtsp_live.signaling")

REAUTH_INTERVAL = 12 * 3600  # the web camera re-authenticates every 12 h
AUTH_FATAL = (p.ACK_MISSING_TOKEN, p.ACK_NOT_CAMERA)
# events whose NOT_AUTHORIZED ack means "credentials are bad"
AUTH_EVENTS = ("camera_authentication", "protobuf_version", "features")
QUALITY_FROM_SIZE_BYTE = {5: p.QUALITY_SD, 6: p.QUALITY_HD, 7: p.QUALITY_FHD}


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""


class Signaling:
    def __init__(self, cfg, state, manager_factory: Callable[["Signaling"], object],
                 on_snapshot_request: Callable[[], None]) -> None:
        self.cfg = cfg
        self.state = state  # app.State: resolution, webrtc mode, etc.
        self.manager = manager_factory(self)
        self.on_snapshot_request = on_snapshot_request
        self.sio: Optional[socketio.AsyncClient] = None
        self.authenticated = False
        self.auth_failures = 0
        self._started = time.monotonic()
        self._stop = asyncio.Event()

    # -- outbound helpers -----------------------------------------------------

    async def _emit(self, event: str, payload: bytes, timeout: float = 10.0) -> Optional[int]:
        sio = self.sio
        if sio is None or not sio.connected:
            return None
        try:
            ack = await sio.call(event, payload, timeout=timeout)
        except socketio.exceptions.TimeoutError:
            log.debug("%s: no ack", event)
            return None
        except Exception as exc:
            log.debug("%s: emit failed: %s", event, exc)
            return None
        code = ack[0] if isinstance(ack, (list, tuple)) and ack else ack
        if isinstance(code, int) and code != p.ACK_OK:
            log.warning("Signaling rejected %s (code %d)", event, code)
            if code == p.ACK_NOT_AUTHORIZED and event not in AUTH_EVENTS:
                asyncio.ensure_future(self._authenticate())
        return code if isinstance(code, int) else None

    def _fire(self, event: str, payload: bytes) -> None:
        asyncio.ensure_future(self._emit(event, payload))

    async def send_offer(self, request_id: str, sdp: str) -> None:
        self._fire("webrtc", p.encode_webrtc(self.cfg.token, self.cfg.fingerprint, request_id,
                                              p.MSG_OFFER, sdp=sdp))

    async def send_candidate(self, request_id: str, candidate: str, mid: str) -> None:
        if not candidate.startswith("a="):
            candidate = "a=" + candidate
        self._fire("webrtc", p.encode_webrtc(self.cfg.token, self.cfg.fingerprint, request_id,
                                              p.MSG_CANDIDATE, sdp=candidate, mid=mid))

    async def send_connection_info(self, request_id: str, local: int, remote: int) -> None:
        if self.cfg.mode != "webcam":
            return  # Buddy3D wire format for this event is unconfirmed
        self._fire("webrtc_connection_info", p.encode_connection_info(
            self.cfg.token, self.cfg.fingerprint, request_id, local, remote))

    async def send_rejected(self, request_id: str) -> None:
        self._fire("client_trigger", p.encode_client_trigger_error(
            self.cfg.token, self.cfg.fingerprint, request_id))

    def _status(self, request_id: Optional[str] = None) -> bytes:
        cfg, st = self.cfg, self.state
        mode = p.WEBRTC_MODE_ON if self.webrtc_on else p.WEBRTC_MODE_OFF
        info = p.StatusInfo(token=cfg.token, request_id=request_id or None,
                            width=st.width, height=st.height, camera_name=cfg.camera_name,
                            hw_version=cfg.hw_version, webrtc_mode=mode)
        if cfg.mode == "buddy3d":
            info.fw_version = cfg.fw_version
            info.webrtc_status = p.ENABLE if self.webrtc_on else p.DISABLE
            info.video_quality = self.manager.quality_cap or p.QUALITY_FHD
            info.snapshot_interval = cfg.snapshot_interval
            info.snapshot_enable = p.ENABLE if cfg.snapshots_enabled else p.DISABLE
            info.ethernet_ipv4 = _local_ip() or None
            info.uptime_seconds = int(time.monotonic() - self._started)
            info.timezone = time.tzname[0] if time.tzname else None
        return p.encode_status(info)

    async def send_status(self, request_id: Optional[str] = None) -> None:
        await self._emit("status", self._status(request_id))

    def _features(self) -> bytes:
        return p.encode_features(self.cfg.token, self.cfg.fingerprint,
                                 features_json(self.cfg.mode), hw_version=self.cfg.hw_version,
                                 fw_version=self.cfg.fw_version or None)

    def _protobuf_version(self, request_id: Optional[str] = None) -> bytes:
        return p.encode_protobuf_version(self.cfg.token, self.cfg.fingerprint, request_id or None)

    @property
    def webrtc_on(self) -> bool:
        return self.manager.enabled and self.cfg.live_enabled

    # -- auth -------------------------------------------------------------------

    async def _authenticate(self) -> bool:
        code = await self._emit("camera_authentication",
                                p.encode_camera_authentication(self.cfg.token, self.cfg.fingerprint))
        if code == p.ACK_OK:
            self.authenticated = True
            self.auth_failures = 0
            log.info("Signed in to Prusa Connect signaling as camera %s", mask(self.cfg.token))
            if self.cfg.mode == "webcam":
                # the browser web camera announces itself; a Buddy3D waits to be asked
                await self._emit("protobuf_version", self._protobuf_version())
                await self._emit("features", self._features())
            return True
        self.authenticated = False
        self.auth_failures += 1
        if code in AUTH_FATAL or code == p.ACK_NOT_AUTHORIZED:
            log.error("Prusa Connect refused this camera's token (code %s). Check PRUSA_TOKEN "
                      "and FINGERPRINT; a token stays bound to the first fingerprint used with it.",
                      code)
        elif code is None:
            log.warning("No answer to camera authentication")
        return False

    # -- inbound events -------------------------------------------------------------

    def _register_handlers(self, sio: socketio.AsyncClient) -> None:
        @sio.event
        async def connect():
            log.info("Connected to %s", self.cfg.signaling_url)

        @sio.event
        async def disconnect(*_args):
            self.authenticated = False
            log.warning("Signaling connection lost")

        @sio.event
        async def connect_error(data):
            log.warning("Signaling connect error: %s", data)

        @sio.on("trigger")
        async def on_trigger(data=None, *_):
            try:
                t = p.decode_trigger(_bytes(data))
            except Exception:
                return p.ACK_DECODE_MESSAGE
            code = p.ACK_UNSUPPORTED_FEATURE
            if t.get_features:
                self._fire("features", self._features())
                code = p.ACK_OK
            if t.get_protocol_information:
                self._fire("protobuf_version", self._protobuf_version(t.request_id))
                code = p.ACK_OK
            if t.get_status:
                self._fire("status", self._status(t.request_id))
                code = p.ACK_OK
            if t.get_snapshot:
                self.on_snapshot_request()
                code = p.ACK_OK
            return code

        @sio.on("configuration")
        async def on_configuration(data=None, *_):
            try:
                c = p.decode_configuration(_bytes(data))
            except Exception:
                return p.ACK_DECODE_MESSAGE
            handled = False
            if c.webrtc_mode in (p.WEBRTC_MODE_ON, p.WEBRTC_MODE_OFF):
                await self._set_webrtc(c.webrtc_mode == p.WEBRTC_MODE_ON)
                handled = True
            if self.cfg.mode == "buddy3d" and c.video_quality in (p.QUALITY_SD, p.QUALITY_HD,
                                                                   p.QUALITY_FHD):
                self._set_quality(c.video_quality)
                handled = True
            return p.ACK_OK if handled else p.ACK_UNSUPPORTED_FEATURE

        @sio.on("webrtc")
        async def on_webrtc(data=None, *_):
            try:
                m = p.decode_webrtc(_bytes(data))
            except Exception:
                return p.ACK_DECODE_MESSAGE
            if m.stream_status == p.STREAM_STATUS_STOP:
                asyncio.ensure_future(self.manager.stop_all())
                return p.ACK_OK
            if not m.request_id:
                return p.ACK_INVALID_COMMAND
            if m.message_type == p.MSG_REQUEST:
                asyncio.ensure_future(self.manager.on_request(m.request_id, m))
            elif m.message_type == p.MSG_ANSWER:
                asyncio.ensure_future(self.manager.on_answer(m.request_id, m.sdp))
            elif m.message_type == p.MSG_CANDIDATE:
                asyncio.ensure_future(self.manager.on_candidate(m.request_id, m.sdp, m.mid))
            else:
                return p.ACK_INVALID_COMMAND
            return p.ACK_OK

        @sio.on("error")
        async def on_error(*args):
            log.warning("Signaling server error event: %s", args[:1])

        if self.cfg.mode == "buddy3d":
            @sio.on("set_webrtc_mode")
            async def on_set_webrtc_mode(data=None, *_):
                mode = _first_varint(data)
                if mode is not None:
                    await self._set_webrtc(mode == 1)
                return p.ACK_OK

            async def on_video_size(data=None, *_):
                raw = _bytes(data)
                quality = QUALITY_FROM_SIZE_BYTE.get(raw[0]) if raw else None
                if quality is None and raw:
                    quality = QUALITY_FROM_SIZE_BYTE.get(_first_varint(raw) or 0)
                if quality:
                    self._set_quality(quality)
                return p.ACK_OK

            sio.on("change_video_size", on_video_size)
            sio.on("save_video_size", on_video_size)

        @sio.on("*")
        async def catch_all(event, *args):
            log.debug("Unhandled signaling event %r", event)

    async def _set_webrtc(self, on: bool) -> None:
        if self.manager.enabled != on:
            log.info("Prusa Connect turned live view %s", "on" if on else "off")
        self.manager.enabled = on
        if not on:
            await self.manager.stop_all()

    def _set_quality(self, quality: int) -> None:
        names = {p.QUALITY_SD: "SD", p.QUALITY_HD: "HD", p.QUALITY_FHD: "FHD"}
        log.info("Prusa Connect set live quality to %s", names.get(quality, quality))
        self.manager.quality_cap = quality
        self.manager.reapply_quality()

    # -- run loop -----------------------------------------------------------------

    async def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
            self.sio = sio
            self._register_handlers(sio)
            kwargs = {}
            if self.cfg.mode == "buddy3d":
                kwargs["auth"] = {"token": self.cfg.token}
            try:
                await sio.connect(self.cfg.signaling_url, transports=["websocket"],
                                  headers={"Origin": self.cfg.origin}, socketio_path="socket.io",
                                  wait_timeout=20, **kwargs)
                connected_at = time.monotonic()
                last_auth = connected_at
                if not await self._authenticate():
                    raise ConnectionError("camera authentication failed")
                while sio.connected and not self._stop.is_set():
                    await asyncio.sleep(5)
                    if self.authenticated and time.monotonic() - last_auth > REAUTH_INTERVAL:
                        last_auth = time.monotonic()
                        await self._authenticate()
                if time.monotonic() - connected_at > 60:
                    backoff = 2.0
            except Exception as exc:
                log.warning("Signaling connection failed: %s", exc)
            finally:
                self.authenticated = False
                try:
                    await sio.disconnect()
                except Exception:
                    pass
            await self.manager.stop_all()
            if self._stop.is_set():
                break
            # keep hammering low after repeated auth refusals
            delay = backoff if self.auth_failures < 3 else 300.0
            delay *= random.uniform(0.8, 1.2)
            log.info("Reconnecting to signaling in %.0fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stop.set()
        if self.sio is not None:
            try:
                await self.sio.disconnect()
            except Exception:
                pass


def _bytes(data) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, list) and data and isinstance(data[0], (bytes, bytearray)):
        return bytes(data[0])
    if isinstance(data, str):
        return data.encode("latin-1", "ignore")
    if isinstance(data, int):
        return bytes([data & 0xFF])
    return b""


def _first_varint(data) -> Optional[int]:
    raw = _bytes(data)
    if not raw:
        return None
    try:
        for no, wire, value in p.iter_fields(raw):
            if no == 1 and wire == 0:
                return int(value)
    except ValueError:
        pass
    return raw[0] if len(raw) == 1 else None
