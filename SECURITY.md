# Security Policy

This project writes to live Fronius inverter Modbus registers when
`write.enabled=true` (see [docs/POWER_LIMIT_CONTROL.md](docs/POWER_LIMIT_CONTROL.md)),
and exposes an unauthenticated HTTP control surface on the monitoring port
when those writes are enabled. Vulnerabilities in either path can affect
physical inverter operation, so we take reports seriously.

## Supported versions

Security fixes are applied to the latest minor release and backported to
the previous minor where practical. Older releases are best-effort.

| Version | Supported |
|---------|-----------|
| 1.8.x   | yes       |
| 1.7.x   | best-effort |
| < 1.7   | no        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security reports.**

Email **stefan.maldaianu@gmail.com** with:

- A description of the issue and the affected component (collector,
  monitoring HTTP server, write protocol, MQTT command path, container
  image, build pipeline, …).
- Steps to reproduce, including the configuration used (with secrets
  redacted) and the version / commit you tested against.
- Impact: what an attacker on the same network / on the host / on the
  internet (if exposed) could achieve.
- Any suggested fix, if you have one.

You can expect:

- **Acknowledgement** within 5 business days.
- A **triage decision** (accepted / declined / needs more info) within
  10 business days.
- A **fix or mitigation plan** for accepted reports within 30 days,
  depending on severity. Critical issues affecting write-path safety
  are prioritised.

I am a single maintainer working on this project in my spare time;
realistic timelines are appreciated.

## Out of scope

The following are explicitly out of scope:

- Misconfiguration by the operator (e.g. exposing the monitoring port to
  the public internet without authentication, or enabling `write` on an
  untrusted network). The README and `docs/MONITORING.md` document the
  expected deployment model: behind a trusted network or a reverse proxy
  with authentication.
- Vulnerabilities in third-party dependencies (`pymodbus`, `paho-mqtt`,
  `influxdb-client`, `fastapi`, `uvicorn`, `psutil`, etc.) — report
  those upstream. We will rebuild / bump versions when upstream
  releases a fix.
- Issues in the Fronius DataManager firmware or in the Modbus TCP
  surface exposed by the inverter — report those to Fronius.
- Denial of service achievable only by an attacker who already has full
  network access to a control-plane port (Modbus 502, monitoring HTTP).
  Those ports are not designed to survive a hostile actor on the same
  segment.

## Hall of fame

Accepted reports will be credited in `CHANGELOG.md` under the release
that fixes them, unless the reporter prefers to remain anonymous.
