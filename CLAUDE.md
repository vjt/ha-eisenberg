# ha-eisenberg -- Project Memory

## What This Is
Home Assistant custom component for Arlo cameras, named after skating
legend Arlo Eisenberg. Talks to Arlo cloud API via REST + MQTT
WebSocket. Controls live streaming, captures motion events with AI
classification, manages snapshots and media archival.

## Architecture
- **API client** (`eisenberg/`): typed async client for Arlo REST API +
  MQTT events. Pydantic models for all request/response types. Parses
  at the boundary, crashes on unexpected data. Raw MQTT 3.1.1 packet
  handling over WebSocket (no external MQTT library).
- **HA integration** (`custom_components/eisenberg/`): camera, binary
  sensors (motion/person/vehicle/animal), sensors (battery/signal),
  siren switch, config flow with push approval.
- **Event-driven**: MQTT is the primary data source, not polling.
  REST API used only for commands and initial discovery.

## Tech Stack
- **Python 3.12+**, async (aiohttp)
- **Pydantic** for all models
- **pytest** -- `pytest tests/ -x -q`
- **pyright** -- strict mode, `include` limits to project sources only
- **`./scripts/check.sh`** -- chains pyright + pytest + ruff
- **ruff** -- linter + formatter

## Engineering Standards

### Type System
- **Pydantic models only.** No `@dataclass`. All structured types
  use Pydantic `BaseModel`.
- **Type annotations on all signatures.** No exceptions.
- **Enums over dicts.** Before adding a constant mapping, create a
  StrEnum. Inline dicts drift and duplicate.

### Error Handling
- **Never swallow exceptions.** Handle explicitly or let crash.
- **No default arguments.** Every parameter explicit.
- **No `.get()` with fallbacks** on data that must exist. Parse at
  the boundary, crash inside.
- **Unknown MQTT events**: log at INFO with full payload (not crash).
  Known events that fail parsing: log at WARNING with Pydantic error.

### Architecture
- **Constructor injection.** No global state, no singletons.
- **No leaky abstractions.** Each layer owns its domain.
- **Parse at the boundary.** JSON from Arlo API gets parsed into
  Pydantic models at the HTTP/MQTT layer. Inside the codebase,
  types guarantee correctness.

### Testing
- Assert outcomes, not call sequences.
- Mock at boundaries (Arlo API, MQTT), real dependencies inside.
- Use production code in tests -- never hardcode expected strings.

## Arlo API Reference

### Hosts
- Auth: `ocapi-app.arlo.com` (base64 token in Authorization header)
- API: `myapi.arlo.com` (raw token + xCloudId header)

### Auth Flow
- First time: email/password -> push approval -> trust cookie (14 days)
- Subsequent: trust cookie -> instant auth (no push)
- Token: ~2hr lifetime, proactive refresh
- Key: `factorId` from `getFactorId` MUST be passed to `startAuth`

### MQTT
- WebSocket: `wss://mqtt-cluster-z1-1.arloxcld.com:8084/mqtt`
- Topics: `d/{xCloudId}/out/#` (device), `u/{userId}/in/#` (user)
- Protocol: MQTT 3.1.1, binary packets

### Streaming
- `POST /hmsweb/users/devices/startStream` with mobile UA -> RTSP URL
- Browser UA -> DASH URL (not useful for HA)
- RTSP URL valid ~30 min via Wowza media server

### Camera
- Arlo Essential XL HD (VMC2052A), battery+solar, WiFi, cloud-only
- No RTSP/ONVIF/local streaming -- all via cloud API

## Access
- SSH to HAOS: `ssh root@homeassistant -p 22222`
- SSH to HA container: `ssh hassio@homeassistant`
- Deploy path: `/mnt/data/supervisor/homeassistant/custom_components/`
- Client library path (inside HA Docker container):
  `/usr/local/lib/python3.14/site-packages/eisenberg/`
