# Live-Status-Pills: übersetzte Labels aus dem WS-Payload

**Problem:** Die per WebSocket aktualisierte Pill im Upgrade-Dashboard zeigte den rohen Code (`in_progress`).

**Entscheidung:** Der Server liefert das Label. `DeploymentStatusConsumer.deployment_update` läuft unter der
User-Sprache (`AreaConsumerMixin.dispatch`, #224) und ergänzt `status_label` (Deployment) und
`result.status_label` (DeploymentResult) aus den Choices der Models. Der Broadcast selbst (sync, in der
Agent-Request-Sprache) bleibt sprachneutral, daher Übersetzung pro Listener im Consumer.
JS-Mapping per `gettext()` wäre eine zweite Label-Liste gewesen (genau das war `STATUS_LABELS` in `app.js`).

- `status` (Maschinen-Code) bleibt unverändert; unbekannte Codes → Label = Code; fehlendes Feld (alter Server) → JS-Fallback auf den Code.
- `upgrade_dashboard.html` und `app.js` (Deployment-Detail/-Liste) nutzen `status_label`; die Deployment-Einträge aus `STATUS_LABELS` entfallen (Station-Stati bleiben, eigener Consumer).
- Tests: `tests/test_permission_ws_feeds.py` (EN + DE, Fallback).
