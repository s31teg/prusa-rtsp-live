"""Tunes aiortc's H.264 encoder for a camera relay.

aiortc encodes with libx264's default "medium" preset, a 3 Mbit/s ceiling and
a 250-frame keyframe interval. For live printer video we want a faster preset,
a configurable bitrate range and a short keyframe interval so a new viewer gets
a picture quickly. Output stays Constrained Baseline (profile-level-id 42e01f),
the profile the Buddy3D camera uses and every Connect viewer accepts.
"""
from __future__ import annotations

import fractions
import logging
from typing import Iterator

import av
from aiortc.codecs import h264, vpx

log = logging.getLogger("prusa_rtsp_live.encoder")

_settings = {"preset": "veryfast", "fps": 25}


def configure(preset: str, max_bitrate: int, start_bitrate: int, fps: int) -> None:
    _settings["preset"] = preset
    _settings["fps"] = max(1, fps)
    h264.MAX_BITRATE = max_bitrate
    h264.DEFAULT_BITRATE = min(start_bitrate, max_bitrate)
    h264.MIN_BITRATE = min(h264.MIN_BITRATE, h264.DEFAULT_BITRATE)
    h264.MAX_FRAME_RATE = _settings["fps"]
    h264.H264Encoder._encode_frame = _encode_frame
    vpx.MAX_BITRATE = max_bitrate
    vpx.DEFAULT_BITRATE = min(start_bitrate, max_bitrate)
    vpx.MIN_BITRATE = min(vpx.MIN_BITRATE, vpx.DEFAULT_BITRATE)
    vpx.MAX_FRAME_RATE = _settings["fps"]
    log.debug("H.264 encoder: preset=%s fps=%d bitrate=%d..%d", preset, fps,
              h264.MIN_BITRATE, max_bitrate)


def _encode_frame(self, frame: av.VideoFrame, force_keyframe: bool) -> Iterator[bytes]:
    if self.codec and (
        frame.width != self.codec.width
        or frame.height != self.codec.height
        or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate > 0.1
    ):
        self.buffer_data = b""
        self.buffer_pts = None
        self.codec = None

    if force_keyframe:
        frame.pict_type = av.video.frame.PictureType.I
    else:
        frame.pict_type = av.video.frame.PictureType.NONE

    if self.codec is None:
        fps = _settings["fps"]
        self.codec = av.CodecContext.create("libx264", "w")
        self.codec.width = frame.width
        self.codec.height = frame.height
        self.codec.bit_rate = self.target_bitrate
        self.codec.pix_fmt = "yuv420p"
        self.codec.framerate = fractions.Fraction(fps, 1)
        self.codec.time_base = fractions.Fraction(1, fps)
        self.codec.gop_size = fps * 2
        self.codec.options = {
            "level": "31",
            "tune": "zerolatency",
            "preset": _settings["preset"],
        }
        self.codec.profile = "Baseline"

    data_to_send = b""
    for package in self.codec.encode(frame):
        data_to_send += bytes(package)

    if data_to_send:
        yield from self._split_bitstream(data_to_send)
