# prusa-rtsp-live

Puts any RTSP camera (UniFi Protect, Reolink, Tapo, go2rtc, Frigate…) into
Prusa Connect with **live WebRTC video**, not just a snapshot every few
seconds. Snapshots keep uploading as well.

It runs in one container and speaks the same protocol as Prusa's own cameras:

| `MODE` | Pretends to be | Live view in Connect | Token comes from |
|---|---|---|---|
| `webcam` (default) | Prusa's browser **web camera** (the phone/laptop camera page) | Yes: Camera tab, **Play live stream** | Camera → Add camera → **Add new camera** |
| `buddy3d` | A **Buddy3D** camera (firmware 3.1.6 protocol, per [pibuddycam]) | Depends on Prusa's server: pibuddycam reports viewers are often refused for non-genuine Buddy3D cameras (listed under "Other cameras"). Snapshots and settings work | Camera → Add camera → **Add new other camera** (an existing camera API token works too) |

> Unofficial. Built from the protocol in Prusa's public web camera page and the
> pibuddycam reverse-engineering notes. Prusa can change things at any time.

## Quick start

1. **Get a token.** Prusa Connect → your printer → **Camera** → **Add camera** →
   **Add new camera**. The new camera card shows a **Token**: copy it.
   *Don't scan its QR code with a phone.* That would register the phone as the
   camera, and the token is bound to the first device that uses it. Start the
   container before the pairing code on the card expires.
2. **Get the RTSP address.** In UniFi Protect: camera → Settings → Advanced →
   RTSP → enable a quality (High = 1080p/4K, Medium = 720p) and copy the link,
   e.g. `rtsps://192.168.1.10:7441/AbCdEfGh?enableSrtp`.
3. **Deploy** with the settings as environment variables (see below).
4. Open the printer's **Camera** tab in Connect and press **Play live stream**.
   The dashboard tile keeps showing snapshots.

Logs should show:

```
Signed in to Prusa Connect signaling as camera AbCd…xy
Camera stream is 1920x1080 (h264)
Camera registered with Prusa Connect (webcam mode)
Snapshots are reaching Prusa Connect (180 KB)
[aBcD1234] live view requested
[aBcD1234] streaming (p2p)
```

## Deploying with Portainer

The settings are environment variables, so the compose file never holds your
token. Required: `PRUSA_TOKEN` and `CAMERA_URL`. Set `MODE` to `buddy3d` to
switch protocols (default `webcam`). Everything else in the table below is
optional.

**Option A: pull the image** that GitHub Actions builds on every push to `main`
(`ghcr.io/s31teg/prusa-rtsp-live:latest`, amd64 + arm64).

1. The repository is private, so the image is too. Create a GitHub token:
   GitHub → Settings → Developer settings → Personal access tokens →
   Tokens (classic) → scope **read:packages**.
2. Portainer → **Registries** → Add registry → **Custom registry**:
   URL `ghcr.io`, username `s31teg`, password = that token.
3. Portainer → **Stacks** → Add stack → Web editor: paste
   `docker-compose.yml`. Under **Environment variables** add `PRUSA_TOKEN`,
   `CAMERA_URL` and, if needed, `MODE`. Deploy.

**Option B: build on the Docker host** from the repository.

1. Create a fine-grained GitHub token with **Contents: read** on this repository.
2. Portainer → **Stacks** → Add stack → **Repository**: URL
   `https://github.com/s31teg/prusa-rtsp-live`, turn on Authentication
   (username `s31teg`, the token), compose path `docker-compose.build.yml`.
   Add the environment variables and deploy. Turn on GitOps updates if you
   want it to rebuild when the repository changes.

**Plain Docker:** `cp .env.example .env`, fill it in, `docker compose up -d`.

