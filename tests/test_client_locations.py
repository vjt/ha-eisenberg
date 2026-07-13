"""get_locations must include shared locations (issue #21).

Arlo returns devices shared from another account under `sharedLocations`, a
sibling of `userLocations` in the /locations response. The gatewayDeviceIds
that map a base station to its location live ONLY in that shared entry — the
user's own default location can be empty. Reading just `userLocations` strands
every shared device on a phantom, device-less location, so mode set/get lands
on the wrong location and never reaches the physical base (DirkWeber1972's
VMB4000, shared from owner P7EK4).
"""

from __future__ import annotations

from aioresponses import aioresponses

from tests.test_client_commands import MYAPI, make_authed_client

_LOCATIONS_URL = f"{MYAPI}/hmsdevicemanagement/users/USER-123/locations"


def _body() -> dict:
    # Mirrors Dirk's log: own location is empty; the shared one gateways the base.
    return {
        "success": True,
        "data": {
            "userLocations": [
                {
                    "locationId": "own-empty",
                    "locationName": "Home Assistant",
                    "gatewayDeviceIds": [],
                }
            ],
            "sharedLocations": [
                {
                    "locationId": "shared-real",
                    "locationName": "Owner House",
                    "gatewayDeviceIds": ["P7EK4-183-13483128_4RD17372A3A28"],
                }
            ],
        },
    }


class TestGetLocationsIncludesShared:
    async def test_shared_location_is_returned(self) -> None:
        with aioresponses() as m:
            m.get(_LOCATIONS_URL, payload=_body())
            async with make_authed_client() as client:
                locations = await client.get_locations()

        by_id = {loc.location_id: loc for loc in locations}
        # Both the user's own location AND the shared one are present.
        assert "own-empty" in by_id
        assert "shared-real" in by_id
        # The gateway mapping that drives device->location resolution survives.
        assert by_id["shared-real"].gateway_device_ids == ["P7EK4-183-13483128_4RD17372A3A28"]

    async def test_locations_are_deduped_by_id(self) -> None:
        # A location appearing in more than one bucket must not double up.
        body = _body()
        body["data"]["ownedLocations"] = body["data"]["userLocations"]
        with aioresponses() as m:
            m.get(_LOCATIONS_URL, payload=body)
            async with make_authed_client() as client:
                locations = await client.get_locations()

        ids = [loc.location_id for loc in locations]
        assert ids.count("own-empty") == 1
