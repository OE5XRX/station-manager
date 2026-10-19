# Rollout Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the functional use of `StationTag` with a `RolloutGroup` (0..1 per station) that carries an update channel (`release`/`dev`) and a rollout-sequence position; upgrades target the newest OTA-ready image of the group's channel for the station's platform, never a downgrade.

**Architecture:** `RolloutGroup` lives in `apps/stations` next to `StationTag`; `Station.rollout_group` FK. One resolver module `apps/rollouts/targeting.py` decides every upgrade target (UI single/group upgrade, dashboard, station card). Migration is additive first (new nullable fields + one data migration in `apps/rollouts`), each app then drops its old tag fields when its code is converted.

**Tech Stack:** Django 6.0, DRF 3.17 + drf-spectacular, pytest-django + django_test_migrations (`migrator` fixture), SQLite in tests / PostgreSQL in prod, ruff 0.16.8.

**Spec:** `docs/superpowers/specs/2026-10-19-rollout-groups-design.md`

## Global Constraints

- Tests top-level `tests/test_*.py`; run `.venv/bin/python -m pytest -q`; lint `.venv/bin/ruff check . && .venv/bin/ruff format --check .` (ruff 0.16.8).
- Templates: never multi-line `{# … #}` — use `{% comment %}…{% endcomment %}`.
- All UI work: invoke `Skill("frontend-design")` first; reuse existing CSS classes (`panel`, `pill`, `t-mono`, `btn`…) and `images/_channel_badge.html` for channel badges.
- Channel values exactly `"release"` and `"dev"`; no group ⇒ channel `"release"`.
- Permissions only through `apps/accounts/permissions.py` areas: group CRUD + station→group = `stations.manage`; sequence edit + group upgrade = `rollouts.manage`; single upgrade = `deployments.manage`. If the registry text changes, regenerate `docs/permissions.md` (see `tests/test_permissions_doc.py` for the command).
- No changes to `station_agent/`, image import, or provisioning.
- Every station-group change (UI, API, migration with n>1 tags) writes `StationAuditLog` with `EventType.ROLLOUT_GROUP_CHANGED`.
- Commit per task, message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Station whose `current_image_release` is an older **dev** image but now sits in a **release** group whose latest release has an *older* tag → must show "not newer", never queue a downgrade (Task 2 test `test_release_group_older_than_current_dev_is_not_newer`).
2. Same tag exists in both channels (`2026.10.19-04` release and dev) → station on release moved to dev group gets `not_newer`, not an upgrade (Task 2 `test_same_tag_other_channel_is_not_newer`).
3. Migration on dirty data: deployment with `target_type="tag"` and `target_tag=NULL`, MF target with deleted canary tag, tag whose name collides with an already-existing group → completes without exception (Task 1 `test_migration_survives_orphans_and_collisions`).
4. Deleting a `RolloutGroup` that is in the sequence / targeted by a deployment / MF target → no 500 (entry cascades + renumber signal, deployment SET_NULL, MF CASCADE / SET_NULL) (Task 3 `test_delete_group_in_sequence_renumbers`, Task 6 `test_delete_group_view_with_references`).
5. Group upgrade with mixed platforms + a station without platform → one deployment per machine, unprovisioned counted as skipped (Task 3 `test_upgrade_group_mixed_machines_and_unprovisioned`).

---

### Task 1: RolloutGroup model, additive schema, data migration

**Files:**
- Modify: `apps/stations/models.py` (add `RolloutGroup`, `Station.rollout_group`, `StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED = "rollout_group_changed", _("Rollout Group Changed")`), `apps/stations/admin.py` (register `RolloutGroup`)
- Modify: `apps/rollouts/models.py` (`RolloutSequenceEntry.group` FK → `stations.RolloutGroup`, `CASCADE`, `null=True` for now, `related_name="rollout_entries"`)
- Modify: `apps/deployments/models.py` (`TargetType.GROUP = "group", _("By Group")` added — keep `TAG` for now; `target_group` FK → `stations.RolloutGroup`, `SET_NULL`, null, `related_name="deployments"`)
- Modify: `apps/module_firmware/models.py` (`Scope.GROUP = "group", _("Group override")` added; `group` FK `CASCADE` null `related_name="firmware_targets"`; `canary_group` FK `SET_NULL` null `related_name="+"`)
- Create: migrations `stations/0028_rolloutgroup.py`, `rollouts/0004_entry_group.py`, `deployments/0004_target_group.py`, `module_firmware/0014_target_group.py` (makemigrations), `rollouts/0005_tags_to_groups.py` (RunPython, depends on all four, reverse = noop)
- Create: `apps/rollouts/tag_migration.py` — pure data logic used by 0005 (takes `apps` registry)
- Test: `tests/test_rollout_groups_migration.py`, `tests/test_rollout_group_model.py`

