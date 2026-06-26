"""Tests for MQTT event stream dispatcher."""

from __future__ import annotations

from unittest.mock import MagicMock

from eisenberg.mqtt import TopicRouter, build_subscribe_topics


class TestBuildSubscribeTopics:
    def test_one_base_no_extras(self) -> None:
        topics = build_subscribe_topics(
            x_cloud_ids=["BASE-A"],
            user_id="USER",
            extra_topics=[],
        )
        assert topics == ["d/BASE-A/out/#", "u/USER/in/#"]

    def test_multiple_bases_preserve_order(self) -> None:
        topics = build_subscribe_topics(
            x_cloud_ids=["BASE-A", "BASE-B"],
            user_id="USER",
            extra_topics=[],
        )
        assert topics == [
            "d/BASE-A/out/#",
            "d/BASE-B/out/#",
            "u/USER/in/#",
        ]

    def test_device_topics_appended_and_deduped(self) -> None:
        # A doorbell whose own allowedMqttTopics name a resource our wildcards
        # would already cover, plus one that lands outside them.
        topics = build_subscribe_topics(
            x_cloud_ids=["BASE-A"],
            user_id="USER",
            extra_topics=[
                "d/BASE-A/out/#",  # duplicate of the wildcard — must collapse
                "d/OTHER-CLOUD/out/doorbells/FB1/#",
                "u/USER/in/#",  # duplicate of the user wildcard
            ],
        )
        assert topics == [
            "d/BASE-A/out/#",
            "u/USER/in/#",
            "d/OTHER-CLOUD/out/doorbells/FB1/#",
        ]


class TestTopicRouter:
    def test_register_and_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/cameras/+/is", handler)
        matched = router.match("d/CLOUD/out/cameras/CAM123/is")
        assert matched == [handler]

    def test_wildcard_hash(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/#", handler)
        matched = router.match("d/CLOUD/out/cameras/CAM/is")
        assert matched == [handler]

    def test_no_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("d/+/out/cameras/+/is", handler)
        matched = router.match("u/USER/in/feed/live")
        assert matched == []

    def test_multiple_handlers(self) -> None:
        router = TopicRouter()
        h1 = MagicMock()
        h2 = MagicMock()
        router.register("d/+/out/#", h1)
        router.register("d/+/out/cameras/+/is", h2)
        matched = router.match("d/CLOUD/out/cameras/CAM/is")
        assert h1 in matched
        assert h2 in matched

    def test_exact_match(self) -> None:
        router = TopicRouter()
        handler = MagicMock()
        router.register("u/USER/in/feed/live", handler)
        matched = router.match("u/USER/in/feed/live")
        assert matched == [handler]
        assert router.match("u/USER/in/feed/other") == []
