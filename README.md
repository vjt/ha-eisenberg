# Eisenberg — Arlo for Home Assistant

A Home Assistant custom integration for Arlo cameras, named after
skating legend Arlo Eisenberg. Built around event-driven MQTT (no
polling) with a typed Pydantic API client.

## What you get

- **Live RTSPS streaming** with sub-second lag (forced TCP, ffmpeg
  low-delay flags).
- **Camera entity** with snapshots, motion thumbnails and stream
  keyframes cached on disk so the tile survives restarts and stays
  populated while disarmed.
- **Binary sensors** — generic motion (from MQTT `motionDetected`)
  plus AI-classified person / vehicle / animal detections.
- **Security mode** select — armAway / armHome / standby via Arlo's v3
  location automation API (with revision tracking).
- **Siren switch**.
- **Battery / signal** sensors.
- **Base-station connectivity** binary sensor.
- **Snapshot service** — `eisenberg.snapshot` for dashboard buttons or
  automations.
- **Media archival** — opt-in storage of motion clips, thumbnails and
  stream keyframes to a configured `media_dirs` location, with rolling
  retention (default 14 days).

## Installation

### HACS (recommended)

1. Add this repo as a custom repository in HACS (category: Integration).
2. Install **Eisenberg**.
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → search "Eisenberg".

### Manual

Copy `custom_components/eisenberg/` into your HA `custom_components/`
directory, install the `pyeisenberg` Python package into the HA Python
environment, and restart.

## Configuration

The config flow asks for your Arlo email and password.

- If your browser is already trusted at Arlo, login is silent — no
  push needed.
- Otherwise, a push notification is sent to your phone. Approve it in
  the Arlo app, then click **Submit**. Each click is a single API call
  — no polling — so rate-limit risk stays low.

After login you pick a media storage location (or **Disabled** to skip
archival).

### Options

- **Storage Location** — change the archive directory.
- **Detection sensor reset timeout** — how long person/vehicle/animal
  binary sensors stay on after a detection (default 30 s).
- **Archived media retention** — days to keep on disk (default 14).

## Services

### `eisenberg.snapshot`

Request a fresh full-frame snapshot from a camera. The image arrives
asynchronously via MQTT and refreshes the camera tile. Fails with a
clear error if the camera is in standby (Arlo refuses cloud snapshots
while disarmed).

```yaml
service: eisenberg.snapshot
target:
  entity_id: camera.front_door
```

## Events

The integration fires `eisenberg_media` events on motion detection
with `device_id`, `category`, `categories`, `content_url`,
`thumbnail_url`, `duration`, `timestamp`. Use these in automations to
log clips elsewhere or trigger downstream actions.

## Architecture

- **`eisenberg/`** — pure async Arlo client. REST + raw MQTT 3.1.1
  over WebSocket. Pydantic models for every payload.
- **`custom_components/eisenberg/`** — the HA integration. A single
  coordinator owns the client + MQTT stream and pushes state to
  entities via `_handle_coordinator_update`.

## Camera support

Tested against the **Arlo Essential XL HD (VMC2052A)** (battery + solar,
WiFi, cloud-only). Other Arlo models that share the same v3 automation
+ MQTT shapes should work — file an issue if yours doesn't.

## Limitations

- All control flows through Arlo's cloud — there's no local API on
  these cameras.
- The trust cookie Arlo issues lasts about 14 days. When it expires,
  HA fires a reauth; one click re-fires a push.

## Development

```bash
./scripts/check.sh    # pyright + pytest + ruff
```

The deploy skill (`/eisenberg-deploy`) pushes to a HAOS box over SSH.
The release skill (`/eisenberg-release`) cuts PyPI + GitHub releases.

## License

MIT.
