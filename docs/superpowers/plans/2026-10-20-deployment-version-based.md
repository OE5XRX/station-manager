# Deployment version-based — implementation plan

Spec: `docs/superpowers/specs/2026-10-20-deployment-version-based-design.md`.
Execution: subagent-driven-development, TDD, fan-out ≤ 2. Tests top-level
`tests/test_*.py`, `.venv/bin/python -m pytest -q`, ruff 0.16.8
(`uvx ruff@0.16.8 format . && uvx ruff@0.16.8 check .`). `{% comment %}`
rule for multi-line template comments.

## Task 1 — model, migrations, service, agent endpoints (sequential, first)

Files: `apps/deployments/{models,services,admin,consumers,api_views,views}.py`,
`apps/deployments/migrations/0007..0009`, `apps/monitoring/engine.py`,
`apps/dashboard/{views.py,templates/dashboard/index.html}`,
`apps/stations/views.py`, `apps/rollouts/templates/rollouts/_station_upgrade_card.html`,
`apps/rollouts/views.py` (only `select_related` + minimal create-kwargs so the
suite stays green), deployment list/detail templates (minimal field swap),
`tests/conftest.py` + every test constructing `Deployment(image_release=…)`.

1. Tests first: `tests/test_deployment_version_migration.py` (MigrationExecutor:
   old deployment with image_release + 2 results → after 0009 results carry the
   image, deployment has tag/channel); `tests/test_deployment_service.py`
   (plan matrix, create_deployment incl. supersession conflict → SKIP_ACTIVE,
   NoEligibleStationsError rollback, audit on commit);
   `tests/test_deployment_agent_per_station.py` (fleet deployment with a qemu
   and an RPi station → `check` returns each station's own sha/size and
   unchanged JSON keys, `download` serves each station's rootfs key, `commit`
   matches the result's tag).
2. Model + migrations per spec; service per spec.
3. Swap every reader of `deployment.image_release` to `result.image_release`
   / `deployment.image_tag`. `__str__` uses tag + channel.
4. Keep `DeploymentCreateView` + upgrade views + API create working by routing
   them through `create_deployment` with an explicit tag/channel (form still
   passes the old select's release tag/channel until Task 3).
5. Full suite green, ruff clean, commit.

## Task 2 — upgrade views + User API (parallel with Task 3, after Task 1)

Files: `apps/rollouts/views.py`, `apps/api/{write_serializers,write_views,read_serializers}.py`,
OpenAPI, `tests/test_rollout_group_upgrades.py`, `tests/test_api_write_deployments.py`,
`tests/test_api_read_deployments.py`, new tests as needed.

- `UpgradeStationView`/`UpgradeGroupView` call `create_deployment` with the
  tag/channel from `resolve_upgrade`; group buckets by `(tag, channel)`;
  membership re-check under lock kept; skipped counts include service skips.
  Test: mixed qemu+RPi group with different latest tags → 2 deployments, each
  result has its machine's image.
- API create: `DeploymentCreateSerializer` fields `image_tag`, `image_channel`
  (default `release`), `target_station`, `strategy`, `phase_config`; skip
  reason → field 400 (`no_platform` → `target_station`, others → `image_tag`);
  conflict → 400. Read serializer: `image_tag`, `image_channel`,
  `skipped_stations`; result serializer gains `image_release`. Permission
  matrix tests stay green; schema test asserts new fields and absence of
  `image_release` on Deployment create.

## Task 3 — mask, preview, detail, vocabulary (parallel with Task 2, after Task 1)

Files: `apps/deployments/{forms,views,urls}.py`, deployment templates (+ new
`_preview.html`), `templates/includes/sidebar.html`,
`apps/stations/templates/stations/tag_list.html`, images list template,
`apps/rollouts/templates/rollouts/sequence_edit.html` (+ dashboard wording),
`tests/test_deployments.py`, `tests/test_deployment_form_version.py`.

- `DeploymentForm` (plain `forms.Form`): `version` ChoiceField from
  `available_versions()` (optgroups per channel, value `channel|tag`), target
  fields as before (group/station querysets), strategy, phase_config.
  `clean()` validates the pair exists and runs `plan_deployment`; nothing
  servable → non-field error listing reasons.
- `DeploymentCreateView` → `FormView` calling `create_deployment`; flash
  "created with N station(s), M skipped".
- `DeploymentPreviewView` (GET, `deployments.manage`): HTMX partial table.
- Detail: image column per result, skipped panel, channel badge.
- Vocabulary changes per brief part C + sweep; list them in PR body.
- Skill `frontend-design` for all UI work.

## Task 4 — integration (controller)

Rebase/merge both, full suite, ruff, push, PR body (model change, migration,
unchanged agent endpoints, vocabulary list), CI green, review-loop.
