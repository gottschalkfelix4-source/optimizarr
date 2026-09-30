import { ShieldCheck } from "lucide-react";
import { type Settings } from "../../lib/api";
import { Callout, Field, NumberField, Panel, Select, SliderField, Toggle } from "../../components/ui";
import type { UpdateFn } from "./types";

export function OutputTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  return (
    <div className="space-y-4">
      <Callout tone="success" icon={<ShieldCheck className="size-4" />}>
        Diese Regeln entscheiden, ob ein fertiger Encode überhaupt behalten wird. Fällt auch nur
        eine Prüfung durch, wird das Ergebnis gelöscht und das Original bleibt exakt so, wie es
        war.
      </Callout>

      <Panel title="Prüfregeln">
        <div className="space-y-4">
          <Toggle
            checked={draft.output.require_smaller}
            onChange={(require_smaller) => update("output", { require_smaller })}
            label="Ergebnis muss kleiner sein als das Original"
            hint="Verhindert größere Ergebnisse. „Trotzdem konvertieren“ und H.264 bei aktivierter vollständiger Umstellung sind davon ausgenommen."
          />
          <Field
            label="Mindestersparnis zum Behalten"
            hint="Ergebnisse unter dieser Ersparnis werden verworfen. „Trotzdem konvertieren“ und H.264 im Umstellungsmodus sind davon ausgenommen."
          >
            <SliderField
              value={draft.output.min_accept_saving_percent}
              onChange={(min_accept_saving_percent) =>
                update("output", { min_accept_saving_percent })
              }
              min={0}
              max={50}
              format={(v) => `${v} %`}
              ariaLabel="Mindestersparnis zum Behalten"
            />
          </Field>
          <Toggle
            checked={draft.output.verify_output}
            onChange={(verify_output) => update("output", { verify_output })}
            label="Ergebnisdatei nach dem Encode prüfen"
            hint="Kontrolliert Codec, Laufzeit und Lesbarkeit, bevor irgendetwas ersetzt wird."
          />
          <Field label="Erlaubte Laufzeit-Abweichung">
            <NumberField
              value={draft.output.max_duration_drift_seconds}
              onChange={(max_duration_drift_seconds) =>
                update("output", { max_duration_drift_seconds })
              }
              min={0.1}
              max={60}
              step={0.5}
              suffix="Sek"
              ariaLabel="Erlaubte Laufzeit-Abweichung"
            />
          </Field>
          <Toggle
            checked={draft.output.verify_full_decode}
            onChange={(verify_full_decode) => update("output", { verify_full_decode })}
            label="Gesamte Video- und Tonspuren decodieren"
            hint="Erkennt beschädigte Pakete hinter lesbaren Metadaten. Liest die gesamte Ausgabe und benötigt zusätzliche Zeit."
          />
          <Toggle
            checked={draft.output.verify_vmaf}
            onChange={(verify_vmaf) => update("output", { verify_vmaf })}
            label="Qualität der fertigen Datei messen (VMAF / SSIM)"
            hint="Sehr gründlich, kostet aber zusätzliche Rechenzeit pro Datei."
          />
          {draft.output.verify_vmaf && (
            <div className="space-y-4">
            <Field label="Erforderliche erfolgreiche Messungen" hint="Zwei Ausschnitte werden geprüft. Das Qualitätsminimum gilt für den schlechtesten gemessenen Ausschnitt.">
              <NumberField value={draft.output.min_quality_samples} onChange={(min_quality_samples) => update("output", { min_quality_samples })} min={1} max={2} ariaLabel="Erforderliche erfolgreiche Messungen" />
            </Field>
            <Field label="Qualitätsminimum (VMAF-Skala)">
              <SliderField
                value={draft.output.min_accept_vmaf}
                onChange={(min_accept_vmaf) => update("output", { min_accept_vmaf })}
                min={70}
                max={99}
                step={0.5}
                ariaLabel="Qualitätsminimum (VMAF-Skala)"
              />
            </Field>
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Wohin das Ergebnis geht">
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Ausgabe-Modus">
            <Select
              value={draft.output.mode}
              onChange={(mode) => update("output", { mode })}
              ariaLabel="Ausgabe-Modus"
              options={[
                { value: "replace", label: "Original ersetzen" },
                { value: "sidecar", label: "Daneben ablegen" },
                { value: "separate_dir", label: "In eigenen Ordner schreiben" },
              ]}
            />
          </Field>
          {draft.output.mode === "replace" && (
            <Field
              label="Was mit dem Original passiert"
              hint="Im Papierkorb kannst du Originale bis zum Ablauf der Aufbewahrungszeit wiederherstellen."
            >
              <Select
                value={draft.output.original_action}
                onChange={(original_action) => update("output", { original_action })}
                ariaLabel="Was mit dem Original passiert"
                options={[
                  { value: "trash", label: "In den Papierkorb verschieben (empfohlen)" },
                  { value: "delete", label: "Sofort löschen" },
                  { value: "keep", label: "Behalten (als .original)" },
                ]}
              />
            </Field>
          )}
          {draft.output.mode === "sidecar" && (
            <Field label="Namenszusatz">
              <input
                className="field font-mono text-sm"
                value={draft.output.sidecar_suffix}
                onChange={(e) => update("output", { sidecar_suffix: e.target.value })}
                aria-label="Namenszusatz"
              />
            </Field>
          )}
          {draft.output.mode === "separate_dir" && (
            <Field label="Ausgabeordner" hint="Die Ordnerstruktur der Bibliothek wird nachgebildet.">
              <input
                className="field font-mono text-sm"
                value={draft.output.output_dir}
                onChange={(e) => update("output", { output_dir: e.target.value })}
                placeholder="/output"
                aria-label="Ausgabeordner"
              />
            </Field>
          )}
          {draft.output.original_action === "trash" && draft.output.mode === "replace" && (
            <>
              <Field
                label="Papierkorb-Ordner"
                hint={
                  <>
                    Leer lassen (empfohlen): Jede Bibliothek bekommt einen versteckten Ordner{" "}
                    <code className="text-ink-300">.optimizarr-trash</code> in ihrem Wurzelordner.
                    Er liegt auf demselben Dateisystem, das Verschieben dauert deshalb keine
                    Sekunde, und der Scanner übergeht ihn. Ein eigener Pfad auf einer anderen
                    Platte bedeutet dagegen, jedes Original komplett zu kopieren.
                  </>
                }
              >
                <input
                  className="field font-mono text-sm"
                  value={draft.output.trash_dir}
                  onChange={(e) => update("output", { trash_dir: e.target.value })}
                  placeholder="leer = <Bibliotheksordner>/.optimizarr-trash"
                  aria-label="Papierkorb-Ordner"
                />
              </Field>
              <Field label="Aufbewahrung" hint="0 behält Originale unbegrenzt.">
                <NumberField
                  value={draft.output.trash_retention_days}
                  onChange={(trash_retention_days) => update("output", { trash_retention_days })}
                  min={0}
                  max={365}
                  suffix="Tage"
                  ariaLabel="Aufbewahrung"
                />
              </Field>
            </>
          )}
        </div>
      </Panel>

      <Panel title="Dateirechte" subtitle="Unraid erwartet üblicherweise 99:100 (nobody:users)">
        <div className="grid gap-5 md:grid-cols-3">
          <Field label="Rechte setzen">
            <Toggle
              checked={draft.output.set_permissions}
              onChange={(set_permissions) => update("output", { set_permissions })}
              label="Besitzer und Rechte anpassen"
            />
          </Field>
          <Field label="Benutzer-ID (UID)">
            <NumberField
              value={draft.output.uid}
              onChange={(uid) => update("output", { uid })}
              min={0}
              ariaLabel="Benutzer-ID (UID)"
            />
          </Field>
          <Field label="Gruppen-ID (GID)">
            <NumberField
              value={draft.output.gid}
              onChange={(gid) => update("output", { gid })}
              min={0}
              ariaLabel="Gruppen-ID (GID)"
            />
          </Field>
          <Field label="Dateirechte (oktal)">
            <input
              className="field font-mono text-sm"
              value={draft.output.file_mode}
              onChange={(e) => update("output", { file_mode: e.target.value })}
              placeholder="0664"
              aria-label="Dateirechte (oktal)"
            />
          </Field>
          <div className="md:col-span-2">
            <Toggle
              checked={draft.output.preserve_mtime}
              onChange={(preserve_mtime) => update("output", { preserve_mtime })}
              label="Änderungsdatum des Originals übernehmen"
              hint="Plex und Jellyfin behandeln die Datei dann nicht als neu. Aus (Standard): Sie bemerken die neue Datei und lesen Codec und Größe neu ein."
            />
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Queue                                                                      */
/* -------------------------------------------------------------------------- */