**Interfaces:**
- Produces: `apps.stations.models.RolloutGroup(name, slug, channel, description, created_at)` with `RolloutGroup.Channel.RELEASE/DEV`, `Meta.ordering=["name"]`, CheckConstraint `rolloutgroup_slug_not_reserved` (reuse `RESERVED_TAG_SLUGS`), `clean()` mirror like `StationTag`; `Station.rollout_group` (`related_name="stations"`); `RolloutGroup.effective_channel` not needed — use `targeting.station_channel`.
- Produces: `apps.rollouts.tag_migration.migrate_tags_to_groups(apps, schema_editor=None) -> None`.

Data migration algorithm (`migrate_tags_to_groups`):
1. functional tag ids = entry.tag_id ∪ Deployment.target_tag_id ∪ MFTarget.tag_id ∪ MFTarget.canary_tag_id (non-null).
2. per tag: group = existing group with `slug == tag.slug`, else create `name=tag.name, slug=tag.slug, channel="release", description=tag.description`; if `name` already taken by another group, use `f"{tag.name} ({tag.slug})"`, then numeric suffix until free. Map tag_id → group.
3. entries: `group = map[tag_id]` (position unchanged). Deployments: `target_type in ("tag")` → `"group"`, `target_group = map.get(target_tag_id)`. MF targets: `scope == "tag"` → `"group"`, `group = map[tag_id]`; `canary_group = map.get(canary_tag_id)`.
4. stations: for each station with `rollout_group_id is None` carrying ≥1 functional tag: rank tags by (sequence position or +inf, tag name); pick first; set group. If >1 functional tags: `StationAuditLog` row `event_type="rollout_group_changed"`, `message=f"Migration: functional tags {sorted names} → rollout group '{group.name}' (front-most in sequence)"`, `user=None`.
5. Never raise on missing refs: skip silently; wrap each station's audit insert so a failure can't abort (create via historical model, no `.log()` helper).

- [ ] **Step 1: Write failing tests** in `tests/test_rollout_group_model.py`: `test_reserved_slug_rejected` (create slug `__unassigned__` → IntegrityError), `test_station_group_set_null_on_delete`, `test_channel_default_release`. In `tests/test_rollout_groups_migration.py` (pattern `tests/test_role_to_groups_migration.py`, `migrator.apply_initial_migration` at the four pre-add leaf migrations of stations/rollouts/deployments/module_firmware, `apply_tested_migration([("rollouts","0005_tags_to_groups")])`):
  - `test_station_with_zero_tags_gets_no_group`
  - `test_station_with_one_functional_tag_gets_that_group` (tag in sequence pos 0; group name/slug equal, channel `release`, entry.group set, position kept)
  - `test_station_with_n_tags_gets_frontmost_and_audit` (tags `b` pos 0, `a` pos 1, `c` only in a deployment → group `b`; one audit row with `rollout_group_changed`)
  - `test_tie_break_by_name_for_tags_outside_sequence` (two deployment-only tags `zeta`,`alpha` → `alpha`)
  - `test_label_only_tags_create_no_group` (tag only on stations → no group, station keeps tags)
  - `test_deployment_and_mf_targets_remapped` (`target_type=="group"`, `target_group` set; MF `scope=="group"`, `group`/`canary_group` set)
  - `test_migration_survives_orphans_and_collisions` (Review Focus 3)
  - `test_tags_unchanged` (StationTag count + station.tags identical after)
- [ ] **Step 2:** Run `.venv/bin/python -m pytest -q tests/test_rollout_group_model.py tests/test_rollout_groups_migration.py` → FAIL (model/migration missing).
- [ ] **Step 3:** Implement models, `makemigrations stations rollouts deployments module_firmware`, write `0005_tags_to_groups` calling `tag_migration.migrate_tags_to_groups`.
- [ ] **Step 4:** Tests pass; full suite `.venv/bin/python -m pytest -q` green; `manage.py makemigrations --check --dry-run` clean.
- [ ] **Step 5:** Commit `feat(stations): RolloutGroup model + tag→group data migration`.

### Task 2: Upgrade target resolver

**Files:**
- Create: `apps/rollouts/targeting.py`
- Test: `tests/test_rollout_targeting.py`

