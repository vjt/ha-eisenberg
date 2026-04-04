# Eisenberg Design Spec

Home Assistant custom component for Arlo cameras, named after skating
legend Arlo Eisenberg. Talks to the Arlo cloud API via REST + MQTT
WebSocket. No local network access (Arlo cameras are cloud-only).

## Naming

| Thing              | Name                             |
|--------------------|----------------------------------|
| Repo               | `ha-eisenberg`                   |
| PyPI package       | `pyeisenberg`                    |
| Python module      | `eisenberg` (`import eisenberg`) |
| HA custom component| `custom_components/eisenberg/`   |
| HA domain          | `eisenberg`                      |

## Scope (v1)

- Live stream: on-demand RTSP via `startStream` API
- Motion/person/vehicle/animal detection: real-time via MQTT
- Snapshots: on-demand via REST, delivered via MQTT
- Battery and signal monitoring
- Siren control (on/off)
- Mode awareness (armAway/armHome/standby)
- Media archival to HA media storage (configurable)

### Not in scope

- Continuous recording (battery camera, no persistent stream)
- Audio streaming / two-way audio
- Cloud clip playback as media library
- Geofence management
- Base station management (camera is standalone)

## Architecture

Two packages in one repo, mirroring ha-verisure:

```
ha-eisenberg/
  eisenberg/                  # API client library (pip-installable as pyeisenberg)
    __init__.py               # Public API exports
    client.py                 # EisenbergClient: auth, REST, session management
    mqtt.py                   # MQTT WebSocket: connect, subscribe, dispatch
    models.py                 # Pydantic models for all API/MQTT payloads
    exceptions.py             # Typed exceptions
  custom_components/
    eisenberg/                # HA integration
      __init__.py             # Setup/teardown
      config_flow.py          # Auth flow (email/pass -> push -> trust cookie)
      coordinator.py          # Event-driven coordinator (MQTT, not polling)
      camera.py               # Camera entity (snapshot + RTSP stream)
      binary_sensor.py        # Motion, person, vehicle, animal detection
      sensor.py               # Battery, signal strength, WiFi RSSI
      switch.py               # Siren on/off
      const.py                # Domain, config keys
      manifest.json
      translations/
  tests/
  pyproject.toml
  CLAUDE.md
```

The `eisenberg/` library owns all Arlo API knowledge. The HA
integration imports from it and never touches raw JSON or HTTP.

## Arlo API Protocol

### Hosts

| Host                    | Purpose              | Auth header          |
|-------------------------|----------------------|----------------------|
| `ocapi-app.arlo.com`   | Authentication       | `base64(token)`      |
| `myapi.arlo.com`       | Everything else      | Raw token + `xCloudId` header |

### Authentication

Two-phase auth with browser trust optimization:

**First-time setup** (requires push approval once):

1. `POST /api/auth` with email + base64(password) -> token (authCompleted=false)
2. `POST /api/getFactorId` with factorType=BROWSER -> 400 (not trusted)
3. `POST /api/startAuth` with factorType="" -> sends PUSH to phone
4. `POST /api/finishAuth` -> poll until user approves
5. `GET /api/validateAccessToken`
6. `POST /api/startPairingFactor` with browserAuthCode -> sets trust cookie (14 days)

**Trusted browser flow** (instant, no push):

1. `POST /api/auth` -> token
2. `POST /api/getFactorId` with factorType=BROWSER -> 200, returns factorId
3. `POST /api/startAuth` with factorId + factorType=BROWSER -> authCompleted=true instantly
4. `GET /hmsweb/users/session/v3` -> mqttUrl, session cookies

**Critical detail:** The `factorId` from `getFactorId` MUST be passed
to `startAuth`. Without it, `startAuth` falls back to PUSH even with
the trust cookie present. pyaarlo doesn't do this, which is why they
always need push/IMAP.

**Token lifecycle:**

- Token lifetime: ~2 hours
- Browser trust cookie: 14 days, refreshed on every `startAuth`
- Device ID: persistent UUID per client (`x-user-device-id` header)
- Client proactively re-auths before token expiry (not reactive on 401)

**Required headers for `ocapi-app.arlo.com`:**

```
Content-Type: application/json; charset=UTF-8
Source: arloCamWeb
Auth-Version: 2
X-User-Device-Id: <persistent UUID>
X-User-Device-Type: BROWSER
X-User-Device-Automation-Name: <base64("BROWSER")>
X-Service-Version: v3
Authorization: <base64(token)>  (when token available)
```

**Required headers for `myapi.arlo.com`:**

```
Content-Type: application/json; charset=utf-8
Authorization: <raw token>
Auth-Version: 2
xCloudId: <device xCloudId>
```

### MQTT Event Stream

