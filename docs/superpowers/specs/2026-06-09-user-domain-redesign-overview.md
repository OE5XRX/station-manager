# User-Domain-Redesign — Overview

**Status:** Master-Design, brainstormed 2026-06-09, aufgeteilt in Sub-Specs am 2026-06-12.
**Rolle dieses Dokuments:** Architektur-Übersicht + Audience-Modell + Design-Entscheidungen + Roadmap. **Nicht** der Implementation-Leitfaden — die konkreten Sektionen leben in den drei Sub-Specs (siehe Sektion 5).

Dieses Overview-Dokument hält die Gesamt-Architektur konsistent. Wer einen Sub-Spec implementiert, liest zuerst das Overview, dann den jeweiligen Sub-Spec.

---

## 1. Ziel und Motivation

Die User-Verwaltung im station-manager strukturell überarbeiten und gleichzeitig zu einem **Mitgliederverzeichnis** öffnen.

Heute besteht sie aus einer Admin-only Liste und einem überladenen Edit-Form, das de facto die zentrale User-Management-Surface ist — ohne dass das aus dem URL- oder Template-Namen ersichtlich wäre. Mitglieder selbst haben keinen Einblick in andere Mitglieder.

Dieses Redesign:

- Führt eine echte Detail-Seite ein (audience-aware Tabs).
- Reduziert das Edit-Form auf Identity + neue Profil-Felder.
- Baut List/Create/Delete-Templates mobil-tauglich um.
- Schließt die Audit-Lücken bei den Identity-CRUD-Operationen.
- Macht die List- + Detail-Seite zu einem **Mitgliederverzeichnis** für Vereins-Mitglieder (Membership-Level ≥ MEMBER).
- Erweitert das User-Modell um Kontakt- und Standortdaten (Adresse, Telefon, Locator + lat/lon, Avatar, Bio, QTH-Name, QRZ-URL).

Der Redesign folgt dem etablierten Pattern aus `station_detail.html` (Tabs + Cards + Summary-Bar).

---

## 2. Architektur-Bausteine

### 2.1 Schema-Erweiterung (10 neue Felder am User-Modell)

| Feld | Typ | Zweck |
|---|---|---|
| `bio` | `TextField(max_length=500, blank=True)` | Selbst-Description |
| `avatar` | `ImageField(upload_to="avatars/", null=True, blank=True)` | Profilbild |
| `qth_name` | `CharField(max_length=128, blank=True)` | HAM-Standortname |
| `qrz_url` | `URLField(max_length=200, blank=True)` | QRZ-Profil-Link |
| `address` | `TextField(blank=True)` | Postadresse als Freitext |
| `phone` | `CharField(max_length=32, blank=True)` | Telefon |
| `latitude` | `DecimalField(9, 6, null=True, blank=True)` | Aus Geocoding |
| `longitude` | `DecimalField(9, 6, null=True, blank=True)` | Aus Geocoding |
| `locator` | `CharField(max_length=6, blank=True)` | Maidenhead 6-char |
| `is_directory_visible` | `BooleanField(default=True)` | Master-Switch für Sichtbarkeit |

Konvention: `username` trägt im Verein das **Rufzeichen** (Callsign). Kein Schema-Change.

### 2.2 Audience-Modell (vier Sichtbarkeitsstufen)

| Audience | Sieht List | Sieht Detail | Sichtbare Felder im Detail |
|---|---|---|---|
| **Admin** | ✓ inkl. Applicants | ✓ alle User | alle Felder |
| **Self** | ✓ ohne Applicants | ✓ eigene Detail-Seite | alle eigenen Felder (read-only) |
| **Member** (≥ MEMBER) | ✓ ohne Applicants | ✓ andere Mitglieder | nur „öffentliche" Felder |
| **Applicant** | ✗ | ✓ nur eigene Detail-Seite | alle eigenen Felder (read-only) |

`is_directory_visible=False` reduziert die Member-Sicht auf Callsign + Membership-Pill + Avatar.

