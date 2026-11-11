"""Arlo's presigned media links never leave the integration.

A motion event's contentUrl and thumbnailUrl are presigned: anyone holding one
fetches the user's clip or thumbnail without credentials until it expires.
The `eisenberg_media` event lands in the recorder database and logs land in
bug reports, so neither may carry a link (HACS review, hacs/default#13515).
The event names the archived copies instead, as files and media source ids.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from custom_components.eisenberg.const import CONF_MEDIA_DIR, EVENT_MEDIA
from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg.redact import REDACTED, loggable, redact_links

if TYPE_CHECKING:
    import pytest

CAM = "A5DFEXAMPLE13FD"
SECRET = "Signature=SIG"
CLIP_URL = f"https://arlos3-prod-z1.s3.amazonaws.com/x/clip.mp4?{SECRET}"
THUMB_URL = f"https://arlos3-prod-z1.s3.amazonaws.com/x/thumb.jpg?{SECRET}"
FEED_TOPIC = "u/USER/in/feed/live"


class _Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def read(self) -> bytes:
        return self._body


class _Session:
    """Arlo's media host: answers every link with ``status``, or raises."""

    def __init__(self, status: int, error: Exception | None) -> None:
        self._status = status
        self._error = error

    def get(self, url: str) -> _Resp:
        if self._error is not None:
            raise self._error
        return _Resp(self._status, url.encode())


async def _run(fn: Any, *args: Any) -> Any:
    return fn(*args)


def _coord(
    media_root: Path, media_dir: str, status: int, error: Exception | None
) -> tuple[EisenbergCoordinator, list[dict[str, Any]]]:
    fired: list[dict[str, Any]] = []
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord.hass = SimpleNamespace(  # type: ignore[assignment]
        bus=SimpleNamespace(
            async_fire=lambda event_type, data: (
                fired.append(data)  # type: ignore[misc]
                if event_type == EVENT_MEDIA
                else None
            )
        ),
        config=SimpleNamespace(media_dirs={"local": str(media_root)}),
        async_add_executor_job=_run,
    )
    coord.entry = SimpleNamespace(options={CONF_MEDIA_DIR: media_dir})  # type: ignore[assignment]
    coord._http_session = _Session(status, error)  # type: ignore[assignment]
    coord.motion_events = {}
    coord.latest_thumbnails = {}
    coord.data = {}

    async def _cache(device_id: str, url: str) -> None:
        return None

    coord._cache_image_bytes = _cache  # type: ignore[method-assign]
    coord._push_to_entities = lambda: None  # type: ignore[method-assign]
    return coord, fired


def _motion(**extra: Any) -> dict[str, Any]:
    return {
        "type": "motion",
        "deviceId": CAM,
        "objCategory": "Person",
        "objCategories": ["Person"],
        "duration": "12",
        "contentUrl": CLIP_URL,
        "thumbnailUrl": THUMB_URL,
        "utcCreatedDate": 1793000000000,
        **extra,
    }


class TestMediaEvent:
    async def test_event_carries_no_link(self, tmp_path: Path) -> None:
        coord, fired = _coord(tmp_path, "local", 200, None)
        await coord._handle_feed(FEED_TOPIC, _motion())
        assert len(fired) == 1
        assert SECRET not in json.dumps(fired[0])

    async def test_event_names_the_archived_copies(self, tmp_path: Path) -> None:
        coord, fired = _coord(tmp_path, "local", 200, None)
        await coord._handle_feed(FEED_TOPIC, _motion())
        event = fired[0]
        clip = Path(event["content_file"])
        thumb = Path(event["thumbnail_file"])
        assert clip.read_bytes() == CLIP_URL.encode()
        assert thumb.read_bytes() == THUMB_URL.encode()
        assert event["content_media_id"] == (
            f"media-source://media_source/local/{clip.relative_to(tmp_path)}"
        )
        assert event["thumbnail_media_id"] == (
            f"media-source://media_source/local/{thumb.relative_to(tmp_path)}"
        )

    async def test_event_keeps_the_motion_facts(self, tmp_path: Path) -> None:
        coord, fired = _coord(tmp_path, "local", 200, None)
        await coord._handle_feed(FEED_TOPIC, _motion())
        event = fired[0]
        assert event["device_id"] == CAM
        assert event["category"] == "Person"
        assert event["categories"] == ["Person"]
        assert event["duration"] == "12"
        assert event["timestamp"] == 1793000000000

    async def test_archival_off_names_nothing(self, tmp_path: Path) -> None:
        coord, fired = _coord(tmp_path, "", 200, None)
        await coord._handle_feed(FEED_TOPIC, _motion())
        event = fired[0]
        assert event["content_file"] is None
        assert event["content_media_id"] is None
        assert event["thumbnail_file"] is None
        assert event["thumbnail_media_id"] is None
        assert SECRET not in json.dumps(event)


class TestLogsCarryNoLink:
    async def test_failed_download(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        coord, fired = _coord(tmp_path, "local", 403, None)
        await coord._handle_feed(FEED_TOPIC, _motion())
        assert "HTTP 403" in caplog.text
        assert SECRET not in caplog.text
        assert fired[0]["content_media_id"] is None

    async def test_download_error_quoting_the_link(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        coord, fired = _coord(tmp_path, "local", 200, ValueError(f"bad link {CLIP_URL}"))
        await coord._handle_feed(FEED_TOPIC, _motion())
        assert "ValueError" in caplog.text
        assert SECRET not in caplog.text
        assert fired[0]["content_file"] is None

    async def test_unparseable_motion_event(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        coord, fired = _coord(tmp_path, "local", 200, None)
        payload = _motion()
        del payload["utcCreatedDate"]
        await coord._handle_feed(FEED_TOPIC, payload)
        assert "Failed to parse motion event" in caplog.text
        assert SECRET not in caplog.text
        assert fired == []


class TestRedact:
    def test_links_redacted_at_any_depth(self) -> None:
        payload = {
            "contentUrl": CLIP_URL,
            "nested": {"list": [THUMB_URL, "rtsps://host/stream?egressToken=T", 3]},
            "deviceId": CAM,
        }
        assert redact_links(payload) == {
            "contentUrl": REDACTED,
            "nested": {"list": [REDACTED, REDACTED, 3]},
            "deviceId": CAM,
        }

    def test_loggable_is_cut_json(self) -> None:
        assert (
            loggable({"deviceId": CAM, "url": CLIP_URL}, 20)
            == json.dumps({"deviceId": CAM, "url": REDACTED})[:20]
        )
