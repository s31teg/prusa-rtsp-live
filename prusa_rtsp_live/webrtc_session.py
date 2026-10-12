"""WebRTC live view sessions, one per Connect viewer.

Mirrors the behaviour of Prusa's browser web camera (camera side):
  1. the viewer sends a REQUEST with ICE servers and video settings,
  2. the camera creates the peer connection and sends an OFFER,
  3. the viewer answers, both sides trickle candidates,
  4. once connected the camera reports the connection type and applies the
     quality cap Prusa asked for (for example SD on TURN relays).
"""
from __future__ import annotations

import asyncio
import fractions
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import av
from aiortc import (MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection,
                    RTCRtpSender, RTCSessionDescription)
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp

from . import proto as p
from .source import FrameSource

log = logging.getLogger("prusa_rtsp_live.webrtc")

DEFAULT_STUN = ["stun:stun.l.google.com:19302"]
DISCONNECT_GRACE = 30.0

# same table as Prusa's web camera: quality -> (width, height, max bitrate)
QUALITY_CAPS = {
    p.QUALITY_SD: (640, 480, 900_000),
    p.QUALITY_HD: (1280, 720, 2_500_000),
    p.QUALITY_FHD: (1920, 1080, 6_500_000),
}
DEFAULT_FPS_CAP = 12
TURN_FALLBACK = (640, 480, 10)  # used on TURN when Prusa sent no settings


@dataclass
class Caps:
    width: int
    height: int
    fps: int


