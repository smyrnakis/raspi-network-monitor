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
- Availability timeline and ping-latency chart.
- Detailed filtered history and CSV export.
- Runtime settings, retention, SQLite backups, and health checks.
- Native `systemd` services with a dedicated unprivileged user.

Raspberry Pi 3 remains a compatibility target but has not been tested.

## Installation

Raspberry Pi OS needs Python 3.9 or newer, `python3-venv`, `ping`, `ip`, and
`systemd`.

```sh
git clone https://github.com/smyrnakis/raspi-network-monitor.git
cd raspi-network-monitor
cp config.example.toml config.local.toml
```

Edit `config.local.toml`, especially the site ID, timezone, probe targets, and
web binding. Keep deployment-specific addresses and credentials out of Git.

```sh
sudo sh deploy/install.sh "$PWD" "$PWD/config.local.toml"
sudo systemctl enable --now raspi-network-monitor.service
sudo systemctl enable --now raspi-network-monitor-api.service
```

Open `http://<raspberry-pi-hostname>.local:8080/` from the same LAN. The example
binds to local IPv4 interfaces. Never forward port 8080 from the router.

If UFW is active, restrict access to the relevant network:

```sh
sudo ufw allow in on eth0 from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw allow in on tun0 from 10.8.0.0/24 to any port 8080 proto tcp
sudo ufw status
```

Replace interface names and subnets with the installation's actual values.

For an update, pull or unpack the new source, run `deploy/install.sh` again,
then restart both services. The installer preserves the existing configuration
and database.

## Recommended settings

The public example provides suitable starting values:

| Setting | Default | Purpose |
| --- | ---: | --- |
| Check interval | 10 seconds | Responsive detection without excessive load |
| Round timeout | 8 seconds | Prevents overlapping rounds |
| Failure threshold | 3 rounds | Filters brief probe failures |
| Recovery threshold | 2 rounds | Confirms stable recovery |
| Raw probe retention | 7 days | Recent detailed diagnosis |
| Latency summaries | 548 days | Compact long-term trends |
| Incident retention | Indefinite | Preserves the important events |

Use two independent external IP targets. Choose stable DNS and HTTPS targets
that do not contain private or dynamic-DNS names. Keep the default VPN-interface
deny list so VPN routing cannot be mistaken for native internet health.

The Settings page can change intervals, thresholds, probes, retention, and the
short-incident dashboard filter. A manual test is diagnostic and does not alter
monitoring history.

## Routine checks

Service and API health:

```sh
systemctl status raspi-network-monitor.service raspi-network-monitor-api.service
curl -s http://127.0.0.1:8080/api/v1/health
journalctl -u raspi-network-monitor.service -u raspi-network-monitor-api.service \
  -p warning --since today --no-pager
```

Database integrity, size, and row counts:

```sh
sudo /opt/raspi-network-monitor/venv/bin/raspi-network-monitor-ops check
```

`quick_check` must be `ok`. With the defaults, raw probe counts should broadly
stabilize after seven to eight days. SQLite may retain freed pages for reuse, so
the file does not necessarily shrink after cleanup.

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
add MTBF and incident notes.

Never commit credentials, private keys, personal email addresses, public IP
addresses, or dynamic-DNS URLs.

## License

Licensed under the [MIT License](LICENSE).
