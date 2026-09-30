# Optimizarr Projektprüfung und Verbesserungen

**Historischer Befundbericht zu `2241f1d`.** Die folgenden acht Bugs und zehn
Verbesserungspunkte wurden inzwischen abgearbeitet. Der aktuelle Stand und die
Prüfnachweise stehen in [ABARBEITUNG.md](ABARBEITUNG.md); Betriebsabläufe stehen in
[BETRIEB.md](BETRIEB.md). Beschriebene Fehler und alte Testgrenzen beziehen sich
ausschließlich auf den damaligen Prüfstand.

Stand: 30. September 2026. Geprüfter Commit: `2241f1d`. Die Prüfung umfasst Backend, zentrale Frontend-Abläufe, Einstellungen, Dateisicherheit, Analyse und Qualitätsmessung, Warteschlange, Papierkorb, Datenbank, KI-Anbindung und Docker/CI.

Es wurden **acht Fehler gezielt in einer isolierten Umgebung reproduziert**. Drei haben hohe Priorität: Im Sidecar-Modus kann die Quelle ohne Sicherung überschrieben werden, die Wiederanlaufbereinigung kann eine fremde Datei löschen, und Fehler beim Laden der Sicherheitseinstellungen können die Anmeldung abschalten. Diese drei Punkte sollten vor einer weiteren Veröffentlichung behoben werden.

Die Anwendung hat bereits eine gute Grundlage: umfangreiche Tests, protokollierte Originalsicherung, Schutz vor veränderten Quelldateien, getrennte Prüfung von Hardware-Encoding und -Decoding sowie eine klare Unterscheidung zwischen gemessenem VMAF und SSIM-Schätzung. Die folgenden Befunde betreffen konkrete Lücken in diesen Abläufen.

## Prioritäten

P1 bedeutet hohe Priorität wegen möglichem Dateiverlust oder aufgehobener Anmeldung. P2 bezeichnet relevante Funktionsfehler. Die Verbesserungsvorschläge sind getrennt von den reproduzierten Bugs aufgeführt.

| Befund | Priorität | Fehler |
| --- | --- | --- |
| B1 | P1 | Sidecar und leerer separater Ausgabeordner können die Quelle überschreiben |
| B2 | P1 | Wiederanlaufbereinigung löscht eine später angelegte fremde Zieldatei |
| B3 | P1 | Fehler beim Laden der Sicherheitseinstellungen geben Anfragen frei |
| B4 | P2 | Ohne bisherigen Scan wird der erste periodische Scan nie fällig |
| B5 | P2 | Eine leere Wochentagsauswahl erlaubt Encoding an allen Tagen |
| B6 | P2 | Sammelaktionen reihen ignorierte und übersprungene Dateien ohne Erzwingen ein |
| B7 | P2 | Erneute Analyse verliert den Status einer konvertierten Datei |
| B8 | P2 | Unerwartete Fehler einzelner Scan-Aufgaben werden als Erfolg verbucht |

## Reproduzierte Bugs

### B1 Ausgabe ohne Ersetzen kann das Original überschreiben

