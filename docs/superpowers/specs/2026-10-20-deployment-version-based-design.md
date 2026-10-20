# Deployment: version-based mask + vocabulary — design

Follow-up to #169 (rollout groups). Status: approved by user 2026-10-20.

## Problem

`/deployments/create/` (and `POST /api/v1/deployments/`) pick **one
`ImageRelease`**, i.e. one `(tag, machine, channel)` row. A fleet or group
deployment therefore pushes a qemu image to Raspberry Pis (or vice versa) —
the form never checks the station's machine. The user decision from #169
("platform comes from the station, the type doesn't matter") was only applied
to the upgrade dashboard.

## Decisions

1. The operator picks a **version = (tag, channel)**, nothing machine-specific.
   For every target station the server resolves the `ImageRelease` with that
   tag + channel for the station's machine (`current_image_release.machine`,
   same platform source as `apps/rollouts/targeting.station_platform`).
2. Stations that cannot be served are **skipped with a reason**, never
   silently: `no_platform` (machine unknown, i.e. no current image),
   `no_image` (version not imported for that machine), `not_ota_ready`
   (rootfs artifact missing), `active_deployment` (mid-flight result blocks
   supersession). Skips are shown in a live form preview **and** persisted on
   the deployment (detail page, API).
3. The mask is the explicit path for channel switches and downgrades
   (#169 user decision 1). No channel restriction is enforced; the preview
   shows the station's group channel and flags *channel switch* and
   *downgrade* (via `targeting.is_newer`) per station.
4. The upgrade dashboard (station + group upgrade) creates its deployments
   through the **same service**. Group upgrade buckets stations by resolved
   target tag (mixed qemu+RPi with different latest tags → one deployment per
   tag, each restricted to its stations).
5. Agent protocol unchanged: `check`/`download`/`result`/`commit` under
   `/api/v1/deployments/` keep URLs and JSON fields (`deployment_result_id`,
   `target_tag`, `checksum_sha256`, `size_bytes`, `download_url`, …). They
   now read the image from the **result**, so each station gets its own
   machine's artifact. App/model/table names stay `deployments`.
6. User API create takes `image_tag` + `image_channel` (default `release`)
   instead of `image_release`; still station-targeted only. No compat layer.
   OpenAPI updated.

## Data model

`Deployment`
- `image_tag` CharField(64) — chosen version (snapshot).
- `image_channel` CharField(32, default `release`).
- `skipped_stations` JSONField(list): `[{"station_id", "station_name",
  "reason", "message"}]`.
- `image_release` FK **removed** (after backfill).

`DeploymentResult`
- `image_release` FK → `images.ImageRelease`, `PROTECT`,
  `related_name="deployment_results"`, non-null. This is what the agent gets.

Migrations (forward-only, must never abort; split so Postgres never mixes
data updates with schema alteration in one transaction):
1. `0007` add `image_tag`/`image_channel` (blank defaults), `skipped_stations`,
   nullable `DeploymentResult.image_release`.
2. `0008` RunPython backfill: `deployment.image_tag/channel ← image_release.tag/channel`;
   every result's `image_release ← deployment.image_release` (via
   `.update()` with a subquery — no per-row loop). Reverse = noop.
3. `0009` make `DeploymentResult.image_release` non-null, drop
   `Deployment.image_release`.

Every existing deployment has a non-null `image_release` (PROTECT, NOT NULL),
so the backfill is total; history stays readable (tag/channel on the
deployment, tag/machine per result).

## Service — `apps/deployments/services.py`

```python
SKIP_NO_PLATFORM, SKIP_NO_IMAGE, SKIP_NOT_OTA_READY, SKIP_ACTIVE

@dataclass(frozen=True)
class StationPlan:  # station, release | None, reason ("ok" | SKIP_*), message
    channel_switch: bool; downgrade: bool   # informational (preview)

def plan_deployment(tag, channel, stations) -> list[StationPlan]   # 1 query for releases
def available_versions() -> list[(channel, tag, [machines])]       # OTA-ready, non-archived
def create_deployment(*, tag, channel, target_type, target_group=None,
                      target_station=None, stations=None, user,
                      strategy=IMMEDIATE, phase_config=None, audit_message=None) -> Deployment
```

`create_deployment`: resolves `stations` (default `Deployment.get_target_stations()`
semantics), plans, opens one `transaction.atomic()`, locks the station rows
(`select_for_update`, deadlock note from `write_views.DeploymentViewSet`),
creates the deployment, one result per servable station (with its
`image_release`, `previous_version`), calls `supersede_pending_for_station`
per station (conflict → result removed, station recorded as
`SKIP_ACTIVE`), writes `skipped_stations`, status `IN_PROGRESS` if ≥1
result. Raises `NoEligibleStationsError(plans)` (rolls back) when nothing is
servable — callers render the reasons (form error / 400 / flash). Audit:
one `StationAuditLog` FIRMWARE_UPDATE per targeted station, deferred via
`on_commit` (existing `_defer_audit_log` pattern, moved to the service).

## UI

- `/deployments/create/`: `version` select (optgroup per channel, label
  `tag · machines`), target type / group / station, strategy, phase config.
  HTMX preview (`/deployments/create/preview/`, GET, same permission) renders a
  per-station table: station, machine, group+channel, resolved image or skip
  reason, badges *channel switch* / *downgrade*. Server-side validation is the
  same service, so preview == outcome. Skill `frontend-design`.
- Detail: header shows tag + channel badge + machines summary; results table
  gets an *Image* column (tag · machine); a *Skipped stations* panel when
  `skipped_stations` is non-empty.
- List / dashboard / station upgrade card / monitoring messages: use
  `image_tag` (deployment) or `result.image_release` (per station).

## Vocabulary (UI is English, no .po)

Provisioning = first image · Deployment = distribute a version to
station/group/fleet (up or down) · Upgrade Dashboard = convenience view ·
Rollout = only rollout group / rollout sequence. Changes are listed in the PR.

## Non-goals

No agent / linux-image change. No API group/fleet create. No phased-strategy
execution (still stored only). No change to provisioning or image import.

## Testing

Migration backfill (old rows → result image + tag/channel), service plan
matrix (ok / no_platform / no_image / not_ota_ready / active / channel switch /
downgrade), mixed-machine fleet deployment → agent `check` + `download` per
station return that station's artifact, `commit` compares the result's tag,
form + preview, upgrade views via service (mixed group → per-tag buckets),
API create 201/400/403 with `image_tag`/`image_channel`, OpenAPI schema.
