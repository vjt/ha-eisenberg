# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
