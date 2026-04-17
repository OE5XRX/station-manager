# Contributing

Thanks for considering a contribution. This repo runs the OE5XRX
station fleet in production, so we're a bit careful.

## Workflow

1. **Fork** and create a feature branch: `git checkout -b fix/my-thing`
2. **Write a test** for anything that isn't obvious UI polish. We use
   pytest-django — see the existing `tests.py` files under `apps/*`
   for style
3. **Run locally** until green:

   ```bash
   docker compose exec web pytest
   docker compose exec web ruff check .
   docker compose exec web ruff format --check .
   ```

4. **Commit** with a meaningful subject + body. "Why, not what" — the
   diff shows what changed; the message should say why
5. **Push and open a PR** against `main`. Fill in the template
6. **CI must be green** before review (lint + tests)
7. A maintainer reviews + merges. Squash or rebase; no merge commits

## Commit messages

```
apps/tunnel: drop AllowedHostsOriginValidator for /ws/agent/*

CLI WebSocket clients don't send an Origin header, so the validator
was 403-ing every station-agent terminal request. Browser WS routes
keep the validator.
```

Rules of thumb:
- Subject ≤ 72 chars, imperative mood
- Body wraps at 72
- Reference the prod issue (or Linear ticket) if there is one

## Scope of a PR

- **Small is better.** One concern per PR.
- **No drive-by reformatting.** Ruff already enforces style — don't
  re-wrap lines or rearrange imports outside the diff you actually
  care about.
- **Migrations** that touch production data need a plan. Call it out
  in the PR body: "Requires downtime: yes/no", "Data migration: yes/no".

## Translations

Strings are marked with `gettext_lazy`. If you add user-facing text,
regenerate the catalogs:

```bash
docker compose exec web python manage.py makemessages -l de -l en
# edit locale/<lang>/LC_MESSAGES/django.po
docker compose exec web python manage.py compilemessages
```

## Security

Don't file security issues as public PRs or issues — see
[SECURITY.md](SECURITY.md) for the private disclosure path.

## License

By contributing you agree your work is licensed under
[GPL-3.0-or-later](LICENSE), same as the rest of the codebase.
