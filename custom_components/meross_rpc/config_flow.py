"""Config flow for Meross (Wi-Fi RPC and Bluetooth)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiorefoss.common import (
    ConnectionOptions,
    fmt_macaddress,
    get_info,
    get_info_auth,
    mac_address_from_name,
)
from aiorefoss.exceptions import (
    DeviceConnectionError,
    InvalidAuthError,
    MacAddressMismatchError,
)
from aiorefoss.rpc_device import RpcDevice
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_HOST,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .ble.const import (
    CONF_MODEL,
    CONF_RETRY_COUNT,
    DEFAULT_RETRY_COUNT,
    MODEL_FRIENDLY_NAME,
    MerossModel,
)
from .ble.parser import MerossAdvertisement, parse_advertisement_data
from .const import (
    CONF_CONNECTION,
    CONNECTION_BLUETOOTH,
    CONNECTION_WIFI,
    DOMAIN,
    LOGGER,
    is_bluetooth_connection,
)
from .coordinator import async_reconnect_soon

INTERNAL_WIFI_AP_IP = "10.10.10.1"
MANUAL_SCAN_DURATION = 15


def _format_ble_unique_id(address: str) -> str:
    return address.replace(":", "").replace("-", "").lower()


def _short_address(address: str) -> str:
    parts = address.replace("-", ":").split(":")
    return f"{parts[-2].upper()}{parts[-1].upper()}"[-4:]


def _name_from_discovery(discovery: MerossAdvertisement) -> str:
    """Config entry / confirm title (no MAC suffix)."""
    return discovery.friendly_name


def _label_from_discovery(discovery: MerossAdvertisement) -> str:
    """Picker label; include full address when choosing among several devices."""
    return f"{discovery.friendly_name} ({discovery.address})"


def _collect_discovered_service_info(
    hass: HomeAssistant,
) -> list[BluetoothServiceInfoBleak]:
    seen: set[str] = set()
    results: list[BluetoothServiceInfoBleak] = []
    for connectable in (True, False):
        for info in async_discovered_service_info(hass, connectable):
            if info.address in seen:
                continue
            seen.add(info.address)
            results.append(info)
    return results


async def async_validate_input(
    hass: HomeAssistant,
    host: str,
    info: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    options = ConnectionOptions(
        ip_address=host,
        username=data.get(CONF_USERNAME),
        password=data.get(CONF_PASSWORD),
        device_mac=info[CONF_MAC],
    )

    device = await RpcDevice.create(
        async_get_clientsession(hass),
        options,
    )
    try:
        await device.initialize()
    finally:
        await device.shutdown()

    return {
        "name": device.name,
        "model": device.model,
    }


class RefossConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross (Wi-Fi / Bluetooth)."""

    VERSION = 1
    MINOR_VERSION = 1

    host: str = ""
    info: dict[str, Any] = {}
    device_info: dict[str, Any] = {}

    def __init__(self) -> None:
        """Initialize flow state used by Bluetooth setup."""
        self._discovered: MerossAdvertisement | None = None
        self._discovered_devices: dict[str, MerossAdvertisement] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask Wi-Fi vs Bluetooth, then continue to the matching path."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["wifi", "bluetooth_setup"],
        )

    async def async_step_wifi(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle Wi-Fi / local network setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                self.info = await self._async_get_info(host)
            except DeviceConnectionError:
                errors["base"] = "cannot_connect"
            else:
                mac = fmt_macaddress(self.info[CONF_MAC])
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured({CONF_HOST: host})
                self.host = host
                if get_info_auth(self.info):
                    return await self.async_step_credentials()

                try:
                    device_info = await async_validate_input(
                        self.hass, host, self.info, {}
                    )
                except DeviceConnectionError:
                    errors["base"] = "cannot_connect"
                except MacAddressMismatchError:
                    errors["base"] = "mac_address_mismatch"
                else:
                    if device_info["model"]:
                        return self.async_create_entry(
                            title=device_info["name"],
                            data={
                                CONF_CONNECTION: CONNECTION_WIFI,
                                CONF_MAC: self.info[CONF_MAC],
                                CONF_HOST: self.host,
                                "model": device_info["model"],
                            },
                        )
                    errors["base"] = "firmware_not_fully_supported"

        return self.async_show_form(
            step_id="wifi",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the credentials step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_USERNAME] = "admin"
            try:
                device_info = await async_validate_input(
                    self.hass, self.host, self.info, user_input
                )
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except DeviceConnectionError:
                errors["base"] = "cannot_connect"
            except MacAddressMismatchError:
                errors["base"] = "mac_address_mismatch"
            else:
                if device_info["model"]:
                    return self.async_create_entry(
                        title=device_info["name"],
                        data={
                            **user_input,
                            CONF_CONNECTION: CONNECTION_WIFI,
                            CONF_MAC: self.info[CONF_MAC],
                            CONF_HOST: self.host,
                            "model": device_info["model"],
                        },
                    )
                errors["base"] = "firmware_not_fully_supported"
        else:
            user_input = {}

        schema = {
            vol.Required(CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")): str,
        }
        return self.async_show_form(
            step_id="credentials", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle configuration by re-auth."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""

        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        host = reauth_entry.data[CONF_HOST]

        if user_input is not None:
            try:
                info = await self._async_get_info(host)
            except (DeviceConnectionError, InvalidAuthError):
                return self.async_abort(reason="reauth_unsuccessful")

            user_input[CONF_USERNAME] = "admin"
            try:
                await async_validate_input(self.hass, host, info, user_input)
            except (DeviceConnectionError, InvalidAuthError):
                return self.async_abort(reason="reauth_unsuccessful")
            except MacAddressMismatchError:
                return self.async_abort(reason="mac_address_mismatch")

            return self.async_update_reload_and_abort(
                reauth_entry, data_updates=user_input
            )

        schema = {
            vol.Required(CONF_PASSWORD): str,
        }

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a flow initialized by zeroconf discovery."""
        host = discovery_info.host
        if mac := mac_address_from_name(discovery_info.name):
            await self._async_discovered_mac(mac, host)
        try:
            self.info = await self._async_get_info(host)
        except DeviceConnectionError:
            return self.async_abort(reason="cannot_connect")
        if not mac:
            mac = fmt_macaddress(self.info[CONF_MAC])
            await self._async_discovered_mac(mac, host)

        self.host = host
        self.context.update(
            {
                "title_placeholders": {"name": self.info["name"]},
                "configuration_url": f"http://{host}",
            }
        )

        if get_info_auth(self.info):
            return await self.async_step_credentials()
        try:
            self.device_info = await async_validate_input(
                self.hass, self.host, self.info, {}
            )
        except DeviceConnectionError:
            return self.async_abort(reason="cannot_connect")

        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle discovery confirm."""
        errors: dict[str, str] = {}

        if not self.device_info["model"]:
            errors["base"] = "firmware_not_fully_supported"
            model = "Refoss"
        else:
            model = self.device_info["model"]
            if user_input is not None:
                return self.async_create_entry(
                    title=self.device_info["name"],
                    data={
                        CONF_CONNECTION: CONNECTION_WIFI,
                        CONF_MAC: self.info[CONF_MAC],
                        CONF_HOST: self.host,
                        "model": model,
                    },
                )
            self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm_discovery",
            description_placeholders={
                "model": model,
                "host": self.host,
            },
            errors=errors,
        )

    async def _async_discovered_mac(self, mac: str, host: str) -> None:
        """Abort and reconnect soon if the device with the mac address is already configured."""
        if (
            current_entry := await self.async_set_unique_id(mac)
        ) and current_entry.data.get(CONF_HOST) == host:
            LOGGER.debug("async_reconnect_soon: host: %s, mac: %s", host, mac)
            await async_reconnect_soon(self.hass, current_entry)
        if host == INTERNAL_WIFI_AP_IP:
            self._abort_if_unique_id_configured()
        else:
            self._abort_if_unique_id_configured({CONF_HOST: host})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a reconfiguration flow initialized by the user."""
        errors = {}
        reconfigure_entry = self._get_reconfigure_entry()
        self.host = reconfigure_entry.data[CONF_HOST]
        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                info = await self._async_get_info(host)
            except DeviceConnectionError:
                errors["base"] = "cannot_connect"
            else:
                mac = fmt_macaddress(info[CONF_MAC])
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_mismatch(reason="another_device")

                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data_updates={CONF_HOST: host},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_HOST, default=self.host): str}),
            description_placeholders={"device_name": reconfigure_entry.title},
            errors=errors,
        )

    async def _async_get_info(self, host: str) -> dict[str, Any]:
        """Get info from refoss device."""
        return await get_info(async_get_clientsession(self.hass), host)

    # --- Bluetooth path ---

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """HA bluetooth matched manifest rules."""
        await self.async_set_unique_id(_format_ble_unique_id(discovery_info.address))
        self._abort_if_unique_id_configured()

        parsed = parse_advertisement_data(
            discovery_info.device, discovery_info.advertisement
        )
        if not parsed:
            return self.async_abort(reason="not_supported")

        self._discovered = parsed
        self.context["title_placeholders"] = {
            "name": parsed.friendly_name,
            "address": _short_address(discovery_info.address),
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual Bluetooth add after choosing Bluetooth in the menu."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery = self._discovered_devices[address]
            await self.async_set_unique_id(
                _format_ble_unique_id(address), raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            return self._async_create_ble_entry(discovery)

        await bluetooth.async_request_active_scan(self.hass, MANUAL_SCAN_DURATION)

        if bluetooth.async_scanner_count(self.hass, connectable=False) == 0:
            return self.async_abort(reason="no_bluetooth_adapter")

        current = self._async_current_ids(include_ignore=False)
        for info in _collect_discovered_service_info(self.hass):
            address = info.address
            uid = _format_ble_unique_id(address)
            if uid in current or address in self._discovered_devices:
                continue
            parsed = parse_advertisement_data(info.device, info.advertisement)
            if parsed:
                self._discovered_devices[address] = parsed

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        if len(self._discovered_devices) == 1:
            discovery = next(iter(self._discovered_devices.values()))
            await self.async_set_unique_id(
                _format_ble_unique_id(discovery.address), raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            self._discovered = discovery
            return await self.async_step_bluetooth_confirm()

        return self.async_show_form(
            step_id="bluetooth_setup",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: _label_from_discovery(parsed)
                            for address, parsed in self._discovered_devices.items()
                        }
                    ),
                }
            ),
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm Bluetooth setup."""
        assert self._discovered is not None
        if user_input is not None:
            return self._async_create_ble_entry(self._discovered)

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": _name_from_discovery(self._discovered),
            },
        )

    def _async_create_ble_entry(
        self, discovery: MerossAdvertisement
    ) -> ConfigFlowResult:
        return self.async_create_entry(
            title=_name_from_discovery(discovery),
            data={
                CONF_CONNECTION: CONNECTION_BLUETOOTH,
                CONF_ADDRESS: discovery.address,
                CONF_MODEL: discovery.model.value,
            },
            options={CONF_RETRY_COUNT: DEFAULT_RETRY_COUNT},
        )

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Only Bluetooth entries expose an options flow."""
        return is_bluetooth_connection(config_entry.data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return options flow for BLE entries."""
        return MerossBluetoothOptionsFlow()


class MerossBluetoothOptionsFlow(OptionsFlow):
    """Options: GATT retry count for Bluetooth devices."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        model = self.config_entry.data.get(CONF_MODEL, MerossModel.MS120)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_RETRY_COUNT,
                        default=self.config_entry.options.get(
                            CONF_RETRY_COUNT, DEFAULT_RETRY_COUNT
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                }
            ),
            description_placeholders={
                "model": MODEL_FRIENDLY_NAME.get(MerossModel(model), str(model)),
            },
        )
