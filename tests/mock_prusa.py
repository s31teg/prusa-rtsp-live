"""A stand-in for Prusa Connect, for end-to-end tests.

* PUT /c/info and /c/snapshot (checks token + fingerprint headers)
* Socket.IO camera signaling (camera_authentication, trigger, webrtc relay)
* /viewer: a page that behaves like Connect's live viewer (same offer/answer and
  candidate handling as CamerasView in connect.prusa3d.com), talking to this
  server over a plain WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys

import socketio
from aiohttp import WSMsgType, web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from prusa_rtsp_live import proto as p  # noqa: E402

log = logging.getLogger("mock")
HERE = pathlib.Path(__file__).parent


class Mock:
    def __init__(self, token: str, ice: p.IceConfiguration | None = None,
                 video: p.VideoSettings | None = None) -> None:
        self.token = token
        self.ice = ice
        self.video = video
        self.bound_fingerprint: str | None = None
        self.events: list[tuple] = []  # (kind, detail)
        self.snapshots: list[bytes] = []
        self.infos: list[dict] = []
        self.camera_sid: str | None = None
        self.viewers: dict[str, web.WebSocketResponse] = {}
        self.sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*",
                                        max_http_buffer_size=10_000_000)
        self.app = web.Application(client_max_size=20_000_000)
        self.sio.attach(self.app)
        self.app.router.add_put("/c/info", self.put_info)
        self.app.router.add_put("/c/snapshot", self.put_snapshot)
        self.app.router.add_get("/viewer", self.viewer_page)
        self.app.router.add_get("/viewer-ws", self.viewer_ws)
        self._register()

    # -- HTTP ----------------------------------------------------------------

    def _check(self, request: web.Request) -> web.Response | None:
        token = request.headers.get("token")
        fp = request.headers.get("fingerprint")
        if token != self.token:
            return web.json_response({"message": "bad token"}, status=401)
        if self.bound_fingerprint is None:
            self.bound_fingerprint = fp
        if fp != self.bound_fingerprint:
            return web.json_response({"detail": "Invalid fingerprint"}, status=400)
        return None

    async def put_info(self, request: web.Request) -> web.Response:
        err = self._check(request)
        if err:
            return err
        body = await request.json()
        self.infos.append(body)
        self.events.append(("info", body))
        return web.json_response({"registered": True})

    async def put_snapshot(self, request: web.Request) -> web.Response:
        err = self._check(request)
        if err:
            return err
        data = await request.read()
        self.snapshots.append(data)
        self.events.append(("snapshot", len(data), request.headers.get("Content-Type")))
        return web.Response(status=204)

    async def viewer_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(HERE / "viewer.html")

    async def viewer_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        viewer_id = None
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            kind = data["type"]
            if kind == "request":
                viewer_id = data["id"]
                self.viewers[viewer_id] = ws
                await self.to_camera("webrtc", p.encode_webrtc_request(
                    self.token, viewer_id, ice=self.ice, video=self.video))
            elif kind == "answer":
                await self.to_camera("webrtc", p.encode_webrtc_request(
                    self.token, viewer_id, message_type=p.MSG_ANSWER, sdp=data["sdp"]))
            elif kind == "candidate":
                await self.to_camera("webrtc", p.encode_webrtc_request(
                    self.token, viewer_id, message_type=p.MSG_CANDIDATE,
                    sdp=data["candidate"], mid=data["mid"]))
            elif kind == "stop":
                await self.to_camera("webrtc", p.encode_webrtc_request(
                    self.token, viewer_id, message_type=0,
                    stream_status=p.STREAM_STATUS_STOP))
            elif kind == "report":
                self.events.append(("viewer", data["data"]))
        return ws

    # -- Socket.IO --------------------------------------------------------------

    async def to_camera(self, event: str, payload: bytes):
        if not self.camera_sid:
            raise RuntimeError("camera not connected")
        ack = await self.sio.call(event, payload, to=self.camera_sid, timeout=10)
        self.events.append(("ack", event, ack))
        return ack

    def _register(self) -> None:
        sio = self.sio

        @sio.event
        async def connect(sid, environ, auth=None):
            self.events.append(("connect", environ.get("HTTP_ORIGIN"), auth))

        @sio.on("camera_authentication")
        async def camera_authentication(sid, data):
            msg = dict((no, v) for no, _, v in p.iter_fields(data))
            token, fp = msg.get(1, b"").decode(), msg.get(2, b"").decode()
            self.events.append(("auth", token, fp))
            if token != self.token:
                return p.ACK_NOT_AUTHORIZED
            if self.bound_fingerprint and fp != self.bound_fingerprint:
                return p.ACK_NOT_AUTHORIZED
            self.camera_sid = sid
            asyncio.ensure_future(self._poll_camera())
            return p.ACK_OK

        @sio.on("protobuf_version")
        async def protobuf_version(sid, data):
            self.events.append(("protobuf_version", data.hex()))
            return p.ACK_OK

        @sio.on("features")
        async def features(sid, data):
            self.events.append(("features", data.hex()))
            return p.ACK_OK

        @sio.on("status")
        async def status(sid, data):
            self.events.append(("status", data.hex()))
            return p.ACK_OK

        @sio.on("webrtc_connection_info")
        async def connection_info(sid, data):
            self.events.append(("connection_info", data.hex()))
            return p.ACK_OK

        @sio.on("client_trigger")
        async def client_trigger(sid, data):
            self.events.append(("client_trigger", data.hex()))
            return p.ACK_OK

        @sio.on("webrtc")
        async def webrtc(sid, data):
            m = p.decode_webrtc(data)
            self.events.append(("camera_webrtc", m.message_type, m.request_id))
            ws = self.viewers.get(m.request_id)
            if ws is None:
                return p.ACK_INVALID_COMMAND
            if m.message_type == p.MSG_OFFER:
                await ws.send_json({"type": "offer", "sdp": m.sdp})
            elif m.message_type == p.MSG_CANDIDATE:
                await ws.send_json({"type": "candidate", "candidate": m.sdp, "mid": m.mid})
            return p.ACK_OK

    async def _poll_camera(self) -> None:
        await asyncio.sleep(0.2)
        try:
            await self.to_camera("trigger", p.encode_trigger(
                self.token, "poll-1", get_status=True, get_features=True,
                get_protocol_information=True))
            await self.to_camera("trigger", p.encode_trigger(self.token, "poll-2", get_snapshot=True))
            await self.to_camera("configuration", p.encode_configuration(
                self.token, webrtc_mode=p.WEBRTC_MODE_ON))
        except Exception as exc:
            self.events.append(("poll_error", str(exc)))


async def serve(mock: Mock, port: int) -> web.AppRunner:
    runner = web.AppRunner(mock.app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner
