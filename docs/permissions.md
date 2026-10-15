# Berechtigungen

> Generiert mit `python manage.py permissions_table > docs/permissions.md` aus
> `apps/accounts/permissions.py`. Nicht von Hand bearbeiten — CI prüft Aktualität.

A = Applicant, M = Member, S = Staff, Ad = Admin, – = niemand.
Ein nicht definiertes niedrigeres Verb erbt die Stufe des nächsthöheren.

| Bereich | Name | view | use | manage |
|---|---|---|---|---|
| `dashboard` | Dashboard | A | – | – |
| `profile` | Eigenes Profil | A | A | – |
| `stations` | Stationen | A | S | S |
| `control` | Stationssteuerung | A | M | S |
| `terminal` | Terminal | Ad | Ad | – |
| `topology` | Regionen & Zuweisungen | A | Ad | Ad |
| `deployments` | Deployments | A | S | S |
| `rollouts` | Rollouts | A | Ad | Ad |
| `images` | Images | A | Ad | Ad |
| `provisioning` | Provisioning | Ad | Ad | Ad |
| `alerts` | Alarme | A | S | S |
| `alert_rules` | Alarm-Regeln | A | Ad | Ad |
| `modules` | Module & Firmware | A | S | S |
| `users` | Benutzer | M | Ad | Ad |
| `api_tokens` | API-Tokens | M | M | – |
| `audit` | Audit-Log | Ad | – | – |
| `sso` | SSO-Verwaltung | Ad | Ad | Ad |

**Zusatzregel (einzige zuweisungsbasierte):** Control-Override (Lock preempten)
dürfen `control.manage` sowie — sofern sie `control.use` haben — Station-Admins
der Station und Region-Manager ihrer Region (`can_override_station`).
