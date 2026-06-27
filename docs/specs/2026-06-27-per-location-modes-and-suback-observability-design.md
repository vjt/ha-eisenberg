# Per-location modes (#16) + SUBACK observability (#15 instrumentation)

**Date:** 2026-06-27
**Status:** Design — approved for spec, pending user review
**Issues:** #16 (multi-location snapshot 4006), #15 (subscribe-refusal observability), bundles already-merged #13 + #14.

## Context

GitHub issue #12 (reporter **nyscot**, 17-base owner account) decomposed into
four sub-issues. #13 (MQTT framing) and #14 (connectivity-by-parent) are merged
to `master` but **not yet released** — the live HAOS box still runs 0.3.4.

This release ships the next two improvements and carries #13 + #14 with them:

- **#16 — per-location modes.** `coordinator.py` tracks a single global
  `active_mode` / `location_id` / `_mode_revision` and resolves location via
  `locations[0]` (`coordinator.py:468-470`). On accounts with more than one
  location this gates *every* camera's snapshot on the wrong location's mode,
  so Arlo rejects on-demand snapshots with error 4006 ("Invalid camera activity
  state change"). Single-location accounts (the known majority) are unaffected.

- **#15 instrumentation — SUBACK observability.** The broker partially refuses
  the broad `d/{xCloudId}/out/#` subscription on large accounts. Today grants
  log at DEBUG on the `eisenberg.*` library logger (`mqtt.py:218`), which HA's
  per-integration "Enable debug logging" toggle does **not** elevate (it only
  raises `custom_components.eisenberg`). So a reporter sees the WARNING refusals
  but none of the grants and cannot tell partial-vs-total, nor whether the
  `u/{userId}/in/#` user topic survives. This release surfaces that signal in a
  namespace the supported toggle reaches. **The actual subscribe-model fix is
  out of scope** (needs nyscot's per-device `allowedMqttTopics` capture, which
  this instrumentation is designed to obtain cleanly).

### Verified design input

pyaarlo (`twrecked/pyaarlo`, `location.py:44`) maps location → devices via a
`gatewayDeviceIds` array carried in the `/hmsdevicemanagement/users/{userId}/
locations` response — the same endpoint our `get_locations` already calls
(`client.py:550-582`). A device belongs to a location when its **gateway**
(`DeviceInfo.parent_id`, or its own `device_id` when base-less) is in that
location's `gatewayDeviceIds`. Mode get/set is already identical to ours
(`GET/PUT /hmsweb/automation/v3/activeMode?locationId={id}&revision=N`). Our
`LocationInfo` model has `extra: "ignore"` and currently drops
`gatewayDeviceIds` (`models.py:143-149`).

The exact live shape of `gatewayDeviceIds` is confirmed against pyaarlo source;
it is **re-verified on the live single-location HAOS box during deploy** (the
`get_locations` body already logs at DEBUG, `client.py:565`).

## Scope

In scope:

1. **#16** — per-location mode tracking, device→location resolution, one
   security-mode `select` per location.
2. **#15 instrumentation** — coordinator-level SUBACK summary
   (granted / refused / user-topic) in the `custom_components.eisenberg`
   namespace.
3. Release carrying #13 + #14 + the above (version bump → PyPI → GitHub).

Out of scope (later release, blocked on data):

- The actual #15 subscribe-model change (drop broad wildcard → subscribe to
  per-device `allowedMqttTopics`). Needs nyscot's re-posted topic dump + a
  fresh capture from the instrumented build.
- V3 custom modes (`mode:"custom"` + `{deviceId: uuid}`). pyaarlo supports them;
  we handle only standard `armAway`/`armHome`/`standby`. Tracked separately.

## Part A — #16 per-location modes

### Models (`eisenberg/models.py`)

- `LocationInfo` gains `gateway_device_ids: list[str] = Field(default_factory=list,
  alias="gatewayDeviceIds")`. Missing/absent → empty list (no crash).
- New `LocationState(BaseModel)`: `location_id: str`, `location_name: str | None`,
  `gateway_device_ids: list[str]`, `active_mode: str | None`,
  `mode_revision: int`. This is the per-location runtime record.

### Coordinator (`custom_components/eisenberg/coordinator.py`)

- Replace the three scalars (`active_mode`, `location_id`, `_mode_revision`,
  lines 93-95) with `self.locations: dict[str, LocationState]` keyed by
  `location_id`.
- **Device→location resolution.** Helper
  `location_for_device(device) -> LocationState | None`: gateway =
  `device.parent_id or device.device_id`; return the `LocationState` whose
  `gateway_device_ids` contains that gateway. A device matching no location →
  fall back to the sole/first location **and log WARNING** with the device_id
  and gateway id (loud, not silent).
- **Startup discovery** (replaces `coordinator.py:464-482`): fetch
  `get_locations`; for each location build a `LocationState` and fetch its own
  `get_active_mode` for `active_mode` + `mode_revision`. `len(locations) > 1` →
  INFO line listing each location, its gateway ids, and the devices resolved to
  it.
- **Snapshot gating** (`coordinator.py:484-493` and the on-demand
  `eisenberg.snapshot` path): gate each device on **its own** location's
  `active_mode != "standby"`, not a global.
