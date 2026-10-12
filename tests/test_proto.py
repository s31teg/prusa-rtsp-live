"""Protobuf codec tests.

The expected bytes below were produced by running the encoder functions from
Prusa's own web camera bundle (camera-service-webcam.prusa3d.com, app 0.3.3)
with the same inputs, so these tests pin the bridge to Prusa's wire format.
The server->camera vectors were encoded here and decoded successfully by
Prusa's decoders.
"""
import unittest

from prusa_rtsp_live import proto as p

T = "ABCDEFGHIJ0123456789"
F = "fp-0123456789abcdef"

JS = {
    "auth": "0a144142434445464748494a30313233343536373839121366702d30313233343536373839616263646566",
    "cand": "0a144142434445464748494a30313233343536373839120272341a1366702d30313233343536373839616263646566222d0a28613d63616e6469646174653a31203120756470203120312e322e332e3420352074797020686f737412013028043801",
    "conn": "0a144142434445464748494a30313233343536373839120272351a1366702d30313233343536373839616263646566200428013001",
    "feat": "12144142434445464748494a30313233343536373839220c77656263616d2d302e332e332a03342e3432165b22536f636b6574436f6d222c22576562527463225d3a1366702d30313233343536373839616263646566",
    "offer": "0a144142434445464748494a30313233343536373839120272331a1366702d30313233343536373839616263646566221f0a1d763d300d0a6f3d2d2031203220494e2049503420302e302e302e300d0a28033801",
    "pver": "0a03342e3412144142434445464748494a303132333435363738391a027231221366702d30313233343536373839616263646566",
    "pver_noreq": "0a03342e3412144142434445464748494a30313233343536373839221366702d30313233343536373839616263646566",
    "reject": "0a144142434445464748494a3031323334353637383912027236221366702d303132333435363738396162636465663007",
    "status": "0a080a0608800f10b8082a17120c77656263616d2d302e332e331a0343616d5a02080142144142434445464748494a3031323334353637383952027232",
    "status_rich": "0a080a0608800a10d0051a04200a2801221f121d0a1141413a42423a43433a44443a45453a4646120831302e302e302e322a490a05332e312e3612084e422e312e312e301a0343616d321a080210011a14727473703a2f2f31302e302e302e322f6c697665520f0a0d4575726f70652f4c6f6e646f6e5a040801100142144142434445464748494a303132333435363738394a0d108d1d1a0830642030313a303252037232625a020802",
}


class EncodeMatchesPrusaJs(unittest.TestCase):
    def test_auth(self):
        self.assertEqual(p.encode_camera_authentication(T, F).hex(), JS["auth"])

    def test_protobuf_version(self):
        self.assertEqual(p.encode_protobuf_version(T, F, "r1").hex(), JS["pver"])
        self.assertEqual(p.encode_protobuf_version(T, F).hex(), JS["pver_noreq"])

    def test_features(self):
        msg = p.encode_features(T, F, '["SocketCom","WebRtc"]', hw_version="webcam-0.3.3")
        self.assertEqual(msg.hex(), JS["feat"])

    def test_status_webcam(self):
        s = p.StatusInfo(token=T, request_id="r2", width=1920, height=1080,
                         camera_name="Cam", hw_version="webcam-0.3.3",
                         webrtc_mode=p.WEBRTC_MODE_ON)
        self.assertEqual(p.encode_status(s).hex(), JS["status"])

    def test_status_rich(self):
        s = p.StatusInfo(token=T, request_id="r2b", width=1280, height=720,
                         camera_name="Cam", hw_version="NB.1.1.0", fw_version="3.1.6",
                         webrtc_mode=p.WEBRTC_MODE_ON, webrtc_status=p.ENABLE,
                         video_quality=p.QUALITY_HD, snapshot_interval=10,
                         snapshot_enable=p.ENABLE, ethernet_mac="AA:BB:CC:DD:EE:FF",
                         ethernet_ipv4="10.0.0.2", rtsp_url="rtsp://10.0.0.2/live",
                         uptime_seconds=3725, timezone="Europe/London")
        self.assertEqual(p.encode_status(s).hex(), JS["status_rich"])

    def test_offer(self):
        msg = p.encode_webrtc(T, F, "r3", p.MSG_OFFER, sdp="v=0\r\no=- 1 2 IN IP4 0.0.0.0\r\n")
        self.assertEqual(msg.hex(), JS["offer"])

    def test_candidate(self):
        msg = p.encode_webrtc(T, F, "r4", p.MSG_CANDIDATE,
                              sdp="a=candidate:1 1 udp 1 1.2.3.4 5 typ host", mid="0")
        self.assertEqual(msg.hex(), JS["cand"])

    def test_connection_info(self):
        msg = p.encode_connection_info(T, F, "r5", local=p.CONN_HOST, remote=p.CONN_RELAYED)
        self.assertEqual(msg.hex(), JS["conn"])

    def test_reject(self):
        self.assertEqual(p.encode_client_trigger_error(T, F, "r6").hex(), JS["reject"])


