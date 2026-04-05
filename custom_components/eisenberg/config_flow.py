# custom_components/eisenberg/config_flow.py
"""Config flow for Eisenberg."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from aiohttp import CookieJar
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from eisenberg import (
    AuthenticationError,
    EisenbergClient,
    PushApprovalRequired,
)

from .const import (
    CONF_DETECTION_TIMEOUT,
    CONF_DEVICE_ID,
    CONF_MEDIA_DIR,
    CONF_TRUST_COOKIE,
    DEFAULT_DETECTION_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MEDIA_DIR_DISABLED = "__disabled__"


def _serialize_cookies(cookie_jar: CookieJar) -> list[dict[str, str]]:
    """Extract cookies from aiohttp CookieJar for persistence.

    Values are stored as-is (URL-encoded) — do NOT decode, because
    http.cookies will quote raw '=' characters, breaking Arlo's server.
    """
    cookies: list[dict[str, str]] = []
    for morsel in cookie_jar:
        cookies.append(
            {
                "name": morsel.key,
                "value": morsel.value,
                "domain": morsel["domain"],
                "path": morsel["path"],
            }
        )
    return cookies


class EisenbergConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Eisenberg."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: EisenbergClient | None = None
        self._cookie_jar: CookieJar | None = None
        self._device_id: str = ""
        self._username: str = ""
        self._password: str = ""
        self._factor_auth_code: str = ""
        self._token: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._device_id = f"eisenberg-{uuid.uuid4()}"

            self._cookie_jar = CookieJar(unsafe=True)
            self._client = EisenbergClient(
                email=self._username,
                password=self._password,
                device_id=self._device_id,
                cookie_jar=self._cookie_jar,
            )

            try:
                async with self._client:
                    await self._client.login()
                    self._token = self._client.token
                # Trusted browser — skip push
                return await self.async_step_media_storage()
            except PushApprovalRequired as err:
                self._factor_auth_code = err.factor_auth_code
                # DON'T close the client — we need the session for push approval
                return await self.async_step_push_approval()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
                self._client = None
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "cannot_connect"
                self._client = None

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_push_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Wait for push approval."""
        errors: dict[str, str] = {}

        if user_input is not None and self._client is not None:
            try:
                # Reuse the same client+session from step_user — cookie jar preserved
                async with self._client:
                    await self._client.complete_push_approval(
                        factor_auth_code=self._factor_auth_code,
                        timeout=120,
                    )
                    self._token = self._client.token
                return await self.async_step_media_storage()
            except AuthenticationError:
                errors["base"] = "push_timeout"

        return self.async_show_form(
            step_id="push_approval",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_media_storage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Select media storage location."""
        if user_input is not None:
            media_dir = user_input.get(CONF_MEDIA_DIR, MEDIA_DIR_DISABLED)

            await self.async_set_unique_id(self._username)
            self._abort_if_unique_id_configured()

            # Serialize cookies for persistence so coordinator can restore trust
            cookies: list[dict[str, str]] = []
            if self._cookie_jar is not None:
                cookies = _serialize_cookies(self._cookie_jar)

            return self.async_create_entry(
                title=self._username,
                data={
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_DEVICE_ID: self._device_id,
                    CONF_TRUST_COOKIE: cookies,
                },
                options={
                    CONF_MEDIA_DIR: media_dir if media_dir != MEDIA_DIR_DISABLED else "",
                    CONF_DETECTION_TIMEOUT: DEFAULT_DETECTION_TIMEOUT,
                },
            )

        # Build media dir options from HA config
        media_dirs = self.hass.config.media_dirs
        options = {MEDIA_DIR_DISABLED: "Disabled"}
        for name, path in media_dirs.items():
            options[name] = f"{name} ({path})"

        return self.async_show_form(
            step_id="media_storage",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MEDIA_DIR, default=MEDIA_DIR_DISABLED): vol.In(options),
                }
            ),
        )

    # --- Reauth ---

    async def async_step_reauth(self, entry_data: dict[str, str]) -> ConfigFlowResult:
        """Handle reauth triggered by ConfigEntryAuthFailed."""
        self._username = entry_data[CONF_USERNAME]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm new credentials for reauth."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._device_id = entry.data.get(CONF_DEVICE_ID, f"eisenberg-{uuid.uuid4()}")

            self._cookie_jar = CookieJar(unsafe=True)
            self._client = EisenbergClient(
                email=self._username,
                password=self._password,
                device_id=self._device_id,
                cookie_jar=self._cookie_jar,
            )

            try:
                async with self._client:
                    await self._client.login()

                cookies = _serialize_cookies(self._cookie_jar)
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_DEVICE_ID: self._device_id,
                        CONF_TRUST_COOKIE: cookies,
                    },
                )
            except PushApprovalRequired as err:
                self._factor_auth_code = err.factor_auth_code
                return await self.async_step_reauth_push()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
                self._client = None

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=self._username): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth_push(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth: wait for push approval."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None and self._client is not None:
            try:
                async with self._client:
                    await self._client.complete_push_approval(
                        factor_auth_code=self._factor_auth_code,
                    )

                cookies: list[dict[str, str]] = []
                if self._cookie_jar is not None:
                    cookies = _serialize_cookies(self._cookie_jar)
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_DEVICE_ID: self._device_id,
                        CONF_TRUST_COOKIE: cookies,
                    },
                )
            except AuthenticationError:
                errors["base"] = "push_timeout"

        return self.async_show_form(
            step_id="reauth_push",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        return EisenbergOptionsFlow()


class EisenbergOptionsFlow(OptionsFlow):
    """Options flow for Eisenberg."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        media_dirs = self.hass.config.media_dirs
        options = {"": "Disabled"}
        for name, path in media_dirs.items():
            options[name] = f"{name} ({path})"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MEDIA_DIR,
                        default=opts.get(CONF_MEDIA_DIR, ""),
                    ): vol.In(options),
                    vol.Required(
                        CONF_DETECTION_TIMEOUT,
                        default=opts.get(CONF_DETECTION_TIMEOUT, DEFAULT_DETECTION_TIMEOUT),
                    ): vol.All(int, vol.Range(min=5, max=300)),
                }
            ),
        )
