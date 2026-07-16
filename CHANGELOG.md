# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.3.11 — 2026-07-16

### Changed

- **MQTT subscription refusals no longer log at WARNING (#15).** On grantee/shared
  and multi-base accounts the broker refuses the broad `d/{xCloudId}/out/#`
  wildcard the guest doesn't own — expected and harmless, since each device's own
  `allowedMqttTopics` carry its events regardless. These partial refusals spammed
  the log at WARNING on every startup (6+ lines on large accounts), reading as a
  fault when nothing is wrong. They're now DEBUG — still reachable via the
  integration's "Enable debug logging" toggle when you want the granted/refused
  breakdown. A *total* refusal (the genuine "no events will arrive" failure) still
  logs at ERROR.

## 0.3.10 — 2026-07-16

### Fixed

- **Options flow forced local media storage on just to reach the ffmpeg toggle
  (#23).** The options form (Settings → Devices & Services → Eisenberg →
  Configure) keyed its "Disabled" media-storage choice as the empty string,
  which Home Assistant's form treats as an unset `Required` field — so the form
  refused to submit unless a real storage directory was picked, coupling two
  unrelated settings and blocking access to the new **Route live stream through
  ffmpeg** toggle. The options flow now uses the same non-empty `__disabled__`
  sentinel as the setup flow, shared through one helper so the two can't drift
  again; archival can be left disabled and the ffmpeg toggle set on its own.
  First test coverage of the options flow.

## 0.3.9 — 2026-07-15

### Added

- **Opt-in ffmpeg live streaming for cameras go2rtc can't read natively (#23).**
  On the 2K Essential XL 2nd-gen (VMC3052A), Home Assistant's bundled go2rtc
  can't read Arlo's RTSP stream with its native client ("RTSP wrong input" /
  "RTP header size insufficient") — the failed read tears down Arlo's
  single-use stream session and the live view stays black. A new **Route live
  stream through ffmpeg** option (Settings → Devices & Services → Eisenberg →
  Configure) routes the source through ffmpeg — the tolerant reader every
  mature Arlo integration uses — which handles the stream correctly. Off by
  default: cameras go2rtc already reads natively (e.g. the 1080p VMC2052A) keep
  the leaner, smoother native path unchanged (no video transcode either way).
  Enable it only if your live view is black.

## 0.3.8 — 2026-07-13

### Fixed

- **Security mode set silently no-ops on shared-device accounts (#21).** When
  a base station is shared to your account from another Arlo account, its
  devices live under `sharedLocations` in the locations API — a sibling of
  `userLocations` we were ignoring. Mode set/get landed on your own (empty,
  device-less) default location: the cloud returned `success: True` and the
  select flipped, but the physical base never changed (the Arlo app still
  showed the old mode). `get_locations` now unions owned **and** shared
  locations, and the coordinator resolves each device to the location that
  actually gateways it, so mode commands reach the real base. (If the owner
  grants you only view access, the command now fails loudly instead of
  pretending to succeed.)
- **Duplicate entities / "does not generate unique IDs" on base stations with
  a built-in siren (#21).** Arlo returns such a base as two device records
  sharing one deviceId (the siren twin's modelId suffixed `-siren`), so every
  entity collided on `{deviceId}_*` and Home Assistant dropped half of them.
  Discovery now collapses the twin into a single device (the siren switch
  still works — it targets the deviceId either way).

## 0.3.7 — 2026-07-09

### Added

- **Manual snapshot button (#8).** Each camera now exposes a
  `button.<camera>_snapshot` entity. Pressing it asks Arlo for a fresh
  full-frame snapshot; the image arrives over MQTT and refreshes the camera
  tile, exactly like motion-triggered snapshots. Handy for automations that
  feed a still to an AI/image-analysis step. The standby-guard (Arlo refuses
  cloud snapshots while disarmed → error 4006) and session-retry now live on
  the coordinator, shared by the button and the existing `eisenberg.snapshot`
  service. Verified live end-to-end on HAOS.

## 0.3.6 — 2026-06-28

### Fixed

- **Snapshot 4006 on base-stationed accounts (#16).** Per-device commands
  (snapshot, spotlight, siren) were addressed to the camera id. Arlo routes
  device commands through the controlling base station, so on accounts where
  a camera lives under a real base station (`parentId != deviceId`) the base
  rejected the `→ fullFrameSnapshot` transition with "Invalid camera activity
  state change" (4006) from a cold start. Commands now target the device's
  base station (its `parentId`, or its own id when base-less), and snapshots
  use Arlo's dedicated `fullFrameSnapshot` endpoint — mirroring pyaarlo. The
  0.3.5 per-location mode fix was necessary but not sufficient; this is the
  routing half. Base-less accounts (single camera = its own gateway) are
  unchanged.

## 0.3.5 — 2026-06-27

### Added

- **Per-location security modes (#16).** Modes, revisions and the
  security-mode select are tracked per Arlo location instead of a single
  global `locations[0]`. A device maps to its location via the location's
  `gatewayDeviceIds` (Arlo returns these as `{ownerId}_{deviceId}`; the
  resolver matches the suffix, mirroring pyaarlo). Snapshot gating and mode
  changes now respect each camera's own location. Multi-location accounts get
  one security-mode select per location; single-location accounts are
  unchanged (one select, original entity preserved). Fixes on-demand snapshots
  being rejected with error 4006 on multi-location accounts.
- **MQTT SUBACK observability (#15).** The coordinator logs a SUBACK summary —
  granted/refused counts, the refused topic filters, and the `u/{userId}/in/#`
  user-topic verdict — under the `custom_components.eisenberg` logger, so a
  standard "Enable debug logging" run surfaces it. Previously subscription
  grants were logged only on the `eisenberg.*` library logger, which the
  per-integration debug toggle cannot elevate, making partial broker refusals
  look like total failures.

### Fixed

- **MQTT WebSocket framing (#13).** Buffer inbound bytes and decode every MQTT
  packet per WebSocket frame, including the CONNACK/SUBACK handshake reads.
  Coalesced or split PUBLISHes (e.g. snapshot delivery) are no longer dropped.
- **Base station connectivity sensors (#14).** Resolve connectivity by the
  parent base station (`parentId`), falling back to the device's own id, so
  the `*_base_station_connectivity` sensors no longer stay `unknown` on
  accounts with real (separate) base stations.

## 0.3.4 — 2026-06-27

### Fixed

- **AI detection sensors no longer cross-fire across cameras.** The
  person/vehicle/animal binary sensors fired off stale events: HA
  broadcasts every coordinator update to all entities, and the last
  motion event per device stays in the coordinator forever, so any
  unrelated update from any camera re-triggered a sensor that had
  already auto-reset. On multi-camera setups one camera detecting a
  person lit up every other camera's "Person detected" sensor. Each
  sensor now tracks the key of the last event it acted on (`feedId`,
  falling back to `utcCreatedDate`) and ignores rebroadcasts of an
  already-handled event. Reported with a fix by @anthonytorretti (#11).

### Added

- Regression test for `DetectionSensor` event de-duplication — first
  test coverage of the HA integration layer.

## 0.3.3 — 2026-06-26

### Fixed

- **Doorbells now report state.** Video doorbells (e.g. FB1001A) publish
  on the `doorbells` MQTT resource, not `cameras` — only `cameras/+/is`
  was handled, so doorbell motion/battery/signal never updated. Added
  `doorbells/+/is`, `privacyZones/is` and snapshot handlers. Part of #10.

### Added

- **Subscribe to each device's declared `allowedMqttTopics`** on top of
  the `d/{xCloudId}/out/#` wildcards. Covers doorbells and base-less
  cameras whose events live under a topic root the wildcards miss.
- **SUBACK return codes are now checked.** A broker that refuses a topic
  filter (ACL) is logged at WARNING instead of failing silently — the
  likely cause of "entities never update" on some accounts.
- **More MQTT debug logging:** every received topic, and each device's
  `allowedMqttTopics` at startup. `manifest.json` now lists the
  `eisenberg` logger so HA's one-click debug toggle covers the library.

## 0.3.2 — 2026-06-19

### Fixed

- **Streaming, snapshot, spotlight and siren now work on every camera
  of a multi-base account, not just the first.** Each camera's xCloudId
  is its parent base station's, but every per-device REST call was sent
  with the *first* discovered device's xCloudId. On accounts whose
  cameras span multiple base stations (the standalone Essential cameras
  are each their own base), Arlo rejected the call for any other camera
  with `2217 — The device does not exist`, so live streaming never
  started (the camera entity reported "does not support play stream
  service"), and the siren and spotlight failed the same way.
  `get_devices()` now records each device's own xCloudId and per-device
  calls send the right one. Diagnosed and first patched by
  @anthonytorretti. Closes #7, finishes the streaming half of #2.

## 0.3.1 — 2026-05-13

### Fixed

- **Multi-base-station accounts no longer lose device state.** When an
  Arlo account spans more than one base station, each base has its own
  xCloudId — and we used to subscribe MQTT to only the first one we
  found. Battery, signal, connectivity, motion etc. for cameras on
  other bases never reached HA, leaving those entities stuck on
  `unknown`. `MQTTEventStream` now takes a list of xCloudIds and the
  coordinator computes the full set from the discovered devices.
  Closes #6.
- **Stale tokens now relogin instead of bubbling stack traces.** Arlo
  invalidates a token server-side as soon as the same account logs in
  somewhere else (typically the official Arlo app on a phone). The
  next REST call would come back with `{"error": "2015"}` and
  surfaced as a stack trace on mode change, or as a silent infinite
  spinner on live streaming. The client now raises `SessionExpiredError`
  distinctly for code 2015, and the coordinator runs every
  user-triggered call (snapshot, stream, mode change, siren,
  spotlight) through `call_with_session_retry`, which catches the
  error, relogins silently via the trust cookie, and retries once.
  Closes #2.

### Added

- More setup-time logging: device discovery now prints each camera's
  id, friendly name, model and xCloudId, plus a warning when more
  than one base station is in play. The silent-login start/complete
  is also logged so re-auth attempts are visible at INFO without
  needing `debug` on.

## 0.3.0 — 2026-05-13

### Added

- **Camera spotlight light entity.** Arlo cameras with an integrated
  spotlight (Essential XL HD and family) now expose a Home Assistant
  `light` entity with on/off and brightness control. HA's 0-255
  brightness maps to Arlo's 0-100 intensity. State is sourced from
  the MQTT device-properties dump on
  `cameras/{id}/privacyZones/is` plus any partial property update
  that carries a `spotlight` key. Closes #1.
- `SpotlightState` Pydantic model and `EisenbergClient.set_spotlight()`
  client method (resource `cameras/{id}`, same `notify` endpoint as
  the existing siren control).

## 0.2.0 — 2026-05-13

### Added

- **Multi-factor verification picker.** Arlo migrated its MFA backend
  to the PingOne SDK in early May 2026, and accounts can now have a
  mix of PUSH / EMAIL / SMS factors. The config flow discovers them
  via `GET /api/getFactors` (no side effect) and lets the user pick
  one. PUSH approves on phone; EMAIL/SMS shows an OTP-entry form.
  Single-factor accounts skip the picker. Closes #5 (push never
  arrived for users whose Arlo Secure app was outdated — they can
  now fall back to email/SMS).
- **Reconfigure step** on the integration card (Settings → Devices
  & Services → Eisenberg → ⋮ → Reconfigure). Re-runs the MFA picker
  on demand — no need to wait for trust-cookie expiry or trigger a
  reauth failure.

### Changed

- `PushApprovalRequired` → `MfaRequired(factors=[...])` exception.
  Carries the factor list so the caller can drive the picker.
- `EisenbergClient.start_push_login()` → `start_mfa(factor)`.
  `try_finish_auth(code, otp=None)` — pass `otp` for EMAIL/SMS,
  leave it out for PUSH polling.

## 0.1.3 — 2026-04-28

### Fixed

- Token-refresh threshold tightened from 90 min to 60 min. Arlo tokens
  live ~2 h; the coordinator polls every 30 min, and the previous 90
  min margin could land a refresh at +120 min in the worst-case
  alignment — exactly at token expiry. 60 min guarantees ≥30 min of
  headroom in any alignment.

## 0.1.2 — 2026-04-28

Hygiene release.

### Changed

- Test fixtures and design docs no longer carry the author's real
  Arlo device serial / xCloudId — replaced with synthetic well-formed
  values. Git history rewritten to scrub those identifiers from all
  prior commits before the repo went public. 0.1.0 / 0.1.1 tags and
  GitHub releases deleted as part of the rewrite. PyPI versions
  0.1.0 and 0.1.1 are clean (the published wheels never carried the
  test fixtures) but were superseded for version-numbering hygiene.

## 0.1.1 — 2026-04-28

Packaging-only fix.

### Fixed

- `pyproject.toml` was missing `readme = "README.md"`, so the PyPI
  project page rendered with no long description. 0.1.1 ships the
  same code as 0.1.0 plus the README inline on PyPI.

## 0.1.0 — 2026-04-28

First public release.

### Added

- Camera entity with snapshots, motion thumbnails and stream-keyframe
  caching. Tile survives HA restarts via on-disk archive and reseeds at
  boot. Sub-second live RTSPS streaming (forced TCP, ffmpeg low-delay).
- Binary sensors: motion (from MQTT `motionDetected`), AI-classified
  person/vehicle/animal detections, and base-station connectivity.
- Security mode select (armAway / armHome / standby) backed by Arlo's
  v3 location automation API with revision tracking and one-shot retry
  on revision conflict.
- Battery and signal-strength sensors. Siren switch.
- `eisenberg.snapshot` service for on-demand full-frame snapshots.
  Refuses with a clear error when the camera is in standby.
- Media archival to a configurable `media_dirs` location, with rolling
  retention (default 14 days, user-configurable 1–365).
- `eisenberg_media` HA event fired on motion detection with full
  metadata (categories, content URL, thumbnail URL, timestamp).

### Architecture

- Event-driven coordinator built on raw MQTT 3.1.1 over WebSocket — no
  polling, REST used only for commands and discovery.
- Form-driven push approval: each user click is a single `finishAuth`
  call, so the integration cannot rate-limit a user out by retrying.
- Trust cookie persisted to the config entry; silent re-login at every
  startup. Reauth flow re-uses stored credentials and only asks for a
  password if Arlo rejects the stored one.

### Tooling

- HACS metadata (`hacs.json`) and "Open in HACS" badge.
- pyright (strict on project sources), ruff lint + format, pytest with
  aioresponses; chained via `scripts/check.sh`.
- Deploy and release skills under `.claude/skills/`.
