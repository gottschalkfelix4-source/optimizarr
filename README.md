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

### Ziel: kein H.264 mehr in der Bibliothek

Unter **Einstellungen → Analyse → H.264 vollstaendig nach AV1 konvertieren**
laesst sich die Codec-Umstellung statt der Speicherersparnis priorisieren.
Danach einen Scan starten: Bereits uebersprungene H.264-Dateien werden neu bewertet,
auch wenn sich die Dateien nicht geaendert haben. Kleine und kurze H.264-Dateien
werden ebenfalls beruecksichtigt. Fuer diese Dateien entfallen Bitraten- und
Ersparnisschwellen bei Analyse, automatischem Einreihen und Ergebnisannahme.
**AV1-Ergebnisse duerfen in diesem Modus groesser als die Originale sein.**
Integritaets- und aktivierte Qualitaetspruefungen bleiben erhalten.

Der Modus ist standardmaessig aus. Ordner-/Dateityp-/Codec-Ausschluesse und
ignorierte Dateien gelten weiterhin. Automatisches Einreihen muss separat aktiviert
sein; ansonsten die Kandidaten manuell einreihen. Unter Ausgabe **Ersetzen** verwenden:
Bei Sidecar oder separatem Ausgabeordner bleibt die H.264-Quelle bestehen.
Originale im Papierkorb bleiben bis zum Ablauf der Aufbewahrung erhalten.

Für jede Datei laufen bis zu drei Stufen. Was davon zum Einsatz kommt, stellst du in der
Oberfläche ein.

**Vorab: was du ausgeschlossen hast**
Ganze Codecs lassen sich in den Einstellungen abwählen. Die Liste zeigt, was in der
Bibliothek wirklich vorkommt – mit Dateizahl, Speicherbedarf und der Zahl der Kandidaten,
die dahinter hängen. AV1 ist voreingestellt, weil eine erneute AV1-Kodierung nur Qualität
kostet; wer sein HEVC-Material in Ruhe lassen will, hakt es an. Die Änderung greift sofort
auf die bestehende Kandidatenliste durch, nicht erst beim nächsten Scan – und lässt sich
genauso zurücknehmen.

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
VMAF-Skala als grobe Orientierung. Die Oberfläche unterscheidet gemessenen VMAF,
SSIM-Rohwert und VMAF-Schätzung; die Schätzung ist keine gemessene VMAF-Qualität.
Bei alten Ergebnissen ohne gespeichertes Messverfahren wird dieses als unbekannt angezeigt.

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
dunkle Konzertaufnahme neigt zu Banding. Der Berater bekommt die Messwerte und den
Dateinamen und darf die Einstellungen nachjustieren – innerhalb eines Rahmens, den du
festlegst (standardmäßig maximal ±4 CRF).

Drei Anbieter stehen zur Wahl, einer ist jeweils aktiv:

| Anbieter | Was du brauchst | Wofür |
|---|---|---|
| **Claude (Anthropic API)** | API-Schlüssel von console.anthropic.com | Beste Einschätzung, Abrechnung pro Anfrage |
| **ChatGPT-Anmeldung (Codex)** | Ein ChatGPT-Konto, Anmeldung im Browser | Nutzt das bestehende Abo statt separater Guthaben |
| **OpenAI-kompatibler Endpunkt** | URL, Modellname, optional ein Schlüssel | OpenAI, OpenRouter, Groq, DeepSeek – oder lokal via Ollama, LM Studio, vLLM, LocalAI |

Der Berater ist strikt optional und niemals blockierend: kein Schlüssel, Zeitüberschreitung,
Rate-Limit oder ein Endpunkt, der nicht antwortet, führen einfach dazu, dass die lokale
Entscheidung gilt. Standardmäßig wird er nur bei unsicheren Einschätzungen befragt, und ein
hartes Anfragelimit pro Scan begrenzt die Kosten.