### 2.3 Field-Visibility-Matrix (System-Defaults)

| Feld | Other Member | Self | Admin |
|---|:---:|:---:|:---:|
| Callsign (username), Membership-Pill, Avatar | ✓ | ✓ | ✓ |
| First+Last Name, Email, Bio, QTH-Name, Locator, QRZ-URL | ✓ | ✓ | ✓ |
| Region/Station-Assignments, „Mitglied seit YYYY" | ✓ | ✓ | ✓ |
| Adresse, Telefon, lat/lon-Zahlen, Language | ✗ | ✓ | ✓ |
| is_active, last_login | ✗ | ✓ (eigene) | ✓ |
| SSO Grants/Sessions/Tags, globale Audit | ✗ | ✗ | ✓ |
| Eigene Audit-Einsicht | — | ✓ (DSGVO) | ✓ |

Privacy-Modell: System-Defaults pro Feld + ein einziger Master-Switch `is_directory_visible` pro User. Kein Per-Feld-Opt-out durch den User (Modell A + Master-Switch).

### 2.4 Audit-Erweiterung

Heute geloggt: Membership-Change, Region-Assignment, Region-CRUD, SSO-Events, Station-Assignment (auf Station-Subject-Seite). Lücke: Identity-CRUD und Station-Assignments aus User-Subject-Sicht.

Neue EventTypes in `AccountAuditLog.EventType`:

```
USER_CREATED, USER_UPDATED, USER_DELETED, USER_ACTIVATED, USER_DEACTIVATED,
PASSWORD_CHANGED,
STATION_ASSIGNMENT_CREATED, STATION_ASSIGNMENT_REVOKED
```

Emission im `form_valid` der jeweiligen Views + Doppel-Emit im StationAssignment-Signal (zusätzlich zum bestehenden `StationAuditLog`).

### 2.5 Tracked-Fields für USER_UPDATED

```
TRACKED_USER_FIELDS = {
    "username", "email", "first_name", "last_name", "language",
    "bio", "avatar", "qth_name", "qrz_url", "phone",
    "address", "locator", "is_directory_visible",
}
```

`latitude`/`longitude` werden **nicht** getrackt — sie sind aus `address` abgeleitet.

### 2.6 Geocoding + Locator

- Provider: Nominatim/OpenStreetMap, Free-Tier, kein API-Key.
- User-Agent + Rate-Limit (1 req/s) verpflichtend.
- Pure-Python Maidenhead-Locator-Berechnung aus lat/lon.
- Synchron im `form_valid` getriggert wenn `address` ändert.
- Fail closed: bei Geocoding-Failure bleiben lat/lon/locator unverändert, User kann manuell setzen.

### 2.7 Avatar-Upload

- ImageField, Pillow-Resize auf max 512×512, JPEG-Reencode mit quality=85, max 2 MB.
- Fallback: bestehendes `sb-avatar` Buchstaben-Pattern.
- Immer Member-sichtbar (auch bei `is_directory_visible=False`).

---

## 3. Routing — Soll-Stand

```
GET  users/                         UserListView           audience-aware
GET  users/create/                  UserCreateView         Admin
GET  users/<pk>/                    UserDetailView         audience-aware
GET  users/<pk>/edit/               UserUpdateView         Admin, Identity + Profil
GET  users/<pk>/delete/             UserDeleteView         Admin
GET  accounts/profile/              ProfileView            Self, 4 Forms
POST accounts/profile/password/     ProfilePasswordChangeView   Self

# HTMX-Endpoints unverändert:
POST users/<pk>/membership/         MembershipSetView
POST users/<pk>/region_assignments/ RegionAssignmentCreate
POST region_assignments/<pk>/revoke/RegionAssignmentRevoke
POST users/<pk>/station_assignments/StationAssignmentCreate
POST station_assignments/<pk>/revoke/StationAssignmentRevoke
POST sso/users/<pk>/tag/<gpk>/      (bestehend)
POST sso/users/<pk>/app/<apk>/      (bestehend)
POST sso/sessions/<pk>/revoke/      (bestehend)
```

