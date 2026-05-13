# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
