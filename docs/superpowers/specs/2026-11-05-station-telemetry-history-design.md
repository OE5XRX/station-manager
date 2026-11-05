# Stations-Telemetrie-Historie (Zeitreihen über generische Metriken)

Datum: 2026-11-05 · Branch `feature/station-telemetry-history` · ein PR (Spec + Plan + Code) in `station-manager`
Ersetzt die Fassung vom 2026-10-01 (Code-Katalog-Modell, durch #177 überholt).

## Warum

Seit #177 (generische Metriken) kennt der Server pro Station nur den **aktuellen** Wert
(`StationTelemetry.metrics`). Für Fehlersuche (Spannungseinbrüche beim Senden, Temperaturverlauf,
Netz-Aussetzer), Alarm-Nachbereitung und Trends braucht es eine Zeitachse — nativ im
station-manager, damit Topologie-Rechte (`accessible_stations`) und SSO gelten (Grafana würde
das Rechtemodell duplizieren; Export bleibt späteres Bolt-on).

## Ziele

1. Jeder generische Messwert wird pro Heartbeat historisiert — **ohne** Server-Codeänderung für
   neue Metriken (gleiche `metric_key`s wie #177, Definitionen aus `MetricDefinition`).
2. **Aufbewahrung (User-Entscheidung, Variante B):** Rohdaten (Heartbeat-Auflösung, ~1/min)
   **30 Tage**; danach **stündliche Verdichtung** mit min/max/avg **2 Jahre**.
3. Verlauf auf der Station-Detailseite (Tab „Metriken") und per API.
4. Kurze Einbrüche bleiben sichtbar: pro Heartbeat werden **min, max und avg** gespeichert,
   stündlich werden min-der-mins / max-der-maxs / avg-der-avgs gebildet.

## Nicht-Ziele

- Ereignisse (Reboot, OTA-Ergebnis, online/offline, Boot-Gate) — bleiben im `StationAuditLog`.
- Alarme aus der Historie (Regeln arbeiten weiter auf aktuellen Werten, #177).
- Export (Prometheus/CSV), Vergleich mehrerer Stationen in einem Diagramm — später.
- TimescaleDB oder neue Dependencies.

## Anti-Ziele

- Ein Fehler in der Historie bricht den Heartbeat (Ingest muss fehlerisoliert sein, wie
  `ingest_metrics`).
- Gesperrte (`is_blocked`) Metriken landen trotzdem in der DB.
- Ein Jahresdiagramm lädt Millionen Rohpunkte (immer serverseitig verdichten).

## 1. Datenmodell (neue App `apps/history`)

- `MetricSample` (Rohdaten): `station` FK, `ts` (Heartbeat-Zeit), `metric_key` (wie
  `MetricDefinition.key`), `min`, `max`, `avg` (Float). Unique `(station, metric_key, ts)`.
- `MetricHourly` (Verdichtung): gleiche Spalten + `count`, `ts` = Stundenbeginn (UTC).
  Unique `(station, metric_key, ts)`.
- Beide Tabellen **monatlich RANGE-partitioniert** nach `ts` (native Postgres, handgeschriebene
  Migration; Partition-Pflege per Management-Command). Löschen = `DROP PARTITION`.
- Kein FK auf `MetricDefinition` (Key-String reicht; Definitionen werden nie gelöscht).

## 2. Ingest

- Hook in `apps/monitoring/metrics_ingest.ingest_metrics` **nach** dem Speichern der aktuellen
  Werte: für jeden **akzeptierten** Wert (also nicht blocked, nicht außerhalb des Plausibilitäts-
  bereichs, Definition vorhanden) eine `MetricSample`-Zeile — ein `bulk_create` pro Heartbeat,
  `ignore_conflicts` (doppelter Heartbeat = kein Fehler).
- Fehler in der Historie: loggen, Heartbeat läuft weiter.
- Legacy-Werte (alte Agents, aus Inventory abgeleitet) werden mit historisiert.
- `is_hidden` wird gespeichert, nur nicht angezeigt.

## 3. Verdichtung + Aufbewahrung

- Management-Command `history_maintain` (läuft periodisch, z. B. stündlich — wie `check_alerts`
  über den bestehenden Mechanismus im Deploy/Compose; Builder prüft, wie periodische Jobs heute
  laufen, und hängt sich dort an):
  1. Fehlende `MetricHourly`-Stunden aus `MetricSample` berechnen (nur abgeschlossene Stunden;
     idempotent per Upsert).
  2. Partitionen anlegen (aktueller + nächster Monat) für beide Tabellen.
  3. `MetricSample`-Partitionen älter als **30 Tage** droppen (ganze Monate: Partition wird erst
     gedroppt, wenn ihr *Ende* älter als 30 Tage ist — Rohdaten sind also 30–61 Tage verfügbar;
     zusätzlich zeilenweises Löschen ist nicht nötig).
  4. `MetricHourly`-Partitionen älter als **2 Jahre** droppen.
- Settings: `HISTORY_RAW_RETENTION_DAYS=30`, `HISTORY_HOURLY_RETENTION_DAYS=730`.
- Verdichtung muss vor dem Droppen gelaufen sein (Command-Reihenfolge + Prüfung: keine
  Raw-Partition droppen, deren Stunden noch nicht verdichtet sind).

## 4. API

`GET /api/v1/stations/{id}/metrics/history/?keys=a,b&since=&until=&resolution=`

- `resolution`: `raw | 5m | 1h | 1d | auto` (Default `auto`: so wählen, dass ≤ ~1000 Punkte pro
  Serie zurückkommen). Quelle automatisch: Zeitraum im Raw-Fenster und Auflösung < 1h →
  `MetricSample`; sonst `MetricHourly`.
- Antwort pro Key: Label/Einheit/Precision aus `MetricDefinition`, Punkte `[ts, min, avg, max]`.
- Topologie-Rechte über `accessible_stations` (404 statt 403 für fremde Stationen, wie bestehende
  Station-Endpunkte), Throttle wie andere API-Endpunkte, drf-spectacular-Schema.
- Max. Keys pro Request und max. Zeitraum begrenzen (Builder: sinnvolle Grenzen, dokumentieren).

## 5. UI (Station-Detail, Tab „Metriken")

- Pro Gruppe (system/network/power/…) aufklappbares Diagramm; Zeitraumwahl 1h / 24h / 7d / 30d /
  1y. Linie = avg, Band = min…max (zeigt Einbrüche).
- Lazy-Load per HTMX/JS beim Öffnen; leichtgewichtige Chart-Bibliothek (uPlot o. ä., gebündelt,
  kein CDN). `frontend-design`-Skill ist Pflicht. DE-Locale: Zahlen in Diagrammen mit Punkt.
- Ausgeblendete Metriken nicht anzeigen.

## 6. Tests (Mindestumfang)

- Ingest: Samples für akzeptierte Werte, keine für blocked/out-of-range/ohne Definition,
  doppelter Heartbeat, Fehler isoliert (Heartbeat 200).
- Verdichtung: korrekte min/max/avg/count, nur abgeschlossene Stunden, idempotent.
- Aufbewahrung: Partitions-Drop-Grenzen (30 d / 730 d), kein Drop unverdichteter Stunden.
- API: Rechte-Matrix (eigene/fremde Station, anonym), Auflösungswahl `auto`, Grenzen, Schema.
- Migration auf Postgres (Partitionierung) im CI.

## Dem Builder überlassen

- Exakte Partitionsnamen, Index-Details, Chart-Bibliothek, Batch-Größen.
- Wie der periodische Job eingehängt wird (bestehendes Muster übernehmen).
