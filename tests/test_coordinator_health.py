"""The health tick must keep the integration alive, not fail fast (#32).

natebrockert's log, 42 hours apart:

    02:45:11 WARNING [eisenberg.mqtt] MQTT WebSocket closed/error
    02:45:11 WARNING [...coordinator] MQTT disconnected, will reconnect on next refresh
    ...
    20:40:14 ERROR [...coordinator] Error requesting eisenberg data: 403,
      message='Attempt to decode JSON with unexpected mimetype: text/html',
      url='https://ocapi-app.arlo.com/api/auth'

The event stream died and never came back. `_async_update_data` ran the token
refresh first and let anything that wasn't MfaRequired / AuthenticationError /
RateLimitedError propagate, so the reconnect block further down never ran. A
WAF block page on ocapi is exactly such an exception. Once the socket was down
*and* the refresh was failing, every 30-minute tick aborted at step one and the
integration stayed permanently deaf until HA was restarted.

Two independent properties are asserted here:

1. A non-terminal refresh failure does not starve the rest of the tick. The
   health tick is a sequence of *independent* maintenance steps, so one
   failing must not cancel the others — and the reconnect is the one that
   turns a transient block into a permanent outage when it is skipped.
2. A dropped socket reconnects on its own backoff rather than waiting out a
   30-minute tick, so the deaf window is seconds, not half an hour.

Terminal auth verdicts still tear the entry down: a trust cookie that really
has expired must reach the user as a reauth flow, not be retried forever.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.eisenberg.coordinator import EisenbergCoordinator
from eisenberg import (
    AuthenticationError,
    DeviceInfo,
    MfaRequired,
    RateLimitedError,
)
from eisenberg.exceptions import TransientAPIError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

CLOUD = "A92ZBCN6-2400-115-112185863"
CAM = "5GG39A71A13BD"


def _camera() -> DeviceInfo:
    return DeviceInfo.model_validate(
        {
            "deviceId": CAM,
            "deviceName": "Front",
            "modelId": "VMC2052A",
            "deviceType": "camera",
            "xCloudId": CLOUD,
            "parentId": CAM,
        }
    )


class _FakeClient:
    """Client boundary: does the token need refreshing, and does that work?"""

    def __init__(self, *, refresh_error: BaseException | None = None) -> None:
        self.mqtt_url = "wss://mqtt-cluster-z1-1.arloxcld.com:8084"
        self.user_id = "USER-123"
        self.token = "a-token"
        self.refresh_error = refresh_error
        self.login_calls = 0

    def token_needs_refresh(self) -> bool:
        return True

    async def login(self) -> None:
        self.login_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error


class _FakeHass:
    """Just enough hass to run background tasks and executor jobs."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def async_create_background_task(
        self,
        target: Coroutine[Any, Any, Any],
        name: str,
        eager_start: bool = True,
    ) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(target, name=name)
        self.tasks.append(task)
        return task

    async def async_add_executor_job(self, func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)


def _coordinator(client: _FakeClient) -> Any:
    """A coordinator with only the HA and network boundaries stubbed."""
    coord = EisenbergCoordinator.__new__(EisenbergCoordinator)
    coord.hass = _FakeHass()  # type: ignore[assignment]
    coord.client = client  # type: ignore[assignment]
    coord._devices = [_camera()]
    coord._mqtt = None
    coord._http_session = object()  # type: ignore[assignment]
    coord._mqtt_lock = asyncio.Lock()
    coord._reconnect_task = None
    coord._last_base_bringup = 0.0
    coord.device_states = {}
    coord.basestation_connection = {}
    coord.locations = {}
    coord.data = {}
    coord.async_set_updated_data = lambda data: None  # type: ignore[method-assign]

    # The other health-tick steps are exercised by their own suites; here they
    # only need to not be the thing under test.
    async def _noop() -> None:
        return None

    coord._prune_old_media = lambda *args, **kwargs: _noop()  # type: ignore[method-assign]
    coord._maybe_renew_base_stations = _noop  # type: ignore[method-assign]
    coord._refresh_device_properties = _noop  # type: ignore[method-assign]
    coord._save_cookies = _noop  # type: ignore[method-assign]
    return coord


def _record_ensure_mqtt(coord: Any) -> list[int]:
    """Replace the reconnect with a counter, so the tick's reach is visible."""
    calls: list[int] = []

    async def _ensure() -> bool:
        calls.append(1)
        return True

    coord._ensure_mqtt = _ensure  # type: ignore[method-assign]
    return calls


