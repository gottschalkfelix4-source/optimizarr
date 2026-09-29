import { useQuery } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { useMemo, useState } from "react";
import { endpoints, type Settings } from "../../lib/api";
import { bytes, number } from "../../lib/format";
import { Callout, ErrorState, Field, NumberField, Panel, Select, SliderField, Skeleton, Toggle, cn } from "../../components/ui";
import type { UpdateFn } from "./types";

export function AnalysisTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const mode = draft.analysis.mode;
  return (
    <div className="space-y-4">
      <Panel
        title="H.264 auf AV1 umstellen"
        subtitle="Alle erfassten H.264-Dateien als Konvertierungskandidaten behandeln"
      >
        <Toggle
          label="H.264 vollständig nach AV1 konvertieren"
          checked={draft.analysis.convert_all_h264}
          onChange={(convert_all_h264) => update("analysis", { convert_all_h264 })}
          hint="Umgeht für H.264 die Grenzen für Dateigröße, Laufzeit, Bitrate und Ersparnis, auch beim automatischen Einreihen und beim Annehmen des Ergebnisses. Einzelne AV1-Dateien können dadurch größer werden."
        />
        <p className="mt-3 text-xs leading-relaxed text-ink-400">
          Nach dem Speichern einen Scan starten: Bereits übersprungene H.264-Dateien werden neu
          bewertet. Ausgeschlossene Ordner, Dateitypen, Codecs und ignorierte Dateien bleiben
          ausgenommen. Integritäts- und aktivierte Qualitätsprüfungen gelten weiterhin. Für eine
          Bibliothek ohne H.264 unter Ausgabe den Modus „Original ersetzen“ verwenden; separate
          AV1-Kopien lassen das H.264-Original bestehen.
        </p>
        {draft.analysis.convert_all_h264 && draft.analysis.skip_codecs.includes("h264") && (
          <p className="mt-3 text-sm text-warn-400">
            H.264 ist derzeit unter „Codecs ausschließen“ angehakt. Diesen Ausschluss entfernen,
            damit die Umstellung greift.
          </p>
        )}
      </Panel>
      <Panel
        title="Wie gründlich analysiert wird"
        subtitle="Der wichtigste Kompromiss zwischen Geschwindigkeit und Treffsicherheit"
      >
        <div className="grid gap-3 md:grid-cols-3" role="radiogroup" aria-label="Analysemodus">
          {(
            [
              {
                value: "quick",
                title: "Schnell",
                desc: "Nur Metadaten. Millisekunden pro Datei, gut für einen ersten Überblick über eine große Bibliothek.",
              },
              {
                value: "sample",
                title: "Testkodierung",
                desc: "Kodiert echte Ausschnitte und misst das Ergebnis. Empfohlen – macht aus der Schätzung eine Messung.",
              },
              {
                value: "vmaf",
                title: "Mit Qualitätssuche",
                desc: "Sucht zusätzlich pro Datei den höchsten CRF, der das Qualitätsziel noch hält. Am genauesten, am langsamsten.",
              },
            ] as const
          ).map((option) => (
            <button
              key={option.value}
              role="radio"
              aria-checked={mode === option.value}
              onClick={() => update("analysis", { mode: option.value })}
              className={cn(
                "rounded-lg border p-4 text-left transition-colors",
                mode === option.value
                  ? "border-brand-500 bg-brand-600/10"
                  : "border-ink-700 bg-ink-850/40 hover:border-ink-600",
              )}
            >
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    "grid size-4 place-items-center rounded-full border",
                    mode === option.value ? "border-brand-400 bg-brand-500" : "border-ink-600",
                  )}
                >
                  {mode === option.value && <Check className="size-2.5 text-white" aria-hidden="true" />}
                </span>
                <span className="font-medium text-ink-100">{option.title}</span>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-ink-400">{option.desc}</p>
            </button>
          ))}
        </div>

        {mode !== "quick" && (
          <div className="mt-5 grid gap-5 border-t border-ink-800 pt-5 md:grid-cols-2">
            <Field
              label="Anzahl Testausschnitte"
              hint="Mehr Ausschnitte = zuverlässigere Hochrechnung, aber längere Analyse."
            >
              <SliderField
                value={draft.analysis.sample_count}
                onChange={(sample_count) => update("analysis", { sample_count })}
                min={1}
                max={10}
                ariaLabel="Anzahl Testausschnitte"
              />
            </Field>
            <Field label="Länge pro Ausschnitt">
              <SliderField
                value={draft.analysis.sample_duration}
                onChange={(sample_duration) => update("analysis", { sample_duration })}
                min={4}
                max={60}
                format={(v) => `${v} Sek`}
                ariaLabel="Länge pro Ausschnitt"
              />
            </Field>
            {mode === "vmaf" && (
              <>
                <Field
                  label="Qualitätsziel (VMAF)"
                  hint="94 gilt als visuell kaum unterscheidbar. Unter 90 wird es auf großen Bildschirmen sichtbar."
                >
                  <SliderField
                    value={draft.analysis.target_vmaf}
                    onChange={(target_vmaf) => update("analysis", { target_vmaf })}
                    min={80}
                    max={99}
                    step={0.5}
                    ariaLabel="Qualitätsziel (VMAF)"
                    marks={[
                      { value: 80, label: "80" },
                      { value: 90, label: "90" },
                      { value: 99, label: "99" },
                    ]}
                  />
                </Field>
                <Field label="Suchschritte" hint="Wie oft der CRF-Wert nachjustiert werden darf.">
                  <SliderField
                    value={draft.analysis.vmaf_search_steps}
                    onChange={(vmaf_search_steps) => update("analysis", { vmaf_search_steps })}
                    min={1}
                    max={8}
                    ariaLabel="Suchschritte"
                  />
                </Field>
              </>
            )}
          </div>
        )}
      </Panel>

      <Panel
        title="Wann sich eine Konvertierung lohnt"
        subtitle="Die Schwellen, ab denen eine Datei überhaupt als Kandidat gilt"
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Mindestersparnis"
            hint="Darunter bleibt die Datei unangetastet. Bei unsicheren Schätzungen steigt die Schwelle. Gilt nicht für H.264 im Umstellungsmodus."
          >
            <SliderField
              value={draft.analysis.min_saving_percent}
              onChange={(min_saving_percent) => update("analysis", { min_saving_percent })}
              min={5}
              max={70}
              format={(v) => `${v} %`}
              ariaLabel="Mindestersparnis"
            />
          </Field>
          <Field
            label="Mindestersparnis absolut"
            hint="Auch 40 % von einer 200-MB-Datei sind selten den Rechenaufwand wert."
          >
            <NumberField
              value={draft.analysis.min_saving_mb}
              onChange={(min_saving_mb) => update("analysis", { min_saving_mb })}
              min={0}
              suffix="MB"
              ariaLabel="Mindestersparnis absolut"
            />
          </Field>
          <Field label="Parallele Analysen" hint="Wie viele Dateien gleichzeitig untersucht werden.">
            <NumberField
              value={draft.analysis.analysis_workers}
              onChange={(analysis_workers) => update("analysis", { analysis_workers })}
              min={1}
              max={16}
              ariaLabel="Parallele Analysen"
            />
          </Field>
        </div>
      </Panel>

      <Panel
        title="Dolby Vision"
        subtitle="AV1 kann die Dolby-Vision-Metadaten nicht mitnehmen"
      >
        <div className="space-y-4">
          <Field label="Umgang mit Dolby-Vision-Dateien">
            <Select
              value={draft.analysis.dolby_vision}
              onChange={(dolby_vision) => update("analysis", { dolby_vision })}
              ariaLabel="Umgang mit Dolby-Vision-Dateien"
              options={[
                { value: "skip", label: "Überspringen (empfohlen)" },
                { value: "hdr10_fallback", label: "Profil 7/8 als HDR10 konvertieren" },
              ]}
            />
          </Field>
          <ul className="list-disc space-y-1.5 pl-5 text-xs leading-relaxed text-ink-400">
            <li>
              <strong className="text-ink-200">Profil 5</strong> hat keine HDR10-Basis – ohne die
              Dolby-Vision-Daten stimmen die Farben nicht mehr. Solche Dateien werden immer
              übersprungen, auch beim Erzwingen.
            </li>
            <li>
              <strong className="text-ink-200">Profil 7 und 8</strong> enthalten ein HDR10-Bild.
              Mit „als HDR10 konvertieren“ wird daraus eine AV1-Datei in HDR10; die
              Dolby-Vision-Ebene geht dabei verloren. Sonst werden sie übersprungen, lassen sich
              aber einzeln erzwingen.
            </li>
          </ul>
        </div>
      </Panel>

      <Panel
        title="Codecs ausschließen"
        subtitle="Angehakte Codecs werden nie zu Kandidaten – egal, wie viel sie sparen würden"
      >
        <CodecExclusions
          selected={draft.analysis.skip_codecs}
          onChange={(skip_codecs) => update("analysis", { skip_codecs })}
        />
      </Panel>

      <Panel title="Lernmodell">
        <div className="space-y-4">
          <Toggle
            checked={draft.analysis.use_learning_model}
            onChange={(use_learning_model) => update("analysis", { use_learning_model })}
            label="Aus abgeschlossenen Jobs lernen"
            hint="Optimizarr vergleicht jede Vorhersage mit dem echten Ergebnis und korrigiert künftige Schätzungen. Ohne Trainingsdaten ändert sich nichts."
          />
          <Field
            label="Volles Vertrauen ab"
            hint="Bis dahin wird die gelernte Korrektur nur anteilig angewendet."
          >
            <NumberField
              value={draft.analysis.trust_learning_after_samples}
              onChange={(trust_learning_after_samples) =>
                update("analysis", { trust_learning_after_samples })
              }
              min={3}
              max={500}
              suffix="Jobs"
              ariaLabel="Volles Vertrauen ab"
            />
          </Field>
        </div>
      </Panel>
    </div>
  );
}