def fit(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Scale (src_w, src_h) to fit in (max_w, max_h); keep aspect, even sizes."""
    if src_w <= 0 or src_h <= 0:
        return max_w - max_w % 2, max_h - max_h % 2
    scale = min(1.0, max_w / src_w, max_h / src_h)
    w = max(2, int(src_w * scale) // 2 * 2)
    h = max(2, int(src_h * scale) // 2 * 2)
    return w, h


def ice_servers_from(ice: Optional[p.IceConfiguration]) -> list[RTCIceServer]:
    """Prusa ICE config -> aiortc servers (aiortc uses one STUN + one TURN)."""
    stun: list[str] = []
    turn: list[tuple[int, str, str, str]] = []  # (preference, url, user, credential)
    for server in (ice.servers if ice else []):
        for ep in server.endpoints:
            if not ep.address or not ep.port:
                continue
            if ep.scheme_type == p.SCHEME_STUN and ep.transport != p.TRANSPORT_TLS:
                stun.append(f"stun:{ep.address}:{ep.port}")
            elif ep.scheme_type == p.SCHEME_TURN:
                if ep.transport == p.TRANSPORT_TLS:
                    url, pref = f"turns:{ep.address}:{ep.port}?transport=tcp", 2
                elif ep.transport == p.TRANSPORT_TCP:
                    url, pref = f"turn:{ep.address}:{ep.port}?transport=tcp", 1
                else:
                    url, pref = f"turn:{ep.address}:{ep.port}?transport=udp", 0
                turn.append((pref, url, server.username, server.credential))
    servers: list[RTCIceServer] = []
    relay_only = bool(ice and ice.policy == p.POLICY_RELAY)
    if stun:
        servers.append(RTCIceServer(urls=stun[0]))
    elif not relay_only and DEFAULT_STUN:
        servers.append(RTCIceServer(urls=DEFAULT_STUN[0]))
    if turn:
        _, url, user, cred = sorted(turn, key=lambda t: t[0])[0]
        servers.append(RTCIceServer(urls=url, username=user or None, credential=cred or None))
    return servers


def connection_kind(local: int, remote: int) -> str:
    both = (local, remote)
    if p.CONN_FAIL in both:
        return "failed"
    if p.CONN_RELAYED in both:
        return "turn"
    if p.CONN_SERVER_REFLEXIVE in both:
        return "stun"
    if p.CONN_UNKNOWN in both:
        return "unknown"
    return "p2p"


def scope_applies(scope: int, kind: str) -> bool:
    if kind == "unknown":
        return True
    if scope == p.SCOPE_TURN:
        return kind == "turn"
    if scope == p.SCOPE_STUN:
        return kind == "stun"
    if scope == p.SCOPE_P2P:
        return kind == "p2p"
    if scope == p.SCOPE_TURN_AND_STUN:
        return kind in ("turn", "stun")
    return True


def requested_caps(video: Optional[p.VideoSettings], kind: str) -> Optional[tuple[int, int, int]]:
    """(max_w, max_h, max_fps) Prusa asks for on this connection, or None."""
    caps = None
    if video and video.video_quality in QUALITY_CAPS and scope_applies(video.apply_scope, kind):
        w, h, _ = QUALITY_CAPS[video.video_quality]
        caps = (w, h, video.video_fps if video.video_fps > 0 else DEFAULT_FPS_CAP)
    if kind == "turn" and video is None:
        fallback = TURN_FALLBACK
        caps = fallback if caps is None else tuple(min(a, b) for a, b in zip(caps, fallback))
    return caps


def codec_preferences(names: list[str]):
    """Offer order: H.264 Constrained Baseline first (what Buddy3D sends), then VP8
    (what browser web cameras usually send). The viewer picks the first it supports."""
    codecs = RTCRtpSender.getCapabilities("video").codecs
    ordered = []
    for name in names:
        matching = [c for c in codecs if c.mimeType.lower() == f"video/{name}"]
        if name == "h264":
            matching.sort(key=lambda c: c.parameters.get("profile-level-id") != "42e01f")
        ordered.extend(matching)
    return ordered + [c for c in codecs if c.mimeType.lower() == "video/rtx"]


_CANDIDATE_TYPES = {
    "host": p.CONN_HOST,
    "srflx": p.CONN_SERVER_REFLEXIVE,
    "prflx": p.CONN_PEER_REFLEXIVE,
    "relay": p.CONN_RELAYED,
}


class SessionTrack(MediaStreamTrack):
    """Video track for one viewer: pulls shared frames, applies caps."""

    kind = "video"

    def __init__(self, source: FrameSource, caps: Caps) -> None:
        super().__init__()
        self.source = source
        self.caps = caps
        self._seq = 0
        self._next_due = 0.0

    async def recv(self) -> av.VideoFrame:
        loop = asyncio.get_running_loop()
        while True:
            if self.readyState != "live":
                raise MediaStreamError
            try:
                self._seq, frame = await self.source.next_frame(self._seq, timeout=5.0)
            except asyncio.TimeoutError:
                continue
            now = time.monotonic()
            if self.caps.fps:
                # pace to the cap without aliasing (30 fps in, 20 fps cap -> 20 out)
                if now < self._next_due - 0.004:
                    continue
                period = 1.0 / self.caps.fps
                self._next_due = max(self._next_due + period, now - period)
            caps = self.caps
            return await loop.run_in_executor(None, _prepare, frame, caps.width, caps.height)


def _prepare(frame: av.VideoFrame, max_w: int, max_h: int) -> av.VideoFrame:
    w, h = fit(frame.width, frame.height, max_w, max_h)
    out = frame.reformat(width=w, height=h, format="yuv420p")
    if out is frame:
        # never hand the shared frame to an encoder; it mutates pict_type
        out = _copy_frame(frame)
    out.pts = frame.pts
    out.time_base = frame.time_base or fractions.Fraction(1, 90000)
    return out


def _copy_frame(frame: av.VideoFrame) -> av.VideoFrame:
    copy = av.VideoFrame(frame.width, frame.height, frame.format.name)
    for src, dst in zip(frame.planes, copy.planes):
        if src.line_size == dst.line_size:
            dst.update(bytes(src))
        else:  # different padding: go through a conversion instead
            return frame.reformat(format="nv12").reformat(format=frame.format.name)
    return copy


class Signaler:
    """What a session needs from the signaling client."""

    async def send_offer(self, request_id: str, sdp: str) -> None: ...
    async def send_candidate(self, request_id: str, candidate: str, mid: str) -> None: ...
    async def send_connection_info(self, request_id: str, local: int, remote: int) -> None: ...
    async def send_rejected(self, request_id: str) -> None: ...


class Session:
    def __init__(self, manager: "SessionManager", request_id: str, msg: p.WebRtcMessage) -> None:
        self.manager = manager
        self.request_id = request_id
        self.msg = msg
        self.pc: Optional[RTCPeerConnection] = None
        self.track: Optional[SessionTrack] = None
        self.kind = "unknown"
        self._closed = False
        self._pending: list[tuple[str, str]] = []
        self._answered = False
        self._timers: list[asyncio.TimerHandle] = []
        self._disconnect_timer: Optional[asyncio.TimerHandle] = None
        self._source_held = False

    @property
    def short_id(self) -> str:
        return self.request_id[:8]

    async def start(self) -> None:
        cfg = self.manager.config
        loop = asyncio.get_running_loop()
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers_from(self.msg.ice)))
        src = self.manager.source
        src.acquire(live=True)
        self._source_held = True
        self.track = SessionTrack(src, Caps(cfg.max_width, cfg.max_height, cfg.max_fps))
        self.manager.apply_quality(self)

        transceiver = self.pc.addTransceiver(self.track, direction="sendonly")
        transceiver.setCodecPreferences(codec_preferences(cfg.video_codecs))

        channel = self.pc.createDataChannel("ping-pong")

        @channel.on("open")
        def _on_open() -> None:
            channel.send("Ping")

        @channel.on("message")
        def _on_message(_msg) -> None:
            try:
                channel.send("Ping")
            except Exception:
                pass

        @self.pc.on("connectionstatechange")
        async def _on_state() -> None:
            if self.pc is None:
                return
            state = self.pc.connectionState
            log.info("[%s] connection %s", self.short_id, state)
            if state in ("failed", "closed"):
                await self.close()
            elif state == "disconnected":
                if self._disconnect_timer is None:
                    self._disconnect_timer = loop.call_later(
                        DISCONNECT_GRACE, lambda: asyncio.ensure_future(self.close()))
            elif state == "connected":
                if self._disconnect_timer:
                    self._disconnect_timer.cancel()
                    self._disconnect_timer = None
                await self._on_connected()

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        sdp = self.pc.localDescription.sdp
        await self.manager.signaler.send_offer(self.request_id, sdp)
        log.info("[%s] offer sent (%d ICE candidates)", self.short_id, sdp.count("a=candidate:"))

    async def handle_answer(self, sdp: str) -> None:
        if self.pc is None or self._closed:
            return
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        self._answered = True
        if "\nm=video 0 " in "\n" + sdp:
            log.warning("[%s] viewer rejected the video (no common codec)", self.short_id)
        pending, self._pending = self._pending, []
        for candidate, mid in pending:
            await self.handle_candidate(candidate, mid)

    async def handle_candidate(self, candidate: str, mid: str) -> None:
        if self.pc is None or self._closed:
            return
        if not self._answered:
            self._pending.append((candidate, mid))
            return
        text = candidate[2:] if candidate.startswith("a=") else candidate
        if text.startswith("candidate:"):
            text = text[len("candidate:"):]
        if not text.strip():
            await self.pc.addIceCandidate(None)
            return
        try:
            ice = candidate_from_sdp(text)
        except Exception:
            log.debug("[%s] ignoring unparsable candidate %r", self.short_id, candidate)
            return
        ice.sdpMid = mid or "0"
        ice.sdpMLineIndex = 0
        await self.pc.addIceCandidate(ice)

    async def _on_connected(self) -> None:
        local, remote = self._selected_pair_types()
        self.kind = connection_kind(local, remote)
        log.info("[%s] streaming (%s)", self.short_id, self.kind)
        self.manager.apply_quality(self)
        try:
            await self.manager.signaler.send_connection_info(self.request_id, local, remote)
        except Exception as exc:
            log.debug("connection info not sent: %s", exc)
        ttl = (self.msg.video.ttl if self.msg.video and self.msg.video.ttl else 0) or \
              (self.msg.ice.ttl if self.msg.ice else 0)
        scope = self.msg.video.apply_scope if self.msg.video else p.SCOPE_UNSPECIFIED
        if ttl > 0 and scope_applies(scope, self.kind):
            log.info("[%s] session limited to %ds by Prusa", self.short_id, ttl)
            loop = asyncio.get_running_loop()
            self._timers.append(loop.call_later(ttl, lambda: asyncio.ensure_future(self.close())))

    def _selected_pair_types(self) -> tuple[int, int]:
        try:
            for transceiver in self.pc.getTransceivers():
                ice_transport = transceiver.sender.transport.transport
                connection = ice_transport._connection  # aioice
                for pair in connection._nominated.values():
                    return (_CANDIDATE_TYPES.get(pair.local_candidate.type, p.CONN_UNKNOWN),
                            _CANDIDATE_TYPES.get(pair.remote_candidate.type, p.CONN_UNKNOWN))
        except Exception:
            pass
        return p.CONN_UNKNOWN, p.CONN_UNKNOWN

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for timer in self._timers:
            timer.cancel()
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        if self.track:
            self.track.stop()
        if self.pc:
            try:
                await self.pc.close()
            except Exception:
                pass
        if self._source_held:
            self.manager.source.release(live=True)
            self._source_held = False
        self.manager.forget(self)
        log.info("[%s] session closed", self.short_id)


class SessionManager:
    def __init__(self, config, source: FrameSource, signaler: Signaler) -> None:
        self.config = config
        self.source = source
        self.signaler = signaler
        self.sessions: dict[str, Session] = {}
        self.enabled = True
        self.quality_cap: Optional[int] = None  # Buddy3D change_video_size / configuration

    def forget(self, session: Session) -> None:
        if self.sessions.get(session.request_id) is session:
            del self.sessions[session.request_id]

    async def on_request(self, request_id: str, msg: p.WebRtcMessage) -> None:
        if not (self.enabled and self.config.live_enabled):
            log.info("Live view request refused: live view is turned off")
            await self.signaler.send_rejected(request_id)
            return
        existing = self.sessions.get(request_id)
        if existing is None and len(self.sessions) >= self.config.max_viewers:
            log.info("Live view request refused: %d viewer(s) already watching", len(self.sessions))
            await self.signaler.send_rejected(request_id)
            return
        if existing:
            await existing.close()
        session = Session(self, request_id, msg)
        self.sessions[request_id] = session
        log.info("[%s] live view requested", session.short_id)
        try:
            await session.start()
        except Exception as exc:
            log.exception("[%s] could not start live view: %s", session.short_id, exc)
            await session.close()
            await self.signaler.send_rejected(request_id)

    async def on_answer(self, request_id: str, sdp: str) -> None:
        session = self.sessions.get(request_id)
        if session:
            try:
                await session.handle_answer(sdp)
            except Exception as exc:
                log.warning("[%s] bad answer: %s", session.short_id, exc)
                await session.close()

    async def on_candidate(self, request_id: str, candidate: str, mid: str) -> None:
        session = self.sessions.get(request_id)
        if session:
            try:
                await session.handle_candidate(candidate, mid)
            except Exception as exc:
                log.debug("[%s] candidate ignored: %s", session.short_id, exc)

    async def stop_all(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()

    def apply_quality(self, session: Session) -> None:
        cfg = self.config
        w, h, fps = cfg.max_width, cfg.max_height, cfg.max_fps
        if self.quality_cap in QUALITY_CAPS:
            qw, qh, _ = QUALITY_CAPS[self.quality_cap]
            w, h = min(w, qw), min(h, qh)
        requested = requested_caps(session.msg.video, session.kind)
        if requested:
            w, h, fps = min(w, requested[0]), min(h, requested[1]), min(fps, requested[2])
        if session.track:
            session.track.caps = Caps(w, h, fps)
            log.info("[%s] video cap %dx%d @ %dfps", session.short_id, w, h, fps)

    def reapply_quality(self) -> None:
        for session in self.sessions.values():
            self.apply_quality(session)

    @property
    def viewers(self) -> int:
        return len(self.sessions)


# re-exported for type hints in other modules
SendFn = Callable[..., Awaitable[None]]
