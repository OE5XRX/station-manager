# Module-Firmware GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The whole module-firmware update workflow (types → import → targets → convergence → quarantine release) usable from the normal station-manager GUI, findable via the sidebar, no Django admin.

**Architecture:** New CBVs in focused view modules inside `apps/module_firmware/`, sharing an audit helper and two new services. Templates reuse the existing Tabler-ish design system (`page-head`, `panel`, `t-table`, `pill`, `empty`, `filter-bar`) of `module_firmware/release_list.html`. HTMX for the GitHub preview + polling job list.

**Tech Stack:** Django 6.0, HTMX, pytest-django.

**Spec:** `docs/superpowers/specs/2026-10-18-module-firmware-gui-design.md`

## Global Constraints

- Permission: reads `AreaPermissionMixin` with `area = "modules"`; every mutating view `verb = "manage"` (staff+). Templates gate buttons with `{% if can.modules.manage %}`.
- Every write is audited (helper from Task 1). Audit failure never breaks the mutation.
- Multi-line `{# #}` is forbidden → `{% comment %}` (CI `scripts/check_template_comments.py`). Colors only via CSS tokens (`scripts/check_color_tokens.py`).
- CSP: `script-src 'self' nonce` → no inline JS handlers; behaviour goes into `static/js/app.js` via `data-*` attributes.
- UI strings in templates `{% trans %}` English (like existing templates); Python messages via `gettext`.
- Tests in `tests/module_firmware/test_*.py`, run with `DJANGO_SETTINGS_MODULE=config.settings.test DJANGO_SECRET_KEY=x .venv/bin/python -m pytest -q tests/module_firmware`. Lint `uvx ruff@0.16.8 format . && uvx ruff@0.16.8 check .`.
- New URL patterns go **before** the `<str:uid>/` catch-all in `apps/module_firmware/urls.py`.
- UI tasks: invoke `Skill("frontend-design")` before writing templates; stay inside the existing look.

## Review Focus

- Staff POSTs a ModuleType delete for a type that has only **archived** releases → must be refused (check `ModuleFirmwareRelease.all_objects`), not a ProtectedError 500. (Task 2 test `test_type_delete_blocked_by_archived_release`.)
- Editing a target whose version has since been fully archived must still save other fields. (Task 3 test `test_edit_target_with_archived_version_keeps_choice`.)
- Duplicate fleet target for the same type → form error, not IntegrityError. (Task 3 `test_duplicate_fleet_target_is_form_error`.)
- Member (non-staff) POSTing to release-quarantine / target / type endpoints → 403, nothing changes. (Tasks 2, 3, 5 permission tests.)
- GitHub release with assets only for a *different* prefix → `no_assets`, no Import button. (Task 4 `test_gh_partial_no_assets_state`.)

---

### Task 1: Foundations — event types, audit helper, services

**Files:**
- Modify: `apps/stations/models.py` (StationAuditLog.EventType), new migration `apps/stations/migrations/00xx_…` via `makemigrations stations`
- Create: `apps/module_firmware/audit.py`
- Modify: `apps/module_firmware/services.py`
- Modify: `apps/accounts/permissions.py` (`modules` verb label only)
- Test: `tests/module_firmware/test_gui_services.py`

**Interfaces — Produces:**
- `StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED = "firmware_target_removed"`, `MODULE_QUARANTINE_RELEASED = "module_quarantine_released"` (labels "Firmware Target Removed", "Module Quarantine Released").
- `audit.audit_config(request, *, obj: str, action: str, message: str, details: dict | None = None) -> None` → `AccountAuditLog.log(event_type=CONFIG_CHANGED, actor=request.user, ip_address=client_ip(request), message=message, details={"object": obj, "action": action, **(details or {})})`; swallow+log exceptions.
- `audit.audit_station(request, *, event_type, message, station=None, module=None, changes=None) -> None` → `StationAuditLog.log(..., user=request.user, ip_address=client_ip(request))`; no-op if both subjects None; swallow+log exceptions.
- `services.reconcile_module_type(module_type) -> int` — `reconcile_module(m)` for every `Module` of the type with an open assignment (`assignments__to_ts__isnull=True`); per-module exceptions logged, not raised; returns count reconciled OK.
- `services.release_quarantine(module, *, user, ip_address=None) -> list[str]` — atomic; lock module row first (`select_for_update`), then the module's `QUARANTINED` convergence states (`select_for_update`); set `state=UPDATING, attempts=0, last_error_mode="", last_error_message=""`; then `reconcile_module(module)`; audit `MODULE_QUARANTINE_RELEASED` with module + open-assignment station, `user`, `changes={"released_targets": [versions]}`; returns released target versions (empty list → no-op, no audit).
- permissions `modules.verb_labels["manage"]` = `"Module bestätigen, Firmware importieren/archivieren, Modultypen, Soll-Versionen, Quarantäne freigeben"`.

