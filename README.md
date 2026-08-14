[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/Meross-Tech/meross_rpc?include_prereleases&style=for-the-badge)](https://github.com/Meross-Tech/meross_rpc/releases)

# Meross

Official Home Assistant integration for [Meross](https://www.meross.com/) devices.

- Home Assistant **2025.2.5** or later
- Local control over **Wi-Fi (RPC)** and **Bluetooth**
- Domain: `meross_rpc` (internal). In Home Assistant and HACS, search for **Meross**

## Installation

Install with HACS (recommended) or copy the files manually. Restart Home Assistant after either method.

### Option A: HACS

This repository is not in the HACS default store yet. Add it as a custom repository:

1. Open **HACS → Integrations**
2. Menu (⋮) → **Custom repositories**
3. Repository: `https://github.com/Meross-Tech/meross_rpc`
4. Category: **Integration**
5. Search for **Meross**, install, then **restart Home Assistant**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Meross-Tech&repository=meross_rpc&category=integration)

See [HACS custom repositories](https://www.hacs.xyz/docs/faq/custom_repositories/).

### Option B: Manual

1. Download the latest release from [Releases](https://github.com/Meross-Tech/meross_rpc/releases/latest)
2. Copy the `meross_rpc` folder into `custom_components` in your Home Assistant config directory (the same folder as `configuration.yaml`)
3. Restart Home Assistant

```
configuration.yaml
secrets.yaml
custom_components/
  meross_rpc/
    __init__.py
    manifest.json
    ...
```

Create `custom_components` if it does not exist.

## Configuration

1. Go to **Settings → Devices & services → Add integration**
2. Search for **Meross**
3. Select your product model. The integration uses Wi-Fi or Bluetooth from that model

[![Start Config Flow](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=meross_rpc)

### Wi-Fi devices (EM06P / EM16P)

The device must already be on the same network as Home Assistant. Enter its hostname or IP address. If the device has a password, you will be asked for it.

### Bluetooth devices (MS120 / MS220 / MS420 / MS700)

Home Assistant needs a Bluetooth adapter or an [ESPHome Bluetooth proxy](https://www.home-assistant.io/integrations/bluetooth/). Keep the device nearby during setup, then pick it from the discovered list.

## Supported devices

| Model | Connection | Notes |
|-------|------------|--------|
| EM06P | Wi-Fi | Smart energy monitor |
| EM16P | Wi-Fi | Smart energy monitor |
| MS120 | Bluetooth | Temperature and humidity |
| MS220 | Bluetooth | Door / window, vibration, doorbell / button |
| MS420 | Bluetooth | Water leak / rain |
| MS700 | Bluetooth | Multi-button remote, temperature and humidity |

Firmware: all current versions of the models above.

## Removal

1. **Settings → Devices & services → Meross**
2. Open the device entry → **Delete**
3. Optional: uninstall the integration in HACS, or delete `custom_components/meross_rpc`, then restart Home Assistant

Entities and devices created by this integration are removed with the config entry.

## Support

- Issues: [github.com/Meross-Tech/meross_rpc/issues](https://github.com/Meross-Tech/meross_rpc/issues)
