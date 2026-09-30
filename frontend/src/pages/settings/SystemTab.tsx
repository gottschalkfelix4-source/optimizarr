import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, HardDrive, RotateCcw } from "lucide-react";
import { endpoints, type Settings, type SettingsSaveResult } from "../../lib/api";
import { bytes } from "../../lib/format";
import { useToast } from "../../lib/live";
import { Callout, Field, NumberField, Panel, Select } from "../../components/ui";
import type { UpdateFn } from "./types";

export function SystemTab({
  draft,
  update,
  onReset,
}: {
  draft: Settings;
  update: UpdateFn;
  onReset: (result: SettingsSaveResult) => void;
}) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const { data: info } = useQuery({ queryKey: ["system"], queryFn: endpoints.systemInfo });

  const reset = useMutation({
    mutationFn: () => endpoints.resetSettings(),
    onSuccess: (result) => {
      push("Einstellungen zurückgesetzt.", "success");
      onReset(result);
      queryClient.invalidateQueries();
    },
    onError: (e: Error) => push(e.message, "error"),
  });
  const backup = useMutation({
    mutationFn: endpoints.createBackup,
    onError: (e: Error) => push(e.message, "error"),
  });
  const maintenance = useMutation({
    mutationFn: endpoints.runMaintenance,
    onSuccess: (counts) => {
      push(`${Object.values(counts).reduce((sum, count) => sum + count, 0)} alte Einträge entfernt.`, "success");
      queryClient.invalidateQueries();
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  return (
    <div className="space-y-4">
      <Panel title="Oberfläche">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Größenangaben"
            hint="Binär rechnet in 1024er-Schritten wie Unraid und Linux (GiB), dezimal in 1000er-Schritten wie Festplattenhersteller (GB)."
          >
            <Select
              value={draft.ui.size_unit}
              onChange={(size_unit) => update("ui", { size_unit })}
              ariaLabel="Größenangaben"
              options={[
                { value: "binary", label: "Binär (KiB, MiB, GiB)" },
                { value: "decimal", label: "Dezimal (kB, MB, GB)" },
              ]}
            />
          </Field>
          <Field
            label="Übersicht aktualisieren alle"
            hint="Wie oft die Übersicht Zahlen und laufende Jobs neu abfragt. Live-Ereignisse kommen unabhängig davon sofort an."
          >
            <NumberField
              value={draft.ui.dashboard_refresh_seconds}
              onChange={(dashboard_refresh_seconds) => update("ui", { dashboard_refresh_seconds })}
              min={1}
              max={60}
              suffix="Sek"
              ariaLabel="Übersicht aktualisieren alle"
            />
          </Field>
        </div>
      </Panel>

      <Panel title="System">
        <dl className="space-y-2 text-sm">
          {[
            ["Version", info?.version],
            ["Python", info?.python],
            ["Plattform", info?.platform],
            ["CPU-Kerne", info?.cpu_count ? String(info.cpu_count) : undefined],
            ["Konfiguration", info?.paths.config],
            ["Arbeitsverzeichnis", info?.paths.transcode],
            [
              "Freier Speicher (Arbeitsverzeichnis)",
              info ? bytes(info.paths.transcode_free_gb * 1024 ** 3, 0) : undefined,
            ],
          ].map(([label, value]) => (
            <div key={String(label)} className="flex flex-wrap justify-between gap-2">
              <dt className="text-ink-500">{label}</dt>
              <dd className="font-mono text-xs text-ink-300">{value ?? "-"}</dd>
            </div>
          ))}
        </dl>
      </Panel>

      <Panel title="Aufbewahrung und Sicherung">
        <div className="grid gap-5 md:grid-cols-2">
          {([
            ["history_retention_days", "Verlauf aufbewahren", 0, 3650],
            ["job_retention_days", "Abgeschlossene Jobs aufbewahren", 0, 3650],
            ["scan_retention_days", "Scanverlauf aufbewahren", 0, 3650],
            ["restored_manifest_retention_days", "Protokolle abgeschlossener Wiederherstellungen", 0, 3650],
            ["max_learning_samples", "Lernmessungen behalten", 2000, 100000],
            ["max_backups", "Sicherungen behalten", 1, 100],
          ] as const).map(([key, label, min, max]) => <Field key={key} label={label} hint={key.endsWith("days") ? "Tage; 0 bewahrt dauerhaft auf." : undefined}>
            <NumberField value={draft.maintenance[key]} onChange={(value) => update("maintenance", { [key]: value })} min={min} max={max} ariaLabel={label} />
          </Field>)}
        </div>
        <p className="hint mt-4">Die Bereinigung läuft täglich. Konvertierte Dateien und ihre verbuchte Ersparnis bleiben erhalten. Offene Jobs und Papierkorb-Originale haben eigene Aufbewahrungsregeln.</p>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button className="btn-ghost" disabled={maintenance.isPending} onClick={() => maintenance.mutate()}>Jetzt bereinigen</button>
          <button className="btn-ghost" disabled={backup.isPending} onClick={() => backup.mutate()}>Konfiguration sichern</button>
          {backup.data && <a className="btn-ghost" href={backup.data.download_url} download>Sicherung herunterladen</a>}
        </div>
        <p className="hint mt-3">Für eine Sicherung müssen laufende Jobs und Scans beendet sein. Sie enthält Datenbank, Einstellungen, Anmeldedaten und Wiederherstellungsprotokolle. Mediendateien separat sichern. <a className="text-brand-400 underline" href="https://github.com/gottschalkfelix4-source/optimizarr/blob/main/docs/BETRIEB.md" target="_blank" rel="noreferrer">Anleitung zur Wiederherstellung bei gestopptem Container</a></p>
      </Panel>

      <Panel title="Zurücksetzen" subtitle="Setzt alle Einstellungen auf die Werkseinstellung zurück">
        <Callout tone="warn" icon={<AlertTriangle className="size-4" />}>
          Bibliothekspfade, gefundene Dateien und der Verlauf bleiben erhalten – nur die
          Einstellungen werden zurückgesetzt. Gespeicherte API-Schlüssel und der Webhook werden
          dabei gelöscht; die Anmeldedaten unter „Sicherheit“ bleiben erhalten.
        </Callout>
        <button
          className="btn-danger mt-4"
          onClick={() => {
            if (window.confirm("Alle Einstellungen auf Standardwerte zurücksetzen?")) {
              reset.mutate();
            }
          }}
          disabled={reset.isPending}
        >
          <RotateCcw className="size-4" aria-hidden="true" />
          Einstellungen zurücksetzen
        </button>
      </Panel>

      <Panel title="Speicherorte">
        <div className="flex items-start gap-3 text-sm text-ink-400">
          <HardDrive className="mt-0.5 size-4 shrink-0 text-ink-500" aria-hidden="true" />
          <p className="leading-relaxed">
            Datenbank und Einstellungen liegen unter <code className="text-ink-300">/config</code>,
            Zwischendateien beim Encoden unter <code className="text-ink-300">/transcode</code>. In
            Unraid sollte <code className="text-ink-300">/transcode</code> auf einer SSD oder im
            Cache-Pool liegen – dort entsteht während des Encodens die komplette Ausgabedatei.
            Ersetzte Originale landen, sofern kein eigener Papierkorb eingestellt ist, im
            versteckten Ordner <code className="text-ink-300">.optimizarr-trash</code> der
            jeweiligen Bibliothek.
          </p>
        </div>
      </Panel>
    </div>
  );
}