The image has a health check (snapshots must keep reaching Connect) and the
container carries the `autoheal=true` label, so a
[willfarrell/autoheal](https://github.com/willfarrell/docker-autoheal)
container restarts it if it stalls.

## Networking

Use `network_mode: host`. WebRTC sends video over UDP straight to the viewer.
With Docker's bridge network the container advertises a private 172.x
address, and viewers on your LAN end up on Prusa's TURN relay, which free
accounts get at SD 12 fps. Prusa streams 1080p free when a direct (STUN or
LAN) path works.

## Configuration

| Variable | Default | |
|---|---|---|
| `PRUSA_TOKEN` | required | 20-character token, or the web camera link |
| `CAMERA_URL` | required | `rtsp://` / `rtsps://` address (anything FFmpeg opens works) |
| `MODE` | `webcam` | `webcam` or `buddy3d` |
| `CAMERA_NAME` | `RTSP Camera` | Name shown in Connect |
| `FINGERPRINT` | derived from token | Device identity. Set it when reusing a token another device already used (for example `camera0000000001` from jtee3d/prusa_connect_rtsp) |
| `SNAPSHOT_INTERVAL` | `10` | Seconds; webcam mode allows 10, 30, 60 |
| `SNAPSHOT_MAX_WIDTH` | `1920` | Snapshots are scaled down to this width |
| `SNAPSHOTS` / `LIVE` | `true` | Turn either half off |
| `VIDEO_MAX_WIDTH` × `VIDEO_MAX_HEIGHT` | `1920` × `1080` | Live video is scaled to fit |
| `VIDEO_MAX_FPS` | `25` | |
| `VIDEO_MAX_BITRATE` | `4000000` | Upper limit; the bitrate adapts to the viewer's connection |
| `VIDEO_START_BITRATE` | `2000000` | |
| `VIDEO_CODECS` | `h264,vp8` | Offer order. Chrome, Edge, Safari and the Prusa app take H.264 (Constrained Baseline, like the Buddy3D); others fall back to VP8 |
| `X264_PRESET` | `veryfast` | `ultrafast` uses less CPU, `faster` gives better quality |
| `MAX_VIEWERS` | `2` | Simultaneous live viewers |
| `RTSP_TRANSPORT` | `tcp` | `udp` if your camera needs it |
| `SOURCE_IDLE_SECONDS` | `15` | How long the camera stays open after the last viewer or snapshot |
| `STUN_FALLBACK` | Google STUN | Used if Prusa sends no ICE servers; empty disables |
| `API_URL`, `SIGNALING_URL`, `SIGNALING_ORIGIN`, `HW_VERSION`, `FW_VERSION`, `MANUFACTURER`, `MODEL` | per mode | Overrides for when Prusa moves something |
| `LOG_LEVEL` | `INFO` | `DEBUG` for protocol detail |
| `TZ` | `Europe/London` (compose) | Time zone for log timestamps |

## Resource use

The camera is decoded only while needed. With no viewers only keyframes are
decoded for snapshots, about 2% of one CPU core. A 1080p live view, which
decodes the camera and re-encodes it for WebRTC, took about 65% of one core in
testing. Lower `VIDEO_MAX_*`, use the camera's Medium stream, or set
`X264_PRESET=ultrafast` on a small machine.

## How it works

1. `PUT /c/info` registers the camera (`type: WEB` in webcam mode) and
   `PUT /c/snapshot` uploads JPEGs, both with `token` / `fingerprint` headers.
2. A Socket.IO connection to `camera-signaling.prusa3d.com` authenticates as
   the camera and answers Connect's `trigger` / `configuration` messages with
   protobuf (schema 4.4). The status message reports WebRTC mode **on**, which
   is what makes Connect show the play button.
3. When someone presses play, Connect sends a `webrtc` REQUEST with Prusa's
   STUN/TURN servers and the quality it wants. The bridge creates a peer
   connection (aiortc), sends the OFFER, takes the ANSWER and candidates, and
   streams the camera re-encoded to fit (Prusa's TURN caps are honoured).

The protobuf encoders produce byte-identical output to the encoders in
Prusa's own web camera page (`tests/test_proto.py` pins this).

## Testing

```
pip install -r requirements.txt
python -m unittest tests.test_proto tests.test_logic
# end to end against a mock Connect (needs an RTSP test stream and Playwright):
python tests/e2e.py webcam      # Chromium viewer (VP8)
python tests/e2e_h264.py webcam # aiortc viewer (H.264)
```

## Credits

- [pibuddycam] by bullitt186 (MIT): Buddy3D protocol reverse engineering.
- Prusa's web camera page (camera-service-webcam.prusa3d.com), whose
  protocol the default mode follows.

[pibuddycam]: https://github.com/bullitt186/pibuddycam
