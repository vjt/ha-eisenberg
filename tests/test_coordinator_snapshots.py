"""Snapshot delivery over MQTT.

Arlo answers a snapshot request on one of two topics depending on the model:
`fullFrameSnapshotAvailable` (carrying presignedFullFrameSnapshotUrl) or
`lastImageSnapshotAvailable` (carrying presignedLastImageUrl). Issue #26
(torselden) reported the second one falling through as an Unhandled topic —
the request succeeded, Arlo delivered the image, but the URL was never cached,
never archived, and the tile never refreshed. pyaarlo keys off the same
property (camera.py: action == "lastImageSnapshotAvailable"). Both topics must
land in the same cache/archive/push path, for cameras and doorbells alike.
"""

from __future__ import annotations

from typing import Any

from custom_components.eisenberg.coordinator import EisenbergCoordinator

CAM = "A5DFEXAMPLE13FD"
LAST_IMAGE_TOPIC = f"d/GSYQ8B-CLOUD/out/cameras/{CAM}/lastImageSnapshotAvailable"
FULL_FRAME_TOPIC = f"d/GSYQ8B-CLOUD/out/cameras/{CAM}/fullFrameSnapshotAvailable"
LAST_IMAGE_URL = "https://arlolastimage-z1.arlo.com/x/lastImage.jpg?Signature=SIG"
FULL_FRAME_URL = "https://example.com/fullframe.jpg"


class _RecordingCoordinator:
    """Coordinator with the download/archive/push boundary stubbed out."""

    def __init__(self) -> None:
        self.coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
        self.coord.latest_snapshots = {}
        self.coord.data = {}
        self.cached: list[tuple[str, str]] = []
        self.archived: list[tuple[str, str, str, str]] = []
        self.pushes = 0

        async def _cache(device_id: str, url: str) -> None:
            self.cached.append((device_id, url))

        async def _archive(device_id: str, url: str, media_type: str, ext: str) -> None:
            self.archived.append((device_id, url, media_type, ext))

        def _push(data: Any) -> None:
            self.pushes += 1

        self.coord._cache_image_bytes = _cache  # type: ignore[method-assign]
        self.coord._archive_media = _archive  # type: ignore[method-assign]
        self.coord.async_set_updated_data = _push  # type: ignore[method-assign]


def _payload(url: str) -> dict[str, Any]:
    return {
        "action": "lastImageSnapshotAvailable",
        "resource": f"cameras/{CAM}",
        "properties": {"presignedLastImageUrl": url},
    }


class TestLastImageSnapshot:
    async def test_records_presigned_url(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_last_image_snapshot(LAST_IMAGE_TOPIC, _payload(LAST_IMAGE_URL))
        assert rec.coord.latest_snapshots[CAM] == LAST_IMAGE_URL

    async def test_caches_bytes_before_url_expires(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_last_image_snapshot(LAST_IMAGE_TOPIC, _payload(LAST_IMAGE_URL))
        assert rec.cached == [(CAM, LAST_IMAGE_URL)]

    async def test_archives_as_snapshot(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_last_image_snapshot(LAST_IMAGE_TOPIC, _payload(LAST_IMAGE_URL))
        assert rec.archived == [(CAM, LAST_IMAGE_URL, "snapshot", "jpg")]

    async def test_pushes_coordinator_update_so_tile_refreshes(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_last_image_snapshot(LAST_IMAGE_TOPIC, _payload(LAST_IMAGE_URL))
        assert rec.pushes == 1

    async def test_malformed_payload_does_not_cache(self) -> None:
        # Missing presignedLastImageUrl — must not poison the cache with a
        # bogus entry, and must not crash the MQTT listen loop.
        rec = _RecordingCoordinator()
        await rec.coord._handle_last_image_snapshot(
            LAST_IMAGE_TOPIC, {"action": "lastImageSnapshotAvailable", "properties": {}}
        )
        assert rec.coord.latest_snapshots == {}
        assert rec.cached == []

    async def test_short_topic_ignored(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_last_image_snapshot("d/X/out", _payload(LAST_IMAGE_URL))
        assert rec.coord.latest_snapshots == {}


class TestFullFrameSnapshotStillWorks:
    """Regression guard — issue #26's fix must not disturb the original path."""

    async def test_full_frame_still_cached_and_archived(self) -> None:
        rec = _RecordingCoordinator()
        await rec.coord._handle_snapshot(
            FULL_FRAME_TOPIC,
            {"properties": {"presignedFullFrameSnapshotUrl": FULL_FRAME_URL}},
        )
        assert rec.coord.latest_snapshots[CAM] == FULL_FRAME_URL
        assert rec.cached == [(CAM, FULL_FRAME_URL)]
        assert rec.archived == [(CAM, FULL_FRAME_URL, "snapshot", "jpg")]


class _RecordingMQTT:
    def __init__(self) -> None:
        self.topics: list[str] = []

    def on(self, topic: str, _handler: Any) -> None:
        self.topics.append(topic)

    def on_disconnect(self, _handler: Any) -> None:
        pass


class TestSnapshotSubscriptions:
    def _topics(self) -> list[str]:
        coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
        mqtt = _RecordingMQTT()
        coord._mqtt = mqtt  # type: ignore[assignment]
        coord._register_mqtt_handlers()
        return mqtt.topics

    def test_subscribes_last_image_for_cameras(self) -> None:
        assert "d/+/out/cameras/+/lastImageSnapshotAvailable" in self._topics()

    def test_subscribes_last_image_for_doorbells(self) -> None:
        # Doorbells are a distinct Arlo resource (issue #10) and answer on
        # their own topic tree.
        assert "d/+/out/doorbells/+/lastImageSnapshotAvailable" in self._topics()

    def test_still_subscribes_full_frame(self) -> None:
        topics = self._topics()
        assert "d/+/out/cameras/+/fullFrameSnapshotAvailable" in topics
        assert "d/+/out/doorbells/+/fullFrameSnapshotAvailable" in topics