Persistent WebSocket to `wss://mqtt-cluster-z1-1.arloxcld.com:8084/mqtt`
with subprotocol `mqtt`. MQTT 3.1.1 binary protocol, not a broker
library -- raw packet construction/parsing.

**Subscribed topics:**

- `d/{xCloudId}/out/#` -- device-originated events
- `u/{userId}/in/#` -- user/cloud-originated events

**Common message envelope:**

All PUBLISH payloads are JSON with a consistent structure:

```json
{
  "from": "<deviceId or server>",
  "transId": "<unique transaction ID>",
  "action": "is",
  "resource": "<resource path>",
  "properties": { ... }  // or "states": { ... }
}
```

**Connection management:**

- PINGREQ every 60s for keepalive
- Auto-reconnect on disconnect with exponential backoff (1s -> 60s max)
- On reconnect: re-subscribe, re-fetch device state via REST
- Extended MQTT downtime: entities go `unavailable`

### MQTT Event Types (captured from live system)

#### Motion Detection

Topic: `d/{xCloudId}/out/cameras/{deviceId}/is`

```json
{"properties": {"motionDetected": true}}   // PIR triggered
{"properties": {"activityState": "alertStreamActive", "dateStarted": 1775341447000}}
{"properties": {"activityState": "idle", "dateStarted": 1775341473000}}
{"properties": {"motionDetected": false}}  // PIR reset
```

#### AI Classification (motion event with media)

Topic: `u/{userId}/in/feed/live`

```json
{
  "resource": "feedNotification",
  "type": "motion",
  "objCategory": "Person",
  "objCategories": ["Person"],
  "objRegion": "0.598,0.269,0.746,0.770",
  "deviceId": "AGSEXAMPLE001",
  "duration": "00:00:26",
  "contentUrl": "https://arlos3-prod-z1.s3.amazonaws.com/.../recordings/xxx.mp4?...",
  "thumbnailUrl": "https://arlos3-prod-z1.s3.amazonaws.com/.../recordings/xxx_thumb.jpg?...",
  "contentType": "video/mp4",
  "mediaMeta": {"codec_tag_string": "hvc1", "height": "1080", "width": "1920", "bit_rate": "790192"},
  "activeMode": "armAway",
  "utcCreatedDate": 1775341447317
}
```

Known `objCategory` values: `Person`, `Vehicle`, `Animal` (from Arlo AI).

#### Media Upload

Topic: `u/{userId}/in/library/add`

```json
{
  "resource": "mediaUploadNotification",
  "deviceId": "AGSEXAMPLE001",
  "presignedContentUrl": "https://.../.mp4?...",
  "presignedThumbnailUrl": "https://.../_thumb.jpg?...",
  "presignedLastImageUrl": "https://.../_thumb.jpg?...",
  "recordingStopped": true
}
```

Presigned URLs expire after ~24 hours.

#### Snapshot

Request via REST, response via MQTT.

Topic: `d/{xCloudId}/out/cameras/{deviceId}/fullFrameSnapshotAvailable`

```json
{
  "action": "fullFrameSnapshotAvailable",
  "resource": "cameras/{deviceId}",
  "properties": {
    "presignedFullFrameSnapshotUrl": "https://arlos3-prod-z1.arlo.com/.../fullFrameSnapshot.jpg?...",
    "disablePrivacyZones": false
  }
}
```

Snapshot lifecycle via MQTT:
1. `activityState: "fullFrameSnapshot"` (acknowledged)
2. `activityState: "fullFrameSnapshot"` + `dateStarted` (camera waking)
3. `fullFrameSnapshotAvailable` with presigned URL
4. `activityState: "idle"` (done)

#### Mode Changes

Topic: `u/{userId}/in/automation/activeMode/is`

```json
{
  "resource": "automation/activeMode",
  "properties": {
    "properties": {"mode": "armAway"},
    "revision": 1775339550697
  }
}
```

Known modes: `armAway`, `armHome`, `standby`.

Each mode change also produces:
- `u/.../automation/geofences/is` (geofence config sync)
- `u/.../feed/live` with `type: "modeChange"`
- `d/.../devices/{id}/states/is` with new device state config

When camera enters `armAway`, its state includes motion trigger rules:

```json
{
  "states": {
    "motionStart": {
      "actions": {
        "recordVideo": {"AGSEXAMPLE001": {}},
        "pushNotification": {}
      }
    },
    "activeMode": "armAway"
  }
}
```

In `standby`, the `motionStart` key is absent (no triggers).

#### Siren

Topic: `d/{xCloudId}/out/siren/{deviceId}/is`

```json
// Siren ON
{
  "properties": {
    "sirenState": "on",
    "sirenTrigger": "manual",
    "duration": 180,
    "volume": 8,
    "pattern": "alarm",
    "sirenTimestamp": 1775340116000
  }
}

// Siren OFF
{
  "properties": {
    "sirenState": "off",
    "duration": 0,
    "sirenTimestamp": 1775340118000
  }
}
```