/** Codec exclusions.
 *
 * This used to be a comma-separated text field, which meant guessing the name
 * ffprobe uses: typing "h265" looked accepted and did nothing at all.  Now the
 * library itself supplies the list, with the file counts that make the choice
 * obvious, and the consequence of the pending change is spelled out before it
 * is saved - checking HEVC drops however many candidates are behind it.
 */
function CodecExclusions({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (values: string[]) => void;
}) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["library", "codecs"],
    queryFn: endpoints.libraryCodecs,
  });
  const [pending, setPending] = useState("");

  const chosen = useMemo(() => new Set(selected), [selected]);

  // The list the server knows, plus anything picked in this session that is
  // not in the library at all.
  const rows = useMemo(() => {
    const items = data?.items ?? [];
    const known = new Map((data?.known ?? []).map((k) => [k.codec, k.label]));
    const extra = selected
      .filter((codec) => !items.some((i) => i.codec === codec))
      .map((codec) => ({
        codec,
        label: known.get(codec) ?? codec.toUpperCase(),
        files: 0,
        total_size: 0,
        candidates: 0,
        excluded: false,
      }));
    return [...items, ...extra];
  }, [data, selected]);

  const addable = useMemo(
    () => (data?.known ?? []).filter((k) => !rows.some((r) => r.codec === k.codec)),
    [data, rows],
  );

  // What saving would do - counted against the state the server has stored.
  const losing = rows
    .filter((r) => chosen.has(r.codec) && !r.excluded)
    .reduce((sum, r) => sum + r.candidates, 0);
  const returning = rows.filter((r) => !chosen.has(r.codec) && r.excluded);

  function toggle(codec: string) {
    onChange(chosen.has(codec) ? selected.filter((c) => c !== codec) : [...selected, codec]);
  }

  if (isLoading) return <Skeleton className="h-40" />;
  if (isError && !data) return <ErrorState compact error={error} onRetry={() => refetch()} />;

  return (
    <div className="space-y-3">
      <div className="space-y-2">
        {rows.map((row) => {
          const active = chosen.has(row.codec);
          return (
            <label
              key={row.codec}
              className={cn(
                "flex cursor-pointer items-center gap-3 rounded-lg border px-4 py-3 transition-colors",
                active
                  ? "border-brand-600/40 bg-brand-600/10"
                  : "border-ink-700/70 bg-ink-850/40 hover:border-ink-600",
              )}
            >
              <input
                type="checkbox"
                checked={active}
                onChange={() => toggle(row.codec)}
                className="size-4 shrink-0 rounded border-ink-600 bg-ink-800 accent-brand-500"
              />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-ink-100">{row.label}</p>
                <p className="mt-0.5 text-xs text-ink-500">
                  {row.files ? (
                    <>
                      {number(row.files)} Dateien · {bytes(row.total_size)}
                      {row.candidates > 0 && (
                        <span className="text-save-400"> · {number(row.candidates)} Kandidaten</span>
                      )}
                    </>
                  ) : (
                    "keine Dateien in der Bibliothek"
                  )}
                </p>
              </div>
              <span className="shrink-0 font-mono text-xs text-ink-500">{row.codec}</span>
            </label>
          );
        })}
      </div>

      {addable.length > 0 && (
        <Field
          label="Weiteren Codec ausschließen"
          hint="Auch für Material, das erst später in der Bibliothek landet."
        >
          <Select
            value={pending}
            onChange={(codec) => {
              if (codec) onChange([...selected, codec]);
              setPending("");
            }}
            ariaLabel="Weiteren Codec ausschließen"
            options={[
              { value: "", label: "Codec wählen …" },
              ...addable.map((k) => ({ value: k.codec, label: k.label })),
            ]}
          />
        </Field>
      )}

      {losing > 0 && (
        <Callout tone="warn">
          Beim Speichern fallen <strong>{number(losing)} Kandidaten</strong> aus der Liste. Die
          Dateien bleiben auf der Platte unverändert – sie werden nur nicht mehr vorgeschlagen.
        </Callout>
      )}

      {returning.map((row) => (
        <Callout key={row.codec} tone="info">
          {row.label} wird wieder zugelassen. Die {number(row.files)} betroffenen Dateien werden
          beim nächsten Scan neu bewertet.
        </Callout>
      ))}

      {!chosen.has("av1") && (
        <Callout tone="warn">
          AV1 ist nicht ausgeschlossen. Eine AV1-Datei erneut nach AV1 zu kodieren kostet
          Qualität und spart nichts – das sollte angehakt bleiben.
        </Callout>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Encoding                                                                   */
/* -------------------------------------------------------------------------- */