- [ ] **Step 1: Failing tests**: `test_audit_config_writes_config_changed` (details.object/action, actor, message), `test_audit_station_skips_without_subject`, `test_reconcile_module_type_only_assigned_modules` (two modules, one without open assignment → returns 1; with a fleet target and matching release the assigned module's `firmware_convergence` becomes `updating`), `test_release_quarantine_resets_state_and_audits` (CS quarantined attempts=3 → updating/0/errors blank, module rollup `updating`, one `MODULE_QUARANTINE_RELEASED` row with user + station), `test_release_quarantine_noop_when_none` (returns `[]`, no audit row).
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** the above; `makemigrations stations` for the choices change.
- [ ] **Step 4: Run module_firmware + `tests/test_permissions_registry.py`, verify PASS.**
- [ ] **Step 5: Commit** `feat(module_firmware): audit helper + quarantine-release/reconcile services`.

### Task 2: Module types CRUD

**Files:**
- Create: `apps/module_firmware/forms.py`, `apps/module_firmware/views_types.py`, templates `module_firmware/type_list.html`, `type_form.html`, `type_confirm_delete.html`
- Modify: `apps/module_firmware/urls.py`
- Test: `tests/module_firmware/test_gui_types.py`

**Interfaces:**
- Consumes: `audit.audit_config`.
- Produces: URL names `module_firmware:type_list` (`types/`), `type_create` (`types/new/`), `type_update` (`types/<int:pk>/edit/`), `type_delete` (`types/<int:pk>/delete/`). `forms.ModuleTypeForm` (fields `key, display_name, hw_repo, firmware_repo, release_asset_prefix`; on an existing instance `key` is `disabled=True`). Regexes: repo `^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$` (blank ok), prefix `^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$` (blank ok). Helper `views_types.type_in_use(module_type) -> bool` (modules, `ModuleFirmwareRelease.all_objects`, targets).
- List annotates counts `module_count`, `release_count` (all_objects incl. archived), `target_count` and shows "import-ready" vs. "repo/prefix missing".
- Delete GET shows confirm page (or the "in use" explanation); POST refuses with `messages.error` + redirect when in use; also catches `ProtectedError`. Success messages on create/update/delete. Audit: obj `"module_type"`, actions `created/updated/deleted`, details `key` + `changes` (`{field: {"old","new"}}` from `form.changed_data`).

- [ ] **Step 1: Failing tests**: list 200 for applicant; create/update/delete 403 for member, login-redirect for anon; staff create → row + one CONFIG_CHANGED audit with `details["object"] == "module_type"`; `test_repo_format_validated` (`"not a repo"` → form error); `test_prefix_trailing_dash_rejected` (`"fm-"`); `test_key_readonly_on_edit` (POST with changed key keeps old key); `test_type_delete_unused_ok`; `test_type_delete_blocked_by_module`; `test_type_delete_blocked_by_archived_release`.
- [ ] **Step 2: FAIL.** **Step 3: Implement** (invoke frontend-design first; copy the form layout of `stations/tag_form.html`). **Step 4: PASS + ruff.** **Step 5: Commit** `feat(module_firmware): module type CRUD in the GUI`.

### Task 3: Firmware targets CRUD

**Files:**
- Modify: `apps/module_firmware/forms.py` (add `ModuleFirmwareTargetForm`), `apps/module_firmware/admin.py` (admin form: keep its own class, untouched behaviour)
- Create: `apps/module_firmware/views_targets.py`, templates `target_list.html`, `target_form.html`
- Modify: `apps/module_firmware/urls.py`, `static/js/app.js` (scope toggle)
- Test: `tests/module_firmware/test_gui_targets.py`

**Interfaces:**
- Consumes: `audit.audit_config`, `audit.audit_station`, `services.reconcile_module_type`.
- Produces: URL names `target_list` (`targets/`), `target_create` (`targets/new/`), `target_update` (`targets/<int:pk>/edit/`), `target_delete` (`targets/<int:pk>/delete/`, POST only).
- `ModuleFirmwareTargetForm(ModelForm)`: Meta fields `scope, tag, station, canary_tag`; extra `release = ChoiceField` with value `f"{module_type_id}:{version}"`, grouped choices `[(type.display_name, [(value, "1.2.0 — vhf, uhf"), …]), …]` from non-archived releases (distinct type+version, variants joined, versions desc). On edit the current `(module_type, version)` is included even if archived (label suffix " (archived)") and is the initial value. `clean_release()` parses + rejects values not in the choices; `_post_clean` sets `instance.module_type_id/version` before model validation so `Model.clean` + unique constraints surface as form errors.
- Widgets carry `data-scope-field="tag|station|canary_tag"`, scope select `data-scope-select`; app.js hides fields whose scope doesn't match (`canary_tag` only for `fleet`).
- Views: create sets `created_by`; after create/update/delete → `services.reconcile_module_type(type)`; audit obj `"module_firmware_target"` actions `created/updated/deleted` with details `module_type` (key), `scope`, `version`, `tag`, `station`, `canary_tag` (slugs/names) + `changes` on update; station scope also `audit_station(event_type=FIRMWARE_TARGET_SET|FIRMWARE_TARGET_REMOVED, station=…)`.
- List: grouped per module type; per target show variant coverage (`ModuleFirmwareRelease.objects.filter(module_type, version).values_list("variant")`), created_by/updated_at, edit/delete for manage; empty state with CTA (and CTA to release list if no releases imported).

- [ ] **Step 1: Failing tests**: list 200 applicant; writes 403 member; staff create fleet target via `release="<pk>:1.0.0"` → target + CONFIG_CHANGED audit; `test_station_target_writes_station_audit` (FIRMWARE_TARGET_SET with user); delete station target → FIRMWARE_TARGET_REMOVED; `test_unknown_release_value_rejected` (`"<pk>:9.9.9"` → form error, no row); `test_tag_scope_requires_tag`; `test_canary_only_on_fleet`; `test_duplicate_fleet_target_is_form_error`; `test_edit_target_with_archived_version_keeps_choice`; `test_target_create_reconciles_assigned_modules` (assigned module rollup becomes `updating`).
- [ ] **Step 2: FAIL.** **Step 3: Implement** (frontend-design first). **Step 4: PASS + ruff + `tests/module_firmware/test_fw_admin.py` still green.** **Step 5: Commit** `feat(module_firmware): firmware target (Soll-Version) management UI`.

### Task 4: Import UX

**Files:**
- Modify: `apps/module_firmware/views.py` (release list ctx, GH partial, import/archive/restore), templates `release_list.html`, `_github_releases.html`
- Create: template `module_firmware/_import_jobs.html`
- Modify: `apps/module_firmware/urls.py`
- Test: `tests/module_firmware/test_gui_import.py`

**Interfaces:**
- Consumes: `audit.audit_config`; Task 2 URL names `type_create`, `type_update`.
- Produces: URL `module_firmware:import_jobs` (`firmware/jobs/`, view verb) rendering `_import_jobs.html` with last 20 jobs; context `jobs`, `has_active` (any pending/running). The partial's wrapper div carries `hx-get` + `hx-trigger="every 5s"` + `hx-swap="outerHTML"` **only when `has_active`**. `release_list.html` includes it in place of the old timeline. Ready jobs list imported versions via `job.release` (single FK) — show `job.release.version` when set.
- Release list ctx adds `all_types_count` and `incomplete_types` (types lacking firmware_repo or prefix); setup panel: none → "No module type configured" + manage CTA to `type_create`; incomplete → list with missing field names + manage `type_update` link.
- GH partial rows gain `variants` (list of variant strings, `""` rendered as "(bandless)"), `missing_variants` (sorted distinct non-empty `Module.variant` of this type not in `variants`), and state `no_assets` when `parse_variant_assets` returns `[]` (checked before imported/queued) → no Import button, hint showing expected pattern `"{prefix}-<variant>.signed.bin + .bundle"`.
- `FirmwareImportView`: `messages.success` "Import of %(tag)s queued." / `messages.info` "already in progress" / `messages.error` for not-import-ready type; audit obj `"module_firmware_import"` action `queued` (details module_type, tag) only when a job was created. Archive/Restore: messages + audit obj `"module_firmware_release"` actions `archived`/`restored` (details module_type, variant, version) only when state actually changed.

- [ ] **Step 1: Failing tests**: `test_release_list_setup_hint_no_types` (staff sees link to `type_create`), `test_release_list_lists_incomplete_types`; `test_gh_partial_shows_variants` (mock `github_releases.fetch_releases` returning assets `fm-sa818-vhf.signed.bin(.bundle)`, `fm-sa818-uhf…` → "vhf" and "uhf" in body); `test_gh_partial_no_assets_state` (assets `other-vhf.signed.bin(.bundle)` → "no_assets"-hint text present, no `name="tag"` input); `test_gh_partial_missing_variant_warning` (module variant `uhf`, release only vhf → "uhf" warning); `test_import_jobs_partial_polls_only_when_active`; `test_import_writes_audit_and_message`; `test_archive_restore_audited_once` (second archive no extra audit).
- [ ] **Step 2: FAIL.** **Step 3: Implement** (frontend-design first). **Step 4: PASS (incl. existing `test_fw_views.py`) + ruff.** **Step 5: Commit** `feat(module_firmware): import setup hints, variant preview, live job status`.

### Task 5: Convergence overview, quarantine release, module-detail firmware panel

**Files:**
- Create: `apps/module_firmware/views_convergence.py`, template `module_firmware/convergence.html`
- Modify: `apps/module_firmware/urls.py`, `apps/module_firmware/views.py` (ModuleDetailView ctx), `module_detail.html`
- Test: `tests/module_firmware/test_gui_convergence.py`

**Interfaces:**
- Consumes: `services.release_quarantine`, `reconciler.effective_target`.
- Produces: URL `convergence` (`convergence/`), `module_release_quarantine` (`<str:uid>/release-quarantine/`, POST, manage; redirects to `next` if it is a safe local path via `url_has_allowed_host_and_scheme`, else module detail; `messages.success` / `messages.info` for no-op).
- Helper `views_convergence.firmware_row(module, assignment) -> dict` with keys `target` (ModuleFirmwareTarget|None), `has_release` (bool: release for module.variant+target.version exists), `quarantine` (latest QUARANTINED ConvergenceState|None). Used by both the overview (per paginated row) and ModuleDetailView (`ctx["firmware"]`).
- Overview: queryset Modules with prefetched open assignment (same Prefetch as `ModuleListView`), filters `type` (key), `state` (`firmware_convergence`), `station` (id, open assignment), `q` (uid icontains); KPI counts per Convergence value over all modules; paginate 50 (reuse `_pagination.html`).
- Detail page panel "Firmware": reported version, variant, effective target (version + scope badge + link `target_list`), rollup pill, quarantine attempts/error + release button (manage, `data-confirm`).

- [ ] **Step 1: Failing tests**: overview 200 applicant, shows module uid, reported + target version; `test_overview_filter_state`; `test_overview_no_release_for_variant_hint`; `test_release_quarantine_member_forbidden` (403, state unchanged); `test_release_quarantine_staff` (state reset, redirect, audit row); `test_release_quarantine_rejects_external_next`; detail page shows effective target version.
- [ ] **Step 2: FAIL.** **Step 3: Implement** (frontend-design first). **Step 4: PASS + ruff.** **Step 5: Commit** `feat(module_firmware): convergence overview + quarantine release`.

### Task 6: Sidebar section

**Files:**
- Modify: `templates/includes/sidebar.html`
- Test: `tests/module_firmware/test_gui_sidebar.py`

**Interfaces:** Consumes URL names from Tasks 2–5 + existing `module_list`, `release_list`.

- New `nav-section` "Module Firmware" gated `{% if can.modules.view %}` placed after "Operations"; entries + active rules exactly as in spec §1; remove the old "Modules" item from Operations. Icons: inline SVGs in the existing style.

- [ ] **Step 1: Failing tests**: applicant dashboard page contains links to `release_list`, `target_list`, `convergence`, `type_list`; on `target_list` page the Targets item has `aria-current="page"`.
- [ ] **Step 2: FAIL.** **Step 3: Implement.** **Step 4: Full suite `python -m pytest -q` + ruff + `python scripts/check_template_comments.py` + `python scripts/check_color_tokens.py`.** **Step 5: Commit** `feat(ui): module firmware section in sidebar`.