Also accompanied by `u/.../automation/panicState/is` with
`audibleEmergency: "siren"/"none"`.

#### Device Properties (full dump)

Topic: `d/{xCloudId}/out/cameras/{deviceId}/privacyZones/is`

Contains comprehensive device state including:

- `batteryLevel`, `lowBattery`, `chargerTech`, `chargingState`
- `signalStrength`, WiFi RSSI, SSID, IP address, MAC
- `motionDetected`, `audioDetected`
- `nightVisionMode`, `hdr`, `resolution`, `videoMode`
- `powerSaveMode`, `autoLowPowerMode`
- Motion zones with named zones and coordinates
- Siren state, spotlight config, speaker/mic volume
- `streamingMode: "eventBased"`

#### Connectivity

Topic: `d/{xCloudId}/out/basestation/connectivity/is`

```json
{
  "properties": {
    "connectivity": [{
      "type": "wifi",
      "connected": true,
      "ssid": "...",
      "wifiRssi": -67,
      "signalStrength": 2,
      "ipAddr": "192.168.x.x",
      "connectionState": "Connected"
    }]
  }
}
```

#### Live Stream

Topic: `d/{xCloudId}/out/cameras/{deviceId}/is`

```json
// Stream active
{"properties": {"activityState": "userStreamActive", "dateStarted": ...}}
{"properties": {"activityState": "startUserStream"}}

// Stream ended
{"properties": {"activityState": "idle"}}  // transId contains "All clients disconnected"
```

### REST Commands

#### Start Stream

`POST /hmsweb/users/devices/startStream`

**Critical:** The URL format in the response depends on User-Agent:

- Browser UA -> DASH `.mpd` URL (for web player)
- Mobile UA (`Arlo/4.0 (iPhone; iOS 18.0)`) -> `rtsp://` URL on Wowza

For HA integration, use mobile UA + `x-user-device-type: PHONE` to get
RTSP, which HA's stream component supports natively.

Response:

```json
{
  "data": {
    "url": "rtsp://arlostreaming20221-z1-prod.wowza.arlo.com:443/vzmodulelive/...",
    "sipCallInfo": { ... },
    "iceServers": { ... }
  }
}
```

The RTSP URL is valid for ~30 minutes.

#### Request Snapshot

`POST /hmsweb/users/devices/notify/{deviceId}`

```json
{
  "from": "{userId}_web",
  "to": "{deviceId}",
  "action": "set",
  "resource": "cameras/{deviceId}",
  "publishResponse": true,
  "properties": {"activityState": "fullFrameSnapshot"},
  "transId": "web!snapshot!{timestamp}"
}
```

Response comes via MQTT (`fullFrameSnapshotAvailable`).

#### List Devices

`GET /hmsweb/v2/users/devices`

Used at startup for initial device discovery. Returns all devices with
properties including battery level, connectivity, model info.

#### Establish Session

`GET /hmsweb/users/session/v3`

Returns `mqttUrl` and sets session cookies. Called after auth.

## Pydantic Models

All models in `eisenberg/models.py`. Parse at the boundary, crash
inside.

| Model              | Source                              | Key fields |
|--------------------|-------------------------------------|------------|
| `DeviceState`      | `d/.../cameras/{id}/is`             | motionDetected, activityState, dateStarted, signalStrength |
| `MotionEvent`      | `u/.../feed/live` (type=motion)     | objCategory, objCategories, objRegion, contentUrl, thumbnailUrl, duration, mediaMeta |
| `ModeChangeEvent`  | `u/.../feed/live` (type=modeChange) | activeMode |
| `MediaUpload`      | `u/.../library/add`                 | presignedContentUrl, presignedThumbnailUrl, recordingStopped |
| `SnapshotAvailable`| `d/.../fullFrameSnapshotAvailable`  | presignedFullFrameSnapshotUrl |
| `ActiveMode`       | `u/.../automation/activeMode/is`    | mode (armAway/armHome/standby), revision |
| `SirenState`       | `d/.../siren/{id}/is`               | sirenState, sirenTrigger, duration, volume, pattern |
| `DeviceProperties` | `d/.../cameras/{id}/privacyZones/is`| batteryLevel, chargingState, signalStrength, connectivity, motionZones |
| `Connectivity`     | `d/.../basestation/connectivity/is` | wifi connected, ssid, rssi, ipAddr |
| `StreamResponse`   | startStream REST response           | url (RTSP), sipCallInfo, iceServers |
| `DeviceInfo`       | GET /hmsweb/v2/users/devices        | deviceId, deviceName, modelId, xCloudId |

## HA Integration

### Config Flow

Multi-step:

