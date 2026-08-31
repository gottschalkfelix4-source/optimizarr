# Optimizarr

**Findet heraus, welche Dateien deiner Medienbibliothek sich wirklich lohnen, nach AV1
umgewandelt zu werden – und mit welchen Einstellungen.**

Die meisten Transcoding-Werkzeuge kodieren einfach alles nach denselben Regeln neu. Das
Ergebnis: manche Dateien schrumpfen um 60 %, andere werden *größer* als vorher, und man
merkt es erst, wenn das Original schon weg ist.

Optimizarr geht anders vor. Es misst nach, bevor es etwas anfasst – und es fasst nichts an,
solange das Ergebnis nicht nachweislich besser ist.

![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED?logo=docker&logoColor=white)
![Unraid](https://img.shields.io/badge/Unraid-Template-F15A2C)
![Intel QSV](https://img.shields.io/badge/Intel-QSV%20%2F%20VAAPI-0071C5?logo=intel&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Wie die Entscheidung zustande kommt

Für jede Datei laufen bis zu drei Stufen. Was davon zum Einsatz kommt, stellst du in der
Oberfläche ein.

**1. Metadaten-Heuristik (Millisekunden pro Datei)**
Aus Auflösung, Bildrate, Codec und Bitrate wird die *Bits pro Pixel* der Quelle berechnet
und auf AV1-Verhältnisse umgerechnet. Eine 30-Mbit/s-H.264-Datei hat viel Luft nach unten,
eine sparsame HEVC-Web-Version praktisch keine. Dateien der zweiten Sorte werden hier schon
aussortiert – ohne dass eine einzige Sekunde kodiert wurde.

**2. Testkodierung (Standard)**
Optimizarr schneidet mehrere kurze Ausschnitte aus der Datei, kodiert sie tatsächlich nach
AV1 und misst das Ergebnis. Aus einer Schätzung wird eine Messung. Nebenbei wird geprüft,
wie viel Filmkorn im Material steckt – körnige Filme sind der klassische Fall, bei dem
naive Einstellungen die Datei aufblähen, und gleichzeitig der Fall, bei dem
Filmkorn-Synthese am meisten spart.

**3. Qualitätssuche (optional)**
Zusätzlich wird pro Datei der höchste CRF-Wert gesucht, der das Qualitätsziel noch hält.
Gemessen wird mit VMAF, wenn das ffmpeg es kann, sonst mit SSIM – umgerechnet auf die
vertraute VMAF-Skala, damit „94 ist praktisch nicht unterscheidbar" weiter gilt.

### Das Lernmodell

Nach jeder abgeschlossenen Konvertierung vergleicht Optimizarr die Vorhersage mit dem
tatsächlichen Ergebnis. Eine Ridge-Regression lernt aus diesen Paaren – nicht die
Dateigröße selbst, sondern den *Fehler* der Heuristik. Das hat einen angenehmen
Nebeneffekt: ohne Trainingsdaten ist die Korrektur exakt 1,0, das Modell kann also nie
Unsinn produzieren, sondern nur besser werden. Bis zur eingestellten Reifegrenze wird die
gelernte Korrektur nur anteilig beigemischt.

Im ersten Testlauf lag die Vorhersage 4 % daneben; mit wachsender Datenbasis wird sie
genauer, weil sie deine Bibliothek und deine Hardware kennenlernt.

### Der KI-Berater (optional)

Messwerte sehen nicht, *was* für ein Film das ist. Ein flächiger Anime verträgt einen
deutlich höheren CRF, ein körniger 70er-Jahre-Klassiker braucht Filmkorn-Synthese, eine
dunkle Konzertaufnahme neigt zu Banding. Wenn du einen Anthropic-API-Schlüssel hinterlegst,
bekommt Claude die Messwerte und den Dateinamen und darf die Einstellungen nachjustieren –
innerhalb eines Rahmens, den du festlegst (standardmäßig maximal ±4 CRF).

Der Berater ist strikt optional und niemals blockierend: kein Schlüssel, Zeitüberschreitung
oder Rate-Limit führen einfach dazu, dass die lokale Entscheidung gilt. Standardmäßig wird
er nur bei unsicheren Einschätzungen befragt, und ein hartes Anfragelimit pro Scan
begrenzt die Kosten.

---

## Was garantiert nicht passiert

Bevor ein Original ersetzt wird, muss das Ergebnis **jede** dieser Prüfungen bestehen:

| Prüfung | Was geprüft wird |
|---|---|
| Integrität | Ist die Datei lesbar, enthält sie tatsächlich AV1, stimmt die Laufzeit? |
| Größe | Ist das Ergebnis kleiner als das Original? |
| Mindestersparnis | Lohnt der Gewinn den Qualitätsverlust überhaupt? |
| Qualität (optional) | Erreicht die fertige Datei das VMAF-Minimum? |

Fällt eine Prüfung durch, wird das Ergebnis gelöscht, das Original bleibt **bitgenau**
erhalten, und die Datei wird mit einer nachvollziehbaren Begründung als „übersprungen"
markiert – damit derselbe Versuch nicht beim nächsten Scan wieder Rechenzeit kostet.

Originale wandern standardmäßig in einen Papierkorb-Ordner statt gelöscht zu werden. Das
Änderungsdatum bleibt erhalten, damit Plex und Jellyfin die Datei nicht als neu behandeln.

---

## Intel-GPU-Unterstützung

Die Hardware wird beim Start selbst ermittelt – du musst nichts wissen und nichts angeben.
Optimizarr liest den Render-Node aus, fragt `vainfo` nach den Fähigkeiten und macht dann
den einzigen Test, der wirklich zählt: **eine echte Testkodierung von zehn Bildern.** Erst
wenn die durchläuft, gilt ein Encoder als nutzbar.

| Hardware | Was passiert |
|---|---|
| Intel Arc, Core Ultra (Meteor Lake) | AV1 wird auf der GPU kodiert – schnell und CPU-schonend |
| iGPU ab 12. Generation (UHD 730/770) | AV1-Encoding auf der CPU, Dekodieren übernimmt die GPU |
| Ältere iGPU (UHD 630 usw.) | SVT-AV1 auf der CPU, GPU hilft beim Dekodieren älterer Codecs |
| Keine GPU | SVT-AV1 auf der CPU |

Schlägt ein Hardware-Encode unterwegs fehl, wird der Job automatisch auf der CPU
wiederholt, statt als Fehler zu enden.

---

## Installation auf Unraid

1. In den Docker-Einstellungen unter **Template Repositories** diese URL eintragen:
   ```
   https://github.com/gottschalkfelix4-source/optimizarr
   ```
2. **Add Container** → Template `Optimizarr` auswählen.
3. Die Pfade prüfen:

| Pfad | Empfehlung |
|---|---|
| `/config` | `/mnt/user/appdata/optimizarr` – Datenbank, Einstellungen, Papierkorb |
| `/transcode` | Auf **SSD oder Cache-Pool**. Hier entsteht die komplette Ausgabedatei, bevor sie umzieht. Mindestens 50 GB freihalten. |
| `/media` | Deine Bibliothek, mit **Schreibrechten** |
| `/dev/dri` | Als *Device* eintragen – ohne das läuft alles auf der CPU |

4. Container starten und die Weboberfläche unter `http://<server>:8474` öffnen.
5. Unter **Einstellungen → Bibliothek** die konkreten Ordner auswählen, dann **Bibliothek
   scannen**.

### Alternativ mit Docker Compose

```bash
docker compose up -d
```

Die mitgelieferte [`docker-compose.yml`](docker-compose.yml) enthält bereits die richtigen
Volumes und das GPU-Device.

---

## Erste Schritte

Nach dem ersten Scan zeigt die **Übersicht**, wie viel Platz insgesamt zu holen ist. In der
**Bibliothek** siehst du jede Datei mit erwarteter Größe, Ersparnis und Begründung – ein
Klick auf eine Zeile öffnet die Details samt geplantem Encoding-Plan, Tonspur-Behandlung
und der vollständigen Argumentationskette.

Einzelne Dateien reihst du per Klick ein, oder du lässt Kandidaten automatisch einreihen
(**Einstellungen → Warteschlange**). Wer den Server tagsüber braucht, stellt dort ein
Zeitfenster ein – dann wird nur nachts kodiert.

**Empfehlung für den Anfang:** Ausgabe-Modus auf *Daneben ablegen* stellen und ein paar
Dateien konvertieren. So kannst du in Ruhe vergleichen, bevor du auf *Original ersetzen*
umstellst.

---

## Einstellungen

Alles wird in der Oberfläche eingestellt – es gibt **keine Konfigurationsdateien und keine
Umgebungsvariablen** für das Verhalten. Die einzigen Umgebungsvariablen sind `PUID`,
`PGID`, `UMASK` und `TZ`, weil die schon feststehen müssen, bevor die Anwendung überhaupt
startet.

Die drei Qualitätsprofile setzen CRF, Preset und Qualitätsziel gemeinsam:

| Profil | CRF | Für wen |
|---|---|---|
| **Archiv** | 24 | Sammlungen, bei denen jedes Detail zählt. Weniger Ersparnis, langsamster Encode. |
| **Ausgewogen** | 30 | Empfehlung. Deutliche Ersparnis, kaum sichtbarer Unterschied. |
| **Platz sparen** | 35 | Wenn Platz wichtiger ist als das letzte Prozent Bildqualität. |

Alles Weitere lässt sich einzeln nachjustieren: Tonspur-Behandlung (verlustfreie Spuren
nach Opus, das spart bei Blu-ray-Rips oft mehr als das Video selbst), Untertitel-Sprachen,
Zeitplan, Dateirechte, Schwellenwerte.

---

## Entwicklung

```bash
# Backend
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
OPTIMIZARR_CONFIG_DIR=./data/config OPTIMIZARR_TRANSCODE_DIR=./data/transcode \
  .venv/bin/uvicorn app.main:app --reload --app-dir backend --port 8080

# Frontend (Port 5173, leitet /api an 8080 weiter)
cd frontend && npm install && npm run dev

# Tests
.venv/bin/pytest backend/tests -q
```

Die Anwendung braucht `ffmpeg` und `ffprobe` im Pfad; ohne sie funktionieren die
Metadaten- und Encoding-Teile nicht. Im Container kommt beides von `jellyfin-ffmpeg`, weil
das den Intel-QSV-Stack fertig verdrahtet mitbringt.

### Aufbau

```
backend/app/
  core/
    ffmpeg.py      ffprobe/ffmpeg-Wrapper mit Fortschritts-Parsing
    hwaccel.py     Intel-GPU-Erkennung per echter Testkodierung
    predictor.py   Heuristik + gelerntes Korrekturmodell
    quality.py     VMAF/SSIM-Messung, Filmkorn-Schätzung
    planner.py     Analyse-Ergebnis -> ffmpeg-Kommandozeile
    analyzer.py    Entscheidungslogik (die drei Stufen)
    encoder.py     Job-Ausführung und die Sicherheits-Gates
    advisor.py     optionale Claude-API-Schicht
    scanner.py     Bibliotheks-Scan
    worker.py      Warteschlange und Zeitplan
frontend/src/      React + Tailwind
unraid/            Community-Applications-Template
```

---

## Lizenz

MIT