---

## 4. Implementation-Reihenfolge (PR-Übersicht)

Aufgeteilt in **drei sequenzielle PRs**, je auf eigenem Feature-Branch und mit eigenem Spec:

```
                ┌─────────────────────┐
                │ 1a — Foundation     │  ← Phase 1
                │ (Backend / Pure)    │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ 1b — Member-Directory│ ← Phase 2
                │ (Browse-Surface)    │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ 1c — Self-Service   │  ← Phase 3
                │ (Edit/Profile)      │
                └─────────────────────┘
```

**Empfohlen sequenziell** (1a → 1b → 1c). 1a ist Backend-only und muss zuerst. 1b und 1c berühren beide `apps/accounts/views.py` (UserUpdateView), `apps/accounts/urls.py`, und `apps/accounts/templates/accounts/user_form.html` — paralleler Branch-Run ist machbar, aber Merge-Conflicts wären zu erwarten. 1b zuerst hat den Bonus, dass der Self-Edit-Redirect aus 1c ein Ziel (UserDetailView) findet.

Wenn paralleler Run gewünscht: 1b und 1c müssen koordiniert werden — am einfachsten, indem 1c einen extra Rebase nach 1b-Merge macht.

---

## 5. Sub-Specs

### Sub-Spec 1a — Foundation
**Datei:** `2026-06-12-user-domain-1a-foundation-design.md`
**Scope:** Backend-only, kein UI. Schema-Migration + Audit-EventTypes + Visibility-Helper + Geocoding-Service + Avatar-Upload-Pipeline.
**Surface:** Pure Python + Migration + signals.py. Keine Template-Änderungen.
**Build-Phasen:** 5 (parallelisierbar in Round-1).
**Branch:** `feat/user-domain-1a-foundation`.

### Sub-Spec 1b — Member-Directory
**Datei:** `2026-06-12-user-domain-1b-directory-design.md`
**Scope:** Browse-Surface. UserDetailView (audience-aware, Tabs), UserListView (audience-aware, Filter), Card-Migration aus user_form.html in user_detail.html, Audit-Tab + Global-Filter, Mobile-Polish.
**Surface:** Backend + Templates für Read-Seite.
**Build-Phasen:** 4-5.
**Branch:** `feat/user-domain-1b-directory`.

### Sub-Spec 1c — Self-Service
**Datei:** `2026-06-12-user-domain-1c-self-service-design.md`
**Scope:** Write-Surface. UserUpdateForm-Erweiterung mit neuen Feldern, ProfileView komplett-Umbau (4 Forms: Identity / Profil / Adresse / Passwort), Password-Change-Endpoint, Onboarding-Empty-State, Delete-Confirm Impact-Anzeige.
**Surface:** Backend + Templates für Write-Seite.
**Build-Phasen:** 3-4.
**Branch:** `feat/user-domain-1c-self-service`.

---

## 6. Followup-Specs nach diesem Bogen

Drei zusammenhängende Themen sind bewusst ausgeklammert:

### Spec #2 — „Account Lifecycle"
Welcome-Email-getriebener Aufnahme-Flow, Self-Service-PW-Reset, Email-Change-Verification, Soft-Delete. Direkt nach 1a+1b+1c.