**Beim OpenAI-kompatiblen Endpunkt** handelt Optimizarr beim ersten Aufruf selbst aus, was
der Dienst kann: erzwungenes JSON-Schema, JSON-Modus oder nur eine Prompt-Anweisung – und
ebenso, ob er `max_tokens` oder `max_completion_tokens` erwartet und ob er eine
System-Nachricht akzeptiert. Was funktioniert hat, wird gemerkt. Damit läuft derselbe
Code gegen die aktuelle OpenAI-API und gegen ein zwei Jahre altes Ollama.

> **Zur ChatGPT-Anmeldung, damit du es vorher weißt:** Dieser Weg meldet sich so an wie
> OpenAIs eigenes Codex-Kommandozeilenwerkzeug. Vorgesehen ist er für OpenAIs Anwendungen;
> für Drittprogramme wie Optimizarr ist das eine Grauzone, und OpenAI kann den Zugang
> jederzeit einschränken. Wenn du das vermeiden möchtest, nimm einen Platform-API-Schlüssel
> über den Punkt *OpenAI-kompatibler Endpunkt* – der ist der offiziell vorgesehene Weg.

Weil Optimizarr im Container läuft und dein Browser woanders, kann es den OAuth-Rücksprung
nicht selbst auffangen. Der Anmelde-Assistent führt deshalb durch drei Schritte: Link
öffnen, anmelden, und die Adresse der (absichtlich fehlschlagenden) Zielseite zurück ins
Feld kopieren. Wer das Codex-CLI schon eingerichtet hat, kann stattdessen den Inhalt von
`~/.codex/auth.json` einfügen.

