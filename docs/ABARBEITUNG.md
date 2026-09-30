# Umsetzung der Projektprüfung

Stand: 30.09.2026. Ausgangspunkt ist der [Befundbericht zu `2241f1d`](PROJEKTREVIEW.md).
Alle acht Bugs und zehn Verbesserungen sind umgesetzt. Die Betriebsabläufe und
Messbedingungen stehen in [BETRIEB.md](BETRIEB.md).

## Behobene Bugs

| Punkt | Ergebnis | Nachweis |
| --- | --- | --- |
| B1 | Sidecar-Suffix und separater Ordner werden validiert. Identische Quell-/Zielpfade, Hardlinks, Symlinks und bestehende Ziele verhindern die Veröffentlichung. | API-, Alias- und Dateischutztests |
| B2 | Journal v2 enthält Phasen und Dateifingerabdrücke. Rollback entfernt nur nachweislich eigene Ausgaben; unklare Konflikte erhalten Dateien und Journal. | Fremdtarget- und Abbruchtests je Commit-Phase |
| B3 | Einstellungsfehler erhalten einen gültigen Cache; ohne gültige Sicherheit werden Anfragen abgelehnt. Ungültige Sicherheitsdatensätze schalten die Anmeldung nicht ab. | Ausfalltests mit/ohne Cache und beschädigten Einstellungen |
| B4 | Der erste periodische Scan verwendet eine stabile Zeitbasis. | Scheduler ohne Scan-Historie und vorgerückte Testuhr |
| B5 | Wochentage sind auf 0–6 begrenzt, dedupliziert und nicht leer. | API-Grenzwerte und Zeitplanprüfung |
| B6 | Normale Sammelaktionen akzeptieren ausschließlich Kandidaten. Erzwingen ist eine eigene Aktion; abgeschlossene Dateien bleiben geschützt. | Backend-Auswahltests und Frontend-Auswahl über mehrere Seiten |
| B7 | Erneute Analyse erhält konvertierten/ignorierten Status und verbuchte Ersparnisse; Gruppenansichten werden aktualisiert. | API-Reanalyse und Live-Cache-Tests |
| B8 | Unerwartete Teilaufgabenfehler schlagen den Scan fehl. Erwartete Dateifehler werden gezählt und protokolliert. | Probe-Ausfall und Scanabschluss |

## Umgesetzte Verbesserungen