Grobe Sektionen: `AuthToken`-Modell, Email-Infrastruktur, Welcome+Setup-Flow (Admin tippt Identity → System sendet Welcome-Email mit Setup-Link → User klickt Link und setzt erstes Passwort, ersetzt UserCreateView's Password-Felder), Password-Reset-Flow, Email-Change-Verification, Soft-Delete-Pattern mit Manager/Restore, UserCreateView- und UserDeleteView-Refactor.

### Spec #3 — „User-und-Station-Map"
Leaflet-Visualisierung. Locator+lat/lon-Foundation wird in 1a gelegt. Kann parallel zu Spec #2 starten.

### Reihenfolge der Realisierung

1. **1a + 1b + 1c** (dieser Bogen, in dieser Reihenfolge mit 1b/1c parallel).
2. **Spec #2 „Account Lifecycle"** direkt danach.
3. **Spec #3 „User-und-Station-Map"** kann parallel zu Spec #2 starten.

---

## 7. Out-of-Scope (bezogen auf diesen Gesamt-Bogen 1a+1b+1c)

Themen mit geplanten Folge-Specs:

- Welcome-Email + Setup-Token-Flow → Spec #2
- Password-Reset-Flow (forgot-password) → Spec #2
- Email-Change-Verification → Spec #2
- Soft-Delete + Restore → Spec #2
- User-und-Station-Map → Spec #3
- Aufnahme-Workflow für Applicants → eigener Spec, hängt an Spec #2

Themen ohne aktuellen Folge-Spec:

- 2FA / TOTP
- User-Bulk-Operationen
- Mehrfach-Membership-Levels
- Audit-Filter-Bar auf dem Per-User-Audit-Tab
- Membership-Level-Selector im Create-Form (bleibt 2-Schritt: APPLICANT, dann Promote)
- Per-Feld-Privacy-Switch durch User (Modell B)
- Avatar-Lightbox
- Geocoding-Background-Job (Celery)
- Orphaned-Avatar-Cleanup-Job
- Operating-Modes + Bänder-Multi-Select
- Lizenzklasse-Feld
- Activity-Heatmap / Login-Frequenz-Visualisierung

---

## 8. Design-Entscheidungen (Audit-Trail der Brainstorming-Phase)

Festgehalten als Referenz für Implementer und für künftige Reviews:

1. **Detail-Layout:** Tabs wie `station_detail.html` (statt flacher Sektionen oder Two-Pane-Grid).
2. **Edit-Flow:** Separate Edit-Page (statt Inline-Edit oder Modal).
3. **Audience-Scope auf Detail-Audit-Tab:** Account + SSO + Station-Assignments, plus Self-Sichtbarkeit.
4. **Create-Form:** Bleibt 2-Schritt — Applicant default, Promote auf Detail.
5. **Self-Detail für Nicht-Admins:** Ja, jeder sieht eigene Detail-Seite.
6. **Field-Set neue Felder:** Adresse + Telefon + Locator + lat/lon (Pflicht), Avatar, Bio (stark empfohlen). Radio-spezifisch: nur QTH + QRZ-URL (minimal).
7. **Geocoding:** Server-seitig via Nominatim/OSM (statt manueller Locator-Input oder Browser-Geolocation).
8. **Privacy-Modell:** Modell A + Master-Switch `is_directory_visible` (statt reinem Modell A oder per-Field-Opt-out).
9. **Applicant-Filter in List:** Admin sieht Applicants immer, Member nie. Kein Toggle.
10. **Real-Name + Email für Member sichtbar:** Ja (Standard-Vereinsverzeichnis).
11. **Adresse + Telefon für Member:** Beides admin-only.
12. **Topology-Assignments + „Mitglied seit Jahr":** Beides für Member sichtbar.
13. **Password-Change Self-Service:** Ja, als eigenes Panel auf Profile-Page.
14. **Onboarding-Empty-State:** Ja, dezenter Hinweis pro leerer Sektion.
15. **Bevorzugte Kontakt-Methode als Feld:** Nein, skip.
16. **Aufnahme-Flow / Soft-Delete / Email-Verification:** Eigener Folge-Spec (Spec #2 „Account Lifecycle"), nicht in diesen Bogen.
17. **UserCreateView-Übergang bis Spec #2:** Behält Password-Felder wie heute.
18. **Spec-Aufteilung:** 3 Sub-Specs (Foundation / Member-Directory / Self-Service) — Option γ.
19. **Doku-Struktur:** Master-Overview-Doc behalten + 3 vollständige Sub-Specs jetzt extrahieren.