Die Codex-Anbindung im Image verwendet den Kompatibilitaetsstand **0.153.4**
und bietet **GPT-6 Astra** (`gpt-6-astra`) als Standard fuer neue Konfigurationen an.
Sie ist direkt in Optimizarr implementiert; eine separate Codex-CLI wird nicht gestartet.
Nach einem Image-Update bleiben gespeicherte Modelleinstellungen erhalten.
Zum Wechsel unter **Einstellungen → KI-Berater → ChatGPT-Anmeldung** auf
**GPT-6 Astra verwenden** klicken, speichern und **Testen** ausfuehren.
**Liste abrufen** aktualisiert die vom Konto gemeldeten Modelle; ob Astra nutzbar ist,
haengt vom Kontozugang ab. Siehe [Codex-Modelle](https://learn.chatgpt.com/docs/models)
und [Codex 0.153.4](https://learn.chatgpt.com/docs/changelog).

---

## Was garantiert nicht passiert

Bevor ein Original ersetzt wird, gelten die konfigurierten Pruefungen.
Bei **Trotzdem konvertieren** entfallen Groessenbegrenzung und Mindestersparnis:
Auch ein groesseres Ergebnis wird uebernommen. Das gilt ebenfalls fuer H.264 im
H.264-Umstellungsmodus. Integritaets- und aktivierte Qualitaetspruefungen bleiben erhalten:

| Prüfung | Was geprüft wird |
|---|---|
| Integrität | Ist die Datei lesbar, enthält sie tatsächlich AV1, stimmt die Laufzeit? |
| Größe | Ist das Ergebnis kleiner als das Original? |
| Mindestersparnis | Lohnt der Gewinn den Qualitätsverlust überhaupt? |
| Qualität (optional) | Erreichen die geprüften Ausschnitte das Qualitätsminimum (VMAF oder SSIM-Schätzung)? |

Fällt eine Prüfung durch, wird das Ergebnis gelöscht, das Original bleibt **bitgenau**
erhalten, und die Datei wird mit einer nachvollziehbaren Begründung als „übersprungen"
markiert – damit derselbe Versuch nicht beim nächsten Scan wieder Rechenzeit kostet.

Originale wandern standardmäßig in einen Papierkorb statt gelöscht zu werden. Ist unter
**Einstellungen → Ausgabe** kein eigener Papierkorb-Ordner eingetragen, landet das Original
im Ordner `.optimizarr-trash` direkt im jeweiligen Bibliotheksordner. Der liegt auf
demselben Dateisystem wie die Datei. Wenn Hardlinks unterstützt werden, braucht das
Sichern des Originals keine vollständige Kopie. Der Scanner überspringt diesen Ordner.
Nach Ablauf der Aufbewahrungszeit (Standard 14 Tage) werden nur **protokollierte,
unveränderte Originale** gelöscht. Maßgeblich ist der protokollierte Verschiebezeitpunkt.
Alte Papierkorb-Dateien ohne Einzelnachweis werden nicht automatisch übernommen oder
gelöscht – sie bleiben zur manuellen Verwaltung erhalten.

Unter **Papierkorb** stehen Originalpfad, Speicherbedarf, Löschdatum und Konflikte.
**Wiederherstellen** bringt das Original zurück und bewahrt die konvertierte Version
zusätzlich mit einer `.original`-Kennzeichnung auf. Das Original wird anschließend
ignoriert, damit es nicht automatisch erneut konvertiert wird. Geänderte oder fremde
Dateien am Ziel werden nicht überschrieben. Laufende Scans und wartende/laufende Jobs
für die Datei müssen vorher beendet werden. Bei unterbrochener Nacharbeit lässt sich
eine bereits erfolgte Wiederherstellung über denselben Eintrag abschließen.

Vor jeder Dateiersetzung muss ein Wiederherstellungsjournal erfolgreich geschrieben
und auf den Datenträger synchronisiert sein. Scheitert das, bleibt das Original unberührt.
Das Konfigurationsvolume muss diese Synchronisierung unterstützen.

Die neue Datei bekommt standardmäßig **ein neues Änderungsdatum** (`preserve_mtime` ist
aus). So erkennen Plex und Jellyfin sicher, dass sich die Datei geändert hat, und lesen die
Stream-Infos (Codec, Bitrate) neu ein. Wer das alte Datum behalten will, schaltet
*Änderungsdatum übernehmen* ein – dann muss die Mediathek unter Umständen von Hand neu
eingelesen werden.

### Dolby Vision

Dolby Vision wird erkannt und gesondert behandelt, weil ein AV1-Encode die
Dolby-Vision-Ebene nicht mitnehmen kann:

| Quelle | Standard (`Überspringen`) | Einstellung `Als HDR10 kodieren` |
|---|---|---|
| DV Profil 5 (keine HDR10-Basis) | übersprungen | übersprungen – auch beim Erzwingen |
| DV Profil 7/8 (HDR10-Basis) | übersprungen | als HDR10 kodiert, die DV-Ebene entfällt |

Profil 5 hat kein normales HDR-Bild, das man behalten könnte – ohne die DV-Verarbeitung
wären die Farben falsch. Deshalb wird es nie konvertiert, auch nicht über *Erzwingen*.
Profil 7/8 lässt sich per *Erzwingen* auch einzeln als HDR10 kodieren.

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
wiederholt, statt als Fehler zu enden — mit der ffmpeg-Fehlermeldung und dem verwendeten
Befehl im Job-Protokoll, damit nachvollziehbar bleibt, warum.

Encoden und Dekodieren auf der GPU werden **getrennt** geprüft. Kann die GPU zwar
encodieren, aber ihre eigenen Frames nicht zuverlässig entgegennehmen, verliert nur das
Dekodieren die Beschleunigung — die teure Hälfte bleibt auf der GPU.

**Qualität auf der GPU:** Die Encoder kennen keinen CRF-Wert wie SVT-AV1. Optimizarr
rechnet den eingestellten bzw. pro Datei ermittelten CRF deshalb auf die Qualitätsskala des
jeweiligen Encoders um. Beim VAAPI-Encoder (`av1_vaapi`) läuft das über den
Konstant-Quantisierer-Modus (`-rc_mode CQP` mit `-global_quality`), sodass ein höherer CRF
auch dort eine kleinere Datei ergibt. Die Werte sind nicht eins zu eins mit SVT-AV1
vergleichbar – das Lernmodell gleicht die Unterschiede mit jedem fertigen Job weiter aus.

### Wenn trotzdem auf die CPU zurückgefallen wird

```bash
curl -sL https://raw.githubusercontent.com/gottschalkfelix4-source/optimizarr/main/scripts/intel-diagnose.sh | docker exec -i Optimizarr sh
```

Das Skript probiert die Varianten einzeln durch — 10-Bit-Ausgabe, GPU-Dekodierung,
einzelne Encoder-Parameter, VAAPI als Alternative — und zeigt, welche auf deiner Karte
trägt. Nützlich vor allem dann, wenn die Erkennung die GPU als einsatzbereit meldet und
die echten Jobs trotzdem umschwenken: dann unterscheidet sich der echte Encode in einem
Detail vom Prüflauf.

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
| `/config` | `/mnt/user/appdata/optimizarr` – Datenbank und Einstellungen |
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

Alles wird in der Oberfläche eingestellt – es gibt **keine Konfigurationsdateien**, und das
Verhalten hängt nicht von Umgebungsvariablen ab. Die Umgebungsvariablen legen nur fest, was
schon vor dem ersten Start feststehen muss, dazu ein Notausgang:

| Variable | Standard | Wofür |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | Benutzer und Gruppe, unter denen Optimizarr läuft (`0` = root) |
| `UMASK` | `002` | Rechte neu angelegter Dateien und Ordner |
| `TZ` | `Europe/Berlin` | Zeitzone für Zeitfenster und Protokoll |
| `OPTIMIZARR_CONFIG_DIR` | `/config` | Datenbank und Einstellungen |
| `OPTIMIZARR_TRANSCODE_DIR` | `/transcode` | Arbeitsordner für laufende Encodes |
| `OPTIMIZARR_MEDIA_ROOT` | `/media` | Startordner der Ordnerauswahl |
| `OPTIMIZARR_STATIC_DIR` | `/app/static` | Die gebaute Weboberfläche |
| `OPTIMIZARR_FFMPEG` / `OPTIMIZARR_FFPROBE` | – | Eigene ffmpeg-/ffprobe-Binärdatei statt der mitgelieferten |
| `OPTIMIZARR_RESET_AUTH` | – | `1` schaltet die Anmeldung beim Start ab (siehe [Sicherheit](#sicherheit)) |
| `CODEX_*` | – | Überschreibt Endpunkte der ChatGPT-Anmeldung, nur für die Entwicklung |

Die `OPTIMIZARR_*_DIR`-Pfade sind im Image passend gesetzt und müssen normalerweise nicht
angefasst werden.

Die drei Qualitätsprofile setzen CRF, Preset und Qualitätsziel gemeinsam:

| Profil | CRF | Für wen |
|---|---|---|
| **Archiv** | 24 | Sammlungen, bei denen jedes Detail zählt. Weniger Ersparnis, langsamster Encode. |
| **Ausgewogen** | 30 | Empfehlung. Deutliche Ersparnis, kaum sichtbarer Unterschied. |
| **Platz sparen** | 35 | Wenn Platz wichtiger ist als das letzte Prozent Bildqualität. |

Alles Weitere lässt sich einzeln nachjustieren: Tonspur-Behandlung (verlustfreie Spuren
nach Opus, das spart bei Blu-ray-Rips oft mehr als das Video selbst), Untertitel-Sprachen,
Zeitplan, Dateirechte, Schwellenwerte.

Einige Felder werden beim Speichern geprüft, weil sie auf der Kommandozeile oder im
Dateisystem landen:

* **Zusätzliche ffmpeg-Argumente** nehmen nur Encoder- und Muxer-Optionen mit je einem
  Wert an, z. B. `-svtav1-params tune=0`, `-g 240`, `-maxrate:v 8M`, `-metadata title=…`.
  Abgelehnt werden alles, was keine Option ist (ffmpeg würde es als zusätzliche
  Ausgabedatei schreiben), Pfade und URLs in Werten sowie `-y`, `-f`, `-i`, Filter
  (`-vf`, `-filter_complex` …), `-map`, `-c`, `-attach` und Optionen, die die Ausgabe
  kürzen. Die vollständige Liste steht in `backend/app/security.py`.
* **Dateirechte** oktal zwischen `0600` und `0777`, ohne setuid/setgid/Sticky-Bit.
* **Ausgabe- und Papierkorb-Ordner** müssen absolute Pfade sein und dürfen nicht `/` oder
  ein Systemordner (`/etc`, `/usr`, `/proc` …) sein.

---

## Benachrichtigungen

Unter **Einstellungen → Benachrichtigungen** lässt sich eine Webhook-URL eintragen.
Optimizarr schickt dorthin bei den gewählten Ereignissen – Job fertig, Job
fehlgeschlagen oder verworfen, Scan abgeschlossen – einen `POST` mit JSON:

```json
{
  "event": "job.finished",
  "title": "Konvertierung abgeschlossen: Film.mkv",
  "message": "…",
  "data": {"job_id": 12, "state": "done", "saved_bytes": 5368709120, "name": "Film.mkv"},
  "content": "Konvertierung abgeschlossen: Film.mkv: …",
  "text": "Konvertierung abgeschlossen: Film.mkv: …"
}
```

`content` und `text` enthalten dieselbe Zusammenfassung als eine Zeile, damit ein
Discord- (`content`) oder Slack-/Mattermost-Webhook (`text`) ohne Zwischenstück etwas
Lesbares anzeigt. Die Zustellung wartet höchstens 10 Sekunden; schlägt sie fehl, steht das
im Protokoll, am Job ändert sich nichts. **Test senden** in den Einstellungen schickt eine
Probenachricht. Die Webhook-URL enthält meist ein Token und wird deshalb wie ein Passwort
behandelt: Die Oberfläche zeigt sie nach dem Speichern nur noch als `********`.

---

## Sicherheit

**Anmeldung (optional).** Unter **Einstellungen → Sicherheit** lässt sich eine Anmeldung
mit Benutzername und Passwort einschalten (HTTP Basic Auth – der Browser fragt einmal
nach). Sie gilt für die Oberfläche, die API und die Live-Verbindung; frei bleibt nur
`/api/health` für den Docker-Healthcheck. Ohne Anmeldung kann jeder im Netzwerk, der den
Port erreicht, Optimizarr bedienen – und damit Dateien ersetzen lassen. Das Passwort wird
nur als PBKDF2-SHA256-Hash gespeichert. Basic Auth überträgt das Passwort bei jeder
Anfrage; wer Optimizarr außerhalb des Heimnetzes erreichbar macht, sollte einen
Reverse-Proxy mit HTTPS davorschalten. Der Proxy muss den `Host`-Header durchreichen
oder `X-Forwarded-Host` setzen (bei nginx, Nginx Proxy Manager, SWAG und Traefik der
Standard), sonst lehnt der Server die Live-Verbindung wegen fremder Herkunft ab.

**Passwort vergessen?** Den Container einmal mit der Umgebungsvariable
`OPTIMIZARR_RESET_AUTH=1` starten. Die Anmeldung ist dann abgeschaltet (das Protokoll
meldet es), in den Einstellungen lässt sich ein neues Passwort setzen. Danach die Variable
wieder entfernen – solange sie gesetzt ist, wird die Anmeldung bei jedem Start erneut
abgeschaltet.

**Eigene Skripte.** Jede schreibende API-Anfrage (`POST`, `PUT`, `PATCH`, `DELETE`) muss
den Header `X-Optimizarr: 1` mitschicken, sonst antwortet der Server mit 403. Das
verhindert, dass eine fremde Webseite im Browser unbemerkt Aktionen auslöst. Die
Oberfläche sendet ihn automatisch; in eigenen Skripten:

```bash
curl -u admin:PASSWORT -H 'X-Optimizarr: 1' -X POST http://tower:8474/api/scan
```

Lesende Anfragen brauchen den Header nicht. Die Live-Verbindung (`/api/ws`) nimmt nur
Verbindungen von der eigenen Seite an.

**Schlüssel.** API-Schlüssel, die Webhook-URL und das Passwort verlassen den Server nie
im Klartext: Die Einstellungen liefern für gesetzte Werte `********`. Wird `********`
unverändert zurückgeschickt, bleibt der gespeicherte Wert erhalten; ein leeres Feld
löscht ihn.

---

## Entwicklung

Backend und Dateiarbeit werden unter Linux mit Python 3.13 entwickelt und geprüft.
Unter Windows dafür WSL2 oder Docker verwenden. Die [Betriebsanleitung](docs/BETRIEB.md)
beschreibt Sicherung, Wiederherstellung, Aufbewahrung und Bibliotheksbenchmarks.

```bash
# Backend
python3.13 -m venv .venv && .venv/bin/pip install --require-hashes -r backend/dev-requirements.lock
OPTIMIZARR_CONFIG_DIR=./data/config OPTIMIZARR_TRANSCODE_DIR=./data/transcode \
  .venv/bin/uvicorn app.main:app --reload --app-dir backend --port 8080

# Frontend (Port 5173, leitet /api an 8080 weiter)
cd frontend && npm ci && npm run dev

# Tests
.venv/bin/pytest backend/tests -q
```

Die Anwendung braucht `ffmpeg` und `ffprobe` im Pfad; ohne sie funktionieren die
Metadaten- und Encoding-Teile nicht. Im Container kommt beides von `jellyfin-ffmpeg`, weil
das den Intel-QSV-Stack fertig verdrahtet mitbringt.

### Aufbau

```
backend/app/
  security.py      Anmeldung, CSRF-Header, Pruefung riskanter Einstellungen
  core/
    notify.py      Webhook-Benachrichtigungen
    ffmpeg.py      ffprobe/ffmpeg-Wrapper mit Fortschritts-Parsing
    hwaccel.py     Intel-GPU-Erkennung per echter Testkodierung
    predictor.py   Heuristik + gelerntes Korrekturmodell
    quality.py     VMAF/SSIM-Messung, Filmkorn-Schätzung
    planner.py     Analyse-Ergebnis -> ffmpeg-Kommandozeile
    analyzer.py    Entscheidungslogik (die drei Stufen)
    encoder.py     Job-Ausführung und Ergebnisverbuchung
    output_files.py Sichere Dateiersetzung, Speicher und Dateirechte
    output_validation.py Qualitätsprüfung fertiger Encodes
    trash.py       Protokollierter Papierkorb, Aufbewahrung und Wiederherstellung
    group_cache.py Zwischengespeicherte Gruppen, Suche und Seitennavigation
    scanner.py     Bibliotheks-Scan
    worker.py      Warteschlange und Zeitplan
    advisor/
      base.py              gemeinsamer Prompt, Schema, Antwort-Absicherung
      service.py           Budget, Anbieterwahl, Grenzwerte
      provider_anthropic.py
      provider_openai.py   OpenAI-kompatibel, mit Laufzeit-Aushandlung
      provider_codex.py    ChatGPT-Backend
      codex_oauth.py       PKCE-Anmeldung ohne lokalen Browser
frontend/src/      React + Tailwind
unraid/            Community-Applications-Template
```

---

## Lizenz

MIT

### Große Bibliotheken und Veröffentlichung

Film- und Serienübersichten verwenden serverseitige Suche, Sortierung und Seiten
(standardmäßig 50 Einträge). Gruppierungen und Summen werden zwischengespeichert und
nach Änderungen an Bibliothek oder Dateien verworfen. Sammelaktionen für Filme gelten
weiterhin für alle Treffer des Filters, auch auf anderen Seiten.

Die CI lädt das gebaute Docker-Image zunächst lokal, prüft den Start und führt einen
kurzen echten AV1-/Opus-Encode mit Metadatenprüfung und vollständiger Dekodierung aus.
Erst danach werden die Tags dieses bereits geprüften Images veröffentlicht. Bei Pull
Requests laufen dieselben Prüfungen ohne Veröffentlichung.
