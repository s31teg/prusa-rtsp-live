"""Prusa Connect camera HTTP API: camera info and snapshot upload."""
from __future__ import annotations

import json
import logging
from typing import Optional

import aiohttp

from . import proto as p

log = logging.getLogger("prusa_rtsp_live.api")

WEBCAM_FEATURES = ["SocketCom", "WebRtc"]

BUDDY3D_FEATURES = [
    "SocketCom", "UploadInterval", "VideoStream", "GetSnapshot", "FwVer", "HwVer",
    "CameraName", "VideoQuality", "WebRtc", "TurnVideoQualityChange", "trigger_scheme",
]

BUDDY3D_USER_AGENT = "Buddy3D Camera"


def features_for(mode: str) -> list[str]:
    return WEBCAM_FEATURES if mode == "webcam" else BUDDY3D_FEATURES


def features_json(mode: str) -> str:
    # same text as the browser's JSON.stringify / the firmware's bracketed list
    return json.dumps(features_for(mode), separators=(",", ":"))


def info_body(cfg, width: Optional[int], height: Optional[int]) -> dict:
    if cfg.mode == "webcam":
        config = {
            "name": cfg.camera_name,
            "trigger_scheme": cfg.trigger_scheme,
            "hardware": cfg.hw_version,
            "protobuf": p.PROTOBUF_SCHEMA_VERSION,
            "manufacturer": cfg.manufacturer,
            "model": cfg.model,
        }
        if width and height:
            config["resolution"] = {"width": width, "height": height}
        return {
            "config": config,
            "type": "WEB",
            "features": WEBCAM_FEATURES,
            "hardware_version": cfg.hw_version,
        }
    resolution = {"width": width or 1920, "height": height or 1080}
    return {
        "config": {
            "path": "private",
            "name": cfg.camera_name,
            "driver": "private",
            "model": cfg.model,
            "firmware": cfg.fw_version,
            "manufacturer": cfg.manufacturer,
            "trigger_scheme": cfg.trigger_scheme,
            "resolution": dict(resolution),
        },
        "options": {"available_resolutions": [dict(resolution)]},
        "capabilities": ["trigger_scheme"],
        "features": BUDDY3D_FEATURES,
    }


class ConnectApi:
    def __init__(self, cfg, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.http = session

    def _headers(self, content_type: str) -> dict:
        headers = {
            "token": self.cfg.token,
            "fingerprint": self.cfg.fingerprint,
            "Content-Type": content_type,
        }
        if self.cfg.mode == "buddy3d":
            headers["User-Agent"] = BUDDY3D_USER_AGENT
        return headers

    async def put_info(self, width: Optional[int], height: Optional[int]) -> int:
        body = info_body(self.cfg, width, height)
        async with self.http.put(f"{self.cfg.api_url}/c/info", json=body,
                                 headers=self._headers("application/json"),
                                 allow_redirects=False) as resp:
            text = await resp.text()
            if resp.status >= 300:
                log.warning("Camera info rejected: HTTP %d %s", resp.status, text[:300])
            else:
                log.info("Camera registered with Prusa Connect (%s mode)", self.cfg.mode)
            return resp.status

    async def put_snapshot(self, jpeg: bytes) -> int:
        content_type = "image/jpeg" if self.cfg.mode == "webcam" else "image/jpg"
        async with self.http.put(f"{self.cfg.api_url}/c/snapshot", data=jpeg,
                                 headers=self._headers(content_type),
                                 allow_redirects=False) as resp:
            if resp.status >= 300:
                text = await resp.text()
                log.warning("Snapshot rejected: HTTP %d %s", resp.status, text[:300])
            return resp.status
