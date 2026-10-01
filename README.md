# Raspberry Pi Network Monitor

A lightweight, local-first internet reliability monitor for Raspberry Pi 3 and
Raspberry Pi 4. It records connectivity evidence, distinguishes gateway, DNS,
internet, and partial-connectivity failures, and serves a responsive dashboard
without Docker or cloud services.

## Status and features

V1 is running on a Raspberry Pi 4 and includes:

- Independent gateway, external IP, DNS, and HTTPS probes.
- Confirmed incidents with failure and recovery thresholds.
- Monitoring-gap and clock-trust detection.
- Availability timeline, windowed MTBF, and ping-latency chart.
- Detailed filtered history and CSV export.
- Runtime settings, retention, SQLite backups, and health checks.
- Native `systemd` services with a dedicated unprivileged user.

Raspberry Pi 3 remains a compatibility target but has not been tested.

## Installation

Use Raspberry Pi OS with Python 3.9 or newer. Install the required packages:

```sh
sudo apt update
sudo apt install -y git python3 python3-venv iproute2 iputils-ping
python3 --version
```

Clone the repository and create a deployment-specific configuration:

```sh
git clone https://github.com/smyrnakis/raspi-network-monitor.git
cd raspi-network-monitor
cp config.example.toml config.local.toml
vim config.local.toml
```

Each Pi must start with its own configuration and database. Do not copy
`/etc/raspi-network-monitor/config.toml` or the live database from another Pi.
Keep `config.local.toml` and deployment-specific addresses outside Git.

Review these settings before installation:

| Setting | Recommended starting value | Notes |
| --- | --- | --- |
| `site.id` | Unique lowercase identifier | Use letters, digits, `_`, or `-`, for example `home_pi4_02` |
| `site.display_name` | Friendly installation label | Stored with the site's monitoring data |
| `site.timezone` | Local IANA timezone | For example `Europe/Bucharest` |
| `web.host` | `0.0.0.0` | Allows LAN and VPN access; use `127.0.0.1` for access from the Pi only |
| `web.port` | `8080` | Do not forward this port from the router |
| `monitor.interval_seconds` | `10` or `30` | `10` confirms incidents faster; `30` reduces checks and database writes |
| `monitor.round_timeout_seconds` | `8` | Must be below the check interval and above every probe timeout |
| `route.required_interface` | Usually unset | Set to `eth0` or `wlan0` only when monitoring must use that physical interface |
| `route.forbidden_interface_prefixes` | `["tun", "tap", "wg"]` | Prevents a VPN route from being mistaken for native internet health |
| `retention.raw_samples_days` | `7` | Keeps recent probe-level details |
| `retention.latency_aggregates_days` | `548` | Keeps compact long-term latency history |

The failure threshold is three rounds and recovery threshold is two rounds by
default. At a 10-second interval, confirmation normally takes about 30 seconds;
at 30 seconds, about 90 seconds. Probe and timeout delays can add to this.

Keep the automatic gateway probe, two independently operated external-IP
targets, one stable DNS target, and one lightweight HTTPS endpoint. Avoid
private, personal, or dynamic-DNS targets in configurations intended for Git.

Install the application and enable both services:

```sh
sudo sh deploy/install.sh "$PWD" "$PWD/config.local.toml"
sudo systemctl enable --now raspi-network-monitor.service
sudo systemctl enable --now raspi-network-monitor-api.service
```

Open `http://<raspberry-pi-hostname>.local:8080/` from the same LAN, or use the
Pi's private IP address. The dashboard has no application login and is intended
only for a trusted LAN or an existing VPN.

If UFW is active, restrict access to the relevant network:

```sh
sudo ufw allow in on eth0 from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw allow in on tun0 from 10.8.0.0/24 to any port 8080 proto tcp
sudo ufw status
```

Replace interface names and subnets with the installation's actual values.

### Configuration after installation

The Settings page can change intervals, thresholds, probes, retention, and the
short-incident dashboard filter. A manual test is diagnostic and does not alter
monitoring history.

For site, web, storage, or route changes, edit the root-owned configuration and
restart the services:

```sh
sudo vim /etc/raspi-network-monitor/config.toml
sudo systemctl restart raspi-network-monitor.service raspi-network-monitor-api.service
```

### Updating

From the cloned repository:

```sh
git pull --ff-only
sudo sh deploy/install.sh "$PWD" "$PWD/config.local.toml"
sudo systemctl restart raspi-network-monitor.service raspi-network-monitor-api.service
```

The installer preserves `/etc/raspi-network-monitor/config.toml` and the
database. Review release changes before updating configuration manually.

## Verification and routine checks

Verify a new installation, or check service and API health:

```sh
systemctl status raspi-network-monitor.service raspi-network-monitor-api.service --no-pager
curl -s http://127.0.0.1:8080/api/v1/health
sudo /opt/raspi-network-monitor/venv/bin/raspi-network-monitor-ops check
```

The API must report `healthy`; the database check must report `quick_check` as
`ok`. If a service failed, inspect recent logs:

```sh
sudo journalctl -u raspi-network-monitor.service \
  -u raspi-network-monitor-api.service -n 100 --no-pager
```

With the default retention, raw probe counts should broadly stabilize after
seven to eight days. SQLite may retain freed pages for reuse, so the file does
not necessarily shrink after cleanup.

Clock synchronization and recorded clock gaps:

```sh
timedatectl show -p NTPSynchronized -p TimeUSec -p Timezone
timedatectl timesync-status
curl -s 'http://127.0.0.1:8080/api/v1/history?event_type=gap&category=clock_uncertain&limit=100'
```

Create a verified online backup:

```sh
sudo install -d -m 0750 /var/backups/raspi-network-monitor
sudo /opt/raspi-network-monitor/venv/bin/raspi-network-monitor-ops backup \
  /var/backups/raspi-network-monitor/monitor-YYYY-MM-DD.db
```

Never copy or replace the live database directly. For a restore, first verify
the selected backup with `raspi-network-monitor-ops check`, stop both services,
replace the database with owner and group `raspi-monitor`, remove its `-wal` and
`-shm` sidecars, run the check again, and start both services.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Repository layout:

```text
src/home_internet_monitor/  Application package and local web assets
tests/                      Automated test suite
deploy/                     Installer and systemd units
docs/                       Architecture and monitoring semantics
```

Further details:

- [V1 architecture](docs/architecture.md)
- [Monitoring semantics](docs/monitoring-semantics.md)

## Scope

Each installation is independent and intended for LAN or existing-VPN access.
V1 has no application login, public-internet exposure, central dashboard, or
device traffic capture.

V2 priorities are email notifications, device-level traffic attribution,
separate VPN-health probes, and low-frequency speed tests. Future UI work may
add incident notes.

Never commit credentials, private keys, personal email addresses, public IP
addresses, or dynamic-DNS URLs.

## License

Licensed under the [MIT License](LICENSE).