| Punkt | Ergebnis | Nachweis |
| --- | --- | --- |
| I1 Regressionen | Fehler werden als dauerhafte Regressionen auf API-, Datei- und UI-Ebene geprüft. | `test_review_regressions.py`, `test_review_lifecycle.py`, `Library.test.tsx` |
| I2 Datei/DB-Abschluss | Das Journal bleibt bis zum Datenbankabschluss. Wiederanlauf verbucht fertige Ausgaben idempotent; offene Journale verhindern doppelte Aufträge. | Abbruchinjektion in fünf Phasen und mehrfaches Replay |
| I3 Ressourcen | Trockenläufe besitzen eigene UUID-Ausgaben. Jobs reservieren konservativ Transcode-Speicher und warten bei Engpässen. | Gleichzeitige Trockenläufe, Überreservierung und sichtbare Queue-Blockade |
| I4 Herunterfahren | Scans prüfen Abbruchsignale während Walk und Datenbanksynchronisierung. Hintergrundaufgaben werden nachverfolgt; abbrechende Threads und laufende Commit-Abschlüsse werden abgewartet. | Shutdown während eines Disk-Workers, vorhandene Queue-Abbruchtests |
| I5 Qualität | Messabdeckung und schlechtester Ausschnitt werden gespeichert und angezeigt; mindestens zwei Ausschnitte sind Standard. Optional prüft FFmpeg die gesamte Ausgabe durch strenges Decode. | Fehlende Stichprobe, schlechtester Wert, Decode-Fehler und echte AV1/Opus-Dekodierung |
| I6 Große Bibliotheken | Gruppensnapshots entstehen außerhalb der globalen Sperre. Scans laden vollständige ORM-Zeilen in Batches von 500. Benchmark mit 10.000/100.000 Einträgen und Vergleichszielen. | Bei 100.000 Einträgen 239 statt zuvor 494 MiB Spitzen-RSS; Messwerte in BETRIEB.md |
| I7 API-Eingaben | Typisierte und begrenzte IDs, erlaubte Analysetiefen/Encoder, bedingte Ausgabevalidierung und sichtbare CRF-Normalisierung. | Ungültige Requests liefern 422; Einstellungen und Queue-Reihenfolge |
| I8 Aufbewahrung/Backup | Konfigurierbare Aufbewahrung und begrenzte Lern-/Backup-Bestände; konsistentes SQLite/WAL-ZIP. Wiederherstellung in neuen Ordner mit pausierter Automation. ZIPs mit Zugangsdaten bleiben geschützt. | WAL-Roundtrip, Statistikerhalt, Retention, Traversal-Abweisung und Wiederherstellungsstatus |
| I9 Modellgüte | Tatsächlich vor dem Encode angewendete Prognosen werden gespeichert. Prognosefehler, Trainingsfehler und Messanzahl erscheinen getrennt, auch je Encoder. Alte Werte werden als Basisprognosen gekennzeichnet. | Prognoseauswertung bleibt nach erneutem Training unverändert; GPU→CPU-Fallback behält Filmkorn und verwirft GPU-Prognose |
| I10 Reproduzierbarkeit | Vollständige Python-Hash-Locks, Basisimage-Digests und festes Jellyfin-FFmpeg. CI prüft zusätzlich UID 99/GID 100 mit Volumes und echtem Encode. Linux-Entwicklung ist dokumentiert. | Installation mit `--require-hashes`, Backend/Frontend und Image-Smoke-Tests |

## Abschlussprüfung

| Prüfung | Ergebnis |
| --- | --- |
| Backend, Linux/Python 3.13, Hash-Lock | 720 bestanden, 2 ausgelassen |
| Frontend `npm test` | 101 bestanden in 10 Testdateien |
| Frontend `npx tsc --noEmit` | Bestanden |
| Docker-Build inklusive Frontend | Bestanden; `optimizarr:review` |
| Echter CPU-AV1/Opus-Encode, Probe und vollständiges Decode | Als root und UID 99 bestanden |
| Standardbenutzer und Volumes | Config, Transcode, Medien und Backup unter UID 99/GID 100 schreibbar |
| API → Scan → Queue → Ausgabe → Datenbank | Strenges Decode, unverändertes Original, AV1-Sidecar mit UID 99 und abgeschlossenes Journal bestanden |
| Oberfläche im Container | System-Aufbewahrung, Backup-Hilfe und vollständiges Decode im Browser geprüft |

Die zwei ausgelassenen Backend-Tests benötigen libvmaf, das im allgemeinen
Debian-Test-FFmpeg nicht vorhanden ist.
Das ausgelieferte Jellyfin-FFmpeg wird zusätzlich durch echte Encoding-Smokes
geprüft. Gezielte Prüfungen verwenden ausschließlich synthetische Dateien und
isolierte Datenbanken; externe KI-Anfragen sind nicht erforderlich.

GPU-Encoder werden auf Verfügbarkeit geprüft. Ohne durchgereichte Intel-GPU
ist die tatsächliche QSV-/VAAPI-Ausführung nicht Bestandteil der lokalen oder
GitHub-Smoke-Prüfung. CPU-Encoding, Probe und vollständiges Decode werden real
ausgeführt. Der Bibliotheksbenchmark schließt NAS-Latenz und FFmpeg aus.

Die parallelen Scanner-Tests verwenden eine temporäre WAL-Datenbank mit
getrennten Verbindungen. Eine einzelne geteilte In-Memory-Verbindung ließ
Transaktionen anderer Test-Worker gelegentlich zurückrollen und ist für diese
Prüfung ungeeignet.
