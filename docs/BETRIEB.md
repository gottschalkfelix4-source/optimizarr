# Entwicklung, Sicherung und Betrieb

Backend und Dateisicherheit werden unter **Linux mit Python 3.13** geprüft. Die Anwendung verwendet Unix-Signale, Verzeichnis-fsync und Linux-Gerätepfade. Auf Windows ist WSL2 oder Docker die unterstützte Entwicklungsumgebung. Das Frontend kann auf Windows mit Node.js 24 getestet werden.

## Reproduzierbare Entwicklung

Unter Linux im Repository:

```bash
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes -r backend/dev-requirements.lock
.venv/bin/pytest backend/tests -q
cd frontend
npm ci
npm test
npx tsc --noEmit
npm run build
```

`backend/requirements.txt` beschreibt die direkten Abhängigkeiten. `requirements.lock` hält alle Laufzeit-Abhängigkeiten mit Version und SHA-256 fest; `dev-requirements.lock` enthält zusätzlich pytest. Die Locks werden unter Linux/Python 3.13 aktualisiert und mit Tests überprüft. Sie sind keine Windows-Installationsdateien, unter anderem wegen uvloop. Tests entfernen die Login-Overrides `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`, `CODEX_APP_SERVER_LOGIN_CLIENT_ID`, `CODEX_ISSUER_OVERRIDE` und `CODEX_REFRESH_TOKEN_URL_OVERRIDE` vor dem Import; Tests eigener Overrides setzen diese anschließend selbst.

Eine kontrollierte Aktualisierung verwendet pip-tools in einer separaten Entwicklungsumgebung:

```bash
cd backend
python -m piptools compile --generate-hashes --output-file requirements.lock requirements.txt
python -m piptools compile --generate-hashes --output-file dev-requirements.lock dev-requirements.in
```

Die Node- und Debian-Basisimages sind im Dockerfile über Digests festgelegt. Jellyfin-FFmpeg ist über `JELLYFIN_FFMPEG_VERSION` auf `7.1.4-3-trixie` festgelegt. Aktualisierungen ändern diese Werte bewusst und durchlaufen Backend, Frontend, Image-Build, echten AV1/Opus-Encode und vollständiges Decode. Debian-Sicherheitsupdates aus apt bleiben verfügbar; das gesamte apt-Repository wird nicht eingefroren. Die CI prüft zusätzlich UID 99/GID 100 mit schreibbaren Config-, Transcode- und Media-Volumes sowie den echten API-Ablauf vom Scan bis zum abgeschlossenen Ausgabejournal. Erst danach veröffentlicht sie das tatsächlich geprüfte Image.

## Sicherung

Unter **Einstellungen → System → Konfiguration sichern** wird ein ZIP mit einer konsistenten SQLite-Sicherung und den Konfigurationsdateien erstellt. Die SQLite-Backup-API berücksichtigt auch noch nicht in die Hauptdatei übertragene WAL-Daten. Das ZIP enthält Einstellungen, gespeicherte Anmeldedaten, Lernmessungen sowie Papierkorb- und Wiederherstellungsprotokolle. Mediendateien und Papierkorb-Originale sind separat zu sichern.

Laufende Jobs und Scans müssen vorher beendet sein; offene Dateijournale verhindern die Sicherung. Ein ZIP mit Anmeldedaten kann über die Weboberfläche nur mit aktivierter Anmeldung erstellt und heruntergeladen werden. Vorhandene Sicherungen bleiben auch dann geschützt, wenn später Schlüssel entfernt oder die Anmeldung deaktiviert wird. Die Dateien unter `/config/backups` haben Modus 0600. Die eingestellte Anzahl von Sicherungen begrenzt den lokalen Bestand.

Ohne Web-Anmeldung kann bei **gestopptem Container** lokal gesichert werden. Beispiel für das Compose-Layout dieses Repositories:

```bash
docker compose stop optimizarr
docker run --rm --entrypoint python3 \
  --mount type=bind,src="$PWD/data/config",dst=/config \
  ghcr.io/gottschalkfelix4-source/optimizarr:latest \
  -m app.core.upkeep backup
docker compose start optimizarr
```

Der Befehl gibt den ZIP-Pfad innerhalb von `/config/backups` aus. Für Unraid den tatsächlichen Appdata-Pfad als `src` einsetzen. ZIPs mit Anmeldedaten gehören auf einen entsprechend geschützten Sicherungsdatenträger.

## Wiederherstellung

1. Container stoppen und die derzeitige Konfiguration zusätzlich aufbewahren.
2. Das heruntergeladene ZIP im Beispiel als `data/backup.zip` ablegen.
3. In einen neuen Konfigurationsordner wiederherstellen:

```bash
docker run --rm --entrypoint python3 \
  --mount type=bind,src="$PWD/data",dst=/restore \
  ghcr.io/gottschalkfelix4-source/optimizarr:latest \
  -m app.core.upkeep restore /restore/backup.zip /restore/config-restored
```