class TestRefreshFailureDoesNotStarveReconnect:
    """A refresh that never reached Arlo must not cancel the reconnect."""

    @pytest.mark.parametrize(
        "error",
        [
            TransientAPIError("POST /api/auth returned 403 text/html, not JSON"),
            aiohttp.ClientError("connection reset"),
            TimeoutError(),
            KeyError("meta"),
        ],
        ids=["waf-block-page", "client-error", "timeout", "unexpected-shape"],
    )
    async def test_tick_still_reconnects_mqtt(self, error: BaseException) -> None:
        client = _FakeClient(refresh_error=error)
        coord = _coordinator(client)
        calls = _record_ensure_mqtt(coord)

        await coord._async_update_data()

        assert client.login_calls == 1
        assert calls, f"{type(error).__name__} aborted the tick before the MQTT reconnect"

    async def test_failure_is_logged_with_the_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A silent skip is how this stayed invisible for 42 hours."""
        client = _FakeClient(
            refresh_error=TransientAPIError("POST /api/auth returned 403 text/html, not JSON")
        )
        coord = _coordinator(client)
        _record_ensure_mqtt(coord)

        with caplog.at_level("WARNING", logger="custom_components.eisenberg.coordinator"):
            await coord._async_update_data()

        assert "403" in caplog.text

    async def test_current_token_is_kept(self) -> None:
        """Nothing about a block page invalidates the token we already hold."""
        client = _FakeClient(refresh_error=TransientAPIError("blocked"))
        coord = _coordinator(client)
        _record_ensure_mqtt(coord)

        await coord._async_update_data()

        assert client.token == "a-token"


class TestTerminalAuthFailuresStillSurface:
    """The reauth flow is the only way a user fixes an expired trust cookie."""

    @pytest.mark.parametrize(
        "error",
        [
            MfaRequired(factors=[]),
            AuthenticationError("Auth failed: bad credentials"),
            RateLimitedError("Arlo is rate-limiting requests."),
        ],
        ids=["mfa-required", "auth-failed", "rate-limited"],
    )
    async def test_raises_config_entry_auth_failed(self, error: BaseException) -> None:
        coord = _coordinator(_FakeClient(refresh_error=error))
        calls = _record_ensure_mqtt(coord)

        with pytest.raises(ConfigEntryAuthFailed):
            await coord._async_update_data()

        assert not calls, "a terminal auth verdict must stop the tick, not continue it"


class TestEnsureMqttIsIdempotent:
    async def test_no_second_stream_when_one_is_already_up(self) -> None:
        coord = _coordinator(_FakeClient())
        existing = object()
        coord._mqtt = existing

        assert await coord._ensure_mqtt() is True
        assert coord._mqtt is existing

    async def test_reports_failure_without_leaving_a_dead_stream_behind(self) -> None:
        """A half-built stream would make the next tick think it is connected."""
        coord = _coordinator(_FakeClient())

        class _Boom:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def on(self, *args: Any) -> None:
                pass

            def on_disconnect(self, *args: Any) -> None:
                pass

            async def connect(self) -> None:
                raise ConnectionError("broker said no")

        import custom_components.eisenberg.coordinator as module

        original = module.MQTTEventStream
        module.MQTTEventStream = _Boom  # type: ignore[assignment]
        try:
            assert await coord._ensure_mqtt() is False
        finally:
            module.MQTTEventStream = original
        assert coord._mqtt is None


class TestReconnectBackoff:
    """The 30-minute tick is a floor on the deaf window; a drop deserves better."""

    async def test_disconnect_starts_a_reconnect_attempt(self) -> None:
        coord = _coordinator(_FakeClient())
        stream = object()
        coord._mqtt = stream
        attempts = _record_ensure_mqtt(coord)

        # Collapse the backoff so the test doesn't sleep through it.
        import custom_components.eisenberg.coordinator as module

        original = module.MQTT_RECONNECT_BACKOFF
        module.MQTT_RECONNECT_BACKOFF = (0.0,)
        try:
            await coord._handle_mqtt_disconnect(stream)
            assert coord._mqtt is None
            assert coord._reconnect_task is not None
            await coord._reconnect_task
        finally:
            module.MQTT_RECONNECT_BACKOFF = original

        assert attempts, "a dropped socket must not wait for the next health tick"

    async def test_a_second_disconnect_does_not_stack_reconnect_loops(self) -> None:
        coord = _coordinator(_FakeClient())

        started = 0

        async def _slow() -> None:
            nonlocal started
            started += 1
            await asyncio.sleep(0.2)

        coord._reconnect_mqtt_with_backoff = _slow  # type: ignore[method-assign]

        await coord._handle_mqtt_disconnect(None)
        await coord._handle_mqtt_disconnect(None)
        await asyncio.sleep(0)  # let the scheduled loop reach its first await
        assert coord._reconnect_task is not None
        coord._reconnect_task.cancel()
        await asyncio.gather(coord._reconnect_task, return_exceptions=True)

        assert started == 1
        assert len(coord.hass.tasks) == 1

    async def test_shutdown_cancels_a_pending_reconnect(self) -> None:
        coord = _coordinator(_FakeClient())

        async def _forever() -> None:
            await asyncio.sleep(3600)

        coord._reconnect_mqtt_with_backoff = _forever  # type: ignore[method-assign]
        await coord._handle_mqtt_disconnect(None)
        task = coord._reconnect_task
        assert task is not None

        coord._http_session = None
        await coord.async_shutdown()

        assert task.cancelled() or task.done()

    async def test_a_superseded_stream_does_not_clear_the_live_one(self) -> None:
        """The callback fires from the tail of a loop that may already be dead.

        A stream that dropped, was replaced, and only then finishes unwinding
        must not null out its own successor — that would leave the coordinator
        believing MQTT is down while a perfectly good socket is delivering.
        """
        coord = _coordinator(_FakeClient())
        dead, live = object(), object()
        coord._mqtt = live

        await coord._handle_mqtt_disconnect(dead)

        assert coord._mqtt is live
        assert coord._reconnect_task is None
