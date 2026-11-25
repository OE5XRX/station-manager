"""Retention engine: personal audit data after RETENTION_AUDIT_DAYS, IPs after RETENTION_IP_DAYS.

Spec: docs/superpowers/specs/2026-11-18-datenschutz-design.md ("Job für die Löschfristen").
Driven by ``manage.py enforce_retention`` (own compose service ``retention``).

- Without ``RETENTION_ENFORCE=true`` (or with ``--dry-run``) every step only counts.
- Every step is idempotent: a second enforce run reports 0 everywhere.
- Enforce works in batches of ``RETENTION_BATCH_SIZE`` rows, each batch in its own
  transaction (savepoint), so one failing batch never poisons the outer connection.
- Each step runs isolated: an exception is logged, the category lands in
  ``RetentionResult.errors`` and the remaining steps still run.
- ``held_user_ids()`` is the legal-hold hook (Löschsperre): rows of held users are
  skipped by **both** deadlines (365 d delete and 90 d IP clearing).
- SSTV: gallery pictures after ``SstvSettings.retention_days``, transmission
  artefacts (preview, TX audio) after 1 h; the stored files go with their rows
  (``apps.sstv.signals``).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.db.models import Q, TextField
from django.db.models.functions import Cast
from django.utils import timezone

from apps.audit.scrub import SCRUB_PREFILTER, scrub_message

logger = logging.getLogger(__name__)


# Station events that are a person's action even when ``user`` is NULL (e.g. the
# user was hard-purged → SET_NULL). Terminal use lives in TerminalSession instead.
PERSONAL_STATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "control_lock_acquired",
        "control_lock_released",
        "control_lock_transferred",
        "control_lock_preempted",
        "control_command",
        "control_ptt",
        "station_assignment_created",
        "station_assignment_revoked",
    }
)


def _q_pii(field_name: str) -> Q:
    """DB pre-selection: text in ``field_name`` that may contain an e-mail or IP.

    Tighter than SCRUB_PREFILTER (station audit is large and mostly system text):
    an IPv4 needs four dotted groups, an IPv6 is either compressed (``::``) or
    eight full groups. Portable regex (Python ``re`` on SQLite, ARE on Postgres).
    """
    return (
        Q(**{f"{field_name}__contains": "@"})
        | Q(**{f"{field_name}__regex": r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"})
        | Q(**{f"{field_name}__contains": "::"})
        | Q(**{f"{field_name}__regex": r"[0-9A-Fa-f]{1,4}(:[0-9A-Fa-f]{1,4}){7}"})
    )


@dataclass(frozen=True)
class RetentionConfig:
    now: datetime
    audit_days: int
    ip_days: int
    enforce: bool
    batch_size: int

    @property
    def audit_cutoff(self) -> datetime:
        return self.now - timedelta(days=self.audit_days)

    @property
    def ip_cutoff(self) -> datetime:
        return self.now - timedelta(days=self.ip_days)

    @classmethod
    def from_settings(
        cls, *, now: datetime | None = None, dry_run: bool = False
    ) -> "RetentionConfig":
        return cls(
            now=now or timezone.now(),
            # Values < 1 count as 1 day: never "delete everything immediately".
            audit_days=max(1, int(settings.RETENTION_AUDIT_DAYS)),
            ip_days=max(1, int(settings.RETENTION_IP_DAYS)),
            enforce=bool(settings.RETENTION_ENFORCE) and not dry_run,
            batch_size=max(1, int(settings.RETENTION_BATCH_SIZE)),
        )


@dataclass
class RetentionResult:
    enforce: bool
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def held_user_ids() -> frozenset[int]:
    """Legal-hold hook (Löschsperre, phase 2). Users returned here are skipped by
    every step (delete and IP clearing). Empty until the hold feature exists."""
    return frozenset()


def _exclude_held(qs, held, *fk_fields):
    """Drop rows whose ``fk_fields`` point at a held user (NULL FKs stay in)."""
    if not held:
        return qs
    for fk in fk_fields:
        qs = qs.exclude(**{f"{fk}__in": held})
    return qs


def _apply(qs, cfg: RetentionConfig, *, update: dict | None = None) -> int:
    """Count (dry-run) or delete/update ``qs`` in batches; returns affected rows.

    An ``update`` queryset must exclude its own results once updated (filter on
    "field still set"), otherwise this would loop forever.
    """
    if not cfg.enforce:
        return qs.count()
    model = qs.model
    total = 0
    while True:
        pks = list(qs.order_by().values_list("pk", flat=True)[: cfg.batch_size])
        if not pks:
            return total
        with transaction.atomic():
            batch = model.objects.filter(pk__in=pks)
            if update is None:
                batch.delete()
            else:
                batch.update(**update)
        total += len(pks)


def _scrub_value(value):
    """Recursively scrub every string inside a JSON-like value."""
    if isinstance(value, str):
        return scrub_message(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    return value


def _scrub_rows(qs, cfg: RetentionConfig, fields: tuple[str, ...]) -> int:
    """Scrub ``fields`` of the rows in ``qs`` in Python; count only changed rows.

    Keyset pagination over the pk, so rows that match the pre-filter but need no
    change are visited once per run and updates never disturb the iteration.
    """
    model = qs.model
    total = 0
    last_pk = None
    while True:
        page = qs.order_by("pk")
        if last_pk is not None:
            page = page.filter(pk__gt=last_pk)
        rows = list(page.only("pk", *fields)[: cfg.batch_size])
        if not rows:
            return total
        last_pk = rows[-1].pk
        changed = []
        for row in rows:
            dirty = False
            for name in fields:
                old = getattr(row, name)
                new = _scrub_value(old)
                if new != old:
                    setattr(row, name, new)
                    dirty = True
            if dirty:
                changed.append(row)
        total += len(changed)
        if cfg.enforce and changed:
            with transaction.atomic():
                model.objects.bulk_update(changed, list(fields), batch_size=cfg.batch_size)


# --- steps (each: (cfg, held) -> affected rows) -------------------------------------


def _station_audit_deleted(cfg, held):
    from apps.stations.models import StationAuditLog

    qs = StationAuditLog.objects.filter(created_at__lt=cfg.audit_cutoff).filter(
        Q(user__isnull=False) | Q(event_type__in=PERSONAL_STATION_EVENT_TYPES)
    )
    return _apply(_exclude_held(qs, held, "user"), cfg)


def _account_audit_deleted(cfg, held):
    from apps.accounts.models import AccountAuditLog

    qs = AccountAuditLog.objects.filter(created_at__lt=cfg.audit_cutoff)
    return _apply(_exclude_held(qs, held, "actor", "target_user"), cfg)


def _sso_audit_deleted(cfg, held):
    from apps.sso.models import SsoAuditLog

    qs = SsoAuditLog.objects.filter(created_at__lt=cfg.audit_cutoff)
    return _apply(_exclude_held(qs, held, "actor", "target_user"), cfg)


def _terminal_sessions_deleted(cfg, held):
    from apps.tunnel.models import TerminalSession

    qs = TerminalSession.objects.filter(
        Q(ended_at__lt=cfg.audit_cutoff)
        | Q(ended_at__isnull=True, started_at__lt=cfg.audit_cutoff)
    )
    return _apply(_exclude_held(qs, held, "user"), cfg)


def _station_audit_ip_cleared(cfg, held):
    from apps.stations.models import StationAuditLog

    qs = StationAuditLog.objects.filter(created_at__lt=cfg.ip_cutoff, ip_address__isnull=False)
    return _apply(_exclude_held(qs, held, "user"), cfg, update={"ip_address": None})


def _account_audit_ip_cleared(cfg, held):
    from apps.accounts.models import AccountAuditLog

    qs = AccountAuditLog.objects.filter(created_at__lt=cfg.ip_cutoff, ip_address__isnull=False)
    return _apply(
        _exclude_held(qs, held, "actor", "target_user"), cfg, update={"ip_address": None}
    )


def _sso_audit_ip_cleared(cfg, held):
    from apps.sso.models import SsoAuditLog

    qs = SsoAuditLog.objects.filter(created_at__lt=cfg.ip_cutoff, ip_address__isnull=False)
    return _apply(
        _exclude_held(qs, held, "actor", "target_user"), cfg, update={"ip_address": None}
    )


def _station_audit_ip_scrubbed(cfg, held):
    """IPs/e-mails in StationAuditLog free text + ``changes`` JSON after RETENTION_IP_DAYS.

    Covers the station IP-change heartbeat row ("IP address changed: a → b",
    ``user=None``) as well as any user row; held users are skipped via ``user``.
    """
    from apps.stations.models import StationAuditLog

    qs = (
        StationAuditLog.objects.filter(created_at__lt=cfg.ip_cutoff)
        .annotate(changes_text=Cast("changes", TextField()))
        .filter(_q_pii("message") | _q_pii("changes_text"))
    )
    return _scrub_rows(_exclude_held(qs, held, "user"), cfg, ("message", "changes"))


def _token_session_client_cleared(cfg, held):
    from apps.sso.models import TokenSession

    qs = TokenSession.objects.filter(issued_at__lt=cfg.ip_cutoff).filter(
        Q(ip_address__isnull=False) | ~Q(user_agent="") | ~Q(country_code="") | ~Q(city="")
    )
    return _apply(
        _exclude_held(qs, held, "user"),
        cfg,
        update={"ip_address": None, "user_agent": "", "country_code": "", "city": ""},
    )


def _account_token_ip_cleared(cfg, held):
    from apps.accounts.models import AccountToken

    qs = AccountToken.objects.filter(created_at__lt=cfg.ip_cutoff, ip_created__isnull=False)
    return _apply(_exclude_held(qs, held, "user"), cfg, update={"ip_created": None})


def _account_tokens_expired_deleted(cfg, held):
    from apps.accounts.models import AccountToken

    qs = AccountToken.objects.filter(expires_at__lt=cfg.now)
    return _apply(_exclude_held(qs, held, "user"), cfg)


def _token_sessions_pruned(cfg, held):
    from apps.sso.models import TokenSession

    return _apply(_exclude_held(TokenSession.objects.stale(cfg.now), held, "user"), cfg)


def _django_sessions_cleared(cfg, held):
    from django.contrib.sessions.models import Session

    count = Session.objects.filter(expire_date__lt=cfg.now).count()
    if cfg.enforce:
        call_command("clearsessions")
    return count


def _axes_logs_deleted(cfg, held):
    """django-axes login logs/attempts (IP, user agent, username) past the IP window.

    Lockouts last ``AXES_COOLOFF_TIME`` (hours), far below the 90-day window.
    Rows are keyed by username string, not a user FK, so no held-user filter.
    """
    from axes.models import AccessAttempt, AccessFailureLog, AccessLog

    return sum(
        _apply(model.objects.filter(attempt_time__lt=cfg.ip_cutoff), cfg)
        for model in (AccessLog, AccessFailureLog, AccessAttempt)
    )


def _oauth_tokens_cleared(cfg, held):
    """Expired OAuth grants/tokens — deliberately NOT via DOT's ``cleartokens``.

    ``cleartokens`` deletes every revoked refresh token at once, and
    ``TokenSession.refresh_token`` cascades: rotated/revoked sessions would vanish
    before their 30-day forensic window (``TokenSession.objects.stale``) and past a
    legal hold. A refresh token is therefore only removed once its session row is
    gone (``token_sessions_pruned`` runs earlier) and it is revoked or past its
    lifetime.
    """
    from oauth2_provider.models import AccessToken, Grant, IDToken, RefreshToken

    refresh_lifetime = timedelta(
        seconds=settings.OAUTH2_PROVIDER.get("REFRESH_TOKEN_EXPIRE_SECONDS", 14 * 24 * 3600)
    )
    querysets = (
        Grant.objects.filter(expires__lt=cfg.now),
        # Lifetime as DOT validates it: access_token.expires + lifetime.
        RefreshToken.objects.filter(sso_session__isnull=True).filter(
            Q(revoked__isnull=False)
            | Q(access_token__expires__lt=cfg.now - refresh_lifetime)
            | Q(access_token__isnull=True, created__lt=cfg.now - refresh_lifetime)
        ),
        # An access token still referenced by a refresh token must stay: the
        # refresh grant validates through RefreshToken.access_token (SET_NULL
        # would break every idle RP session), same rule as DOT's clear_expired.
        AccessToken.objects.filter(expires__lt=cfg.now, refresh_token__isnull=True),
        # Deleting an IDToken cascades to its AccessToken: only once that one is gone.
        IDToken.objects.filter(expires__lt=cfg.now, access_token__isnull=True),
    )
    return sum(_apply(_exclude_held(qs, held, "user"), cfg) for qs in querysets)


def _audit_messages_scrubbed(cfg, held):
    """Legacy e-mail/IP/UA/city fragments in Account/SSO audit messages.

    No held-user exception: the IP itself stays in ``ip_address`` for held users.
    """
    from apps.accounts.models import AccountAuditLog
    from apps.sso.models import SsoAuditLog

    return sum(
        _scrub_rows(model.objects.filter(SCRUB_PREFILTER), cfg, ("message",))
        for model in (AccountAuditLog, SsoAuditLog)
    )


# A transmission row (preview = user photo, TX audio) becomes eligible for deletion
# this long after creation and is removed by the next retention run (daily, and
# only with RETENTION_ENFORCE). Every state is covered, "done" included: the
# gallery picture of a sent transmission is its own blob (SstvImage.image), never
# the preview file.
SSTV_TRANSMISSION_MAX_AGE = timedelta(hours=1)
# A draft may be sent until it is 1 h old and a TX is hard-stopped after
# duration + 5 s (≤ ~275 s), so a "sending" row is only a leftover past 2 h —
# judged on created_at alone (updated_at misses QuerySet.update() transitions).
SSTV_SENDING_MAX_AGE = timedelta(hours=2)


def _sstv_pictures(cfg, held):
    """Gallery pictures past ``SstvSettings.retention_days`` (stored files via signal)."""
    from apps.sstv.models import SstvImage, SstvSettings

    days = max(1, int(SstvSettings.load().retention_days))
    qs = SstvImage.objects.filter(created_at__lt=cfg.now - timedelta(days=days))
    return _apply(_exclude_held(qs, held, "user"), cfg)


def _sstv_stale_drafts(cfg, held):
    """Previews/TX audio past their use (temporary artefacts, no hold): 1 h after
    creation, 2 h for a row still "sending"."""
    from apps.sstv.models import SstvTransmission

    sending = Q(state=SstvTransmission.State.SENDING)
    qs = SstvTransmission.objects.filter(
        (~sending & Q(created_at__lt=cfg.now - SSTV_TRANSMISSION_MAX_AGE))
        | (sending & Q(created_at__lt=cfg.now - SSTV_SENDING_MAX_AGE))
    )
    return _apply(qs, cfg)


def run_retention(cfg: RetentionConfig) -> RetentionResult:
    # Resolved at call time (module globals), so tests can monkeypatch single steps.
    steps = (
        ("station_audit_deleted", _station_audit_deleted),
        ("account_audit_deleted", _account_audit_deleted),
        ("sso_audit_deleted", _sso_audit_deleted),
        ("terminal_sessions_deleted", _terminal_sessions_deleted),
        ("station_audit_ip_cleared", _station_audit_ip_cleared),
        ("account_audit_ip_cleared", _account_audit_ip_cleared),
        ("sso_audit_ip_cleared", _sso_audit_ip_cleared),
        ("station_audit_ip_scrubbed", _station_audit_ip_scrubbed),
        ("token_session_client_cleared", _token_session_client_cleared),
        ("account_token_ip_cleared", _account_token_ip_cleared),
        ("account_tokens_expired_deleted", _account_tokens_expired_deleted),
        ("axes_logs_deleted", _axes_logs_deleted),
        ("token_sessions_pruned", _token_sessions_pruned),
        ("django_sessions_cleared", _django_sessions_cleared),
        ("oauth_tokens_cleared", _oauth_tokens_cleared),
        ("audit_messages_scrubbed", _audit_messages_scrubbed),
        ("sstv_pictures", _sstv_pictures),
        ("sstv_stale_drafts", _sstv_stale_drafts),
    )
    result = RetentionResult(enforce=cfg.enforce)
    held = held_user_ids()
    for key, step in steps:
        try:
            result.counts[key] = step(cfg, held)
        except Exception:
            logger.exception("retention step %s failed", key)
            result.errors.append(key)
    return result