**Fundstellen:** [Zielpfadberechnung](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/output_files.py#L277), [Dateiersetzung](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/output_files.py#L481), [Ausgabeeinstellungen](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/config.py#L181) und [Namenszusatz in der Oberfläche](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/frontend/src/pages/settings/OutputTab.tsx#L109).

**Auslöser:** Eine MKV-Quelle wird nach MKV ausgegeben, der Modus steht auf `sidecar`, und `sidecar_suffix` ist leer. Die Oberfläche lässt das Feld leeren; das Backend akzeptiert den Wert. Der berechnete Zielpfad ist dann identisch mit der Quelle. Auch `separate_dir` mit leerem `output_dir` fällt auf den Pfad der Quelle mit neuer Container-Endung zurück.

**Folge:** Im Zweig für Ausgaben ohne Ersetzen führt `_stage_and_replace` unmittelbar `os.replace(staging, target)` aus. Originalsicherung, Papierkorb und Prüfung der unveränderten Quelle werden umgangen. Bei gleichem Quell- und Zielnamen geht das Original verloren. Die anschließende Verbuchung behauptet außerdem, das Original sei unberührt geblieben.

**Nachweis:** Mit einer temporären Quelle `film.mkv`, Inhalt `ORIGINAL`, und einer Ausgabe mit Inhalt `ENCODED` wurde `_commit_output` im Sidecar-Modus mit leerem Suffix ausgeführt. Anschließend enthielt die Quelle `ENCODED`; es wurde keine Originalsicherung angelegt. Der separate Modus mit leerem Ordner ergibt bei derselben Endung ebenfalls denselben Zielpfad; dieser zweite Auslöser wurde anhand der Zielpfadberechnung geprüft.

**Korrektur:** Bei allen Modi außer `replace` vor jeder Mutation sicherstellen, dass Quelle und Ziel verschieden sind. Dabei auch vorhandene Hardlinks und Symlinks berücksichtigen. Zusätzlich einen nichtleeren Sidecar-Suffix und einen gesetzten Ausgabeordner für `separate_dir` validieren. Vorhandene Ziele nur mit nachgewiesener Zugehörigkeit zu einer früheren Ausgabe überschreiben.

### B2 Wiederanlaufbereinigung kann eine fremde Datei löschen

**Fundstellen:** [Journal und Fehlerbehandlung](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/output_files.py#L391) und [Rollback der Dateiersetzung](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/output_files.py#L581).

**Auslöser:** Für `film.avi → film.mkv` wird ein Journal geschrieben. Die Dateiersetzung scheitert anschließend, beispielsweise wegen fehlenden Speicherplatzes, bevor die Staging-Datei existiert. Das Journal bleibt liegen. Vor dem nächsten Start legt ein externes Programm eine eigene `film.mkv` an.

**Folge:** `_roll_back_commit` sieht eine vorhandene Quelle, ein vorhandenes Ziel und keine Staging-Datei. Es löscht das Ziel, ohne zu prüfen, ob diese Datei tatsächlich durch den abgebrochenen Vorgang erzeugt wurde. Ein regulär fehlgeschlagener Versuch genügt; ein harter Prozessabbruch ist für diesen Fall nicht notwendig.

**Nachweis:** Ein Fehler zu Beginn von `_stage_and_replace` wurde gezielt simuliert. Danach wurde eine fremde Datei am Ziel angelegt und `recover_interrupted_commits()` aufgerufen. Ergebnis: Ein Journal wurde als bereinigt gezählt und die fremde Datei war gelöscht.

**Korrektur:** Journale mit expliziten Phasen und Dateifingerabdrücken versehen. Ein Ziel nur entfernen, wenn seine Identität zur protokollierten Ausgabe passt. Nach einem nachweislich vollständig zurückgenommenen Fehlversuch das Journal sicher abschließen. Bei unklarer Identität einen sichtbaren Konflikt melden und Dateien erhalten.

### B3 Lesefehler können die Anmeldung abschalten

**Fundstellen:** [Laden der Einstellungen](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/config.py#L470), [Autorisierung](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/security.py#L181) und [erneutes Laden über die API](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/api/routes_system.py#L125).

**Auslöser:** Beim erzwungenen Neuladen der Einstellungen tritt ein Datenbankfehler auf. Solches Neuladen erfolgt unter anderem beim Abruf der Einstellungen und beim Scan. Alternativ wirft die Funktion, die Sicherheitseinstellungen liefert, direkt einen Fehler.

**Folge:** `load_settings` fängt jeden Lesefehler ab, verwendet leere Daten und ersetzt den Cache durch Standardeinstellungen. Deren Anmeldung ist deaktiviert. Zusätzlich beantwortet `_authorized` einen Fehler beim Laden der Sicherheitseinstellungen mit `True`. Nachfolgende Anfragen können dadurch ohne Zugangsdaten zugelassen werden. Auch andere Einstellungen fallen dabei auf Standards zurück, etwa die Pause der Warteschlange.

**Nachweis:** Zunächst wurde eine aktivierte Anmeldung gespeichert. Mit einem simulierten Datenbankfehler lieferte `load_settings(force=True)` anschließend `auth_enabled=False`. Ein separater Aufruf von `_authorized` mit fehlerhafter Einstellungsfunktion und ohne Authorization-Header lieferte `True`.

**Korrektur:** Fehlende Erstkonfiguration von fehlgeschlagenem Lesen unterscheiden. Bei einem Lesefehler einen bekannten gültigen Cache erhalten und die Störung melden; ohne gültige Sicherheitskonfiguration geschützte Anfragen ablehnen oder mit 503 beantworten. Der dokumentierte Notausgang `OPTIMIZARR_RESET_AUTH` bleibt die ausdrückliche Möglichkeit, die Anmeldung zurückzusetzen.

### B4 Der erste periodische Scan wird dauerhaft verschoben

**Fundstelle:** [Berechnung des nächsten Scans](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/worker.py#L449).

**Auslöser:** Es gibt noch keinen Eintrag in `scan_runs`, ein Scanintervall ist aktiviert, und der Startscan ist ausgeschaltet oder wurde mangels Bibliothekspfaden nicht ausgeführt. Der Nutzer richtet später einen Bibliothekspfad ein und wartet auf den automatischen Scan.

**Folge:** `_compute_next` verwendet bei jeder Prüfung die aktuelle Uhrzeit als Basis. Aus `jetzt + Intervall` wird beim nächsten Scheduler-Durchlauf erneut `jetzt + Intervall`. Der Termin wird nie erreicht.

**Nachweis:** Bei leerer Scan-Historie ergab die Berechnung am 30. September um 10 Uhr UTC den 1. Oktober um 10 Uhr. Nach Vorstellen der Testuhr um drei Tage ergab sie den 4. Oktober um 10 Uhr. Der geplante Termin wanderte um dieselben drei Tage weiter.

**Korrektur:** Eine stabile Basis verwenden, beispielsweise den Zeitpunkt der Scheduler-Initialisierung oder der Aktivierung des Intervalls. Alternativ bei vorhandenen Bibliotheken und fehlender Historie einmalig sofort scannen. Den Fall ohne Scan-Historie ausdrücklich testen.

### B5 Eine leere Wochentagsauswahl erlaubt alle Tage

**Fundstellen:** [Zeitplanprüfung](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/worker.py#L41), [Validierung der Wochentage](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/config.py#L247) und [Hinweis in der Oberfläche](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/frontend/src/pages/settings/QueueTab.tsx#L195).

**Auslöser:** Die API erhält bei aktivem Zeitplan `schedule_days: []`. Die Oberfläche verhindert normalerweise das Abwählen des letzten Tages, das Backend nimmt die leere Liste jedoch an. Auch Werte außerhalb von 0 bis 6 sind derzeit nicht begrenzt.

**Folge:** `cfg.schedule_days or list(range(7))` interpretiert die leere Auswahl als alle Tage. Das widerspricht dem Oberflächenhinweis, wonach ohne ausgewählten Tag kein Job startet.

**Nachweis:** Ein aktivierter Zeitplan mit leerer Tagesliste und dem Fenster `00:00–23:59` lieferte für einen Mittwochmittag `allowed=True`.

**Korrektur:** Wochentage auf 0 bis 6 begrenzen und Duplikate normalisieren. Bei aktivem Zeitplan eine leere Auswahl mit 422 ablehnen oder eindeutig als kein erlaubter Tag behandeln. API, Worker und Oberfläche müssen dieselbe Regel verwenden.

### B6 Sammelaktionen umgehen Ignorieren und Überspringen

**Fundstellen:** [Einreihen von Dateien](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/worker.py#L513), [Sammelaktion in der Bibliothek](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/frontend/src/pages/Library.tsx#L268) und [Beginn eines Jobs](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/encoder.py#L235).

**Auslöser:** Eine Datei wurde analysiert und besitzt noch einen Plan. Sie steht anschließend auf `ignored` oder `skipped`. Die Datei wird über die Sammelauswahl oder direkt über `/api/jobs` ohne `force` eingereiht.

**Folge:** `enqueue_files` prüft, ob die Datei fehlt, ob ein Plan vorhanden ist und ob bereits ein aktiver Job existiert. Es prüft ohne Erzwingen weder den Kandidatenstatus noch `ignored`. Ein bestehender Plan genügt, um die Datei einzureihen. Auch `_start_job` hält sie nicht wegen des Ignore-Flags an. Damit gelten andere Regeln als beim Einzelknopf „Trotzdem konvertieren“.

**Nachweis:** Eine ignorierte Datei und eine übersprungene Datei mit vorhandenen Plänen wurden mit `force=False` übergeben. Beide wurden eingereiht; Rückgabewert `added=2`.

**Korrektur:** Ohne `force` nur nichtignorierte Kandidaten akzeptieren. Erzwingen explizit behandeln und den vorherigen Zustand für Abbruch und Wiederholung erhalten. Die Sammelauswahl muss dieselben Regeln und Erklärungen wie die Einzelaktion verwenden. Bereits konvertierte Dateien ebenfalls gesondert behandeln.

### B7 Erneute Analyse verliert den Konvertierungsstatus

**Fundstellen:** [Einzelanalyse](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/api/routes_library.py#L441), [Speichern der Metadaten](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/scanner.py#L624), [Speichern der Analyse](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/scanner.py#L638) und [Analyseknopf](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/frontend/src/pages/Library.tsx#L585).

**Auslöser:** Eine erfolgreich ersetzte Datei mit Zustand `done` wird im Dateidialog erneut analysiert. Der Knopf ist auch für konvertierte Dateien verfügbar, und der Endpunkt hat keine entsprechende Zustandsprüfung.

**Folge:** `_store_probe` setzt den Zustand auf `probed`; nur `queued` und `encoding` sind davon ausgenommen. Die anschließende AV1-Analyse setzt ihn auf `skipped`. Der gespeicherte Konvertierungserfolg geht als Status verloren. Die Statistik berücksichtigt reale Ersparnis nur für Dateien im Zustand `done`, sodass zuvor verbuchte Ersparnis aus der Übersicht verschwindet. Für ignorierte Dateien kann die Einzelanalyse ebenfalls einen widersprüchlichen Status erzeugen.

**Nachweis:** Eine AV1-Datei mit `done`, `original_size=10000`, `size=5000` und gesetztem Konvertierungsdatum wurde durch dieselben beiden Speicherfunktionen geführt. Ihr Zustand war danach `skipped`.

**Korrektur:** Konvertierungsergebnis und aktuelle Analyseentscheidung getrennt speichern oder terminale beziehungsweise ausdrücklich ignorierte Zustände beim Speichern der Analyse erhalten. Eine Metadatenauffrischung darf den Konvertierungserfolg und die realisierte Ersparnis nicht zurücksetzen. Den Ablauf auch über den sichtbaren Analyseknopf testen.

### B8 Scan-Aufgaben verlieren unerwartete Fehler

**Fundstellen:** [Sammlung paralleler Aufgaben](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/scanner.py#L942), [Probe-Aufgabe](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/scanner.py#L763) und [Abschluss des Scans](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/scanner.py#L889).

**Auslöser:** Eine einzelne Probe- oder Speicheraufgabe wirft eine unerwartete Ausnahme, die nicht von ihrer lokalen Fehlerbehandlung aufgefangen wird, etwa einen Datenbankfehler oder einen unerwarteten Fehler des Wrappers.

**Folge:** `_gather_limited` verwendet `asyncio.gather(..., return_exceptions=True)`, wertet die zurückgegebenen Ausnahmen aber nicht aus. `run_scan` kann den Lauf daher als erfolgreich abschließen, obwohl Dateien unbearbeitet geblieben sind. Für diese Fehler fehlt dann auch eine passende Fehlermeldung pro Datei.

**Nachweis:** In einer gezielten Einzeldatei-Scan-Reproduktion warf der Probe-Aufruf `RuntimeError`. Der Scan lieferte dennoch `ok=True`, `probed=0`, `analyzed=0` und `error=""`.

**Korrektur:** Ergebnisse der Aufgaben auswerten, unerwartete Ausnahmen mit Dateibezug protokollieren und Fehler zählen. Einen teilweise fehlgeschlagenen Scan als solchen anzeigen. Erwartete Dateifehler dürfen weiter isoliert werden, sollen aber in Abschlussstatus und Zusammenfassung sichtbar bleiben.

## Verbesserungspunkte

### 1 Regressionstests für Benutzerabläufe ergänzen

Die bestehenden Tests decken viele einzelne Schutzfunktionen ab. Ergänzen sollten sie die acht obigen Auslöser, insbesondere den vollständigen Ablauf von Einstellung über API bis Dateiersetzung. Im Frontend fehlen eigene Tests für die Bibliotheksseite und ihre Sammelaktionen. Sinnvolle Fälle sind leere Ausgabefelder, ignorierte Dateien in einer Auswahl über mehrere Seiten und die erneute Analyse einer konvertierten Datei. Erwartetes Ergebnis sind erhaltene Originale, eindeutige Zustände und konsistente Statistiken.

### 2 Dateiersetzung und Datenbankverbuchung gemeinsam absichern

Das Dateijournal wird in `_commit_output` entfernt, bevor `run_job` die nachträgliche Probe und `_record_success` abgeschlossen hat. Zwischen erfolgreich geänderter Datei und gespeicherten Job- und Bibliotheksdaten besteht damit eine Wiederherstellungslücke. Dieser zusätzliche Absturzfall wurde nicht vollständig reproduziert und ist ein offener Prüfpunkt. Ein Journal sollte Dateiersetzung und Datenbankabschluss als getrennte Phasen erfassen und beim Neustart beide Seiten abgleichen. Gezielte Abbruchtests vor und nach jedem Phasenwechsel sind hier besonders wertvoll.

### 3 Temporäre Dateien und Ressourcen pro Aufruf isolieren

Der [Trockenlauf](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/api/routes_library.py#L578) verwendet einen festen Dateinamen pro Datei-ID. Zwei gleichzeitige Anfragen für dieselbe Datei teilen sich deshalb Ausgabe und Aufräumen. Jeder Aufruf sollte ein eigenes temporäres Verzeichnis oder eine UUID bekommen. Zusätzlich sollten parallele Jobs Speicherbedarf reservieren: Die aktuelle Prüfung kontrolliert lediglich einen festen freien Mindestwert, ohne erwartete Ausgabengrößen und bereits gestartete Jobs gemeinsam zu berücksichtigen. Gleichzeitige Anfragen und mehrere große Jobs sollten gezielt getestet werden.

### 4 Scan und Hintergrundaufgaben kontrolliert beenden

Die Verzeichnisbegehung und Datenbanksynchronisierung prüfen derzeit keinen Abbruchhinweis innerhalb ihrer Schleifen. Ein Abbruch während eines großen Walks wartet dadurch auf dessen Abschluss. Ein threadtaugliches Abbruchsignal sollte regelmäßig geprüft werden. Beim Herunterfahren sollten außerdem die Aufgaben des Schedulers sowie Scan-Aufgaben aus API und Einstellungsänderungen gemeinsam nachverfolgt und innerhalb einer definierten Frist abgewartet werden. So wird auch klar, wann Dateiarbeit wirklich beendet ist.

### 5 Qualitätsprüfung um Messabdeckung ergänzen

Die [Qualitätsstichprobe](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/output_validation.py#L27) bildet den Durchschnitt der erfolgreichen Messungen. Wenn ein Ausschnitt scheitert, kann der andere allein die Annahme entscheiden. Festlegen sollte man eine Mindestzahl erfolgreicher Ausschnitte und neben dem Mittelwert auch den schlechtesten Ausschnitt anzeigen. Als optionale strengere Integritätsprüfung sind echte Dekodiertests sinnvoll: Ein erfolgreicher ffprobe-Aufruf mit passenden Laufzeiten bestätigt keine vollständig fehlerfreie Videospur. Das ist ein zusätzlicher Schutzvorschlag, kein in dieser Prüfung reproduzierter Schaden.

### 6 Große Bibliotheken anhand messbarer Ziele optimieren

Trotz serverseitiger Seitennavigation laden Gruppierung und Scans komplette Bibliotheksbestände in den Python-Speicher. [Gruppensnapshots](https://github.com/gottschalkfelix4-source/optimizarr/blob/2241f1d/backend/app/core/group_cache.py#L49) werden unter einer gemeinsamen Sperre erstellt; ein teurer Neuaufbau hält dadurch weitere Cache-Zugriffe auf. Mit 10000 und 100000 Einträgen sollten Speicherbedarf, Antwortzeiten und Scan-Abbruchzeit gemessen werden. Anschließend teure Arbeit außerhalb der globalen Sperre ausführen, Ergebnisse mit Versionsprüfung veröffentlichen und Scan-Synchronisierung bei Bedarf in begrenzten Mengen bearbeiten.

### 7 API-Validierung und Grenzwerte vereinheitlichen

Einige Aktionen verwenden untypisierte Wörterbücher, etwa die Queue-Sortierung. Ungültige Job-IDs können dort beim `int()`-Aufruf einen 500-Fehler auslösen. Dafür sind Pydantic-Modelle, begrenzte Listenlängen und eindeutige Fehlerantworten sinnvoll. Auch `depth`, `force_encoder`, Zeitplantage und vom Modus abhängige Ausgabeoptionen sollten nur erlaubte Werte akzeptieren. Widersprüchliche CRF-Grenzen sollten entweder abgewiesen oder beim Speichern sichtbar normalisiert werden.

### 8 Datenbankwachstum und Sicherung planbar machen

Verlauf, abgeschlossene Jobs, Jobprotokolle und Wiederherstellungsmanifeste wachsen langfristig. Das Lernmodell verwendet zwar höchstens 2000 aktuelle Trainingsbeispiele, die Speicherung hat dadurch aber keine Aufbewahrungsgrenze. Ergänzen sollte man konfigurierbare Aufbewahrung und eine konsistente SQLite-Sicherung. Bei WAL-Betrieb genügt das Kopieren nur der geöffneten Hauptdatei nicht als verlässlicher Sicherungsablauf. Eine Export- und Wiederherstellungsprobe sollte zur dokumentierten Wartung gehören; Konvertierungsstatistiken und noch benötigte Originalsicherungen müssen erhalten bleiben.

### 9 Angezeigte Modellgüte außerhalb der Trainingsdaten messen

`mean_abs_error_pct` wird aus den Residuen derselben Daten berechnet, auf denen das Modell trainiert wurde. Das misst die Anpassung an bekannte Daten. Für die Frage, wie zuverlässig die nächste Größenvorhersage ist, ist zusätzlich ein Fehler auf späteren, noch nicht zum Training verwendeten Ergebnissen nötig. Trainingsfehler, Prognosefehler und Messanzahl sollten getrennt benannt werden. CPU, QSV und VAAPI können zusätzlich getrennt ausgewertet werden, sobald ausreichend Daten vorliegen.

### 10 Entwicklungsumgebung und CI reproduzierbar halten

Die Anwendung und etliche Tests setzen Linux-Pfade, Unix-Signale und Verzeichnissynchronisierung voraus. Das sollte für lokale Entwicklung ausdrücklich dokumentiert werden; alternativ sind klare Plattformmarkierungen und Windows-Unterstützung nötig. Tests für Codex-Standardwerte sollten die zugehörigen `CODEX_*`-Overrides selbst neutralisieren. Der Docker-Smoke-Test läuft derzeit mit `PUID=0`; zusätzlich sollte ein Lauf mit dem Standardbenutzer und schreibbaren Volumes den wichtigen Rechtepfad prüfen. Für reproduzierbare Images sind vollständige Python-Abhängigkeitsauflösung und kontrollierte Aktualisierung von Basisimage und ffmpeg-Paket sinnvoll.

## Validierung und Grenzen

Die lokalen Prüfungen liefen unter Windows mit Python 3.12.14 und Node.js 24.15.0. Die Backend-CI verwendet Python 3.13 unter Linux; dieser Unterschied begrenzt die Aussage des lokalen Gesamtlaufs.

| Prüfung | Ergebnis |
| --- | --- |
| Frontend `npm test` | 98 Tests in 9 Dateien bestanden |
| Frontend `npm run typecheck` | Bestanden |
| Frontend `npm run build` | Bestanden |
| Backend `pytest backend/tests -q --tb=short` | 610 bestanden und 68 fehlgeschlagen im ersten Windows-Lauf |
| Codex-Tests nach Neutralisieren von `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` | Alle 32 bestanden; erklärt drei Fehler des ersten Gesamtlaufs |
| Gezielte Reproduktionen B1 bis B8 | Alle acht beschriebenen Fehlverhalten beobachtet |
| Docker- und Unraid-Laufzeitprüfung | Nicht ausgeführt, da der lokale Linux-Docker-Daemon nicht erreichbar war |

Die verbleibenden Fehler des ursprünglichen Backend-Laufs umfassen Linux-Pfadannahmen, nicht unterstützte Verzeichnissynchronisierung unter Windows, Unix-basierte Prozess-Testprogramme und VMAF-Probleme mit Windows-Pfaden. Sie sind **kein Nachweis von 65 weiteren Bugs im Linux-Container**. Ein vollständiger Lauf in der vorgesehenen Linux-Umgebung bleibt erforderlich; ein grüner Backend-Gesamtlauf wird hier nicht behauptet.

Die gezielten Reproduktionen liefen mit temporären Dateien und einer eigenen Datenbank. Datenbank- und Probe-Ausfälle wurden absichtlich simuliert. Für Dateireproduktionen wurden ausschließlich die unter Windows nicht verfügbaren Verzeichnissynchronisierung und der dort abweichende Dateideskriptor für `fsync` angepasst. Zielpfadberechnung, Dateibewegungen, Journal und Wiederanlaufbereinigung wurden tatsächlich ausgeführt. Es wurden keine Benutzer-Mediendateien verwendet und keine KI-Anfragen an externe Anbieter gesendet.

Ein [Befundskript](review_checks_2241f1d.py) macht die acht Reproduktionen nachvollziehbar. Es zeigt das damalige Fehlverhalten von `2241f1d`; es ist keine grüne Regressionstestsuite. Nach einer Korrektur müssen seine Erwartungen in passende Tests mit dem gewünschten Verhalten überführt werden.

Mit installierten Backend-Abhängigkeiten lässt sich das Skript aus dem Projektordner über `python docs/review_checks_2241f1d.py /pfad/zum/checkout-von-2241f1d` ausführen. Es erzeugt ausschließlich eigene Testdaten in einem temporären Ordner und zeigt dessen Pfad am Ende an.

## Empfohlene Reihenfolge

1. B1, B2 und B3 mit gezielten Tests beheben, dann die Dateischutztests unter Linux ausführen.
2. B6 und B7 korrigieren, damit Benutzerentscheidungen und Konvertierungsergebnisse erhalten bleiben.
3. B4, B5 und B8 beheben und die dazugehörigen API- und Oberflächenabläufe prüfen.
4. Die zusätzliche Lücke zwischen Dateiersetzung und Datenbankabschluss untersuchen.
5. Parallelität, Qualitätsabdeckung, große Bibliotheken und Wartung anhand der genannten Verbesserungspunkte weiterentwickeln.

Anwendungsdateien wurden für diese Prüfung nicht korrigiert. Neu hinzugekommen sind dieser Bericht und das isolierte Befundskript.
