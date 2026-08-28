# Audit-Notizen — station-manager Web-Service

**Stand:** 2026-05-18, basierend auf Commit `f517ac5` (main).
**Kontext:** Schneller Walk-Through der Codebase. Sticky Points zum späteren Aufgreifen — nicht priorisiert, nicht verifiziert ob's wirklich Bugs sind.

---

## 1. Tunnel-Consumer benutzt String statt Enum

**Datei:** `apps/tunnel/consumers.py`
**Stellen:** Im `TerminalConsumer.connect()` und `disconnect()` — die Aufrufe von `self._audit_log(station, "updated", ...)`.

`StationAuditLog.EventType` hat den Wert `"updated"`, der Code übergibt aber den String-Literal. Funktioniert, ist aber inkonsistent mit dem Rest der Codebase (jedes andere Audit-Log-Call-Site nutzt `StationAuditLog.EventType.XXX`). Mehr noch: Es gibt eigentlich keinen passenden EventType für "Terminal Session Opened/Closed" — das sollte vermutlich `TERMINAL_OPENED` / `TERMINAL_CLOSED` als neue EventType-Werte werden, statt das generische `UPDATED` zu missbrauchen.

**Fix:**
1. Zwei neue EventType-Choices in `apps/stations/models.py` (`TERMINAL_OPENED`, `TERMINAL_CLOSED`) + Migration.
2. Tunnel-Consumer auf die neuen Enum-Werte umstellen.
3. Bonus: Audit-Log-Filter im Audit-Tab gewinnt automatisch zwei neue Kategorien.

---

## 2. Toter Code: `Station.update_from_heartbeat()` vs. HeartbeatView-Logik

**Dateien:** `apps/stations/models.py` (Methode `update_from_heartbeat`) und `apps/api/views.py` (`HeartbeatView.post`).

`Station.update_from_heartbeat(data)` ist im Model definiert und macht im Wesentlichen dasselbe wie `HeartbeatView.post` — `status`, `last_seen`, `current_os_version` etc. setzen + speichern. Die View benutzt die Methode aber nicht, sondern dupliziert die Save-Logik inline (vermutlich weil sie zusätzlich Audit-Logs für Status/OS/IP-Änderungen schreibt und broadcasted, was die Modell-Methode nicht tut).

**Optionen:**
- (a) `Station.update_from_heartbeat` löschen — wird nirgends aufgerufen (mit `Grep` verifizieren), dann ist's einfach toter Code.
- (b) `update_from_heartbeat` zur kanonischen Methode machen und die View darauf umstellen. Würde bedeuten: Methode gibt das `(old_status, old_os, old_ip)`-Tuple zurück oder nimmt einen `changes`-Callback, damit Audit-Logging extern bleibt. Saubereres Layering, aber mehr Arbeit.

Empfehlung: erst `Grep`en ob die Methode wirklich tot ist — sonst Variante (b).

---

## 3. DeviceKey-Auth: kein Index auf `(station_id, is_active)`

**Datei:** `apps/api/authentication.py`, `apps/api/models.py`.

```python
device_key = DeviceKey.objects.select_related("station").get(
    station_id=station_id, is_active=True
)
```

`DeviceKey.station` ist ein `OneToOneField` (= unique Index auf `station_id`), `is_active` hat keinen Index. Bei 100 Stationen reicht der OneToOne-Lookup völlig — die Query trifft eine Zeile per Index-Hit. **Kein akuter Bug**, aber wenn das Fleet wächst oder DeviceKey jemals zu ForeignKey wird (etwa für Historisierung), dann fehlt der Index.

Notiz für später: bei ForeignKey-Refactor einen `models.Index(fields=["station", "is_active"])` mitnehmen.

---

## 4. CLAUDE.md vs. Realität: Token-Rotations-Modell

**Doku-Stelle:** CLAUDE.md > "Token Rotation A/B (Station-Manager)" — beschreibt ein `DeviceToken`-Modell mit `current_key_hash` / `next_key_hash` (SHA-256).