**Interfaces:**
- Consumes: `RolloutGroup.Channel`, `Station.rollout_group`, `Station.current_image_release`.
- Produces:
  - `release_key(tag: str) -> tuple[int, ...]` (all `\d+` runs as ints; `()` if none)
  - `is_newer(candidate: ImageRelease, current: ImageRelease) -> bool` (key compare if both keys non-empty, else `candidate.imported_at > current.imported_at`)
  - `station_channel(station) -> str` (`station.rollout_group.channel` or `"release"`)
  - `station_platform(station) -> str | None`
  - `latest_index() -> dict[tuple[str, str], ImageRelease]` — one query over `ImageRelease.objects.filter(is_latest=True)` keyed `(machine, channel)`
  - `@dataclass(frozen=True) UpgradeTarget(target: ImageRelease | None, reason: str)` with reason constants `OK="ok"`, `NO_PLATFORM="no_platform"`, `NO_RELEASE="no_release"`, `NOT_OTA_READY="not_ota_ready"`, `UP_TO_DATE="up_to_date"`, `NOT_NEWER="not_newer"`; property `ok`.
  - `resolve_upgrade(station, index: dict | None = None) -> UpgradeTarget` — order: no platform → NO_PLATFORM; no latest for (machine, channel) → NO_RELEASE; `target.pk == current.pk` → UP_TO_DATE; `not is_newer(target, current)` → NOT_NEWER; `not target.is_ota_ready` → NOT_OTA_READY; else OK.
  - `reason_message(result: UpgradeTarget, station) -> str` — translated user message per reason (used by views/templates), e.g. NOT_NEWER: `"No newer image in channel %(channel)s: station runs %(current)s, latest is %(target)s (no downgrade)."`

