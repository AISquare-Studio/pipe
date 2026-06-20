# Security Policy

`aisquare.pipe` is a connector framework: connectors receive service credentials
at runtime through a `config` dict and use them to talk to third-party APIs. The
framework itself never persists, logs, or transmits those credentials — but
because connectors handle secrets, we take security reports seriously.

## Supported Versions

The project is pre-1.0 and under active development. Security fixes are applied
to the latest released version. If you need stability, pin a version you have
reviewed.

| Version                  | Supported          |
| ------------------------ | ------------------ |
| latest `0.x`             | :white_check_mark: |
| older `0.x`              | :x: (please upgrade) |

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through GitHub's
**[Report a vulnerability](https://github.com/AISquare-Studio/pipe/security/advisories/new)**
form (repository **Security** tab → **Advisories** → *Report a vulnerability*).
This opens a private advisory visible only to the maintainers.

Please include:

- A description of the issue and its impact
- Steps to reproduce (a minimal proof of concept if possible)
- Affected version(s), connector(s), and environment
- Any suggested remediation

## What to Expect

- **Acknowledgement** within 3 business days.
- An initial assessment and severity triage within 7 business days.
- Coordinated disclosure: we will agree on a timeline with you and credit you in
  the advisory unless you prefer to remain anonymous.

## Scope

**In scope**

- The framework (`src/aisquare/pipe/`) — pipeline, registry, envelope, CLI, MCP server.
- Connectors maintained in this repository (`connectors/*`).

**Out of scope**

- Vulnerabilities in third-party services or their SDKs (report those upstream).
- Credentials you place in your own `.env`, config files, or environment — keep
  these out of version control.

## Handling Credentials Safely

- Never commit credentials. `.env`, `.envrc`, and `.pypirc` are gitignored.
- Connector `config` dicts are runtime-only; do not hard-code secrets in source
  or tests. Test suites mock at the client boundary and require no real credentials.
- Live tests are opt-in and read credentials from environment variables only.
