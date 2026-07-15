# custom_components/eisenberg/config_flow.py
"""Config flow for Eisenberg."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from aiohttp import CookieJar
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
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
    FactorType,
    MfaRequired,
    RateLimitedError,
    SecondFactor,
)

from .const import (
    CONF_DETECTION_TIMEOUT,
    CONF_DEVICE_ID,
    CONF_FFMPEG_STREAM,
    CONF_MEDIA_DIR,
    CONF_MEDIA_RETENTION_DAYS,
    CONF_TRUST_COOKIE,
    DEFAULT_DETECTION_TIMEOUT,
    DEFAULT_FFMPEG_STREAM,
    DEFAULT_MEDIA_RETENTION_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MEDIA_DIR_DISABLED = "__disabled__"
CONF_FACTOR_ID = "factor_id"
CONF_OTP = "otp"


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


def _factor_label(factor: SecondFactor) -> str:
    """Human-friendly label for a factor picker option."""
    type_label = {
        FactorType.PUSH: "Push notification",
        FactorType.EMAIL: "Email",
        FactorType.SMS: "SMS",
    }.get(factor.factor_type, factor.factor_type.value)
    return f"{type_label}: {factor.display_name}"


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
        self._factors: list[SecondFactor] = []
        self._selected_factor: SecondFactor | None = None

    async def _cleanup_client(self) -> None:
        """Close client session if open."""
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    def _new_client(self) -> None:
        """Build a fresh client + cookie jar for an auth attempt."""
        self._cookie_jar = CookieJar(unsafe=True)
        self._client = EisenbergClient(
            email=self._username,
            password=self._password,
            device_id=self._device_id,
            cookie_jar=self._cookie_jar,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._device_id = f"eisenberg-{uuid.uuid4()}"

            self._new_client()
            assert self._client is not None

            try:
                await self._client.__aenter__()
                await self._client.login()
                await self._cleanup_client()
                return await self.async_step_media_storage()
            except MfaRequired as err:
                self._factors = err.factors
                # Skip the picker if there's only one option.
                if len(self._factors) == 1:
                    self._selected_factor = self._factors[0]
                    return await self._fire_mfa()
                return await self.async_step_pick_factor()
            except RateLimitedError:
                errors["base"] = "rate_limited"
                await self._cleanup_client()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
                await self._cleanup_client()
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "cannot_connect"
                await self._cleanup_client()

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

    async def async_step_pick_factor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: pick which MFA factor to use (push / email / SMS)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            picked_id = user_input[CONF_FACTOR_ID]
            for factor in self._factors:
                if factor.factor_id == picked_id:
                    self._selected_factor = factor
                    break
            if self._selected_factor is None:
                errors["base"] = "cannot_connect"
            else:
                return await self._fire_mfa()

        options = {f.factor_id: _factor_label(f) for f in self._factors}
        default = next(
            (f.factor_id for f in self._factors if f.factor_role == "PRIMARY"),
            self._factors[0].factor_id if self._factors else "",
        )
        return self.async_show_form(
            step_id="pick_factor",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FACTOR_ID, default=default): vol.In(options),
                }
            ),
            errors=errors,
        )

    async def _fire_mfa(self) -> ConfigFlowResult:
        """Send the chosen MFA factor and route to the right wait step."""
        assert self._client is not None
        assert self._selected_factor is not None
        try:
            self._factor_auth_code = await self._client.start_mfa(self._selected_factor)
        except RateLimitedError:
            await self._cleanup_client()
            return self.async_abort(reason="rate_limited")
        except AuthenticationError:
            await self._cleanup_client()
            return self.async_abort(reason="push_timeout")

        if self._selected_factor.factor_type == FactorType.PUSH:
            return await self.async_step_push_approval()
        return await self.async_step_otp_entry()

    async def async_step_push_approval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3a (PUSH): user approves push on phone, then submits form.

        One submit = one finishAuth call. No polling, no background task.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            if self._client is None:
                errors["base"] = "cannot_connect"
            else:
                try:
                    approved = await self._client.try_finish_auth(self._factor_auth_code)
                except RateLimitedError:
                    await self._cleanup_client()
                    return self.async_abort(reason="rate_limited")
                except AuthenticationError as err:
                    _LOGGER.warning("finishAuth failed: %s", err)
                    await self._cleanup_client()
                    return self.async_abort(reason="push_timeout")

                if approved:
                    await self._cleanup_client()
                    return await self.async_step_media_storage()
                errors["base"] = "push_pending"

        return self.async_show_form(
            step_id="push_approval",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_otp_entry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3b (EMAIL/SMS): user types in the code they received."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if self._client is None:
                errors["base"] = "cannot_connect"
            else:
                otp = user_input[CONF_OTP].strip()
                try:
                    approved = await self._client.try_finish_auth(self._factor_auth_code, otp=otp)
                except RateLimitedError:
                    await self._cleanup_client()
                    return self.async_abort(reason="rate_limited")
                except AuthenticationError:
                    errors["base"] = "invalid_otp"
                else:
                    if approved:
                        await self._cleanup_client()
                        return await self.async_step_media_storage()
                    errors["base"] = "invalid_otp"

        assert self._selected_factor is not None
        return self.async_show_form(
            step_id="otp_entry",
            data_schema=vol.Schema({vol.Required(CONF_OTP): str}),
            description_placeholders={"destination": self._selected_factor.display_name},
            errors=errors,
        )

    async def async_step_media_storage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: Select media storage location."""
        if user_input is not None:
            media_dir = user_input.get(CONF_MEDIA_DIR, MEDIA_DIR_DISABLED)

            await self.async_set_unique_id(self._username)
            self._abort_if_unique_id_configured()

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
                    CONF_MEDIA_RETENTION_DAYS: DEFAULT_MEDIA_RETENTION_DAYS,
                },
            )

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

    # --- Reauth / Reconfigure ---

    async def async_step_reauth(self, entry_data: dict[str, str]) -> ConfigFlowResult:
        """Handle reauth triggered by ConfigEntryAuthFailed.

        Stored credentials are reused — user only needs to re-approve the
        MFA challenge. Password form is shown only if the stored password
        is rejected by Arlo.
        """
        self._username = entry_data[CONF_USERNAME]
        self._password = entry_data[CONF_PASSWORD]
        self._device_id = entry_data.get(CONF_DEVICE_ID, f"eisenberg-{uuid.uuid4()}")
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User-triggered reconfigure from the integration card.

        Reuses the reauth flow's MFA picker so the user can pick a fresh
        factor (or change device) without waiting for auth failure.
        """
        entry = self._get_reconfigure_entry()
        self._username = entry.data[CONF_USERNAME]
        self._password = entry.data[CONF_PASSWORD]
        self._device_id = entry.data.get(CONF_DEVICE_ID, f"eisenberg-{uuid.uuid4()}")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm: trigger reauth using stored credentials? No fields."""
        if user_input is not None:
            self._new_client()
            assert self._client is not None

            try:
                await self._client.__aenter__()
                await self._client.login()
            except MfaRequired as err:
                self._factors = err.factors
                if len(self._factors) == 1:
                    self._selected_factor = self._factors[0]
                    return await self._fire_mfa_reauth()
                return await self.async_step_reauth_pick_factor()
            except RateLimitedError:
                await self._cleanup_client()
                return self.async_abort(reason="rate_limited")
            except AuthenticationError:
                # Stored password rejected — ask user for new one
                await self._cleanup_client()
                return await self.async_step_reauth_password()
            else:
                # Login succeeded silently (cookie still valid somehow)
                await self._cleanup_client()
                return self._finalize_reauth()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"username": self._username},
        )

    async def async_step_reauth_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stored password rejected — ask user to re-enter it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            self._new_client()
            assert self._client is not None

            try:
                await self._client.__aenter__()
                await self._client.login()
            except MfaRequired as err:
                self._factors = err.factors
                if len(self._factors) == 1:
                    self._selected_factor = self._factors[0]
                    return await self._fire_mfa_reauth()
                return await self.async_step_reauth_pick_factor()
            except RateLimitedError:
                await self._cleanup_client()
                return self.async_abort(reason="rate_limited")
            except AuthenticationError:
                errors["base"] = "invalid_auth"
                await self._cleanup_client()
            else:
                await self._cleanup_client()
                return self._finalize_reauth()

        return self.async_show_form(
            step_id="reauth_password",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": self._username},
            errors=errors,
        )

    async def async_step_reauth_pick_factor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth: pick MFA factor."""
        errors: dict[str, str] = {}

        if user_input is not None:
            picked_id = user_input[CONF_FACTOR_ID]
            for factor in self._factors:
                if factor.factor_id == picked_id:
                    self._selected_factor = factor
                    break
            if self._selected_factor is None:
                errors["base"] = "cannot_connect"
            else:
                return await self._fire_mfa_reauth()

        options = {f.factor_id: _factor_label(f) for f in self._factors}
        default = next(
            (f.factor_id for f in self._factors if f.factor_role == "PRIMARY"),
            self._factors[0].factor_id if self._factors else "",
        )
        return self.async_show_form(
            step_id="reauth_pick_factor",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FACTOR_ID, default=default): vol.In(options),
                }
            ),
            errors=errors,
        )

    async def _fire_mfa_reauth(self) -> ConfigFlowResult:
        """Send the chosen MFA factor for reauth, then route to push/otp."""
        assert self._client is not None
        assert self._selected_factor is not None
        try:
            self._factor_auth_code = await self._client.start_mfa(self._selected_factor)
        except RateLimitedError:
            await self._cleanup_client()
            return self.async_abort(reason="rate_limited")
        except AuthenticationError:
            await self._cleanup_client()
            return self.async_abort(reason="push_timeout")

        if self._selected_factor.factor_type == FactorType.PUSH:
            return await self.async_step_reauth_push()
        return await self.async_step_reauth_otp()

    async def async_step_reauth_push(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth (PUSH): user approves push on phone, then submits form."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if self._client is None:
                errors["base"] = "cannot_connect"
            else:
                try:
                    approved = await self._client.try_finish_auth(self._factor_auth_code)
                except RateLimitedError:
                    await self._cleanup_client()
                    return self.async_abort(reason="rate_limited")
                except AuthenticationError as err:
                    _LOGGER.warning("Reauth finishAuth failed: %s", err)
                    await self._cleanup_client()
                    return self.async_abort(reason="push_timeout")

                if approved:
                    await self._cleanup_client()
                    return self._finalize_reauth()
                errors["base"] = "push_pending"

        return self.async_show_form(
            step_id="reauth_push",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_reauth_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reauth (EMAIL/SMS): user types in the code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if self._client is None:
                errors["base"] = "cannot_connect"
            else:
                otp = user_input[CONF_OTP].strip()
                try:
                    approved = await self._client.try_finish_auth(self._factor_auth_code, otp=otp)
                except RateLimitedError:
                    await self._cleanup_client()
                    return self.async_abort(reason="rate_limited")
                except AuthenticationError:
                    errors["base"] = "invalid_otp"
                else:
                    if approved:
                        await self._cleanup_client()
                        return self._finalize_reauth()
                    errors["base"] = "invalid_otp"

        assert self._selected_factor is not None
        return self.async_show_form(
            step_id="reauth_otp",
            data_schema=vol.Schema({vol.Required(CONF_OTP): str}),
            description_placeholders={"destination": self._selected_factor.display_name},
            errors=errors,
        )

    def _finalize_reauth(self) -> ConfigFlowResult:
        """Persist new cookies + creds back to the entry and reload.

        Shared between reauth and reconfigure flows — the source picks
        which getter HA expects, otherwise the helper raises.
        """
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
        else:
            entry = self._get_reauth_entry()
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
                        default=opts.get(
                            CONF_DETECTION_TIMEOUT,
                            DEFAULT_DETECTION_TIMEOUT,
                        ),
                    ): vol.All(int, vol.Range(min=5, max=300)),
                    vol.Required(
                        CONF_MEDIA_RETENTION_DAYS,
                        default=opts.get(
                            CONF_MEDIA_RETENTION_DAYS,
                            DEFAULT_MEDIA_RETENTION_DAYS,
                        ),
                    ): vol.All(int, vol.Range(min=1, max=365)),
                    vol.Required(
                        CONF_FFMPEG_STREAM,
                        default=opts.get(CONF_FFMPEG_STREAM, DEFAULT_FFMPEG_STREAM),
                    ): bool,
                }
            ),
        )
