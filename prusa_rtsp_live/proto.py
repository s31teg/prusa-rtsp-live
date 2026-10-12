"""Protobuf messages of the Prusa Connect camera protocol (schema "4.4").

Field numbers come from the encoders/decoders shipped in Prusa's own
JavaScript: the browser web camera (camera-service-webcam.prusa3d.com) and the
Connect front end. Both use the same schema as the Buddy3D firmware.

Only the fields this bridge needs are implemented. Encoding is hand-written
(no protoc dependency) and emits fields in ascending order like the JS does.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator, Optional

PROTOBUF_SCHEMA_VERSION = "4.4"

# --- enums -----------------------------------------------------------------

ENABLE = 1
DISABLE = 2

WEBRTC_MODE_UNKNOWN = 0
WEBRTC_MODE_ON = 1
WEBRTC_MODE_OFF = 2

STREAM_STATUS_START = 1
STREAM_STATUS_STOP = 2

CLIENT_TYPE_CAMERA = 1
CLIENT_TYPE_CLIENT = 2

MSG_REQUEST = 1
MSG_ANSWER = 2
MSG_OFFER = 3
MSG_CANDIDATE = 4

SCHEME_STUN = 1
SCHEME_TURN = 2

TRANSPORT_UDP = 1
TRANSPORT_TCP = 2
TRANSPORT_TLS = 3

POLICY_ALL = 1
POLICY_RELAY = 2

QUALITY_SD = 1
QUALITY_HD = 2
QUALITY_FHD = 3

SCOPE_UNSPECIFIED = 0
SCOPE_TURN = 1
SCOPE_STUN = 2
SCOPE_P2P = 3
SCOPE_TURN_AND_STUN = 4
SCOPE_ANY = 5

CONN_UNDEFINED = 0
CONN_HOST = 1
CONN_SERVER_REFLEXIVE = 2
CONN_PEER_REFLEXIVE = 3
CONN_RELAYED = 4
CONN_UNKNOWN = 5
CONN_FAIL = 6
CONN_P2P = 7

RTSP_SERVER_ON = 2
RTSP_SERVER_OFF = 3

ERROR_WEBRTC_START_FAIL = 7

# Socket.IO ack codes
ACK_OK = 0
ACK_NOT_AUTHORIZED = 1
ACK_JOIN_SESSION = 2
ACK_MISSING_TOKEN = 3
ACK_DECODE_MESSAGE = 4
ACK_NOT_CAMERA = 5
ACK_INVALID_COMMAND = 101
ACK_UNSUPPORTED_FEATURE = 102


# --- wire helpers ----------------------------------------------------------

def _varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _key(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def f_uint(field_no: int, value: Optional[int]) -> bytes:
    if value is None:
        return b""
    return _key(field_no, 0) + _varint(int(value))


def f_str(field_no: int, value: Optional[str]) -> bytes:
    if value is None:
        return b""
    data = value.encode("utf-8")
    return _key(field_no, 2) + _varint(len(data)) + data


def f_msg(field_no: int, payload: Optional[bytes]) -> bytes:
    if payload is None:
        return b""
    return _key(field_no, 2) + _varint(len(payload)) + payload


def f_float(field_no: int, value: Optional[float]) -> bytes:
    if value is None:
        return b""
    return _key(field_no, 5) + struct.pack("<f", float(value))


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def iter_fields(data: bytes) -> Iterator[tuple[int, int, object]]:
    """Yield (field_no, wire_type, value). value: int for varint, bytes otherwise."""
    pos = 0
    n = len(data)
    while pos < n:
        key, pos = _read_varint(data, pos)
        field_no, wire = key >> 3, key & 7
        if field_no == 0:
            return
        if wire == 0:
            value, pos = _read_varint(data, pos)
            yield field_no, wire, value
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > n:
                raise ValueError("truncated field")
            yield field_no, wire, data[pos:pos + length]
            pos += length
        elif wire == 5:
            if pos + 4 > n:
                raise ValueError("truncated fixed32")
            yield field_no, wire, data[pos:pos + 4]
            pos += 4
        elif wire == 1:
            if pos + 8 > n:
                raise ValueError("truncated fixed64")
            yield field_no, wire, data[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")


def _s(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else ""


# --- camera -> server ------------------------------------------------------

def encode_camera_authentication(token: str, fingerprint: str) -> bytes:
    return f_str(1, token) + f_str(2, fingerprint)


def encode_protobuf_version(token: str, fingerprint: Optional[str],
                            request_id: Optional[str] = None) -> bytes:
    return (f_str(1, PROTOBUF_SCHEMA_VERSION) + f_str(2, token)
            + f_str(3, request_id) + f_str(4, fingerprint))


def encode_features(token: str, fingerprint: Optional[str], features_json: str,
                    hw_version: Optional[str] = None,
                    fw_version: Optional[str] = None) -> bytes:
    return (f_str(2, token) + f_str(3, fw_version) + f_str(4, hw_version)
            + f_str(5, PROTOBUF_SCHEMA_VERSION) + f_str(6, features_json)
            + f_str(7, fingerprint))


@dataclass
class StatusInfo:
    """Subset of the CameraToServer status message."""
    token: str
    request_id: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    camera_name: Optional[str] = None
    hw_version: Optional[str] = None
    fw_version: Optional[str] = None
    webrtc_mode: Optional[int] = None
    webrtc_status: Optional[int] = None
    # optional extras (Buddy3D profile)
    video_quality: Optional[int] = None
    snapshot_interval: Optional[int] = None
    snapshot_enable: Optional[int] = None
    ethernet_mac: Optional[str] = None
    ethernet_ipv4: Optional[str] = None
    rtsp_url: Optional[str] = None
    uptime_seconds: Optional[int] = None
    timezone: Optional[str] = None


def encode_status(s: StatusInfo) -> bytes:
    # image (1): current_resolution (1) {width 1, height 2}; quality (3)
    image = None
    if s.width and s.height:
        image = f_msg(1, f_uint(1, s.width) + f_uint(2, s.height))
    # control (3): snapshot_interval (4), snapshot_enable (5)
    control = None
    if s.snapshot_interval is not None or s.snapshot_enable is not None:
        control = f_uint(4, s.snapshot_interval) + f_uint(5, s.snapshot_enable)
    # network (4): ethernet (2) {mac 1, ipv4 2}
    network = None
    if s.ethernet_mac or s.ethernet_ipv4:
        network = f_msg(2, f_str(1, s.ethernet_mac) + f_str(2, s.ethernet_ipv4))
    # system (5): fw 1, hw 2, name 3, rtsp_server 6 {mode 1, status 2, url 3},
    #             system_time 10 {timezone 1}, webrtc 11 {mode 1, status 2}
    system = f_str(1, s.fw_version) + f_str(2, s.hw_version) + f_str(3, s.camera_name)
    if s.rtsp_url:
        system += f_msg(6, f_uint(1, RTSP_SERVER_ON) + f_uint(2, ENABLE) + f_str(3, s.rtsp_url))
    if s.timezone:
        system += f_msg(10, f_str(1, s.timezone))
    if s.webrtc_mode is not None:
        system += f_msg(11, f_uint(1, s.webrtc_mode) + f_uint(2, s.webrtc_status))
    # camera_status (9): uptime_raw 2 (uint64), uptime 3 (string)
    camera_status = None
    if s.uptime_seconds is not None:
        camera_status = f_uint(2, s.uptime_seconds) + f_str(3, _fmt_uptime(s.uptime_seconds))
    # video (11): quality 1
    video = f_uint(1, s.video_quality) if s.video_quality is not None else None
    return (f_msg(1, image) + f_msg(3, control) + f_msg(4, network) + f_msg(5, system or None)
            + f_str(8, s.token) + f_msg(9, camera_status) + f_str(10, s.request_id)
            + f_msg(11, video))


def _fmt_uptime(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days}d {hours:02d}:{minutes:02d}"


def encode_webrtc(token: str, fingerprint: Optional[str], request_id: str,
                  message_type: int, sdp: Optional[str] = None, mid: Optional[str] = None,
                  client_type: int = CLIENT_TYPE_CAMERA) -> bytes:
    sdp_msg = None
    if sdp is not None or mid is not None:
        sdp_msg = f_str(1, sdp) + f_str(2, mid)
    return (f_str(1, token) + f_str(2, request_id) + f_str(3, fingerprint)
            + f_msg(4, sdp_msg) + f_uint(5, message_type) + f_uint(7, client_type))


def encode_connection_info(token: str, fingerprint: Optional[str], request_id: str,
                           local: int, remote: int,
                           client_type: int = CLIENT_TYPE_CAMERA) -> bytes:
    return (f_str(1, token) + f_str(2, request_id) + f_str(3, fingerprint)
            + f_uint(4, remote) + f_uint(5, local) + f_uint(6, client_type))


def encode_client_trigger_error(token: str, fingerprint: Optional[str], request_id: str,
                                error_code: int = ERROR_WEBRTC_START_FAIL) -> bytes:
    return (f_str(1, token) + f_str(2, request_id) + f_str(4, fingerprint)
            + f_uint(6, error_code))


# --- server -> camera ------------------------------------------------------

@dataclass
class Trigger:
    get_status: bool = False
    get_features: bool = False
    get_snapshot: bool = False
    get_protocol_information: bool = False
    set_snapshot_enable: Optional[int] = None
    start_device_reboot: bool = False
    camera_token: str = ""
    request_id: str = ""
    unknown: list[int] = field(default_factory=list)


def decode_trigger(data: bytes) -> Trigger:
    t = Trigger()
    for no, wire, value in iter_fields(data):
        if wire == 0:
            if no == 1:
                t.get_status = value == ENABLE
            elif no == 2:
                t.get_features = value == ENABLE
            elif no == 3:
                t.get_snapshot = value == ENABLE
            elif no == 4:
                t.set_snapshot_enable = value
            elif no == 9:
                t.start_device_reboot = value == ENABLE
            elif no == 12:
                t.get_protocol_information = value == ENABLE
            else:
                t.unknown.append(no)
        elif wire == 2:
            if no == 11:
                t.camera_token = _s(value)
            elif no == 13:
                t.request_id = _s(value)
    return t


@dataclass
class Configuration:
    webrtc_mode: Optional[int] = None
    snapshot_interval: Optional[int] = None
    camera_name: Optional[str] = None
    video_quality: Optional[int] = None
    camera_token: str = ""
    request_id: str = ""


def decode_configuration(data: bytes) -> Configuration:
    c = Configuration()
    for no, wire, value in iter_fields(data):
        if no == 3 and wire == 2:  # control
            for cno, cwire, cval in iter_fields(value):
                if cwire == 0 and cno == 12:
                    c.webrtc_mode = cval
                elif cwire == 0 and cno == 5:
                    c.snapshot_interval = cval
        elif no == 4 and wire == 2:  # system
            for sno, swire, sval in iter_fields(value):
                if sno == 1 and swire == 2:
                    c.camera_name = _s(sval)
        elif no == 6 and wire == 2:
            c.camera_token = _s(value)
        elif no == 7 and wire == 2:
            c.request_id = _s(value)
        elif no == 8 and wire == 2:  # video
            for vno, vwire, vval in iter_fields(value):
                if vno == 1 and vwire == 0:
                    c.video_quality = vval
    return c


@dataclass
class IceEndpoint:
    scheme_type: int = 0
    address: str = ""
    port: int = 0
    transport: int = 0


@dataclass
class IceServer:
    endpoints: list[IceEndpoint] = field(default_factory=list)
    username: str = ""
    credential: str = ""


@dataclass
class IceConfiguration:
    servers: list[IceServer] = field(default_factory=list)
    policy: int = 0
    ttl: int = 0


@dataclass
class VideoSettings:
    video_quality: int = 0
    video_fps: int = 0
    plan_priority: int = 0
    ttl: int = 0
    apply_scope: int = 0


@dataclass
class WebRtcMessage:
    camera_token: str = ""
    request_id: str = ""
    fingerprint: str = ""
    sdp: str = ""
    mid: str = ""
    message_type: int = 0
    stream_status: int = 0
    client_type: int = 0
    ice: Optional[IceConfiguration] = None
    video: Optional[VideoSettings] = None


def _decode_endpoint(data: bytes) -> IceEndpoint:
    e = IceEndpoint()
    for no, wire, value in iter_fields(data):
        if no == 1 and wire == 0:
            e.scheme_type = value
        elif no == 2 and wire == 2:
            e.address = _s(value)
        elif no == 3 and wire == 0:
            e.port = value
        elif no == 4 and wire == 0:
            e.transport = value
    return e


def _decode_ice_server(data: bytes) -> IceServer:
    s = IceServer()
    for no, wire, value in iter_fields(data):
        if no == 1 and wire == 2:
            s.endpoints.append(_decode_endpoint(value))
        elif no == 2 and wire == 2:
            s.username = _s(value)
        elif no == 3 and wire == 2:
            s.credential = _s(value)
    return s


def _decode_ice(data: bytes) -> IceConfiguration:
    c = IceConfiguration()
    for no, wire, value in iter_fields(data):
        if no == 1 and wire == 2:
            c.servers.append(_decode_ice_server(value))
        elif no == 2 and wire == 0:
            c.policy = value
        elif no == 3 and wire == 0:
            c.ttl = value
    return c


def _decode_video(data: bytes) -> VideoSettings:
    v = VideoSettings()
    for no, wire, value in iter_fields(data):
        if wire != 0:
            continue
        if no == 1:
            v.video_quality = value
        elif no == 2:
            v.video_fps = value
        elif no == 3:
            v.plan_priority = value
        elif no == 4:
            v.ttl = value
        elif no == 5:
            v.apply_scope = value
    return v


def decode_webrtc(data: bytes) -> WebRtcMessage:
    m = WebRtcMessage()
    for no, wire, value in iter_fields(data):
        if wire == 2:
            if no == 1:
                m.camera_token = _s(value)
            elif no == 2:
                m.request_id = _s(value)
            elif no == 3:
                m.fingerprint = _s(value)
            elif no == 4:
                for sno, swire, sval in iter_fields(value):
                    if sno == 1 and swire == 2:
                        m.sdp = _s(sval)
                    elif sno == 2 and swire == 2:
                        m.mid = _s(sval)
            elif no == 8:
                m.ice = _decode_ice(value)
            elif no == 9:
                m.video = _decode_video(value)
        elif wire == 0:
            if no == 5:
                m.message_type = value
            elif no == 6:
                m.stream_status = value
            elif no == 7:
                m.client_type = value
    return m


# --- server -> camera encoders (used by the test harness) --------------------

def encode_trigger(token: str = "", request_id: str = "", get_status: bool = False,
                   get_features: bool = False, get_snapshot: bool = False,
                   get_protocol_information: bool = False) -> bytes:
    return (f_uint(1, ENABLE if get_status else None)
            + f_uint(2, ENABLE if get_features else None)
            + f_uint(3, ENABLE if get_snapshot else None)
            + f_str(11, token or None)
            + f_uint(12, ENABLE if get_protocol_information else None)
            + f_str(13, request_id or None))


def encode_configuration(token: str = "", webrtc_mode: Optional[int] = None,
                         camera_name: Optional[str] = None,
                         video_quality: Optional[int] = None) -> bytes:
    control = f_uint(12, webrtc_mode) if webrtc_mode is not None else None
    system = f_str(1, camera_name) if camera_name is not None else None
    video = f_uint(1, video_quality) if video_quality is not None else None
    return f_msg(3, control) + f_msg(4, system) + f_str(6, token or None) + f_msg(8, video)


def encode_webrtc_request(token: str, request_id: str, ice: Optional[IceConfiguration] = None,
                          video: Optional[VideoSettings] = None,
                          message_type: int = MSG_REQUEST, sdp: Optional[str] = None,
                          mid: Optional[str] = None, stream_status: Optional[int] = None,
                          client_type: int = CLIENT_TYPE_CLIENT) -> bytes:
    ice_msg = None
    if ice is not None:
        ice_msg = b""
        for srv in ice.servers:
            srv_msg = b""
            for ep in srv.endpoints:
                srv_msg += f_msg(1, f_uint(1, ep.scheme_type) + f_str(2, ep.address)
                                 + f_uint(3, ep.port) + f_uint(4, ep.transport))
            srv_msg += f_str(2, srv.username or None) + f_str(3, srv.credential or None)
            ice_msg += f_msg(1, srv_msg)
        ice_msg += f_uint(2, ice.policy or None) + f_uint(3, ice.ttl or None)
    video_msg = None
    if video is not None:
        video_msg = (f_uint(1, video.video_quality or None) + f_uint(2, video.video_fps or None)
                     + f_uint(3, video.plan_priority or None) + f_uint(4, video.ttl or None)
                     + f_uint(5, video.apply_scope or None))
    sdp_msg = None
    if sdp is not None or mid is not None:
        sdp_msg = f_str(1, sdp) + f_str(2, mid)
    return (f_str(1, token) + f_str(2, request_id) + f_msg(4, sdp_msg)
            + f_uint(5, message_type) + f_uint(6, stream_status) + f_uint(7, client_type)
            + f_msg(8, ice_msg) + f_msg(9, video_msg))
