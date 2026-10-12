"""Configuration from environment variables."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional

from .tokens import DEFAULT_OBFUSCATION_KEY, parse_token

MODES = ("webcam", "buddy3d")

TRIGGER_SCHEMES = {10: "TEN_SEC", 30: "THIRTY_SEC", 60: "SIXTY_SEC"}

DEFAULTS = {
    "webcam": {
        "api_url": "https://camera-service.prusa3d.com",
        "origin": "https://camera-service-webcam.prusa3d.com",
        "hw_version": "webcam-0.3.3",
        "fw_version": "",
        "manufacturer": "RTSP bridge",
        "model": "RTSP camera",
    },
    "buddy3d": {
        "api_url": "https://connect.prusa3d.com",
        "origin": "https://connect.prusa3d.com",
        "hw_version": "NB.1.1.0",
        "fw_version": "3.1.6",
        "manufacturer": "Niceboy",
        "model": "Buddy3D-C1",
    },
}


class ConfigError(ValueError):
    pass


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    mode: str
    token: str
    fingerprint: str
    camera_url: str
    camera_name: str
    api_url: str
    signaling_url: str
    origin: str
    hw_version: str
    fw_version: str
    manufacturer: str
    model: str
    snapshot_interval: int
    snapshot_max_width: int
    snapshot_quality: int
    snapshots_enabled: bool
    live_enabled: bool
    max_viewers: int
    max_width: int
    max_height: int
    max_fps: int
    max_bitrate: int
    start_bitrate: int
    x264_preset: str
    rtsp_transport: str
    source_idle_seconds: int
    health_file: str
    log_level: str
    stun_fallback: str
    video_codecs: list

    @property
    def trigger_scheme(self) -> str:
        return TRIGGER_SCHEMES.get(self.snapshot_interval, "TEN_SEC")


def load() -> Config:
    mode = (_env("MODE", "webcam") or "webcam").lower()
    if mode not in MODES:
        raise ConfigError(f"MODE must be one of {', '.join(MODES)}, got {mode!r}")
    d = DEFAULTS[mode]

    raw_token = _env("PRUSA_TOKEN") or _env("TOKEN")
    if not raw_token:
        raise ConfigError("PRUSA_TOKEN is required (the camera token or the web camera link)")
    key = _env("TOKEN_OBFUSCATION_KEY", DEFAULT_OBFUSCATION_KEY)
    token = parse_token(raw_token, key)
    if not token:
        raise ConfigError("PRUSA_TOKEN is not a 20-character token or a Prusa web camera link")

    camera_url = _env("CAMERA_URL") or _env("RTSP_URL")
    if not camera_url:
        raise ConfigError("CAMERA_URL is required (the RTSP/RTSPS address of the camera)")

    fingerprint = _env("FINGERPRINT")
    if not fingerprint:
        # Stable per token, so a container rebuild keeps the same identity.
        digest = hashlib.sha1 if mode == "webcam" else hashlib.md5
        fingerprint = digest(f"prusa-rtsp-live:{token}".encode()).hexdigest()
    if len(fingerprint) < 16:
        raise ConfigError("FINGERPRINT must be at least 16 characters")

    interval = _int("SNAPSHOT_INTERVAL", 10, 10, 600)
    if mode == "webcam" and interval not in TRIGGER_SCHEMES:
        raise ConfigError("SNAPSHOT_INTERVAL must be 10, 30 or 60 in webcam mode")

    return Config(
        mode=mode,
        token=token,
        fingerprint=fingerprint,
        camera_url=camera_url,
        camera_name=_env("CAMERA_NAME", "RTSP Camera"),
        api_url=(_env("API_URL", d["api_url"]) or "").rstrip("/"),
        signaling_url=(_env("SIGNALING_URL", "https://camera-signaling.prusa3d.com") or "").rstrip("/"),
        origin=_env("SIGNALING_ORIGIN", d["origin"]),
        hw_version=_env("HW_VERSION", d["hw_version"]),
        fw_version=_env("FW_VERSION", d["fw_version"]) or "",
        manufacturer=_env("MANUFACTURER", d["manufacturer"]),
        model=_env("MODEL", d["model"]),
        snapshot_interval=interval,
        snapshot_max_width=_int("SNAPSHOT_MAX_WIDTH", 1920, 320, 7680),
        snapshot_quality=_int("SNAPSHOT_QUALITY", 85, 30, 95),
        snapshots_enabled=_bool("SNAPSHOTS", True),
        live_enabled=_bool("LIVE", True),
        max_viewers=_int("MAX_VIEWERS", 2, 1, 8),
        max_width=_int("VIDEO_MAX_WIDTH", 1920, 320, 3840),
        max_height=_int("VIDEO_MAX_HEIGHT", 1080, 240, 2160),
        max_fps=_int("VIDEO_MAX_FPS", 25, 1, 60),
        max_bitrate=_int("VIDEO_MAX_BITRATE", 4_000_000, 300_000, 20_000_000),
        start_bitrate=_int("VIDEO_START_BITRATE", 2_000_000, 300_000, 20_000_000),
        x264_preset=_env("X264_PRESET", "veryfast"),
        rtsp_transport=_env("RTSP_TRANSPORT", "tcp"),
        source_idle_seconds=_int("SOURCE_IDLE_SECONDS", 15, 0, 3600),
        health_file=_env("HEALTH_FILE", "/tmp/prusa-rtsp-live.health"),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        stun_fallback=os.environ.get("STUN_FALLBACK", "stun:stun.l.google.com:19302").strip(),
        video_codecs=_codecs(_env("VIDEO_CODECS", "h264,vp8")),
    )


def _codecs(raw: str) -> list:
    names = [c.strip().lower() for c in raw.split(",") if c.strip()]
    bad = [c for c in names if c not in ("h264", "vp8")]
    if bad or not names:
        raise ConfigError("VIDEO_CODECS must list h264 and/or vp8, e.g. 'h264,vp8'")
    return names