- **Mode set** (`async_set_active_mode`, `coordinator.py:314-349`): takes a
  `location_id`; updates that `LocationState` optimistically; revision retry
  unchanged but scoped to the location.
- **MQTT event routing.** `_handle_active_mode` (`coordinator.py:749-755`) and
  the `modeChange` feed handler (`coordinator.py:719`) route by the event's
  `locationId` to the matching `LocationState`. Events carry `locationId`
  (`models.py:107,135`). An event for an unknown location → INFO (per CLAUDE.md
  unknown-event rule), no crash.

### Select (`custom_components/eisenberg/select.py`)

- `async_setup_entry` emits **one `SecurityModeSelect` per location**, each
  bound to its `location_id` and named by `location_name` (fall back to a
  generic name when absent). Single-location accounts get exactly one entity —
  unchanged UX and unique_id for the existing entity must be preserved for the
  single-location case to avoid orphaning (`eisenberg_security_mode`); multi
  uses `eisenberg_security_mode_{location_id}`.
- `async_select_option` calls `coordinator.async_set_active_mode(location_id,
  option)`; `current_option` reads that location's `active_mode`.

### Tests (`tests/`)

- Multi-location fixture (2 locations, devices split by gateway): each device
  resolves to the correct location; snapshot gating respects per-location mode;
  a mode event for location B does not move location A.
- Single-location no-regression: one select, existing unique_id, current
  behaviour intact.
- `LocationInfo` parses `gatewayDeviceIds`; absent field → `[]`.
- Device whose gateway matches no location → fallback + WARNING.

## Part B — #15 SUBACK observability instrumentation

### Models (`eisenberg/models.py`)

- `TopicResult(BaseModel)`: `topic: str`, `code: int`, `granted: bool`.
- `SubscribeOutcome(BaseModel)`: `results: list[TopicResult]` with computed
  properties `granted_count`, `refused_count`, `refused_topics: list[str]`, and
  `result_for(topic) -> TopicResult | None` (used to report the user topic).

### MQTT (`eisenberg/mqtt.py`)

- `connect()` (`mqtt.py:200-225`) stops discarding the per-topic codes. After
  parsing the SUBACK it builds a `SubscribeOutcome` from `zip(topics, codes)`
  and stores it as `self.subscribe_outcome: SubscribeOutcome | None`. Existing
  DEBUG/WARNING/ERROR lines (216, 218, 220) stay as-is for library-level debug.

### Coordinator (`custom_components/eisenberg/coordinator.py`)

- After `await self._mqtt.connect()` (`coordinator.py:462`), read
  `self._mqtt.subscribe_outcome` and log in the **`custom_components.eisenberg`**
  namespace (reachable by the integration debug toggle):
  - INFO: `MQTT SUBACK: {granted} granted, {refused} refused of {total}`.
  - WARNING (only if refused): `MQTT refused topic filters: {refused_topics}`.
  - INFO: `MQTT user topic u/{user_id}/in/# = {GRANTED|REFUSED|ABSENT}` — the
    exact signal needed to confirm whether the user push channel survives.

### Instrumentation-over-YAML policy

Proper instrumentation is primary. The coordinator summary is designed so a
reporter needs only the standard "Enable debug logging" toggle — no
`configuration.yaml` `logger:` edits and no Developer Tools `logger.set_level`.
A YAML edit is a last resort: only request one if a future signal genuinely
cannot be surfaced through the integration namespace, and ask the user first.

### Tests (`tests/`)

- `connect()` populates `subscribe_outcome` from a SUBACK packet with mixed
  grant/refuse codes (parse at the boundary, assert the model).
- `SubscribeOutcome` computed properties: counts, `refused_topics`,
  `result_for` for the user topic.
- Coordinator logs the summary with correct counts and the user-topic verdict
  (assert on emitted records / outcome, not call order).

## Verification plan (live HAOS)

Run the `eisenberg-deploy` skill: test + typecheck + deploy to HAOS + smoke. On
the live single-location account this:

1. Confirms the SUBACK summary emits under `custom_components.eisenberg` with
   the normal debug toggle (expect all-granted + user-topic GRANTED — no ACL
   issue on this account, but it validates the logging path and namespace).
2. Captures the real `get_locations` body (DEBUG) → **confirms the live
   `gatewayDeviceIds` field name/shape** for #16.
3. Confirms #16 single-location no-regression: exactly one security-mode select,
   snapshot gating unchanged, entities populate.

If the live `get_locations` shape diverges from pyaarlo's `gatewayDeviceIds`,
revisit the #16 resolution before release.

## Release

`eisenberg-release` skill (bump → PyPI → GitHub tag), version 0.3.5. Ships
#13 + #14 + #16 + #15-instrumentation. After it is live, ping nyscot on #15 for
a fresh capture from the instrumented build (and his re-posted topic dump) to
drive the actual #15 subscribe-model fix in a later release.

## Risks

- **#13 core event-path refactor** field-tested only on one idle account so far;
  the live-HAOS verify step is the field test before release.
- **Multi-location split** has no real multi-location account to field-verify;
  mitigated by fixtures + the degenerate single-location path being identical to
  today.
- **Entity unique_id migration**: single-location must keep
  `eisenberg_security_mode` to avoid orphaning the existing entity.
