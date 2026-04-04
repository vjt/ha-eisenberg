"""Tests for MQTT event stream dispatcher."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from eisenberg.mqtt import MQTTEventStream, TopicRouter


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
