import os
import unittest
from unittest import mock

from prusa_rtsp_live import config as cfgmod
from prusa_rtsp_live import proto as p
from prusa_rtsp_live.tokens import deobfuscate, obfuscate, parse_token
from prusa_rtsp_live.webrtc_session import (connection_kind, fit, ice_servers_from,
                                            requested_caps, scope_applies)

TOKEN = "Zz9yX8wQ3vB7nM2kL5pR"


class Tokens(unittest.TestCase):
    def test_vector_from_prusa_decoder(self):
        # decoded by the BN() function in Prusa's web camera bundle
        self.assertEqual(deobfuscate("v1.LR9bGjlVWj5REDdEDSxGAiNbXSI"), TOKEN)
        self.assertEqual(deobfuscate("v1.NichJyQraicrLEVCUVJAXFlZFUk"), "ABCDEFGHIJ0123456789")

    def test_forms(self):
        link = f"https://camera-service-webcam.prusa3d.com/#t={obfuscate(TOKEN)}"
        self.assertEqual(parse_token(link), TOKEN)
        self.assertEqual(parse_token(f"https://webcam.connect.prusa3d.com/?token={TOKEN}"), TOKEN)
        self.assertEqual(parse_token(f'{{"token": "{TOKEN}", "ssid": "x"}}'), TOKEN)
        self.assertEqual(parse_token(f"  {TOKEN}\n"), TOKEN)
        self.assertIsNone(parse_token("too-short"))
        self.assertIsNone(parse_token("https://camera-service-webcam.prusa3d.com/#t=v1.AAAA"))


class Config(unittest.TestCase):
    def load(self, **env):
        base = {"PRUSA_TOKEN": TOKEN, "CAMERA_URL": "rtsp://cam/live"}
        base.update(env)
        with mock.patch.dict(os.environ, base, clear=True):
            return cfgmod.load()

    def test_defaults_webcam(self):
        c = self.load()
        self.assertEqual(c.mode, "webcam")
        self.assertEqual(c.api_url, "https://camera-service.prusa3d.com")
        self.assertEqual(len(c.fingerprint), 40)
        self.assertEqual(c.fingerprint, self.load().fingerprint, "fingerprint must be stable")
        self.assertEqual(c.trigger_scheme, "TEN_SEC")
        self.assertEqual(c.video_codecs, ["h264", "vp8"])

    def test_buddy3d(self):
        c = self.load(MODE="buddy3d", FINGERPRINT="camera0000000001")
        self.assertEqual(c.api_url, "https://connect.prusa3d.com")
        self.assertEqual(c.fingerprint, "camera0000000001")
        self.assertEqual(c.fw_version, "3.1.6")

    def test_errors(self):
        with self.assertRaises(cfgmod.ConfigError):
            self.load(PRUSA_TOKEN="nope")
        with self.assertRaises(cfgmod.ConfigError):
            self.load(SNAPSHOT_INTERVAL="15")
        with self.assertRaises(cfgmod.ConfigError):
            self.load(MODE="other")
        with self.assertRaises(cfgmod.ConfigError):
            self.load(VIDEO_CODECS="h265")


class Caps(unittest.TestCase):
    def test_fit(self):
        self.assertEqual(fit(3840, 2160, 1920, 1080), (1920, 1080))
        self.assertEqual(fit(1920, 1080, 1280, 720), (1280, 720))
        self.assertEqual(fit(640, 360, 1920, 1080), (640, 360))
        self.assertEqual(fit(2688, 1512, 640, 480), (640, 360))

    def test_scope_and_kind(self):
        self.assertEqual(connection_kind(p.CONN_HOST, p.CONN_RELAYED), "turn")
        self.assertEqual(connection_kind(p.CONN_SERVER_REFLEXIVE, p.CONN_HOST), "stun")
        self.assertEqual(connection_kind(p.CONN_HOST, p.CONN_PEER_REFLEXIVE), "p2p")
        self.assertTrue(scope_applies(p.SCOPE_TURN, "turn"))
        self.assertFalse(scope_applies(p.SCOPE_TURN, "p2p"))
        self.assertTrue(scope_applies(p.SCOPE_TURN, "unknown"))

    def test_requested_caps_like_prusa_webcam(self):
        hd = p.VideoSettings(video_quality=p.QUALITY_HD, video_fps=0, apply_scope=p.SCOPE_TURN)
        self.assertEqual(requested_caps(hd, "turn"), (1280, 720, 12))
        self.assertIsNone(requested_caps(hd, "p2p"))
        self.assertEqual(requested_caps(None, "turn"), (640, 480, 10))
        self.assertIsNone(requested_caps(None, "p2p"))

    def test_ice_servers(self):
        ice = p.IceConfiguration(servers=[
            p.IceServer(endpoints=[p.IceEndpoint(p.SCHEME_STUN, "stun.example", 3478, p.TRANSPORT_UDP)]),
            p.IceServer(endpoints=[
                p.IceEndpoint(p.SCHEME_TURN, "turn.example", 5349, p.TRANSPORT_TLS),
                p.IceEndpoint(p.SCHEME_TURN, "turn.example", 3478, p.TRANSPORT_UDP)],
                username="u", credential="c")])
        servers = ice_servers_from(ice)
        self.assertEqual(servers[0].urls, "stun:stun.example:3478")
        self.assertEqual(servers[1].urls, "turn:turn.example:3478?transport=udp")
        self.assertEqual((servers[1].username, servers[1].credential), ("u", "c"))


if __name__ == "__main__":
    unittest.main()
