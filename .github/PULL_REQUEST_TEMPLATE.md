<!-- Delete sections that don't apply. -->

## What / Why

<!-- 1-3 sentences. What does this change do, and why. -->

## Affected areas

- [ ] `accounts` / auth
- [ ] `api` / DRF endpoints
- [ ] `stations`
- [ ] `firmware`
- [ ] `deployments` / OTA
- [ ] `builder`
- [ ] `tunnel` / terminal
- [ ] `monitoring` / alerts
- [ ] `audit`
- [ ] `dashboard`
- [ ] Infra (compose, nginx, CI)

## Migrations / data

- [ ] No new migrations
- [ ] New migrations, **no** downtime needed
- [ ] New migrations, **downtime required** — describe:

## Testing

<!-- Tests added? Manually verified? Paste log tail if useful. -->

## Breaking changes

- [ ] None — deployed stations + existing users keep working
- [ ] Yes — describe migration path:

## Checklist

- [ ] CI is green (ruff, pytest)
- [ ] Commit messages are meaningful (subject + why in body)
- [ ] `.env.example` updated if new settings were added
- [ ] Translations (`makemessages`) if user-facing strings changed
- [ ] SECURITY.md / README updated if behaviour changed
