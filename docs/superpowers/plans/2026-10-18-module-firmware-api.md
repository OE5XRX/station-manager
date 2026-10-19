# Module-Firmware Automation API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the module_firmware GUI capabilities (#166) under `/api/v1/` with identical permissions, validation and audit.

**Architecture:** Read viewsets subclass `apps.api.read_views.AreaReadOnlyViewSet` with `area = "modules"`; write viewsets mix DRF mixins on top (pattern of `apps/api/write_views.py`). Validation runs the existing GUI Django forms inside the DRF serializers; side effects (audit, reconcile, import queueing, GitHub release rows) are extracted from the GUI views into `apps/module_firmware/ops.py` and called by both GUI and API. New API code lives in `apps/api/module_firmware_views.py` + `apps/api/module_firmware_serializers.py` (keeps the 1000-line `write_views.py` from growing).

**Tech Stack:** Django 6.0, DRF 3.17, drf-spectacular, django-filter, pytest.

**Spec:** `docs/superpowers/specs/2026-10-18-module-firmware-api-design.md`

## Global Constraints

- Permission area `modules` only: reads `modules.view`, every mutation/action (incl. `GET available/`) `modules.manage` via `@action(..., verb="manage")` / AreaPermission defaults. No topology scoping.
- Every mutation audits with the GUI's audit helpers; message ends with `apps.api.audit._token_suffix(request)` (" via API token <prefix>").
- No `fields="__all__"`; explicit field lists. Sensitive/compute fields read-only.
- Do NOT touch `station_agent/` or `apps/control/`.
- Tests: top-level `tests/test_api_module_firmware_*.py`; fixtures `api_topology`, `bearer`, `anon_client` (tests/conftest.py), `session` from `tests.test_api_read_fixtures`. Roles: admin, staff (pass manage) / region_mgr, station_user (MEMBER), applicant (fail manage; pass view).
- Run: `DJANGO_SETTINGS_MODULE=config.settings.test DJANGO_SECRET_KEY=x .venv/bin/python -m pytest -q -p no:cacheprovider <paths>`; lint `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- Django templates: no multi-line `{# #}`.
- GitHub calls are always monkeypatched in tests (`apps.images.github_releases.fetch_releases`).

## Review Focus

- Archived release passed as `release` to target create → 400 on `release` (form choices exclude archived), but PATCH of a target whose current release is archived without sending `release` must still succeed (test in Task 6).
- `module_type` with untrusted repo owner on create/update → 400 on `firmware_repo`; PATCH changing `key` on an existing type → key unchanged (form disables it) (Task 3).
- `lifecycle_status: "deployed"` or `"ready"` with an open assignment → 400 on `lifecycle_status`, nothing audited (Task 4).
- Delete of a module type referenced only by an *archived* release → 409, row survives (Task 3).
- Import of a tag already queued → 409 and no second job; GitHub failure → 502 not 500 (Task 5).

---

### Task 1: Extract shared ops from GUI views (no behaviour change)

**Files:**
- Create: `apps/module_firmware/ops.py`
- Modify: `apps/module_firmware/views.py` (FirmwareImportView, GitHubFirmwareReleasesPartialView, `_audit_release`, Archive/Restore views), `apps/module_firmware/views_targets.py` (create/update/delete side effects, `target_details`, `_target_label`), `apps/module_firmware/services.py` (`audit_suffix` kwarg)
- Test: `tests/module_firmware/test_ops.py` (+ existing GUI tests must stay green unchanged)

**Interfaces (Produces):**
- `services.confirm_registration(module, *, user, audit_suffix: str = "")`, `services.set_lifecycle(module, status, *, user, audit_suffix: str = "")`, `services.release_quarantine(module, *, user, ip_address=None, audit_suffix: str = "")` — suffix appended to the audit message; the two first also gain `ip_address=None` passed to `StationAuditLog.log`.
- `ops.ACTIVE_JOB_STATUSES` (moved from views `_ACTIVE_JOB_STATUSES`).
- `ops.target_details(target) -> dict`, `ops.target_label(target) -> str` (moved; views_targets re-imports them).
- `ops.after_target_created(request, target, *, via="") -> None` — config audit + station-scope audit + `reconcile_module_type`.
- `ops.after_target_updated(request, before, target, *, via="") -> bool` — computes changes from `target_details(before)` vs `target_details(target)`; returns False (no audit/reconcile) when nothing changed; else audits exactly as the GUI does now (incl. FIRMWARE_TARGET_REMOVED for the old station) and reconciles new + old type.
- `ops.after_target_deleted(request, target, details, label, *, via="") -> None`.
- `ops.archive_release(request, release, *, via="") -> bool` / `ops.restore_release(request, release, *, via="") -> bool` — call model `archive()`/`restore()`; on True audit (`obj="module_firmware_release"`, as `_audit_release` now) + `reconcile_module_type`; return the bool.
- `class ImportRejected(Exception)` with attr `field: str` and `code: str` (`"not_ready" | "invalid_tag"`).
- `ops.queue_import(request, module_type_id, tag, *, via="") -> tuple[ModuleFirmwareImportJob | None, bool]` — the body of `FirmwareImportView.post` inside `transaction.atomic()` with `select_for_update` on the type; raises `ModuleType.DoesNotExist`, `ImportRejected(field="tag", code="invalid_tag")`, `ImportRejected(field="module_type", code="not_ready")` (message = joined blockers); returns `(job, True)` when created (and audits `obj="module_firmware_import"`), `(existing_active_job, False)` otherwise.
- `ops.github_release_rows(module_type) -> list[dict]` — rows exactly as built today in `GitHubFirmwareReleasesPartialView` (`{"release", "state", "variants", "missing_variants"}`); raises `github_releases.GitHubAPIError`. Caller checks `import_blockers` first.
- Every `via` string is appended verbatim to each audit message produced.

- [ ] **Step 1:** Write `tests/module_firmware/test_ops.py`: `test_queue_import_creates_then_dedups` (second call returns `(same_job, False)`, one audit row), `test_queue_import_rejects_invalid_tag` (`ImportRejected.field == "tag"`), `test_queue_import_rejects_not_ready_type` (`field == "module_type"`), `test_archive_release_idempotent_audits_once`, `test_after_target_updated_noop_returns_false`, `test_via_suffix_on_audit_messages` (archive with `via=" via API token abc"` → AccountAuditLog message endswith it), `test_release_quarantine_audit_suffix`.
- [ ] **Step 2:** Run → FAIL (ImportError).
- [ ] **Step 3:** Implement `ops.py`, rewire GUI views to call it (GUI messages/redirects unchanged), add service kwargs.
- [ ] **Step 4:** Run `tests/module_firmware/` → all PASS (existing GUI tests unchanged).
- [ ] **Step 5:** Commit `refactor(module_firmware): extract shared ops for GUI + API`.

### Task 2: Read API (5 resources)

**Files:**
- Create: `apps/api/module_firmware_serializers.py`, `apps/api/module_firmware_views.py`
- Modify: `apps/api/router.py`, `apps/api/read_filters.py`
- Test: `tests/test_api_module_firmware_read.py`

**Interfaces (Produces):** viewset classes `ModuleTypeViewSet`, `ModuleViewSet`, `ModuleFirmwareReleaseViewSet`, `ModuleFirmwareImportJobViewSet`, `ModuleFirmwareTargetViewSet` (all `area = "modules"`, read-only in this task) registered as `module-types` / `modules` / `module-firmware-releases` / `module-firmware-import-jobs` / `module-firmware-targets` with basenames `module-type`, `module`, `module-firmware-release`, `module-firmware-import-job`, `module-firmware-target`. Read serializers `ModuleTypeSerializer`, `ModuleSerializer`, `ModuleFirmwareReleaseSerializer`, `ModuleFirmwareImportJobSerializer`, `ModuleFirmwareTargetSerializer`.

Fields:
- ModuleType: `id, key, display_name, hw_repo, firmware_repo, release_asset_prefix, import_blockers (list[str], from trust.import_blockers), module_count, release_count, target_count, created_at, updated_at`.
- Module: `id, uid, uid_source, module_type (id), module_type_key, variant, lifecycle_status, registration_status, last_reported_version, firmware_convergence, first_seen, last_seen, notes, assignment ({station, station_name, slot, since} | null — open assignment), target_version (effective target version | null), has_release (bool), quarantine ({target_version, attempts, last_error_mode, last_error_message, updated_at} | null)` — target/has_release/quarantine via `views_convergence.firmware_row` with the same prefetches as `ConvergenceOverviewView`.
- Release: `id, module_type, module_type_key, variant, version, sha256, size_bytes, source_repo, source_tag, source_github_url, imported_at, imported_by (username|null), archived_at`. No `storage_key`/`cosign_bundle_key`.
- ImportJob: `id, module_type, source_repo, tag, status, error_message, release, requested_by (username|null), created_at, completed_at`.
- Target: `id, module_type, module_type_key, version, scope, tag, station, canary_tag, variant_coverage (sorted list of variants with an active release at that version, "—" for blank), created_by (username|null), created_at, updated_at`.

Filters (django-filter FilterSets in read_filters.py): modules `module_type`, `module_type_key`, `lifecycle_status`, `registration_status`, `uid_source`, `firmware_convergence`, `station` (current open assignment, `.distinct()`), `uid`; search on `uid`. Releases `module_type`, `variant`, `include_archived` (bool; true → `all_objects`). Import jobs `module_type`, `status`. Targets `module_type`, `scope`, `station`, `tag`.

- [ ] **Step 1:** Tests: `test_read_matrix` parametrized over the 5 list routes × roles (admin/staff/region_mgr/station_user → 200, applicant via `session` → 200, anon → 401/403); `test_release_list_hides_archived_unless_include_archived`; `test_release_payload_has_no_storage_keys`; `test_module_detail_exposes_assignment_target_and_quarantine` (module quarantined for current target → `quarantine.target_version` set); `test_module_filter_station_uses_open_assignment`; `test_unknown_id_404`.
- [ ] **Step 2:** Run → FAIL (NoReverseMatch).
- [ ] **Step 3:** Implement serializers, viewsets, filters, router registrations.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(api): read endpoints for module firmware resources`.

### Task 3: ModuleType writes

**Files:** Modify `apps/api/module_firmware_views.py`, `apps/api/module_firmware_serializers.py`; Test `tests/test_api_module_firmware_types.py`.

**Interfaces:** `ModuleTypeWriteSerializer` (fields `key, display_name, hw_repo, firmware_repo, release_asset_prefix`; `validate()` binds `forms.ModuleTypeForm(data=merged, instance=self.instance)` — merged = instance values overlaid with input for PATCH — and maps `form.errors` to a DRF `ValidationError` keyed by field (`__all__` → `non_field_errors`); `create/update` save via the form). Delete uses `views_types.type_in_use`; in use or `ProtectedError` → 409 `{"detail": ...}`. Audits = `audit_config(obj="module_type", action=created|updated|deleted, details as GUI)` with message + suffix; update with no change → no audit. Responses use the read serializer.

- [ ] **Step 1:** Tests: create as staff → 201 + one CONFIG_CHANGED audit whose message endswith `via API token <prefix>`; member/applicant/anon create → 403/403/401-or-403; untrusted `firmware_repo` → 400 with `firmware_repo` key (set `settings.MODULE_FIRMWARE_TRUSTED_REPO_OWNERS=["OE5XRX"]`); bad `release_asset_prefix` → 400; PATCH `key` ignored; PATCH no-op → no audit row; delete unreferenced → 204 + audit; delete with archived release → 409 and type exists.
- [ ] **Step 2–4:** fail → implement (`CreateModelMixin, UpdateModelMixin, DestroyModelMixin`) → pass.
- [ ] **Step 5:** Commit `feat(api): module type create/update/delete`.

### Task 4: Module mutations

**Files:** same modules; Test `tests/test_api_module_firmware_modules.py`.

**Interfaces:** `ModuleUpdateSerializer` fields `notes`, `lifecycle_status` (ChoiceField of `services.OPERATOR_LIFECYCLE_VALUES`), PATCH only (PUT → 405 via `http_method_names` or explicit 405). `lifecycle_status` change → `services.set_lifecycle(..., audit_suffix=suffix, ip_address=client_ip)`; `ValueError` → 400 `{"lifecycle_status": [...]}` and no other field applied (atomic). `notes` change → save + `audit_station(event_type=StationAuditLog.EventType.UPDATED, module=m, station=<open assignment station or None>, changes={"notes": {"old","new"}})`. Actions (`detail=True, methods=["post"], verb="manage"`): `confirm-registration` → `services.confirm_registration`; `release-quarantine` → `services.release_quarantine`, response `{"released_targets": [...], "module": <ModuleSerializer>}` (200 even when empty). No create/delete (405).

- [ ] **Step 1:** Tests: matrix (staff/admin pass, member/applicant 403, anon 401/403) for PATCH and both actions; `deployed` → 400; `ready` with open assignment → 400, unchanged; notes PATCH audited with suffix; confirm idempotent (second call no new audit); release-quarantine on a quarantined module flips state to UPDATING, audit MODULE_QUARANTINE_RELEASED with suffix; on non-quarantined → `released_targets == []`, no audit; DELETE/POST list → 405.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5:** Commit `feat(api): module notes/lifecycle/registration/quarantine actions`.

### Task 5: Release archive/restore/available/import

**Files:** same modules; Test `tests/test_api_module_firmware_releases.py`.

**Interfaces:** Actions on `ModuleFirmwareReleaseViewSet`, all `verb="manage"`: `archive`/`restore` (detail, POST; lookup via `all_objects`; call `ops.archive_release/restore_release(via=suffix)`; 200 with read serializer, idempotent). `available` (list, GET, `?module_type=<id>` required → 400 if missing/non-int; 404 unknown type; blockers → 400 `{"module_type": blockers}`; `GitHubAPIError` → 502) returns `[{tag, html_url, is_latest, state, variants, missing_variants}]` from `ops.github_release_rows`. `import` (list, POST, `url_name="import"`, body `ModuleFirmwareImportInputSerializer{module_type: int, tag: str}`): resolve rows via `ops.github_release_rows` (502 on GitHub error); tag absent → 400 `tag`; state `invalid_tag`/`no_assets` → 400 `tag`; `queued`/`imported` → 409; `ready` → `ops.queue_import(via=suffix)`, if `created` 202 with import-job serializer else 409. `ImportRejected` → 400 on `.field`; unknown type → 400 `module_type`. All `extend_schema` annotated.

- [ ] **Step 1:** Tests: archive/restore idempotent + one audit each + reconcile called (monkeypatch `services.reconcile_module_type` / check via ops); member archive → 403 `required == "modules.manage"`; DELETE release → 405; available happy path (fake release objects with `tag, html_url, is_latest, asset_names`) incl. `missing_variants`; available without param → 400; GitHub error → 502; import ready → 202 + job + audit suffix; second import same tag → 409, one job; unknown tag → 400; untrusted type → 400 `module_type`; member import → 403.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5:** Commit `feat(api): module firmware release archive/restore/available/import`.

### Task 6: Target CRUD

**Files:** same modules; Test `tests/test_api_module_firmware_targets.py`.

**Interfaces:** `ModuleFirmwareTargetWriteSerializer` input fields `release` (PrimaryKeyRelatedField over `ModuleFirmwareRelease.all_objects`, write-only, optional on PATCH), `scope`, `tag`, `station`, `canary_tag` (PK fields, nullable). `validate()`: build form data `{"release": forms.release_value(rel.module_type_id, rel.version)}` (PATCH without release → current instance pair), scope/tag/station/canary_tag merged with instance values; bind `ModuleFirmwareTargetForm(data, instance=self.instance or ModuleFirmwareTarget())`; `form.errors` → field-keyed DRF errors (`__all__` → `non_field_errors`). Save via the form with `created_by=request.user` on create. Side effects: `ops.after_target_created/updated/deleted(via=suffix)`; update snapshot `before` re-fetched with select_related before save. Responses: read serializer (201 on create).

- [ ] **Step 1:** Tests: matrix create/update/delete (staff/admin pass, member/applicant 403, anon 401/403); create fleet target → 201, audit CONFIG_CHANGED with suffix, `reconcile_module_type` called; station target → additional StationAuditLog FIRMWARE_TARGET_SET; second fleet target same type → 400 `non_field_errors`; tag scope without tag → 400; canary on station scope → 400; archived release on create → 400 `release`; PATCH scope-only on target whose release got archived → 200; nonexistent release id → 400 `release`; PATCH no-op → no audit; delete → 204 + FIRMWARE_TARGET_REMOVED for station scope.
- [ ] **Step 2–4:** fail → implement → pass.
- [ ] **Step 5:** Commit `feat(api): module firmware target CRUD`.

### Task 7: OpenAPI + full suite

**Files:** Modify `tests/test_api_write_schema.py` (or new `tests/test_api_module_firmware_schema.py`).

- [ ] **Step 1:** Test asserts schema paths/methods: `/api/v1/module-types/` {get, post}, `/api/v1/module-types/{id}/` {get, put, patch, delete}, `/api/v1/modules/{id}/` {get, patch}, `/api/v1/modules/{id}/confirm-registration/` {post}, `/api/v1/modules/{id}/release-quarantine/` {post}, `/api/v1/module-firmware-releases/{id}/archive/` {post}, `…/restore/` {post}, `/api/v1/module-firmware-releases/available/` {get}, `/api/v1/module-firmware-releases/import/` {post}, `/api/v1/module-firmware-import-jobs/` {get}, `/api/v1/module-firmware-targets/` {get, post}, `/api/v1/module-firmware-targets/{id}/` {get, put, patch, delete}; and that `manage.py spectacular --validate --fail-on-warn` passes (if the existing suite does so; else only no new warnings for these views).
- [ ] **Step 2:** Full suite `python -m pytest -q` + ruff → green.
- [ ] **Step 3:** Commit `test(api): schema coverage for module firmware endpoints`.
