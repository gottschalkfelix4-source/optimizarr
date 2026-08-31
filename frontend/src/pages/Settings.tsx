/** Every setting the application has - no environment variables needed. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Archive,
  Brain,
  Check,
  ChevronRight,
  Cpu,
  FolderOpen,
  FolderPlus,
  Gauge,
  HardDrive,
  Layers,
  Microscope,
  Music4,
  RotateCcw,
  Save,
  Shield,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { endpoints, type Settings, type SettingsPatch } from "../lib/api";
import { bytes, number } from "../lib/format";
import { useToast } from "../lib/live";
import {
  Callout,
  Field,
  Modal,
  NumberField,
  Panel,
  Select,
  SliderField,
  Skeleton,
  Spinner,
  TagListField,
  Toggle,
  cn,
} from "../components/ui";

const TABS = [
  { id: "library", label: "Bibliothek", icon: FolderOpen },
  { id: "analysis", label: "Analyse", icon: Microscope },
  { id: "encoding", label: "Encoding", icon: Gauge },
  { id: "audio", label: "Audio & Untertitel", icon: Music4 },
  { id: "output", label: "Ausgabe & Sicherheit", icon: Shield },
  { id: "queue", label: "Warteschlange", icon: Layers },
  { id: "hardware", label: "Hardware", icon: Cpu },
  { id: "advisor", label: "KI-Berater", icon: Brain },
  { id: "system", label: "System", icon: Archive },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function SettingsPage() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<TabId>("library");
  const [draft, setDraft] = useState<Settings | null>(null);

  const { data: saved, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: endpoints.settings,
  });

  useEffect(() => {
    if (saved && !draft) setDraft(structuredClone(saved));
  }, [saved, draft]);

  const dirty = useMemo(() => {
    if (!saved || !draft) return false;
    return JSON.stringify(saved) !== JSON.stringify(draft);
  }, [saved, draft]);

  const save = useMutation({
    mutationFn: (patch: SettingsPatch) => endpoints.saveSettings(patch),
    onSuccess: (result) => {
      push("Einstellungen gespeichert.", "success");
      queryClient.setQueryData(["settings"], result);
      setDraft(structuredClone(result));
      queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  function update<K extends keyof Settings>(group: K, patch: Partial<Settings[K]>) {
    setDraft((prev) => (prev ? { ...prev, [group]: { ...prev[group], ...patch } } : prev));
  }

  if (isLoading || !draft) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-12" />
        <Skeleton className="h-96" />
      </div>
    );
  }

  return (
    <div className="space-y-4 pb-24">
      {/* ---------------- tabs ---------------- */}
      <div className="panel flex gap-1 overflow-x-auto p-1.5">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={cn(
              "flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              tab === id
                ? "bg-brand-600/15 text-brand-400"
                : "text-ink-400 hover:bg-ink-800/70 hover:text-ink-200",
            )}
          >
            <Icon className="size-4" />
            {label}
          </button>
        ))}
      </div>

      {tab === "library" && <LibraryTab draft={draft} update={update} />}
      {tab === "analysis" && <AnalysisTab draft={draft} update={update} />}
      {tab === "encoding" && <EncodingTab draft={draft} update={update} setDraft={setDraft} />}
      {tab === "audio" && <AudioTab draft={draft} update={update} />}
      {tab === "output" && <OutputTab draft={draft} update={update} />}
      {tab === "queue" && <QueueTab draft={draft} update={update} />}
      {tab === "hardware" && <HardwareTab draft={draft} update={update} />}
      {tab === "advisor" && <AdvisorTab draft={draft} update={update} />}
      {tab === "system" && <SystemTab />}

      {/* ---------------- save bar ---------------- */}
      {dirty && (
        <div className="fixed inset-x-0 bottom-0 z-30 border-t border-ink-700 bg-ink-900/95 backdrop-blur-md">
          <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
            <span className="text-sm text-ink-300">Es gibt ungespeicherte Aenderungen.</span>
            <div className="ml-auto flex gap-2">
              <button
                className="btn-ghost btn-sm"
                onClick={() => saved && setDraft(structuredClone(saved))}
              >
                <X className="size-3.5" />
                Verwerfen
              </button>
              <button
                className="btn-primary btn-sm"
                onClick={() => save.mutate(draft as SettingsPatch)}
                disabled={save.isPending}
              >
                {save.isPending ? <Spinner className="size-3.5" /> : <Save className="size-3.5" />}
                Speichern
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

type UpdateFn = <K extends keyof Settings>(group: K, patch: Partial<Settings[K]>) => void;

/* -------------------------------------------------------------------------- */
/* Library                                                                    */
/* -------------------------------------------------------------------------- */

function LibraryTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [pickerOpen, setPickerOpen] = useState(false);

  const { data: paths } = useQuery({ queryKey: ["library", "paths"], queryFn: endpoints.libraryPaths });

  const addPath = useMutation({
    mutationFn: (path: string) => endpoints.addLibraryPath({ path }),
    onSuccess: () => {
      push("Ordner hinzugefuegt.", "success");
      queryClient.invalidateQueries({ queryKey: ["library"] });
      setPickerOpen(false);
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const removePath = useMutation({
    mutationFn: (id: number) => endpoints.deleteLibraryPath(id),
    onSuccess: () => {
      push("Ordner entfernt.", "success");
      queryClient.invalidateQueries({ queryKey: ["library"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
    },
  });

  const togglePath = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      endpoints.updateLibraryPath(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["library"] }),
  });

  return (
    <div className="space-y-4">
      <Panel
        title="Medien-Ordner"
        subtitle="Diese Pfade durchsucht Optimizarr. Sie muessen im Docker-Template als Volume gemappt sein."
        actions={
          <button className="btn-primary btn-sm" onClick={() => setPickerOpen(true)}>
            <FolderPlus className="size-3.5" />
            Ordner hinzufuegen
          </button>
        }
        bodyClassName={paths?.length ? "space-y-2" : ""}
      >
        {!paths?.length ? (
          <Callout tone="warn">
            Noch kein Ordner konfiguriert. Ohne mindestens einen Pfad kann kein Scan laufen.
          </Callout>
        ) : (
          paths.map((entry) => (
            <div
              key={entry.id}
              className="flex flex-wrap items-center gap-3 rounded-lg border border-ink-700/70 bg-ink-850/40 px-4 py-3"
            >
              <FolderOpen
                className={cn("size-4 shrink-0", entry.exists ? "text-brand-400" : "text-danger-400")}
              />
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-sm text-ink-100">{entry.path}</p>
                <p className="mt-0.5 text-xs text-ink-500">
                  {entry.exists ? (
                    <>
                      {number(entry.file_count ?? 0)} Dateien · {bytes(entry.total_size ?? 0)}
                      {(entry.candidates ?? 0) > 0 && (
                        <span className="text-save-400"> · {entry.candidates} Kandidaten</span>
                      )}
                      {(entry.converted ?? 0) > 0 && <span> · {entry.converted} konvertiert</span>}
                    </>
                  ) : (
                    <span className="text-danger-400">
                      Pfad existiert im Container nicht - Volume-Mapping pruefen
                    </span>
                  )}
                </p>
              </div>
              <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-400">
                <input
                  type="checkbox"
                  checked={entry.enabled}
                  onChange={(e) => togglePath.mutate({ id: entry.id, enabled: e.target.checked })}
                  className="size-4 rounded border-ink-600 bg-ink-800 accent-brand-500"
                />
                aktiv
              </label>
              <button
                onClick={() => {
                  if (
                    window.confirm(
                      `"${entry.path}" entfernen? Die bekannten Dateien dieses Ordners werden aus der Datenbank geloescht - auf der Platte passiert nichts.`,
                    )
                  ) {
                    removePath.mutate(entry.id);
                  }
                }}
                className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-danger-500/15 hover:text-danger-400"
                title="Entfernen"
              >
                <Trash2 className="size-4" />
              </button>
            </div>
          ))
        )}
      </Panel>

      <Panel title="Was gescannt wird">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Dateiendungen"
            hint="Komma-getrennt. Alles andere wird ignoriert."
          >
            <TagListField
              values={draft.library.extensions}
              onChange={(extensions) => update("library", { extensions })}
              placeholder="mkv, mp4, avi"
            />
          </Field>
          <Field
            label="Ausschluss-Muster"
            hint="Pfad-Muster, die uebersprungen werden - z.B. */extras/* oder *sample*"
          >
            <TagListField
              values={draft.library.exclude_patterns}
              onChange={(exclude_patterns) => update("library", { exclude_patterns })}
            />
          </Field>
          <Field
            label="Mindestgroesse"
            hint="Kleinere Dateien lohnen den Aufwand nicht."
          >
            <NumberField
              value={draft.library.min_file_size_mb}
              onChange={(min_file_size_mb) => update("library", { min_file_size_mb })}
              min={0}
              suffix="MB"
            />
          </Field>
          <Field label="Mindestlaufzeit" hint="Filtert Trailer und Schnipsel heraus.">
            <NumberField
              value={draft.library.min_duration_seconds}
              onChange={(min_duration_seconds) => update("library", { min_duration_seconds })}
              min={0}
              suffix="Sek"
            />
          </Field>
        </div>
      </Panel>

      <Panel title="Scan-Zeitplan">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Automatischer Scan alle"
            hint="0 schaltet den automatischen Scan ab."
          >
            <NumberField
              value={draft.library.scan_interval_hours}
              onChange={(scan_interval_hours) => update("library", { scan_interval_hours })}
              min={0}
              max={720}
              suffix="Std"
            />
          </Field>
          <Field
            label="Analyse neu aufrollen nach"
            hint="Alte Einschaetzungen werden erneuert - sinnvoll, weil das Lernmodell besser wird."
          >
            <NumberField
              value={draft.library.reanalyze_after_days}
              onChange={(reanalyze_after_days) => update("library", { reanalyze_after_days })}
              min={0}
              suffix="Tage"
            />
          </Field>
          <div className="space-y-3 md:col-span-2">
            <Toggle
              checked={draft.library.scan_on_start}
              onChange={(scan_on_start) => update("library", { scan_on_start })}
              label="Beim Start des Containers scannen"
            />
            <Toggle
              checked={draft.library.rescan_changed_only}
              onChange={(rescan_changed_only) => update("library", { rescan_changed_only })}
              label="Nur geaenderte Dateien erneut pruefen"
              hint="Deutlich schneller. Ausschalten, um die ganze Bibliothek neu zu bewerten."
            />
            <Toggle
              checked={draft.library.follow_symlinks}
              onChange={(follow_symlinks) => update("library", { follow_symlinks })}
              label="Symlinks folgen"
              hint="Vorsicht bei verschachtelten Shares - kann zu Doppelzaehlungen fuehren."
            />
          </div>
        </div>
      </Panel>

      <DirectoryPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onSelect={(path) => addPath.mutate(path)}
        busy={addPath.isPending}
      />
    </div>
  );
}

function DirectoryPicker({
  open,
  onClose,
  onSelect,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onSelect: (path: string) => void;
  busy: boolean;
}) {
  const [path, setPath] = useState("/media");

  const { data, isLoading } = useQuery({
    queryKey: ["browse", path],
    queryFn: () => endpoints.browse(path),
    enabled: open,
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Ordner auswaehlen"
      subtitle="Pfade wie sie im Container sichtbar sind"
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Abbrechen
          </button>
          <button
            className="btn-primary"
            onClick={() => onSelect(data?.path ?? path)}
            disabled={busy || isLoading}
          >
            {busy ? <Spinner className="size-4" /> : <Check className="size-4" />}
            Diesen Ordner verwenden
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <input
          className="field font-mono text-sm"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="/media/movies"
        />
        <div className="rounded-lg border border-ink-700 bg-ink-950/50">
          <div className="flex items-center gap-2 border-b border-ink-800 px-3 py-2">
            <span className="truncate font-mono text-xs text-ink-400">{data?.path ?? path}</span>
            {data?.parent && (
              <button
                className="ml-auto shrink-0 text-xs text-brand-400 hover:underline"
                onClick={() => setPath(data.parent!)}
              >
                Eine Ebene hoch
              </button>
            )}
          </div>
          <div className="max-h-64 overflow-y-auto">
            {isLoading ? (
              <div className="p-3">
                <Spinner />
              </div>
            ) : data?.entries.length ? (
              data.entries.map((entry) => (
                <button
                  key={entry.path}
                  onClick={() => setPath(entry.path)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-ink-200 transition-colors hover:bg-ink-800/70"
                >
                  <FolderOpen className="size-4 shrink-0 text-ink-500" />
                  <span className="truncate">{entry.name}</span>
                  <ChevronRight className="ml-auto size-3.5 shrink-0 text-ink-600" />
                </button>
              ))
            ) : (
              <p className="px-3 py-4 text-sm text-ink-500">Keine Unterordner.</p>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* Analysis                                                                   */
/* -------------------------------------------------------------------------- */

function AnalysisTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const mode = draft.analysis.mode;
  return (
    <div className="space-y-4">
      <Panel
        title="Wie gruendlich analysiert wird"
        subtitle="Der wichtigste Kompromiss zwischen Geschwindigkeit und Treffsicherheit"
      >
        <div className="grid gap-3 md:grid-cols-3">
          {(
            [
              {
                value: "quick",
                title: "Schnell",
                desc: "Nur Metadaten. Millisekunden pro Datei, gut fuer einen ersten Ueberblick ueber eine grosse Bibliothek.",
              },
              {
                value: "sample",
                title: "Testkodierung",
                desc: "Kodiert echte Ausschnitte und misst das Ergebnis. Empfohlen - macht aus der Schaetzung eine Messung.",
              },
              {
                value: "vmaf",
                title: "Mit Qualitaetssuche",
                desc: "Sucht zusaetzlich pro Datei den hoechsten CRF, der das Qualitaetsziel noch haelt. Am genauesten, am langsamsten.",
              },
            ] as const
          ).map((option) => (
            <button
              key={option.value}
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
                  {mode === option.value && <Check className="size-2.5 text-white" />}
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
              hint="Mehr Ausschnitte = zuverlaessigere Hochrechnung, aber laengere Analyse."
            >
              <SliderField
                value={draft.analysis.sample_count}
                onChange={(sample_count) => update("analysis", { sample_count })}
                min={1}
                max={10}
              />
            </Field>
            <Field label="Laenge pro Ausschnitt">
              <SliderField
                value={draft.analysis.sample_duration}
                onChange={(sample_duration) => update("analysis", { sample_duration })}
                min={4}
                max={60}
                format={(v) => `${v} Sek`}
              />
            </Field>
            {mode === "vmaf" && (
              <>
                <Field
                  label="Qualitaetsziel (VMAF)"
                  hint="94 gilt als visuell kaum unterscheidbar. Unter 90 wird es auf grossen Bildschirmen sichtbar."
                >
                  <SliderField
                    value={draft.analysis.target_vmaf}
                    onChange={(target_vmaf) => update("analysis", { target_vmaf })}
                    min={80}
                    max={99}
                    step={0.5}
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
                  />
                </Field>
              </>
            )}
          </div>
        )}
      </Panel>

      <Panel
        title="Wann sich eine Konvertierung lohnt"
        subtitle="Die Schwellen, ab denen eine Datei ueberhaupt als Kandidat gilt"
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Mindestersparnis"
            hint="Darunter bleibt die Datei unangetastet. Bei unsicheren Schaetzungen erhoeht Optimizarr diese Schwelle automatisch."
          >
            <SliderField
              value={draft.analysis.min_saving_percent}
              onChange={(min_saving_percent) => update("analysis", { min_saving_percent })}
              min={5}
              max={70}
              format={(v) => `${v} %`}
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
            />
          </Field>
          <Field
            label="Diese Codecs ueberspringen"
            hint="AV1 ist hier voreingestellt - eine erneute Kodierung wuerde nur Qualitaet kosten."
          >
            <TagListField
              values={draft.analysis.skip_codecs}
              onChange={(skip_codecs) => update("analysis", { skip_codecs })}
              placeholder="av1, vp9"
            />
          </Field>
          <Field
            label="Parallele Analysen"
            hint="Wie viele Dateien gleichzeitig untersucht werden."
          >
            <NumberField
              value={draft.analysis.analysis_workers}
              onChange={(analysis_workers) => update("analysis", { analysis_workers })}
              min={1}
              max={16}
            />
          </Field>
        </div>
      </Panel>

      <Panel title="Lernmodell">
        <div className="space-y-4">
          <Toggle
            checked={draft.analysis.use_learning_model}
            onChange={(use_learning_model) => update("analysis", { use_learning_model })}
            label="Aus abgeschlossenen Jobs lernen"
            hint="Optimizarr vergleicht jede Vorhersage mit dem echten Ergebnis und korrigiert kuenftige Schaetzungen. Ohne Trainingsdaten aendert sich nichts."
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
            />
          </Field>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Encoding                                                                   */
/* -------------------------------------------------------------------------- */

function EncodingTab({
  draft,
  update,
  setDraft,
}: {
  draft: Settings;
  update: UpdateFn;
  setDraft: React.Dispatch<React.SetStateAction<Settings | null>>;
}) {
  const { push } = useToast();
  const queryClient = useQueryClient();

  const applyProfile = useMutation({
    mutationFn: (name: string) => endpoints.applyProfile(name),
    onSuccess: (result) => {
      push("Profil uebernommen.", "success");
      queryClient.setQueryData(["settings"], result);
      setDraft(structuredClone(result));
    },
  });

  const profiles = [
    {
      id: "archive",
      title: "Archiv",
      desc: "Praktisch nicht unterscheidbar vom Original. Weniger Ersparnis, langsamster Encode.",
      crf: 24,
    },
    {
      id: "balanced",
      title: "Ausgewogen",
      desc: "Empfohlen. Deutliche Ersparnis bei kaum sichtbarem Unterschied.",
      crf: 30,
    },
    {
      id: "space",
      title: "Platz sparen",
      desc: "Maximale Ersparnis, schneller Encode. Auf grossen TVs sichtbar weicher.",
      crf: 35,
    },
  ];

  return (
    <div className="space-y-4">
      <Panel title="Qualitaetsprofil" subtitle="Setzt CRF, Preset und Qualitaetsziel in einem Rutsch">
        <div className="grid gap-3 md:grid-cols-3">
          {profiles.map((profile) => (
            <button
              key={profile.id}
              onClick={() => applyProfile.mutate(profile.id)}
              className={cn(
                "rounded-lg border p-4 text-left transition-colors",
                draft.encoding.profile === profile.id
                  ? "border-brand-500 bg-brand-600/10"
                  : "border-ink-700 bg-ink-850/40 hover:border-ink-600",
              )}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-ink-100">{profile.title}</span>
                <span className="font-mono text-xs text-ink-500">CRF {profile.crf}</span>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-ink-400">{profile.desc}</p>
            </button>
          ))}
        </div>
      </Panel>

      <Panel title="Encoder">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Encoder-Auswahl"
            hint="Automatisch nimmt den GPU-Encoder, sobald ein Testlauf beweist, dass er funktioniert."
          >
            <Select
              value={draft.encoding.encoder}
              onChange={(encoder) => update("encoding", { encoder })}
              options={[
                { value: "auto", label: "Automatisch (empfohlen)" },
                { value: "svt_av1", label: "SVT-AV1 (CPU)" },
                { value: "av1_qsv", label: "Intel QSV AV1 (GPU)" },
                { value: "av1_vaapi", label: "Intel VAAPI AV1 (GPU)" },
              ]}
            />
          </Field>
          <Field label="Container">
            <Select
              value={draft.encoding.container}
              onChange={(container) => update("encoding", { container })}
              options={[
                { value: "mkv", label: "MKV (empfohlen - kann alles)" },
                { value: "mp4", label: "MP4 (kompatibler, verliert Bild-Untertitel)" },
              ]}
            />
          </Field>

          <Field
            label="Basis-Qualitaet (CRF)"
            hint="Niedriger = besser und groesser. Die Analyse verschiebt diesen Wert pro Datei."
          >
            <SliderField
              value={draft.encoding.crf}
              onChange={(crf) => update("encoding", { crf })}
              min={16}
              max={50}
              marks={[
                { value: 16, label: "16 · sehr gut" },
                { value: 32, label: "32" },
                { value: 50, label: "50 · klein" },
              ]}
            />
          </Field>
          <Field
            label="Preset (nur SVT-AV1)"
            hint="Niedriger = langsamer, aber kleinere Dateien bei gleicher Qualitaet. 6 ist ein guter Alltagswert."
          >
            <SliderField
              value={draft.encoding.preset}
              onChange={(preset) => update("encoding", { preset })}
              min={0}
              max={13}
              marks={[
                { value: 0, label: "0 · sehr langsam" },
                { value: 6, label: "6" },
                { value: 13, label: "13 · sehr schnell" },
              ]}
            />
          </Field>

          <Field label="CRF-Untergrenze" hint="Die Analyse darf nicht unter diesen Wert gehen.">
            <NumberField
              value={draft.encoding.crf_min}
              onChange={(crf_min) => update("encoding", { crf_min })}
              min={1}
              max={63}
            />
          </Field>
          <Field label="CRF-Obergrenze">
            <NumberField
              value={draft.encoding.crf_max}
              onChange={(crf_max) => update("encoding", { crf_max })}
              min={1}
              max={63}
            />
          </Field>
        </div>

        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.encoding.allow_crf_adjust}
            onChange={(allow_crf_adjust) => update("encoding", { allow_crf_adjust })}
            label="CRF pro Datei automatisch anpassen"
            hint="Die Analyse sucht den hoechsten CRF, der das Qualitaetsziel noch haelt."
          />
          <Toggle
            checked={draft.encoding.force_10bit}
            onChange={(force_10bit) => update("encoding", { force_10bit })}
            label="Immer in 10 Bit kodieren"
            hint="AV1 komprimiert auch 8-Bit-Quellen in 10 Bit effizienter und vermeidet Farbstufen in Verlaeufen."
          />
          <Toggle
            checked={draft.encoding.auto_film_grain}
            onChange={(auto_film_grain) => update("encoding", { auto_film_grain })}
            label="Filmkorn automatisch erkennen und synthetisieren"
            hint="Bei koernigem Material wird das Korn vor dem Encoden entfernt und bei der Wiedergabe neu erzeugt - das spart sehr viel Bitrate. Auf sauberem Material bleibt die Funktion aus."
          />
          <Toggle
            checked={draft.encoding.deinterlace}
            onChange={(deinterlace) => update("encoding", { deinterlace })}
            label="Interlaced-Material deinterlacen"
          />
          <Toggle
            checked={draft.encoding.copy_chapters}
            onChange={(copy_chapters) => update("encoding", { copy_chapters })}
            label="Kapitelmarken uebernehmen"
          />
          <Toggle
            checked={draft.encoding.copy_attachments}
            onChange={(copy_attachments) => update("encoding", { copy_attachments })}
            label="Anhaenge uebernehmen (Schriftarten fuer ASS-Untertitel)"
          />
        </div>
      </Panel>

      <Panel title="Erweitert">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Maximale Breite"
            hint="0 behaelt die Aufloesung bei. Sonst wird proportional herunterskaliert."
          >
            <NumberField
              value={draft.encoding.max_width}
              onChange={(max_width) => update("encoding", { max_width })}
              min={0}
              suffix="px"
            />
          </Field>
          <Field label="Keyframe-Abstand">
            <NumberField
              value={draft.encoding.keyframe_interval_seconds}
              onChange={(keyframe_interval_seconds) =>
                update("encoding", { keyframe_interval_seconds })
              }
              min={1}
              max={30}
              suffix="Sek"
            />
          </Field>
          <Field
            label="Filmkorn-Synthese fest"
            hint="0 laesst die automatische Erkennung entscheiden. Sonst gilt dieser Wert fuer alle Dateien."
          >
            <SliderField
              value={draft.encoding.film_grain_synthesis}
              onChange={(film_grain_synthesis) => update("encoding", { film_grain_synthesis })}
              min={0}
              max={50}
              format={(v) => (v === 0 ? "auto" : String(v))}
            />
          </Field>
          <Field label="Abbruch nach" hint="Sicherheitsnetz gegen haengende Encodes.">
            <NumberField
              value={draft.encoding.max_encode_hours}
              onChange={(max_encode_hours) => update("encoding", { max_encode_hours })}
              min={1}
              max={72}
              suffix="Std"
            />
          </Field>
          <div className="md:col-span-2">
            <Field
              label="Zusaetzliche ffmpeg-Parameter"
              hint="Werden unveraendert an ffmpeg angehaengt. Nur fuer Leute, die wissen, was sie tun."
            >
              <input
                className="field font-mono text-sm"
                value={draft.encoding.extra_ffmpeg_args}
                onChange={(e) => update("encoding", { extra_ffmpeg_args: e.target.value })}
                placeholder="-svtav1-params enable-overlays=1"
              />
            </Field>
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Audio & subtitles                                                          */
/* -------------------------------------------------------------------------- */

function AudioTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  return (
    <div className="space-y-4">
      <Panel
        title="Tonspuren"
        subtitle="Bei einem 4-GB-Film koennen 1,5 GB auf eine unkomprimierte Tonspur entfallen"
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Umgang mit Audio">
            <Select
              value={draft.audio.mode}
              onChange={(mode) => update("audio", { mode })}
              options={[
                {
                  value: "opus_if_bloated",
                  label: "Nur aufgeblaehte Spuren umwandeln (empfohlen)",
                },
                { value: "copy", label: "Immer unveraendert kopieren" },
                { value: "opus", label: "Alles nach Opus umwandeln" },
              ]}
            />
          </Field>
          <Field
            label="Opus-Bitrate je Kanal"
            hint="48 kbit/s pro Kanal sind bei Opus transparent - 5.1 landet bei rund 288 kbit/s."
          >
            <NumberField
              value={draft.audio.opus_bitrate_per_channel}
              onChange={(opus_bitrate_per_channel) => update("audio", { opus_bitrate_per_channel })}
              min={24}
              max={128}
              suffix="kbit/s"
            />
          </Field>
          {draft.audio.mode === "opus_if_bloated" && (
            <Field
              label="Ab wann gilt eine Spur als aufgeblaeht"
              hint="Spuren darunter werden unveraendert kopiert. Verlustfreie Formate wie TrueHD werden immer umgewandelt."
            >
              <NumberField
                value={draft.audio.bloat_threshold_kbps_per_channel}
                onChange={(bloat_threshold_kbps_per_channel) =>
                  update("audio", { bloat_threshold_kbps_per_channel })
                }
                min={32}
                max={512}
                suffix="kbit/s je Kanal"
              />
            </Field>
          )}
          <Field
            label="Sprachen behalten"
            hint="Leer laesst alle Spuren drin. Sonst ISO-Codes wie deu, eng."
          >
            <TagListField
              values={draft.audio.keep_languages}
              onChange={(keep_languages) => update("audio", { keep_languages })}
              placeholder="deu, eng"
            />
          </Field>
        </div>
        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.audio.drop_commentary}
            onChange={(drop_commentary) => update("audio", { drop_commentary })}
            label="Kommentarspuren entfernen"
          />
          <Toggle
            checked={draft.audio.keep_default_track_always}
            onChange={(keep_default_track_always) => update("audio", { keep_default_track_always })}
            label="Standardspur nie entfernen"
            hint="Schutz davor, aus Versehen eine Datei ohne Ton zu erzeugen."
          />
        </div>
      </Panel>

      <Panel title="Untertitel">
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Umgang mit Untertiteln">
            <Select
              value={draft.subtitles.mode}
              onChange={(mode) => update("subtitles", { mode })}
              options={[
                { value: "copy", label: "Alle uebernehmen (empfohlen)" },
                { value: "text_only", label: "Nur Text-Untertitel (SRT/ASS)" },
                { value: "drop", label: "Alle entfernen" },
              ]}
            />
          </Field>
          <Field label="Sprachen behalten" hint="Erzwungene Untertitel bleiben immer erhalten.">
            <TagListField
              values={draft.subtitles.keep_languages}
              onChange={(keep_languages) => update("subtitles", { keep_languages })}
              placeholder="deu, eng"
            />
          </Field>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Output & safety                                                            */
/* -------------------------------------------------------------------------- */

function OutputTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  return (
    <div className="space-y-4">
      <Callout tone="success" icon={<Shield className="size-4" />}>
        Diese Regeln entscheiden, ob ein fertiger Encode ueberhaupt behalten wird. Faellt auch nur
        eine Pruefung durch, wird das Ergebnis geloescht und das Original bleibt exakt so, wie es
        war.
      </Callout>

      <Panel title="Sicherheitsregeln">
        <div className="space-y-4">
          <Toggle
            checked={draft.output.require_smaller}
            onChange={(require_smaller) => update("output", { require_smaller })}
            label="Ergebnis muss kleiner sein als das Original"
            hint="Sollte immer an bleiben - genau dafuer gibt es dieses Werkzeug."
          />
          <Field
            label="Mindestersparnis zum Behalten"
            hint="Ein Encode, der nur 2 % spart, ist den Qualitaetsverlust nicht wert."
          >
            <SliderField
              value={draft.output.min_accept_saving_percent}
              onChange={(min_accept_saving_percent) =>
                update("output", { min_accept_saving_percent })
              }
              min={0}
              max={50}
              format={(v) => `${v} %`}
            />
          </Field>
          <Toggle
            checked={draft.output.verify_output}
            onChange={(verify_output) => update("output", { verify_output })}
            label="Ergebnisdatei nach dem Encode pruefen"
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
            />
          </Field>
          <Toggle
            checked={draft.output.verify_vmaf}
            onChange={(verify_vmaf) => update("output", { verify_vmaf })}
            label="Qualitaet der fertigen Datei messen (VMAF)"
            hint="Sehr gruendlich, kostet aber zusaetzliche Rechenzeit pro Datei."
          />
          {draft.output.verify_vmaf && (
            <Field label="Mindest-VMAF zum Behalten">
              <SliderField
                value={draft.output.min_accept_vmaf}
                onChange={(min_accept_vmaf) => update("output", { min_accept_vmaf })}
                min={70}
                max={99}
                step={0.5}
              />
            </Field>
          )}
        </div>
      </Panel>

      <Panel title="Wohin das Ergebnis geht">
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Ausgabe-Modus">
            <Select
              value={draft.output.mode}
              onChange={(mode) => update("output", { mode })}
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
              hint="Papierkorb ist die sichere Wahl - du kannst jederzeit zurueck."
            >
              <Select
                value={draft.output.original_action}
                onChange={(original_action) => update("output", { original_action })}
                options={[
                  { value: "trash", label: "In den Papierkorb verschieben (empfohlen)" },
                  { value: "delete", label: "Sofort loeschen" },
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
              />
            </Field>
          )}
          {draft.output.original_action === "trash" && draft.output.mode === "replace" && (
            <>
              <Field label="Papierkorb-Ordner">
                <input
                  className="field font-mono text-sm"
                  value={draft.output.trash_dir}
                  onChange={(e) => update("output", { trash_dir: e.target.value })}
                />
              </Field>
              <Field label="Aufbewahrung" hint="0 behaelt Originale unbegrenzt.">
                <NumberField
                  value={draft.output.trash_retention_days}
                  onChange={(trash_retention_days) => update("output", { trash_retention_days })}
                  min={0}
                  max={365}
                  suffix="Tage"
                />
              </Field>
            </>
          )}
        </div>
      </Panel>

      <Panel title="Dateirechte" subtitle="Unraid erwartet ueblicherweise 99:100 (nobody:users)">
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
            />
          </Field>
          <Field label="Gruppen-ID (GID)">
            <NumberField
              value={draft.output.gid}
              onChange={(gid) => update("output", { gid })}
              min={0}
            />
          </Field>
          <Field label="Dateirechte (oktal)">
            <input
              className="field font-mono text-sm"
              value={draft.output.file_mode}
              onChange={(e) => update("output", { file_mode: e.target.value })}
              placeholder="0664"
            />
          </Field>
          <div className="md:col-span-2">
            <Toggle
              checked={draft.output.preserve_mtime}
              onChange={(preserve_mtime) => update("output", { preserve_mtime })}
              label="Aenderungsdatum des Originals uebernehmen"
              hint="Sorgt dafuer, dass Plex und Jellyfin die Datei nicht als neu behandeln."
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

const WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

function QueueTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const toggleDay = (day: number) => {
    const days = new Set(draft.queue.schedule_days);
    days.has(day) ? days.delete(day) : days.add(day);
    update("queue", { schedule_days: [...days].sort() });
  };

  return (
    <div className="space-y-4">
      <Panel title="Verarbeitung">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Gleichzeitige Konvertierungen"
            hint="Auf einem Heimserver ist 1 fast immer richtig - AV1-Encoding nutzt ohnehin alle Kerne."
          >
            <NumberField
              value={draft.queue.max_concurrent_jobs}
              onChange={(max_concurrent_jobs) => update("queue", { max_concurrent_jobs })}
              min={1}
              max={8}
            />
          </Field>
          <Field
            label="CPU-Threads"
            hint="0 nutzt alle Kerne. Niedriger setzen, wenn parallel noch andere Dienste laufen."
          >
            <NumberField
              value={draft.queue.cpu_threads}
              onChange={(cpu_threads) => update("queue", { cpu_threads })}
              min={0}
              max={128}
            />
          </Field>
          <Field
            label="Prozesspriotitaet (nice)"
            hint="Hoeher = freundlicher zu anderen Diensten. 10 ist ein guter Wert fuer einen NAS."
          >
            <SliderField
              value={draft.queue.nice_level}
              onChange={(nice_level) => update("queue", { nice_level })}
              min={-20}
              max={19}
            />
          </Field>
          <Field
            label="Mindestens freier Speicher"
            hint="Unterhalb dieser Grenze startet kein neuer Job."
          >
            <NumberField
              value={draft.queue.min_free_disk_gb}
              onChange={(min_free_disk_gb) => update("queue", { min_free_disk_gb })}
              min={0}
              suffix="GB"
            />
          </Field>
        </div>
      </Panel>

      <Panel title="Automatik">
        <div className="space-y-4">
          <Toggle
            checked={draft.queue.auto_queue_candidates}
            onChange={(auto_queue_candidates) => update("queue", { auto_queue_candidates })}
            label="Kandidaten nach dem Scan automatisch einreihen"
            hint="Ohne diese Option entscheidest du in der Bibliothek selbst, was konvertiert wird."
          />
          {draft.queue.auto_queue_candidates && (
            <Field
              label="Nur ab dieser Ersparnis automatisch einreihen"
              hint="Schuetzt davor, dass Grenzfaelle ungefragt Rechenzeit verbrauchen."
            >
              <SliderField
                value={draft.queue.auto_queue_min_saving_percent}
                onChange={(auto_queue_min_saving_percent) =>
                  update("queue", { auto_queue_min_saving_percent })
                }
                min={5}
                max={80}
                format={(v) => `${v} %`}
              />
            </Field>
          )}
        </div>
      </Panel>

      <Panel title="Zeitfenster" subtitle="Encoden nur dann, wenn der Server ohnehin nichts tut">
        <div className="space-y-4">
          <Toggle
            checked={draft.queue.schedule_enabled}
            onChange={(schedule_enabled) => update("queue", { schedule_enabled })}
            label="Nur innerhalb eines Zeitfensters konvertieren"
          />
          {draft.queue.schedule_enabled && (
            <>
              <div className="grid gap-5 sm:grid-cols-2">
                <Field label="Beginn">
                  <input
                    type="time"
                    className="field"
                    value={draft.queue.schedule_start}
                    onChange={(e) => update("queue", { schedule_start: e.target.value })}
                  />
                </Field>
                <Field label="Ende" hint="Ein Fenster darf ueber Mitternacht laufen.">
                  <input
                    type="time"
                    className="field"
                    value={draft.queue.schedule_end}
                    onChange={(e) => update("queue", { schedule_end: e.target.value })}
                  />
                </Field>
              </div>
              <Field label="Wochentage">
                <div className="flex flex-wrap gap-2">
                  {WEEKDAYS.map((label, index) => (
                    <button
                      key={label}
                      onClick={() => toggleDay(index)}
                      className={cn(
                        "rounded-lg border px-3 py-1.5 text-sm transition-colors",
                        draft.queue.schedule_days.includes(index)
                          ? "border-brand-500 bg-brand-600/15 text-brand-400"
                          : "border-ink-700 text-ink-400 hover:border-ink-600",
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </Field>
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Hardware                                                                   */
/* -------------------------------------------------------------------------- */

function HardwareTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const { push } = useToast();
  const queryClient = useQueryClient();

  const { data: devices } = useQuery({
    queryKey: ["render-devices"],
    queryFn: endpoints.renderDevices,
  });
  const { data: info } = useQuery({ queryKey: ["system"], queryFn: endpoints.systemInfo });

  const detect = useMutation({
    mutationFn: () => endpoints.detectHardware(),
    onSuccess: (report) => {
      push(report.summary, "success");
      queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const hw = info?.hardware;

  return (
    <div className="space-y-4">
      {hw && (
        <Callout tone={hw.device_present ? "info" : "warn"} icon={<Cpu className="size-4" />}>
          {hw.summary}
        </Callout>
      )}

      <Panel
        title="Intel-Grafik"
        actions={
          <button
            className="btn-ghost btn-sm"
            onClick={() => detect.mutate()}
            disabled={detect.isPending}
          >
            {detect.isPending ? <Spinner className="size-3.5" /> : <Cpu className="size-3.5" />}
            Erneut erkennen
          </button>
        }
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Render-Geraet"
            hint={
              devices?.dri_present
                ? "Gefundene Geraete im Container."
                : "/dev/dri ist nicht sichtbar - im Unraid-Template als Device hinzufuegen."
            }
          >
            {devices?.devices.length ? (
              <Select
                value={draft.hardware.render_device}
                onChange={(render_device) => update("hardware", { render_device })}
                options={devices.devices
                  .filter((d) => d.is_render_node)
                  .map((d) => ({
                    value: d.path,
                    label: `${d.path}${d.writable ? "" : " (kein Schreibzugriff)"}`,
                  }))}
              />
            ) : (
              <input
                className="field font-mono text-sm"
                value={draft.hardware.render_device}
                onChange={(e) => update("hardware", { render_device: e.target.value })}
              />
            )}
          </Field>
        </div>

        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.hardware.hw_encode}
            onChange={(hw_encode) => update("hardware", { hw_encode })}
            label="AV1 auf der GPU kodieren, wenn moeglich"
            hint="Nur Intel Arc und Core Ultra koennen das. Aeltere iGPUs fallen automatisch auf die CPU zurueck."
          />
          <Toggle
            checked={draft.hardware.hw_decode}
            onChange={(hw_decode) => update("hardware", { hw_decode })}
            label="Quellmaterial auf der GPU dekodieren"
            hint="Entlastet die CPU spuerbar. Wird nur fuer Codecs genutzt, die die GPU nachweislich beherrscht."
          />
          <Toggle
            checked={draft.hardware.qsv_low_power}
            onChange={(qsv_low_power) => update("hardware", { qsv_low_power })}
            label="QSV Low-Power-Modus (VDENC)"
            hint="Auf den meisten Intel-Chips zwingend erforderlich. Nur abschalten, wenn das Hardware-Encoding sonst nicht startet."
          />
          <Toggle
            checked={draft.hardware.fallback_to_cpu}
            onChange={(fallback_to_cpu) => update("hardware", { fallback_to_cpu })}
            label="Bei Hardware-Fehlern auf die CPU ausweichen"
            hint="Ein abgebrochener GPU-Encode wird automatisch mit SVT-AV1 wiederholt, statt als Fehler zu enden."
          />
          <Toggle
            checked={draft.hardware.detect_on_start}
            onChange={(detect_on_start) => update("hardware", { detect_on_start })}
            label="Hardware beim Start pruefen"
          />
        </div>
      </Panel>

      {info?.ffmpeg && (
        <Panel title="ffmpeg">
          <dl className="space-y-2 text-sm">
            <div className="flex flex-wrap justify-between gap-2">
              <dt className="text-ink-500">Version</dt>
              <dd className="font-mono text-xs text-ink-300">{info.ffmpeg.version}</dd>
            </div>
            <div className="flex flex-wrap justify-between gap-2">
              <dt className="text-ink-500">Programm</dt>
              <dd className="font-mono text-xs text-ink-300">{info.ffmpeg.binary}</dd>
            </div>
            <div className="flex flex-wrap justify-between gap-2">
              <dt className="text-ink-500">Relevante Encoder</dt>
              <dd className="font-mono text-xs text-ink-300">
                {info.ffmpeg.encoders.join(", ") || "-"}
              </dd>
            </div>
          </dl>
        </Panel>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Advisor                                                                    */
/* -------------------------------------------------------------------------- */

function AdvisorTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const { push } = useToast();
  const [testing, setTesting] = useState(false);
  const { data: info } = useQuery({ queryKey: ["system"], queryFn: endpoints.systemInfo });

  const test = useMutation({
    mutationFn: () =>
      endpoints.testAdvisor({ api_key: draft.advisor.api_key, model: draft.advisor.model }),
    onMutate: () => setTesting(true),
    onSettled: () => setTesting(false),
    onSuccess: (result) => push(result.message, result.ok ? "success" : "error"),
    onError: (e: Error) => push(e.message, "error"),
  });

  return (
    <div className="space-y-4">
      <Callout tone="info" icon={<Sparkles className="size-4" />}>
        Die lokale Analyse entscheidet auch ohne diese Funktion vollstaendig eigenstaendig. Der
        KI-Berater ergaenzt das, was Messwerte nicht sehen koennen: ob ein Film ein koerniger
        Klassiker ist, ein flaechiger Anime oder eine dunkle Konzertaufnahme - und passt CRF und
        Filmkorn entsprechend an. Jede Aenderung wird auf den eingestellten Rahmen begrenzt.
      </Callout>

      {!info?.advisor.sdk_installed && (
        <Callout tone="warn">
          Das Python-Paket <code>anthropic</code> ist im Container nicht installiert - der Berater
          bleibt deaktiviert.
        </Callout>
      )}

      <Panel title="Claude-API">
        <div className="space-y-5">
          <Toggle
            checked={draft.advisor.enabled}
            onChange={(enabled) => update("advisor", { enabled })}
            label="KI-Berater verwenden"
            hint="Ohne Aktivierung werden keinerlei Daten nach aussen gesendet."
          />

          {draft.advisor.enabled && (
            <>
              <div className="grid gap-5 md:grid-cols-2">
                <Field
                  label="API-Schluessel"
                  hint="Wird in der lokalen Datenbank gespeichert und nur an die Anthropic-API gesendet."
                >
                  <div className="flex gap-2">
                    <input
                      type="password"
                      className="field font-mono text-sm"
                      value={draft.advisor.api_key}
                      onChange={(e) => update("advisor", { api_key: e.target.value })}
                      placeholder="sk-ant-..."
                      autoComplete="off"
                    />
                    <button
                      className="btn-ghost shrink-0"
                      onClick={() => test.mutate()}
                      disabled={testing || !draft.advisor.api_key}
                    >
                      {testing ? <Spinner className="size-4" /> : <Check className="size-4" />}
                      Testen
                    </button>
                  </div>
                </Field>
                <Field label="Modell">
                  <Select
                    value={draft.advisor.model}
                    onChange={(model) => update("advisor", { model })}
                    options={[
                      { value: "claude-opus-5", label: "Claude Opus 5 (beste Einschaetzung)" },
                      { value: "claude-sonnet-5", label: "Claude Sonnet 5 (guenstiger)" },
                      { value: "claude-haiku-4-5", label: "Claude Haiku 4.5 (am guenstigsten)" },
                    ]}
                  />
                </Field>
                <Field
                  label="Wann gefragt wird"
                  hint="Jede Anfrage kostet Geld. 'Nur bei Unsicherheit' fragt genau dann, wenn es etwas bringt."
                >
                  <Select
                    value={draft.advisor.mode}
                    onChange={(mode) => update("advisor", { mode })}
                    options={[
                      { value: "uncertain_only", label: "Nur bei unsicherer Einschaetzung (empfohlen)" },
                      { value: "all_candidates", label: "Bei jedem Kandidaten" },
                      { value: "explain_only", label: "Nur erklaeren, nichts aendern" },
                    ]}
                  />
                </Field>
                <Field label="Maximale Anfragen pro Scan" hint="Harte Obergrenze fuer die Kosten.">
                  <NumberField
                    value={draft.advisor.max_calls_per_scan}
                    onChange={(max_calls_per_scan) => update("advisor", { max_calls_per_scan })}
                    min={0}
                    max={5000}
                  />
                </Field>
                {draft.advisor.mode === "uncertain_only" && (
                  <Field
                    label="Unsicher unterhalb von"
                    hint="Bezieht sich auf die Sicherheit der lokalen Schaetzung."
                  >
                    <SliderField
                      value={draft.advisor.uncertain_below_confidence}
                      onChange={(uncertain_below_confidence) =>
                        update("advisor", { uncertain_below_confidence })
                      }
                      min={0}
                      max={1}
                      step={0.05}
                      format={(v) => `${Math.round(v * 100)} %`}
                    />
                  </Field>
                )}
                <Field
                  label="Maximale CRF-Verschiebung"
                  hint="Begrenzt, wie stark die KI die Qualitaetseinstellung veraendern darf."
                >
                  <NumberField
                    value={draft.advisor.max_crf_delta}
                    onChange={(max_crf_delta) => update("advisor", { max_crf_delta })}
                    min={0}
                    max={15}
                  />
                </Field>
              </div>

              <div className="space-y-3 border-t border-ink-800 pt-5">
                <Toggle
                  checked={draft.advisor.allow_setting_changes}
                  onChange={(allow_setting_changes) => update("advisor", { allow_setting_changes })}
                  label="Einstellungen anpassen lassen"
                  hint="Aus: die KI liefert nur eine Begruendung, aendert aber keine Werte."
                />
                <Toggle
                  checked={draft.advisor.include_filename}
                  onChange={(include_filename) => update("advisor", { include_filename })}
                  label="Dateinamen mitsenden"
                  hint="Hilft beim Erkennen von Anime, Klassikern oder Handyaufnahmen. Aus, wenn dir das zu viel Information ist."
                />
              </div>

              {info?.advisor.calls_used ? (
                <p className="text-xs text-ink-500">
                  In diesem Scan wurden bisher {info.advisor.calls_used} Anfragen gestellt.
                </p>
              ) : null}
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* System                                                                     */
/* -------------------------------------------------------------------------- */

function SystemTab() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const { data: info } = useQuery({ queryKey: ["system"], queryFn: endpoints.systemInfo });

  const reset = useMutation({
    mutationFn: () => endpoints.resetSettings(),
    onSuccess: () => {
      push("Einstellungen zurueckgesetzt.", "success");
      queryClient.invalidateQueries();
    },
  });

  return (
    <div className="space-y-4">
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

      <Panel
        title="Zuruecksetzen"
        subtitle="Setzt alle Einstellungen auf die Werkseinstellung zurueck"
      >
        <Callout tone="warn" icon={<AlertTriangle className="size-4" />}>
          Bibliothekspfade, gefundene Dateien und der Verlauf bleiben erhalten - nur die
          Einstellungen werden zurueckgesetzt.
        </Callout>
        <button
          className="btn-danger mt-4"
          onClick={() => {
            if (window.confirm("Alle Einstellungen auf Standardwerte zuruecksetzen?")) {
              reset.mutate();
            }
          }}
          disabled={reset.isPending}
        >
          <RotateCcw className="size-4" />
          Einstellungen zuruecksetzen
        </button>
      </Panel>

      <Panel title="Speicherorte">
        <div className="flex items-start gap-3 text-sm text-ink-400">
          <HardDrive className="mt-0.5 size-4 shrink-0 text-ink-500" />
          <p className="leading-relaxed">
            Datenbank und Papierkorb liegen unter <code className="text-ink-300">/config</code>,
            Zwischendateien beim Encoden unter <code className="text-ink-300">/transcode</code>. In
            Unraid sollte <code className="text-ink-300">/transcode</code> auf einer SSD oder im
            Cache-Pool liegen - dort entsteht waehrend des Encodens die komplette Ausgabedatei.
          </p>
        </div>
      </Panel>
    </div>
  );
}