Der Zielordner muss neu oder leer sein. Pfadtraversal, Verknüpfungen, doppelte Archivnamen und beschädigte Datenbanken werden abgewiesen. Die fertige Konfiguration wird als Verzeichnis übernommen, nachdem die Prüfung erfolgreich war. Die bisherige Konfiguration wird nicht überschrieben.

4. Den Config-Bind-Mount im Compose- beziehungsweise Unraid-Template auf den wiederhergestellten Ordner ändern und den Container starten. Im Compose-Beispiel `./data/config-restored:/config` verwenden.
5. Bibliothekspfade und die tatsächlich vorhandenen Dateien prüfen, danach einen manuellen Scan ausführen. Bei Bedarf Dateijournale und Papierkorb-Konflikte im Protokoll prüfen.
6. Neue Jobs einreihen, Warteschlange freigeben und automatische Scans nach Bedarf wieder aktivieren.

Eine Konfigurationssicherung setzt Mediendateien nicht auf einen früheren Stand zurück. Deshalb schließt die Wiederherstellung alte wartende/laufende Jobs, pausiert die Warteschlange und deaktiviert automatische Scans. Aufgezeichnete Konvertierungen und Ersparnisse bleiben im Sicherungsstand erhalten. Ein erneuter Scan gleicht geänderte Dateien mit dem aktuellen Datenträger ab.

## Aufbewahrung und Herunterfahren

Die Einstellungen für Verlauf, abgeschlossene Jobs, Scans und Protokolle abgeschlossener Wiederherstellungen stehen unter **System**. `0 Tage` bewahrt diese Daten dauerhaft auf. Lernmessungen und Sicherungsanzahl haben eigene Grenzen. Die tägliche Bereinigung entfernt weder aktive Jobs noch konvertierte Bibliothekszeilen und ihre Ersparnisse. Offene Journale und noch benötigte Papierkorb-Originale bleiben erhalten; für Originale gilt die separate Papierkorb-Aufbewahrung.

Scan-Abbruch prüft ein threadtaugliches Signal pro Verzeichnis, Datei und Datenbankeintrag. Hintergrundaufgaben aus Startup, API, Einstellungen und Scheduler werden gemeinsam nachverfolgt. Beim Stop erhalten sie eine Schonfrist; Dateiarbeit wird tatsächlich abgewartet. Die Dateiersetzung und ihre Datenbankverbuchung werden nach Beginn gemeinsam beendet. Compose gibt dem Container dafür 60 Sekunden. Eine im Betriebssystem blockierte Netzwerk-Dateisystemoperation kann diese Schonfrist überschreiten; ein hartes Beenden wird beim nächsten Start über das Journal behandelt.

Neue Ausgabepfade werden ohne Überschreiben bestehender Dateien veröffentlicht. Auf einem Dateisystem, das die dafür verwendeten Hardlinks nicht unterstützt, verweigert die Anwendung diese Übernahme mit einer Fehlermeldung und behält das Original.

## Bibliotheksbenchmark

```bash
python scripts/benchmark-library.py --files 10000
python scripts/benchmark-library.py --files 100000
```

Jede Größe läuft in einem eigenen Prozess. Der Benchmark verwendet synthetische SQLite-Daten und echte API-Antworten. Laufzeiten von Verzeichnisbegehung und `stat` auf einem echten Share sowie FFmpeg sind ausgeschlossen. Die Abbruchmessung verwendet kooperatives, synthetisches Dateiarbeiten; sie misst keine blockierte NAS-Verbindung.

Gemessen am 30.09.2026 unter Linux/Python 3.13 in Docker Desktop, 12 logischen CPU-Kernen:

| Einträge | Dateiseite | Serien, erster Aufbau | Serien, Cache | Sync | Kooperativer Abbruch | Spitzen-RSS |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10.000 | 43 ms | 214 ms | 3,4 ms | 0,52 s | 1,2 ms | 105 MiB |
| 100.000 | 69 ms | 1,54 s | 6,1 ms | 5,59 s | 1,2 ms | 239 MiB |

Der 100.000-Einträge-Lauf benötigte vor der Begrenzung der ORM-Batches rund 494 MiB. Gruppensnapshots werden außerhalb der globalen Sperre aufgebaut und nur bei unveränderter Version veröffentlicht. Scan-Synchronisierung hält höchstens 500 vollständige ORM-Datensätze gleichzeitig. Als Vergleichsziele für diesen synthetischen Aufbau gelten: Dateiseite und Cache-Antwort unter 200 ms, erster Gruppenaufbau unter 3 s, Spitzen-RSS unter 300 MiB und kooperativer Abbruch unter 2 s. Die Messwerte sind Vergleichswerte dieser Umgebung, keine Zusage für NAS- oder Encoding-Laufzeiten.
