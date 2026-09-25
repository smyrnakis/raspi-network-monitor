# Raspberry Pi Network Monitor

A lightweight, local-first internet reliability monitor for Raspberry Pi 3 and
Raspberry Pi 4 installations.

The system will record connectivity evidence, distinguish confirmed outages
from gateway, DNS, partial-connectivity, and monitoring failures, and provide a
local dashboard and incident export. It is designed to continue operating
without WAN access and without Docker or cloud services.

## Project status

Initial design phase. The V1 monitoring semantics are defined before functional
implementation begins; architecture and configuration contracts follow.

No deployment-specific addresses, credentials, private keys, email addresses,
or dynamic-DNS URLs belong in this repository.

## Hardware targets

- Raspberry Pi 3
- Raspberry Pi 4

The same source and deployment procedure will support both. Dependency and
resource choices must be validated on both classes of hardware, with the Pi 3
setting the lower resource baseline.

## Planned stack

- Python 3 with bounded asynchronous probes
- FastAPI and a locally served responsive frontend
- SQLite in WAL mode
- Native `systemd` services
- Optional Caddy reverse proxy for LAN HTTPS

All frontend assets will be served locally. Docker and external cloud services
are not V1 requirements.

## Repository layout

```text
src/home_internet_monitor/  Python package
tests/                      Automated tests
deploy/                     Native installation and service files
docs/                       Architecture and operations documentation
```

## Documentation

- [Monitoring semantics](docs/monitoring-semantics.md)

## Development

Development commands and supported Python versions will be documented after
the Raspberry Pi host inventories and dependency compatibility review are
complete.

## Security

Never commit secrets, private keys, credentials, personal email addresses,
public IP addresses, or dynamic-DNS URLs. Use ignored local configuration or
deployment environment files for installation-specific values.

## License

Licensed under the [MIT License](LICENSE).