1. **`user`** -- email + password
2. **`push_approval`** -- "approve push on your phone" (first time / trust cookie expired only)
3. **`media_storage`** -- dropdown of `hass.config.media_dirs` keys + "Disabled" (default: disabled)
4. **Done** -- stores credentials, device_id UUID, trust cookie data

Auto-discovers all cameras, creates entities for each.

**Reauth:** triggered by `ConfigEntryAuthFailed` if trust cookie dies.
Same push approval step, then done.

**Reconfigure:** change credentials or media storage path.

**Options flow:** media storage selection, detection sensor auto-reset
timeout.

### Coordinator

Event-driven via MQTT, not polling:

- Startup: REST device discovery -> open MQTT connection
- MQTT events update entity state in real-time
- Extends `DataUpdateCoordinator` but `_async_update_data` only does
  periodic health checks (token refresh, device list sync)
- Owns `EisenbergClient`, passes to entities for commands
- Token refresh: proactive before ~2hr expiry

### Entities

| Entity              | Platform        | Source                          |
|---------------------|-----------------|---------------------------------|
| Camera              | `camera`        | Snapshot URL + RTSP stream      |
| Motion detected     | `binary_sensor` | `motionDetected` from MQTT      |
| Person detected     | `binary_sensor` | `objCategory: "Person"` from feed/live |
| Vehicle detected    | `binary_sensor` | `objCategory: "Vehicle"`        |
| Animal detected     | `binary_sensor` | `objCategory: "Animal"`         |
| Battery             | `sensor`        | `batteryLevel`, device_class=battery |
| Signal strength     | `sensor`        | WiFi RSSI / signal strength     |
| Siren               | `switch`        | on/off via notify endpoint      |

**Detection binary sensors:** `motionDetected` resets via MQTT
(`motionDetected: false`). AI classification sensors (person/vehicle/animal)
auto-reset after configurable timeout (default 30s) since MQTT only
sends reset for generic motion.

### Camera Entity

`async_camera_image()`:
- If media archival enabled: serve latest snapshot/thumbnail from local storage
- Otherwise: proxy the presigned S3 URL (works until expiry)

`async_stream_source()`:
1. Call `startStream` with mobile UA -> RTSP URL
2. Return URL to HA stream component
3. URL valid ~30 min, stream component re-requests if needed

### Media Archival

**Configuration:** user selects from `hass.config.media_dirs` during
config flow. Can be changed via options/reconfigure.

**Storage structure:**
```
{media_dir}/eisenberg/{YYYY-MM-DD}/{timestamp}_{type}.ext
```

Examples:
- `eisenberg/2026-04-05/1775341447_motion_person.mp4`
- `eisenberg/2026-04-05/1775341447_motion_person_thumb.jpg`
- `eisenberg/2026-04-05/1775340478_snapshot.jpg`

**Behavior:**
- Motion clip + thumbnail downloaded automatically on motion events
- Snapshots saved on request
- Downloads in background tasks, never block MQTT processing
- No automatic cleanup (user's storage, user's rules)
- Files browsable via HA Media Browser (inside registered media dir)

**HA Events:** `eisenberg_media` fired on every motion/snapshot
regardless of archival setting. Contains metadata (timestamp,
classification, URLs) + local path if archived.

## Error Handling

### MQTT Event Logging

| Scenario                        | Level     | Content |
|---------------------------------|-----------|---------|
| Known event, parsed OK          | `DEBUG`   | Topic, resource, key fields |
| Known event, parse failed       | `WARNING` | Full raw JSON, Pydantic error |
| Unknown topic/resource          | `INFO`    | Full raw JSON, topic |

Unknown events are logged at `INFO` (not `DEBUG`) so they appear in
default HA logs. This is how we discover new event types for modeling.

### Auth Failures

- 401 from REST -> re-auth immediately (instant via trust cookie)
- MQTT disconnect with auth error -> re-auth, reconnect
- Trust cookie expired -> `ConfigEntryAuthFailed` -> HA reauth flow
- Wrong credentials -> clear error in UI, no retry loop

### Battery Camera Constraints

- Camera sleeps most of the time, rely on MQTT push not polling
- Don't spam snapshot/stream requests (burns battery)
- Stream wakes camera for up to 30 min

## Tech Stack

- Python 3.12+, async (aiohttp)
- Pydantic >= 2.0 for all models
- pytest + pytest-asyncio for tests
- pyright strict mode
- ruff linter + formatter
- No external MQTT library (raw MQTT 3.1.1 packet handling)

## Camera Hardware

Developed against Arlo Essential XL HD (VMC2052A):
- Battery-powered with solar charger
- WiFi, cloud-only (no RTSP, no ONVIF, no local streaming)
- PIR motion sensor -> cloud recording -> AI classification
- On-demand streaming via API -> temporary RTSP URL
- Events delivered via MQTT WebSocket