- [ ] **Step 1: Failing tests:** `test_release_key_parses_date_tags` (`"2026.10.19-04"` → `(2026,10,19,4)`, `"v1.2.3"` → `(1,2,3)`), `test_dev_group_targets_latest_dev_same_machine` (rpi dev latest + qemu dev latest + rpi release latest newer than dev → picks rpi dev), `test_no_group_targets_release`, `test_release_group_older_than_current_dev_is_not_newer` (RF1), `test_same_tag_other_channel_is_not_newer` (RF2), `test_no_platform`, `test_no_release_in_channel`, `test_up_to_date`, `test_not_ota_ready_only_when_newer`, `test_index_reused_no_extra_queries` (`django_assert_num_queries(0)` with prebuilt index and select_related station).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat(rollouts): channel-aware upgrade target resolver`.

### Task 3: Rollouts app on groups (sequence, upgrades, dashboard, station card)

**Files:**
- Modify: `apps/rollouts/models.py` (entry: drop `tag`, `group` non-null, constraint `uniq_group_per_sequence` replaces `uniq_tag_per_sequence`), new migration `rollouts/0006_drop_entry_tag.py` (depends `0005`)
- Modify: `apps/rollouts/forms.py` (`SequenceAddForm.group` over `RolloutGroup`), `apps/rollouts/services.py` (`add_entry(sequence, group, by_user)`), `apps/rollouts/grouping.py`, `apps/rollouts/views.py`, `apps/rollouts/urls.py` (`upgrade/group/<slug:group_slug>/`), templates `sequence_edit.html`, `upgrade_dashboard.html`, `_dashboard_row.html`, `_station_upgrade_card.html`
- Modify: `apps/stations/views.py` (station detail upgrade context via `resolve_upgrade`: context `upgrade` = `UpgradeTarget`, `upgrade_message`)
- Modify: `apps/api/read_serializers.py` / `write_serializers.py` / `write_views.py` / `read_filters.py` for rollout-sequence-entries: field `tag` → `group` (PK)
- Modify tests: `tests/test_rollouts.py`, `tests/test_rollout_sequence_add_500.py`, `tests/test_api_read_rollouts.py`, `tests/test_api_write_rollouts.py` (tag fixtures → group)
- Test: `tests/test_rollout_group_upgrades.py`

**Interfaces:**
- Consumes: Task 2 `resolve_upgrade`, `latest_index`, `reason_message`, `UpgradeTarget`.
- Produces: `group_stations_by_sequence(stations) -> OrderedDict[str, list]` keyed by group slug: sequence groups by position, then remaining groups by name, then `UNASSIGNED_KEY`; uses `station.rollout_group_id` (caller `select_related("rollout_group")`). URL name `rollouts:upgrade_group` kwarg `group_slug`. Deployments created by group upgrade: `target_type=Deployment.TargetType.GROUP`, `target_group=group`.

Behavior: `UpgradeStationView` uses `resolve_upgrade`; non-OK → `messages.error/info(reason_message(...))`, redirect, no Deployment. `UpgradeGroupView`: any group (no sequence membership needed); stations `Station.objects.filter(rollout_group=group)`; bucket by machine; per station `resolve_upgrade(s, index)`; OK ones per machine → one Deployment; rest skipped; audit message `f"Upgrade triggered (group '{group.name}', channel {group.channel}): …"`. Dashboard rows: group display name + channel; per-station `UpgradeTarget`; up-to-date list = reason `UP_TO_DATE`; "Unassigned" header labelled "No group" with release badge.

- [ ] **Step 1: Failing tests** in `tests/test_rollout_group_upgrades.py`: `test_upgrade_station_dev_group_queues_dev_image`, `test_upgrade_station_refuses_downgrade` (no Deployment row, message contains "no downgrade"), `test_upgrade_station_without_platform_message`, `test_upgrade_group_creates_group_deployment` (`target_type == "group"`, `target_group == group`), `test_upgrade_group_mixed_machines_and_unprovisioned` (RF5), `test_upgrade_group_works_for_group_outside_sequence`, `test_dashboard_buckets_by_group_with_channel` (order: seq groups, other groups, no-group), `test_dashboard_dev_station_up_to_date_on_dev_latest`, `test_station_card_shows_not_newer_reason`, `test_sequence_add_remove_reorder_groups` (admin, 0→1→2 entries, remove middle renumbers, reorder 200), `test_delete_group_in_sequence_renumbers` (RF4).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (frontend-design for templates). **Step 4:** New + converted existing tests PASS, full suite green. **Step 5:** Commit `feat(rollouts): sequence, upgrades and dashboard on rollout groups`.

### Task 4: Deployments "By Group"

**Files:**
- Modify: `apps/deployments/models.py` (remove `TargetType.TAG` + `target_tag`; `get_target_stations` GROUP branch `Station.objects.filter(rollout_group=self.target_group)`), migration `deployments/0005_drop_target_tag.py` (depends `rollouts.0005`)
- Modify: `apps/deployments/forms.py` (`target_group`, error `"A target group is required when target type is 'By Group'."`), `views.py`, `serializers.py`, `api_views.py`, templates `deployment_form.html` / `deployment_list.html` / `deployment_detail.html` (show "By Group: <name>", or "By Group (deleted)" when `target_group` is NULL)
- Modify: `apps/api/read_serializers.py` (`target_group`), `read_filters.py`
- Modify tests: `tests/test_deployments*.py`, `tests/test_api_read_deployments.py`, others referencing `target_tag`/`TargetType.TAG` (grep)
- Test: `tests/test_deployments_by_group.py`

**Interfaces:**
- Consumes: Task 1 `Deployment.TargetType.GROUP`, `target_group`.
- Produces: no `target_tag` anywhere (`grep -rn target_tag apps templates tests` empty except migrations).

- [ ] **Step 1: Failing tests:** `test_create_by_group_targets_group_stations` (form POST, results for group stations only), `test_by_group_requires_group`, `test_detail_shows_deleted_group_label`, `test_api_deployment_exposes_target_group`.
- [ ] **Step 2–4:** FAIL → implement (frontend-design) → PASS + full suite. **Step 5:** Commit `feat(deployments): deploy by rollout group`.

### Task 5: Module firmware targets on groups

**Files:**
- Modify: `apps/module_firmware/models.py` (remove `Scope.TAG`, `tag`, `canary_tag`; constraints: `uniq_group_target_per_type_group` (fields `module_type, group`, cond `scope="group"`), `target_fleet_has_no_ref` / `target_group_has_group_only` / `target_station_has_station_only` mirrors with `group`, `canary_group_only_on_fleet`; `clean()` messages with "group"), migration `module_firmware/0015_drop_tag_scope.py` (depends `rollouts.0005`; remove old constraints before fields)
- Modify: `reconciler.py` (`effective_target`: station > group(`station.rollout_group_id`) > fleet; canary gate on `canary_group_id`), `ops.py` (snapshot keys `group`, `canary_group`; label `f"{key} group {slug}"`), `forms.py` (`fields = ("scope", "group", "station", "canary_group")`, `data-scope-field` values, uniq error text), `views_targets.py`, `admin.py`, templates `_target_cell.html`, `target_form.html`, `target_list.html` (+ JS that toggles `data-scope-field`)
- Modify: `apps/api/module_firmware_serializers.py`, `module_firmware_views.py`
- Modify tests: `tests/module_firmware/**`, `tests/test_api_module_firmware_targets.py` etc. (grep `canary_tag|scope=.tag|Scope.TAG`)
- Test: `tests/module_firmware/test_group_scope.py`

**Interfaces:**
- Consumes: Task 1 fields.
- Produces: `effective_target(station, module_type)` signature unchanged.

- [ ] **Step 1: Failing tests:** `test_precedence_station_over_group_over_fleet`, `test_group_target_ignored_for_other_group`, `test_canary_group_gates_fleet_target`, `test_unique_group_target_per_type`, `test_group_scope_requires_group_only`, `test_api_target_group_fields`.
- [ ] **Step 2–4:** FAIL → implement → PASS + full suite. **Step 5:** Commit `feat(module_firmware): group scope and canary group`.

### Task 6: Group CRUD UI + station group assignment UI

**Files:**
- Modify: `apps/stations/views.py` (`RolloutGroupListView` `stations` view, `RolloutGroupCreateView/UpdateView/DeleteView` `stations.manage`), `apps/stations/forms.py` (`RolloutGroupForm(name, slug, channel, description)`; `StationForm` gains `rollout_group`), `apps/stations/urls.py` (`groups/`, `groups/create/`, `groups/<int:pk>/edit/`, `groups/<int:pk>/delete/` → names `group_list`, `group_create`, `group_edit`, `group_delete`)
- Create templates: `apps/stations/templates/stations/group_list.html`, `group_form.html`, `group_confirm_delete.html` (mirror tag templates)
- Modify: `templates/includes/sidebar.html` (Rollout groups entry after Tags), `stations/_station_table.html` + `station_detail.html` (group name + channel badge), `station_form.html`
- Modify: Station update view → on `rollout_group` change write `StationAuditLog` `ROLLOUT_GROUP_CHANGED` `"Rollout group: <old or 'none'> → <new or 'none'> by <user>"`
- Test: `tests/test_rollout_group_ui.py`

- [ ] **Step 1: Failing tests:** `test_staff_can_create_group_with_channel`, `test_member_gets_403_on_create`, `test_group_list_visible_to_applicant_readonly`, `test_station_form_sets_group_and_audits`, `test_station_list_shows_group_badge`, `test_delete_group_view_with_references` (RF4: group in sequence + deployment + MF target → 302, no 500).
- [ ] **Step 2–4:** FAIL → implement (frontend-design) → PASS + full suite. **Step 5:** Commit `feat(stations): rollout group CRUD and station assignment UI`.

### Task 7: API rollout-groups + station field + OpenAPI

**Files:**
- Modify: `apps/api/read_serializers.py` (`RolloutGroupSerializer` fields `id, name, slug, channel, description, created_at`; Station read adds `rollout_group`), `read_views.py` (`RolloutGroupViewSet(AreaReadOnlyViewSet)` area `stations`, search name/slug, ordering name), `write_serializers.py` (`RolloutGroupWriteSerializer` reserved-slug check like StationTag; Station write serializer gains `rollout_group`), `write_views.py` (`RolloutGroupViewSet` CRUD like `StationTagViewSet`, `audit_config_write`; station update audits group change with `ROLLOUT_GROUP_CHANGED` via `audit_station_write`), `router.py` (`rollout-groups`), `read_filters.py` (Station filter `rollout_group`)
- Modify: schema tests `tests/test_api_read_schema.py`, `tests/test_api_write_schema.py`, `tests/test_api_schema.py` as needed
- Test: `tests/test_api_rollout_groups.py`

- [ ] **Step 1: Failing tests:** permission matrix parametrized over (admin, staff, member, applicant, anon) × (list, retrieve, create, update, delete) for `/api/v1/rollout-groups/` (read: applicant+ 200, anon 401/403 per existing pattern; write: staff/admin 2xx, member/applicant 403); `test_reserved_slug_400`; `test_invalid_channel_400`; `test_station_patch_rollout_group_audits`; `test_station_filter_by_rollout_group`; `test_schema_lists_rollout_groups_paths`.
- [ ] **Step 2–4:** FAIL → implement → PASS + full suite. **Step 5:** Commit `feat(api): rollout-groups resource and station rollout_group`.

### Task 8: Final verification

- [ ] `grep -rn "target_tag\|canary_tag\|Scope.TAG\|TargetType.TAG" apps templates tests --include='*.py' --include='*.html' | grep -v migrations` → empty.
- [ ] `.venv/bin/python manage.py makemigrations --check --dry-run` → "No changes detected".
- [ ] Regenerate `docs/permissions.md` if `tests/test_permissions_doc.py` fails.
- [ ] Full suite + ruff green; commit leftovers.