class DecodeRoundTrip(unittest.TestCase):
    def test_trigger(self):
        t = p.decode_trigger(p.encode_trigger(T, "req-9", get_status=True, get_features=True,
                                              get_protocol_information=True))
        self.assertTrue(t.get_status and t.get_features and t.get_protocol_information)
        self.assertFalse(t.get_snapshot)
        self.assertEqual((t.camera_token, t.request_id), (T, "req-9"))

    def test_configuration(self):
        c = p.decode_configuration(p.encode_configuration(
            T, webrtc_mode=p.WEBRTC_MODE_OFF, camera_name="Cam X", video_quality=p.QUALITY_FHD))
        self.assertEqual(c.webrtc_mode, p.WEBRTC_MODE_OFF)
        self.assertEqual(c.camera_name, "Cam X")
        self.assertEqual(c.video_quality, p.QUALITY_FHD)

    def test_webrtc_request(self):
        ice = p.IceConfiguration(servers=[
            p.IceServer(endpoints=[p.IceEndpoint(p.SCHEME_STUN, "stun.l.google.com", 19302, p.TRANSPORT_UDP)]),
            p.IceServer(endpoints=[p.IceEndpoint(p.SCHEME_TURN, "coturn.prusa3d.com", 3478, p.TRANSPORT_UDP),
                                   p.IceEndpoint(p.SCHEME_TURN, "coturn.prusa3d.com", 5349, p.TRANSPORT_TLS)],
                        username="1789811200:43202", credential="c3VwZXJzZWNyZXQ=")],
            policy=p.POLICY_ALL, ttl=600)
        vid = p.VideoSettings(video_quality=p.QUALITY_HD, video_fps=24, plan_priority=2, ttl=300,
                              apply_scope=p.SCOPE_TURN)
        m = p.decode_webrtc(p.encode_webrtc_request(T, "req-10", ice=ice, video=vid))
        self.assertEqual(m.message_type, p.MSG_REQUEST)
        self.assertEqual(m.request_id, "req-10")
        self.assertEqual(m.ice, ice)
        self.assertEqual(m.video, vid)

    def test_webrtc_candidate(self):
        m = p.decode_webrtc(p.encode_webrtc_request(
            T, "req-11", message_type=p.MSG_CANDIDATE, sdp="a=candidate:1 1 udp 1 1.2.3.4 5 typ host", mid="0"))
        self.assertEqual((m.message_type, m.sdp, m.mid),
                         (p.MSG_CANDIDATE, "a=candidate:1 1 udp 1 1.2.3.4 5 typ host", "0"))

    def test_garbage_does_not_crash_decoder(self):
        with self.assertRaises(ValueError):
            p.decode_webrtc(b"\x0a\xff")


if __name__ == "__main__":
    unittest.main()