**Code-Realität:** `apps/api/models.py` heißt `DeviceKey` und speichert Ed25519-Public-Keys (`current_public_key` / `next_public_key`), nicht Hashes. Plus: Es gibt einen Generate-Button in der UI der den **privaten Schlüssel einmalig anzeigt** — was bei einem Hash-basierten Modell gar nicht ginge.

Diskrepanz auflösen:
- Wenn die CLAUDE.md die Zielarchitektur beschreibt (Token-in-Image statt Operator-copy-paste): das als FUTURE markieren, Migration als Plan-Doc schreiben.
- Wenn die CLAUDE.md veraltet ist: den Token-Rotation-Block neu schreiben damit er das Ed25519-DeviceKey-Modell beschreibt.

Vermutung: Die Doku ist Zielzustand. Das Image-Injection-Pattern aus `apps/provisioning/guestfish.py` (Token in `/etc/stationagent/`) passt zur Doku-Beschreibung, ist aber heute pro-Boot-statisches Token, kein A/B-Hash.

---

## 5. Heartbeat: silent broadcast-failure swallowt alles

**Datei:** `apps/api/views.py`, `HeartbeatView.post()`, gegen Ende:

```python
try:
    from apps.stations.consumers import broadcast_station_status
    broadcast_station_status(station)
except Exception:
    logger.exception("Failed to broadcast station status via WebSocket.")
```

Pattern ist OK (best-effort broadcast), aber das `except Exception` ist sehr breit. Wenn der Channel-Layer in Redis-Trouble gerät, sieht man's nur im Log — die Heartbeat-Response bleibt `200 OK`. Bei prod-Symptomen "Dashboard updated nicht" muss man explizit nach `logger.exception`-Strings grep'en.

**Vorschlag:** Eine Monitoring-Alert-Rule `BROADCAST_FAILED` einführen, die Engine alle 30s die letzten N solchen Log-Einträge zählt. Oder schlanker: ein Prometheus-Counter `heartbeat_broadcast_errors_total`. Falls Prom nicht aufgesetzt ist, lohnt sich's nicht — siehe ob's eh schon Plan ist.

---

## 6. Optional: `HeartbeatView` ist 80 Zeilen lang

Nicht kritisch, aber: die View macht heute Heartbeat-Persistierung + 3 verschiedene Audit-Log-Entscheidungen + Inventory-Update + WebSocket-Broadcast. Splittable in eine `heartbeat_service.process(station, data)`-Funktion, was Testbarkeit verbessert (heute braucht's HTTP-Request-Mocking).

Low-Prio — erst angehen wenn ein zweiter Caller (z.B. Bulk-Import-Tool, Test-Fixture-Setup) den Heartbeat-Pfad wiederbenutzen muss.

---

## Nicht-Findings (für Vollständigkeit)

Die folgenden Dinge sehen verdächtig aus, sind aber **korrekt** und mit gutem Grund so:

- `select_for_update()` in `supersession.py` lockt nur non-terminal Rows → Fleet mit langer History zahlt sonst auf jeder Deploy-Create. Steht im Comment.
- `_check_deployment_complete` macht conditional `UPDATE ... WHERE status=IN_PROGRESS` — explizit damit Cancel-Events nicht überschrieben werden. Steht im Comment.
- `transaction.on_commit` für deferred Audit-Logs im rollouts-Pfad — explizit damit Audit-DB-Fehler nicht die Main-Transaction rollback-only machen. Steht im Comment.
- `agent_ws_routes` ohne `AllowedHostsOriginValidator` — Agent ist ein CLI-Client ohne Origin-Header. Steht im Comment.
- HTTP-Range-Path verweigert Range bei non-seekable streams statt fallback → sonst Bandwidth-DoS-Vektor. Steht im Comment.

Das Reasoning-im-Comment-Niveau in dieser Codebase ist überdurchschnittlich, also: vor "fixen" immer den Block-Comment lesen.
