# Security Policy

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private reporting
channel instead:

→ https://github.com/OE5XRX/station-manager/security/advisories/new

We aim to acknowledge reports within 48 hours and ship a fix (or a
clear mitigation path) within 14 days for high-severity issues.

If you can't use GitHub advisories, email `security@oe5xrx.at` with the
subject `[station-manager security]`.

## Scope

In scope:

- The Django application in this repo (all `apps/*`, `config/*`)
- The `station_agent/` code that ships onto the fleet
- Our prod deploy config under `deploy/` (`docker-compose.prod.yml`,
  `nginx.conf`)
- The published container image `ghcr.io/oe5xrx/station-manager`

Out of scope:

- Third-party dependencies (report upstream — we track them via
  Dependabot)
- The underlying infrastructure (Hetzner, Let's Encrypt, GitHub)
- Physical access to station hardware

## Supported versions

Only `main` is actively maintained. We don't backport — if you're
running an older image, upgrade first.

## What we care about

This is amateur-radio fleet-management software. The realistic threats:

- **Impersonating a station** — bypassing Ed25519 signature auth
- **Remote code execution via the terminal WebSocket** — the tunnel
  app is the highest-risk surface
- **Pushing a malicious OTA** — signing keys, deployment ACLs
- **Privilege escalation between member / operator / admin roles**
- **Secret exposure** — SMTP / Telegram / Django key leaking through
  error pages, logs, or public endpoints

If you find anything in those buckets, we want to hear about it fast.

## Crypto & verification

- Station authentication: **Ed25519** signatures, current/next slot
  rotation. No shared tokens.
- Container images on GHCR: signed by GitHub's built-in attestation.
  Verify with
  `cosign verify-attestation --type slsaprovenance ghcr.io/oe5xrx/station-manager:TAG`
  against the repo's OIDC identity.
- The companion [linux-image][li] releases are signed with
  [cosign keyless][cosign] — see its SECURITY.md for verification.

[li]: https://github.com/OE5XRX/linux-image
[cosign]: https://docs.sigstore.dev/cosign/signing/overview/
