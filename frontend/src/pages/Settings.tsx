/** Every setting the application has - no environment variables needed. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Archive,
  Bell,
  Brain,
  Check,
  ChevronRight,
  Cpu,
  FolderOpen,
  FolderPlus,
  Gauge,
  HardDrive,
  Layers,
  Lock,
  Microscope,
  Music4,
  RotateCcw,
  Save,
  Send,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { AdvisorSettings } from "../components/AdvisorSettings";
import {
  endpoints,
  SECRET_MASK,
  type Settings,
  type SettingsPatch,
  type SettingsSaveResult,
} from "../lib/api";
import { bytes, number } from "../lib/format";
import { useDebouncedValue } from "../lib/hooks";
import { useToast } from "../lib/live";
import {
  canUseBrowsedPath,
  diffSettings,
  normalizeDirPath,
  normalizeSettings,
  rebase,
  toggleWeekday,
} from "../lib/settings";
import {
  Callout,
  ErrorState,
  Field,
  Modal,
  NumberField,
  Panel,
  SecretField,
  Select,
  SliderField,
  Skeleton,
  Spinner,
  TagListField,
  Toggle,
  cn,
  withCurrent,
} from "../components/ui";

const TABS = [
  { id: "library", label: "Bibliothek", icon: FolderOpen },
  { id: "analysis", label: "Analyse", icon: Microscope },
  { id: "encoding", label: "Encoding", icon: Gauge },
  { id: "audio", label: "Audio & Untertitel", icon: Music4 },
  { id: "output", label: "Ausgabe & Prüfung", icon: ShieldCheck },
  { id: "queue", label: "Warteschlange", icon: Layers },
  { id: "hardware", label: "Hardware", icon: Cpu },
  { id: "advisor", label: "KI-Berater", icon: Brain },
  { id: "notifications", label: "Benachrichtigungen", icon: Bell },
  { id: "security", label: "Sicherheit", icon: Lock },
  { id: "system", label: "System", icon: Archive },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function SettingsPage() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<TabId>("library");
  const [draft, setDraft] = useState<Settings | null>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const {
    data: saved,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ["settings"],
    queryFn: endpoints.settings,
    select: normalizeSettings,
  });

  // The draft follows the server.  When the saved settings change underneath it
  // (queue paused from the header, reset, Codex login), untouched fields take the
  // new value and only real edits survive.  The old base is captured here: the
  // updater runs later, when ``base.current`` already points at the new value.
  const base = useRef<Settings | null>(null);
  useEffect(() => {
    if (!saved) return;
    const oldBase = base.current;
    setDraft((prev) => (prev && oldBase ? rebase(oldBase, prev, saved) : structuredClone(saved)));
    base.current = saved;
  }, [saved]);

  const patch = useMemo(
    () => (saved && draft ? diffSettings(saved, draft) : {}),
    [saved, draft],
  );
  const dirty = Object.keys(patch).length > 0;

  /** Take a fresh server answer as the new clean state. */
  const adopt = (result: SettingsSaveResult | Settings) => {
    const { applied: _applied, ...settings } = result as SettingsSaveResult;
    void _applied;
    const clean = normalizeSettings(settings as Settings);
    base.current = clean;
    queryClient.setQueryData(["settings"], clean);
    setDraft(structuredClone(clean));
  };

  const save = useMutation({
    mutationFn: (p: SettingsPatch) => endpoints.saveSettings(p),
    onSuccess: (result) => {
      push("Einstellungen gespeichert.", "success");
      adopt(result);
      queryClient.invalidateQueries({ queryKey: ["system"] });
      reportApplied(result, push, (keys) =>
        keys.forEach((key) => queryClient.invalidateQueries({ queryKey: [key] })),
      );
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const applyProfile = useMutation({
    mutationFn: (name: string) => endpoints.applyProfile(name),
    onSuccess: (raw) => {
      push("Profil übernommen.", "success");
      const result = normalizeSettings(raw);
      // Keep unsaved edits elsewhere; the profile wins on the fields it sets.
      const oldBase = base.current;
      setDraft((prev) =>
        prev && oldBase ? rebase(oldBase, prev, result, true) : structuredClone(result),
      );
      base.current = result;
      queryClient.setQueryData(["settings"], result);
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const onApplyProfile = (name: string) => {
    if (
      dirty &&
      !window.confirm(
        "Das Profil wird sofort gespeichert. Deine übrigen ungespeicherten Änderungen bleiben als Entwurf erhalten – Felder, die das Profil setzt, bekommen aber die Profilwerte. Fortfahren?",
      )
    ) {
      return;
    }
    applyProfile.mutate(name);
  };

  function update<K extends keyof Settings>(group: K, groupPatch: Partial<Settings[K]>) {
    setDraft((prev) => (prev ? { ...prev, [group]: { ...prev[group], ...groupPatch } } : prev));
  }

  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = -1;
    if (e.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (e.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = TABS.length - 1;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next].id);
    tabRefs.current[TABS[next].id]?.focus();
  };

  if (isError && !saved) {
    return (
      <Panel>
        <ErrorState
          error={error}
          onRetry={() => refetch()}
          title="Einstellungen konnten nicht geladen werden"
        />
      </Panel>
    );
  }

  if (isLoading || !draft || !saved) {
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
      <div
        className="panel flex gap-1 overflow-x-auto p-1.5"
        role="tablist"
        aria-label="Bereiche der Einstellungen"
      >
        {TABS.map(({ id, label, icon: Icon }, index) => (
          <button
            key={id}
            ref={(el) => {
              tabRefs.current[id] = el;
            }}
            id={`settings-tab-${id}`}
            role="tab"
            aria-selected={tab === id}
            aria-controls={`settings-panel-${id}`}
            tabIndex={tab === id ? 0 : -1}
            onClick={() => setTab(id)}
            onKeyDown={(e) => onTabKey(e, index)}
            className={cn(
              "flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              tab === id
                ? "bg-brand-600/15 text-brand-400"
                : "text-ink-400 hover:bg-ink-800/70 hover:text-ink-200",
            )}
          >
            <Icon className="size-4" aria-hidden="true" />
            {label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`settings-panel-${tab}`}
        aria-labelledby={`settings-tab-${tab}`}
      >
        {tab === "library" && <LibraryTab draft={draft} update={update} />}
        {tab === "analysis" && <AnalysisTab draft={draft} update={update} />}
        {tab === "encoding" && (
          <EncodingTab
            draft={draft}
            update={update}
            onProfile={onApplyProfile}
            applying={applyProfile.isPending}
          />
        )}
        {tab === "audio" && <AudioTab draft={draft} update={update} />}
        {tab === "output" && <OutputTab draft={draft} update={update} />}
        {tab === "queue" && <QueueTab draft={draft} update={update} />}
        {tab === "hardware" && <HardwareTab draft={draft} update={update} />}
        {tab === "advisor" && <AdvisorSettings draft={draft} saved={saved} update={update} />}
        {tab === "notifications" && (
          <NotificationsTab
            draft={draft}
            saved={saved}
            update={update}
            unsaved={"notifications" in patch}
          />
        )}
        {tab === "security" && <SecurityTab draft={draft} saved={saved} update={update} />}
        {tab === "system" && <SystemTab draft={draft} update={update} onReset={adopt} />}
      </div>

      {/* ---------------- save bar ---------------- */}
      {dirty && (
        <div className="fixed inset-x-0 bottom-0 z-30 border-t border-ink-700 bg-ink-900/95 backdrop-blur-md">
          <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
            <span className="text-sm text-ink-300">Es gibt ungespeicherte Änderungen.</span>
            <div className="ml-auto flex gap-2">
              <button
                className="btn-ghost btn-sm"
                onClick={() => setDraft(structuredClone(saved))}
              >
                <X className="size-3.5" aria-hidden="true" />
                Verwerfen
              </button>
              <button
                className="btn-primary btn-sm"
                onClick={() => save.mutate(patch)}
                disabled={save.isPending}
              >
                {save.isPending ? (
                  <Spinner className="size-3.5" />
                ) : (
                  <Save className="size-3.5" aria-hidden="true" />
                )}
                Speichern
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Tell what a save or reset did to files that had been analysed already. */
function reportApplied(
  result: SettingsSaveResult,
  push: (message: string, tone?: "success" | "error" | "info") => void,
  invalidate: (keys: string[]) => void,
) {
  const applied = result.applied;
  const codecs = applied?.codec_exclusions;
  if (applied?.h264_reanalysis) {
    push(
      `${applied.h264_reanalysis} H.264-Dateien zur Neubewertung vorgemerkt. Jetzt einen Scan starten.`,
      "info",
    );
    invalidate(["files", "stats", "library"]);
  }
  if (codecs && (codecs.excluded || codecs.restored)) {
    const parts: string[] = [];
    if (codecs.excluded) parts.push(`${codecs.excluded} aus der Kandidatenliste entfernt`);
    if (codecs.restored) parts.push(`${codecs.restored} zur Neubewertung vorgemerkt`);
    push(`Codec-Ausschluss angewendet: ${parts.join(", ")}.`, "info");
    invalidate(["files", "stats", "library", "jobs"]);
  }
  if (codecs?.queued_untouched) {
    push(
      `${codecs.queued_untouched} bereits eingereihte Datei(en) bleiben in der Warteschlange – dort ` +
        "kannst du sie einzeln entfernen.",
      "info",
    );
  }
}

type UpdateFn = <K extends keyof Settings>(group: K, patch: Partial<Settings[K]>) => void;

/* -------------------------------------------------------------------------- */
/* Library                                                                    */
/* -------------------------------------------------------------------------- */

function LibraryTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [pickerOpen, setPickerOpen] = useState(false);

  const {
    data: paths,
    isError,
    error,
    refetch,
  } = useQuery({ queryKey: ["library", "paths"], queryFn: endpoints.libraryPaths });

  const addPath = useMutation({
    mutationFn: (path: string) => endpoints.addLibraryPath({ path }),
    onSuccess: () => {
      push("Ordner hinzugefügt.", "success");
      queryClient.invalidateQueries({ queryKey: ["library"] });
      setPickerOpen(false);
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const removePath = useMutation({
    mutationFn: (id: number) => endpoints.deleteLibraryPath(id),
    onSuccess: (result) => {
      const cancelled = result?.jobs_cancelled ?? 0;
      push(
        cancelled
          ? `Ordner entfernt, ${cancelled} laufende oder wartende Konvertierung(en) abgebrochen.`
          : "Ordner entfernt.",
        "success",
      );
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["library"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const togglePath = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      endpoints.updateLibraryPath(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["library"] }),
    onError: (e: Error) => push(e.message, "error"),
  });

  return (
    <div className="space-y-4">
      <Panel
        title="Medien-Ordner"
        subtitle="Diese Pfade durchsucht Optimizarr. Sie müssen im Docker-Template als Volume gemappt sein."
        actions={
          <button className="btn-primary btn-sm" onClick={() => setPickerOpen(true)}>
            <FolderPlus className="size-3.5" aria-hidden="true" />
            Ordner hinzufügen
          </button>
        }
        bodyClassName={paths?.length ? "space-y-2" : ""}
      >
        {isError && !paths ? (
          <ErrorState compact error={error} onRetry={() => refetch()} />
        ) : !paths?.length ? (
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
                aria-hidden="true"
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
                      Pfad existiert im Container nicht – Volume-Mapping prüfen
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
                      `„${entry.path}“ entfernen? Die bekannten Dateien dieses Ordners werden aus der Datenbank gelöscht und ihre wartenden oder laufenden Konvertierungen abgebrochen – auf der Platte passiert nichts.`,
                    )
                  ) {
                    removePath.mutate(entry.id);
                  }
                }}
                className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-danger-500/15 hover:text-danger-400"
                title="Ordner entfernen"
                aria-label={`Ordner ${entry.path} entfernen`}
              >
                <Trash2 className="size-4" aria-hidden="true" />
              </button>
            </div>
          ))
        )}
      </Panel>

      <Panel title="Was gescannt wird">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Dateiendungen"
            hint="Durch Kommas getrennt, übernommen mit Enter oder beim Verlassen des Feldes. Alles andere wird ignoriert."
          >
            <TagListField
              values={draft.library.extensions}
              onChange={(extensions) => update("library", { extensions })}
              placeholder="mkv, mp4, avi"
              ariaLabel="Dateiendungen"
            />
          </Field>
          <Field
            label="Ausschluss-Muster"
            hint="Pfad-Muster, die übersprungen werden – z. B. */extras/* oder *sample*. Durch Kommas getrennt; Leerzeichen gehören zum Muster."
          >
            <TagListField
              values={draft.library.exclude_patterns}
              onChange={(exclude_patterns) => update("library", { exclude_patterns })}
              ariaLabel="Ausschluss-Muster"
            />
          </Field>
          <Field label="Mindestgröße" hint="Kleinere Dateien lohnen den Aufwand nicht.">
            <NumberField
              value={draft.library.min_file_size_mb}
              onChange={(min_file_size_mb) => update("library", { min_file_size_mb })}
              min={0}
              suffix="MB"
              ariaLabel="Mindestgröße"
            />
          </Field>
          <Field label="Mindestlaufzeit" hint="Filtert Trailer und Schnipsel heraus.">
            <NumberField
              value={draft.library.min_duration_seconds}
              onChange={(min_duration_seconds) => update("library", { min_duration_seconds })}
              min={0}
              suffix="Sek"
              ariaLabel="Mindestlaufzeit"
            />
          </Field>
        </div>
      </Panel>

      <Panel title="Scan-Zeitplan">
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Automatischer Scan alle" hint="0 schaltet den automatischen Scan ab.">
            <NumberField
              value={draft.library.scan_interval_hours}
              onChange={(scan_interval_hours) => update("library", { scan_interval_hours })}
              min={0}
              max={720}
              suffix="Std"
              ariaLabel="Automatischer Scan alle"
            />
          </Field>
          <Field
            label="Analyse neu aufrollen nach"
            hint="Alte Einschätzungen werden erneuert – sinnvoll, weil das Lernmodell besser wird."
          >
            <NumberField
              value={draft.library.reanalyze_after_days}
              onChange={(reanalyze_after_days) => update("library", { reanalyze_after_days })}
              min={0}
              suffix="Tage"
              ariaLabel="Analyse neu aufrollen nach"
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
              label="Nur geänderte Dateien erneut prüfen"
              hint="Deutlich schneller. Ausschalten, um die ganze Bibliothek neu zu bewerten."
            />
            <Toggle
              checked={draft.library.follow_symlinks}
              onChange={(follow_symlinks) => update("library", { follow_symlinks })}
              label="Symlinks folgen"
              hint="Vorsicht bei verschachtelten Shares – kann zu Doppelzählungen führen."
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

export function DirectoryPicker({
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
  const [input, setInput] = useState("/media");
  // Typing "/media/movies" must not fire a request per letter.
  const debounced = useDebouncedValue(input, 350);
  const path = normalizeDirPath(debounced);

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ["browse", path],
    queryFn: ({ signal }) => endpoints.browse(path, { signal }),
    enabled: open && path !== "",
    retry: false,
    placeholderData: (prev) => prev,
  });

  // Navigating by click is a deliberate choice - no need to wait for the debounce.
  const go = (target: string) => setInput(target);
  const settled = normalizeDirPath(input) === path;
  const usable =
    settled && canUseBrowsedPath(input, data?.path, { fetching: isFetching, error: isError });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Ordner auswählen"
      subtitle="Pfade, wie sie im Container sichtbar sind"
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Abbrechen
          </button>
          <button
            className="btn-primary"
            onClick={() => data && onSelect(data.path)}
            disabled={busy || !usable}
            title={usable ? undefined : "Erst warten, bis der Ordner angezeigt wird"}
          >
            {busy ? <Spinner className="size-4" /> : <Check className="size-4" aria-hidden="true" />}
            Diesen Ordner verwenden
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <input
          className="field font-mono text-sm"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="/media/movies"
          aria-label="Pfad im Container"
          spellCheck={false}
          data-autofocus
        />
        <div className="rounded-lg border border-ink-700 bg-ink-950/50">
          <div className="flex items-center gap-2 border-b border-ink-800 px-3 py-2">
            <span className="truncate font-mono text-xs text-ink-400">
              {isError ? path : (data?.path ?? path)}
            </span>
            {isFetching && <Spinner className="size-3 text-ink-500" />}
            {!isError && data?.parent && (
              <button
                className="ml-auto shrink-0 text-xs text-brand-400 hover:underline"
                onClick={() => go(data.parent!)}
              >
                Eine Ebene hoch
              </button>
            )}
          </div>
          <div className="max-h-64 overflow-y-auto">
            {isError ? (
              <p className="px-3 py-4 text-sm text-danger-400" role="alert">
                {(error as Error)?.message || "Diesen Ordner gibt es im Container nicht."}
              </p>
            ) : !data ? (
              <div className="p-3">
                <Spinner />
              </div>
            ) : data.entries.length ? (
              data.entries.map((entry) => (
                <button
                  key={entry.path}
                  onClick={() => go(entry.path)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-ink-200 transition-colors hover:bg-ink-800/70"
                >
                  <FolderOpen className="size-4 shrink-0 text-ink-500" aria-hidden="true" />
                  <span className="truncate">{entry.name}</span>
                  <ChevronRight className="ml-auto size-3.5 shrink-0 text-ink-600" aria-hidden="true" />
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

function EncodingTab({
  draft,
  update,
  onProfile,
  applying,
}: {
  draft: Settings;
  update: UpdateFn;
  onProfile: (name: string) => void;
  applying: boolean;
}) {
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
      desc: "Maximale Ersparnis, schneller Encode. Auf großen TVs sichtbar weicher.",
      crf: 35,
    },
  ];

  return (
    <div className="space-y-4">
      <Panel
        title="Qualitätsprofil"
        subtitle="Setzt CRF, Preset und Qualitätsziel in einem Rutsch und speichert sofort"
      >
        <div className="grid gap-3 md:grid-cols-3">
          {profiles.map((profile) => (
            <button
              key={profile.id}
              onClick={() => onProfile(profile.id)}
              disabled={applying}
              aria-pressed={draft.encoding.profile === profile.id}
              className={cn(
                "rounded-lg border p-4 text-left transition-colors disabled:opacity-60",
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
            hint="„Automatisch“ nimmt den GPU-Encoder, sobald ein Testlauf beweist, dass er funktioniert."
          >
            <Select
              value={draft.encoding.encoder}
              onChange={(encoder) => update("encoding", { encoder })}
              ariaLabel="Encoder-Auswahl"
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
              ariaLabel="Container"
              options={[
                { value: "mkv", label: "MKV (empfohlen – kann alles)" },
                { value: "mp4", label: "MP4 (kompatibler, verliert Bild-Untertitel)" },
              ]}
            />
          </Field>

          <Field
            label="Basis-Qualität (CRF)"
            hint="Niedriger = besser und größer. Die Analyse verschiebt diesen Wert pro Datei."
          >
            <SliderField
              value={draft.encoding.crf}
              onChange={(crf) => update("encoding", { crf })}
              min={16}
              max={50}
              ariaLabel="Basis-Qualität (CRF)"
              marks={[
                { value: 16, label: "16 · sehr gut" },
                { value: 32, label: "32" },
                { value: 50, label: "50 · klein" },
              ]}
            />
          </Field>
          <Field
            label="Preset (nur SVT-AV1)"
            hint="Niedriger = langsamer, aber kleinere Dateien bei gleicher Qualität. 6 ist ein guter Alltagswert."
          >
            <SliderField
              value={draft.encoding.preset}
              onChange={(preset) => update("encoding", { preset })}
              min={0}
              max={13}
              ariaLabel="Preset (nur SVT-AV1)"
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
              ariaLabel="CRF-Untergrenze"
            />
          </Field>
          <Field label="CRF-Obergrenze">
            <NumberField
              value={draft.encoding.crf_max}
              onChange={(crf_max) => update("encoding", { crf_max })}
              min={1}
              max={63}
              ariaLabel="CRF-Obergrenze"
            />
          </Field>
        </div>

        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.encoding.allow_crf_adjust}
            onChange={(allow_crf_adjust) => update("encoding", { allow_crf_adjust })}
            label="CRF pro Datei automatisch anpassen"
            hint="Die Analyse sucht den höchsten CRF, der das Qualitätsziel noch hält."
          />
          <Toggle
            checked={draft.encoding.force_10bit}
            onChange={(force_10bit) => update("encoding", { force_10bit })}
            label="Immer in 10 Bit kodieren"
            hint="AV1 komprimiert auch 8-Bit-Quellen in 10 Bit effizienter und vermeidet Farbstufen in Verläufen."
          />
          <Toggle
            checked={draft.encoding.auto_film_grain}
            onChange={(auto_film_grain) => update("encoding", { auto_film_grain })}
            label="Filmkorn automatisch erkennen und synthetisieren"
            hint="Bei körnigem Material wird das Korn vor dem Encoden entfernt und bei der Wiedergabe neu erzeugt – das spart sehr viel Bitrate. Auf sauberem Material bleibt die Funktion aus."
          />
          <Toggle
            checked={draft.encoding.deinterlace}
            onChange={(deinterlace) => update("encoding", { deinterlace })}
            label="Interlaced-Material deinterlacen"
          />
          <Toggle
            checked={draft.encoding.copy_chapters}
            onChange={(copy_chapters) => update("encoding", { copy_chapters })}
            label="Kapitelmarken übernehmen"
          />
          <Toggle
            checked={draft.encoding.copy_attachments}
            onChange={(copy_attachments) => update("encoding", { copy_attachments })}
            label="Anhänge übernehmen (Schriftarten für ASS-Untertitel)"
          />
        </div>
      </Panel>

      <Panel title="Erweitert">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Maximale Breite"
            hint="0 behält die Auflösung bei. Sonst wird proportional herunterskaliert."
          >
            <NumberField
              value={draft.encoding.max_width}
              onChange={(max_width) => update("encoding", { max_width })}
              min={0}
              suffix="px"
              ariaLabel="Maximale Breite"
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
              ariaLabel="Keyframe-Abstand"
            />
          </Field>
          <Field
            label="Filmkorn-Synthese fest"
            hint="0 lässt die automatische Erkennung entscheiden. Sonst gilt dieser Wert für alle Dateien."
          >
            <SliderField
              value={draft.encoding.film_grain_synthesis}
              onChange={(film_grain_synthesis) => update("encoding", { film_grain_synthesis })}
              min={0}
              max={50}
              format={(v) => (v === 0 ? "auto" : String(v))}
              ariaLabel="Filmkorn-Synthese fest"
            />
          </Field>
          <Field label="Abbruch nach" hint="Sicherheitsnetz gegen hängende Encodes.">
            <NumberField
              value={draft.encoding.max_encode_hours}
              onChange={(max_encode_hours) => update("encoding", { max_encode_hours })}
              min={1}
              max={72}
              suffix="Std"
              ariaLabel="Abbruch nach"
            />
          </Field>
          <div className="md:col-span-2">
            <Field
              label="Zusätzliche ffmpeg-Parameter"
              hint="Werden unverändert an ffmpeg angehängt. Nur für Leute, die wissen, was sie tun."
            >
              <input
                className="field font-mono text-sm"
                value={draft.encoding.extra_ffmpeg_args}
                onChange={(e) => update("encoding", { extra_ffmpeg_args: e.target.value })}
                placeholder="-svtav1-params enable-overlays=1"
                aria-label="Zusätzliche ffmpeg-Parameter"
                spellCheck={false}
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
        subtitle="Bei einem 4-GB-Film können 1,5 GB auf eine unkomprimierte Tonspur entfallen"
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Umgang mit Audio">
            <Select
              value={draft.audio.mode}
              onChange={(mode) => update("audio", { mode })}
              ariaLabel="Umgang mit Audio"
              options={[
                { value: "opus_if_bloated", label: "Nur aufgeblähte Spuren umwandeln (empfohlen)" },
                { value: "copy", label: "Immer unverändert kopieren" },
                { value: "opus", label: "Alles nach Opus umwandeln" },
              ]}
            />
          </Field>
          <Field
            label="Opus-Bitrate je Kanal"
            hint="48 kbit/s pro Kanal sind bei Opus transparent – 5.1 landet bei rund 288 kbit/s."
          >
            <NumberField
              value={draft.audio.opus_bitrate_per_channel}
              onChange={(opus_bitrate_per_channel) => update("audio", { opus_bitrate_per_channel })}
              min={24}
              max={128}
              suffix="kbit/s"
              ariaLabel="Opus-Bitrate je Kanal"
            />
          </Field>
          {draft.audio.mode === "opus_if_bloated" && (
            <Field
              label="Ab wann gilt eine Spur als aufgebläht"
              hint="Spuren darunter werden unverändert kopiert. Verlustfreie Formate wie TrueHD werden immer umgewandelt."
            >
              <NumberField
                value={draft.audio.bloat_threshold_kbps_per_channel}
                onChange={(bloat_threshold_kbps_per_channel) =>
                  update("audio", { bloat_threshold_kbps_per_channel })
                }
                min={32}
                max={512}
                suffix="kbit/s je Kanal"
                ariaLabel="Ab wann gilt eine Spur als aufgebläht"
              />
            </Field>
          )}
          <Field
            label="Sprachen behalten"
            hint="Leer lässt alle Spuren drin. Sonst ISO-Codes wie deu, eng – durch Kommas getrennt."
          >
            <TagListField
              values={draft.audio.keep_languages}
              onChange={(keep_languages) => update("audio", { keep_languages })}
              placeholder="deu, eng"
              ariaLabel="Audio-Sprachen behalten"
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
              ariaLabel="Umgang mit Untertiteln"
              options={[
                { value: "copy", label: "Alle übernehmen (empfohlen)" },
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
              ariaLabel="Untertitel-Sprachen behalten"
            />
          </Field>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Output & checks                                                            */
/* -------------------------------------------------------------------------- */

function OutputTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
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
            hint="Verhindert größere Ergebnisse. Gilt nicht für H.264, wenn die vollständige Umstellung unter Analyse aktiviert ist."
          />
          <Field
            label="Mindestersparnis zum Behalten"
            hint="Ergebnisse unter dieser Ersparnis werden verworfen. H.264 im Umstellungsmodus ist davon ausgenommen."
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
            checked={draft.output.verify_vmaf}
            onChange={(verify_vmaf) => update("output", { verify_vmaf })}
            label="Qualität der fertigen Datei messen (VMAF)"
            hint="Sehr gründlich, kostet aber zusätzliche Rechenzeit pro Datei."
          />
          {draft.output.verify_vmaf && (
            <Field label="Mindest-VMAF zum Behalten">
              <SliderField
                value={draft.output.min_accept_vmaf}
                onChange={(min_accept_vmaf) => update("output", { min_accept_vmaf })}
                min={70}
                max={99}
                step={0.5}
                ariaLabel="Mindest-VMAF zum Behalten"
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
              hint="Papierkorb ist die sichere Wahl – du kannst jederzeit zurück."
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

const WEEKDAYS = [
  { short: "Mo", long: "Montag" },
  { short: "Di", long: "Dienstag" },
  { short: "Mi", long: "Mittwoch" },
  { short: "Do", long: "Donnerstag" },
  { short: "Fr", long: "Freitag" },
  { short: "Sa", long: "Samstag" },
  { short: "So", long: "Sonntag" },
];

function QueueTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const { push } = useToast();
  const toggleDay = (day: number) => {
    const next = toggleWeekday(draft.queue.schedule_days, day);
    if (!next) {
      push("Mindestens ein Wochentag muss ausgewählt bleiben.", "info");
      return;
    }
    update("queue", { schedule_days: next });
  };

  return (
    <div className="space-y-4">
      <Panel title="Verarbeitung">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Gleichzeitige Konvertierungen"
            hint="Auf einem Heimserver ist 1 fast immer richtig – AV1-Encoding nutzt ohnehin alle Kerne."
          >
            <NumberField
              value={draft.queue.max_concurrent_jobs}
              onChange={(max_concurrent_jobs) => update("queue", { max_concurrent_jobs })}
              min={1}
              max={8}
              ariaLabel="Gleichzeitige Konvertierungen"
            />
          </Field>
          <Field
            label="CPU-Threads"
            hint="0 nutzt alle Kerne. Begrenzt pro Job den SVT-AV1-Encoder und die Decoder-Threads – niedriger setzen, wenn parallel noch andere Dienste laufen."
          >
            <NumberField
              value={draft.queue.cpu_threads}
              onChange={(cpu_threads) => update("queue", { cpu_threads })}
              min={0}
              max={128}
              ariaLabel="CPU-Threads"
            />
          </Field>
          <Field
            label="Prozesspriorität (nice)"
            hint="Höher = freundlicher zu anderen Diensten. 10 ist ein guter Wert für einen NAS."
          >
            <SliderField
              value={draft.queue.nice_level}
              onChange={(nice_level) => update("queue", { nice_level })}
              min={-20}
              max={19}
              ariaLabel="Prozesspriorität (nice)"
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
              ariaLabel="Mindestens freier Speicher"
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
              hint="Schützt davor, dass Grenzfälle ungefragt Rechenzeit verbrauchen. H.264 im Umstellungsmodus wird unabhängig von dieser Schwelle eingereiht."
            >
              <SliderField
                value={draft.queue.auto_queue_min_saving_percent}
                onChange={(auto_queue_min_saving_percent) =>
                  update("queue", { auto_queue_min_saving_percent })
                }
                min={5}
                max={80}
                format={(v) => `${v} %`}
                ariaLabel="Nur ab dieser Ersparnis automatisch einreihen"
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
                    aria-label="Beginn des Zeitfensters"
                  />
                </Field>
                <Field label="Ende" hint="Ein Fenster darf über Mitternacht laufen.">
                  <input
                    type="time"
                    className="field"
                    value={draft.queue.schedule_end}
                    onChange={(e) => update("queue", { schedule_end: e.target.value })}
                    aria-label="Ende des Zeitfensters"
                  />
                </Field>
              </div>
              <Field
                label="Wochentage"
                hint="Mindestens ein Tag bleibt ausgewählt – ohne Tag würde nie konvertiert."
              >
                <div className="flex flex-wrap gap-2" role="group" aria-label="Wochentage">
                  {WEEKDAYS.map((day, index) => {
                    const active = draft.queue.schedule_days.includes(index);
                    return (
                      <button
                        key={day.short}
                        onClick={() => toggleDay(index)}
                        aria-pressed={active}
                        aria-label={day.long}
                        title={day.long}
                        className={cn(
                          "rounded-lg border px-3 py-1.5 text-sm transition-colors",
                          active
                            ? "border-brand-500 bg-brand-600/15 text-brand-400"
                            : "border-ink-700 text-ink-400 hover:border-ink-600",
                        )}
                      >
                        {day.short}
                      </button>
                    );
                  })}
                </div>
              </Field>
              {draft.queue.schedule_days.length === 0 && (
                <Callout tone="danger">
                  Es ist kein Wochentag ausgewählt – so startet nie ein Job. Bitte mindestens
                  einen Tag wählen.
                </Callout>
              )}
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

/** Options for the render device: render nodes first, then card* nodes, and
 *  the stored value even when the container does not show it (any more). */
export function renderDeviceOptions(
  devices: { path: string; writable: boolean; is_render_node: boolean }[],
  current: string,
): { value: string; label: string }[] {
  const sorted = [...devices].sort(
    (a, b) => Number(b.is_render_node) - Number(a.is_render_node) || a.path.localeCompare(b.path),
  );
  const options = sorted.map((d) => ({
    value: d.path,
    label:
      `${d.path}${d.is_render_node ? "" : " (kein Render-Node)"}` +
      `${d.writable ? "" : " (kein Schreibzugriff)"}`,
  }));
  return withCurrent(options, current, (v) => `${v} (im Container nicht gefunden)`);
}

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
            {detect.isPending ? <Spinner className="size-3.5" /> : <Cpu className="size-3.5" aria-hidden="true" />}
            Erneut erkennen
          </button>
        }
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Render-Gerät"
            hint={
              devices?.dri_present
                ? "Gefundene Geräte im Container. Für die Arc/iGPU wird ein renderD-Gerät gebraucht."
                : "/dev/dri ist nicht sichtbar – im Unraid-Template als Device hinzufügen."
            }
          >
            {devices?.devices.length ? (
              <Select
                value={draft.hardware.render_device}
                onChange={(render_device) => update("hardware", { render_device })}
                ariaLabel="Render-Gerät"
                options={renderDeviceOptions(devices.devices, draft.hardware.render_device)}
              />
            ) : (
              <input
                className="field font-mono text-sm"
                value={draft.hardware.render_device}
                onChange={(e) => update("hardware", { render_device: e.target.value })}
                aria-label="Render-Gerät"
              />
            )}
          </Field>
        </div>

        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.hardware.hw_encode}
            onChange={(hw_encode) => update("hardware", { hw_encode })}
            label="AV1 auf der GPU kodieren, wenn möglich"
            hint="Nur Intel Arc und Core Ultra können das. Ältere iGPUs fallen automatisch auf die CPU zurück."
          />
          <Toggle
            checked={draft.hardware.hw_decode}
            onChange={(hw_decode) => update("hardware", { hw_decode })}
            label="Quellmaterial auf der GPU dekodieren"
            hint="Entlastet die CPU spürbar. Wird nur für Codecs genutzt, die die GPU nachweislich beherrscht."
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
            label="Hardware beim Start prüfen"
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
/* Notifications                                                              */
/* -------------------------------------------------------------------------- */

function NotificationsTab({
  draft,
  saved,
  update,
  unsaved,
}: {
  draft: Settings;
  saved: Settings;
  update: UpdateFn;
  unsaved: boolean;
}) {
  const { push } = useToast();
  const stored = saved.notifications.webhook_url === SECRET_MASK;
  const url = draft.notifications.webhook_url;
  // The masked value tells the server to use the stored address; a typed one
  // is tested as it is, before saving.
  const testable = url !== "" && (url !== SECRET_MASK || stored);

  const test = useMutation({
    mutationFn: () => endpoints.testNotification(url),
    onSuccess: (result) => {
      const ok = result?.ok !== false;
      push(
        result?.message || (ok ? "Testnachricht gesendet." : "Senden fehlgeschlagen."),
        ok ? "success" : "error",
      );
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  return (
    <div className="space-y-4">
      <Panel
        title="Webhook"
        subtitle="Optimizarr schickt bei den gewählten Ereignissen eine JSON-Nachricht per POST"
      >
        <div className="space-y-5">
          <Field
            label="Webhook-Adresse"
            hint={
              <>
                Zum Beispiel ein Home-Assistant-Webhook, ntfy, Gotify oder ein eigener Dienst. Der
                Inhalt ist <code className="text-ink-300">{"{event, title, message, data}"}</code>.
                Leer = keine Benachrichtigungen.
              </>
            }
          >
            <SecretField
              type="text"
              value={draft.notifications.webhook_url}
              stored={stored}
              onChange={(webhook_url) => update("notifications", { webhook_url })}
              placeholder="https://…"
              ariaLabel="Webhook-Adresse"
            />
          </Field>
          <div className="space-y-3 border-t border-ink-800 pt-5">
            <Toggle
              checked={draft.notifications.notify_on_job_done}
              onChange={(notify_on_job_done) => update("notifications", { notify_on_job_done })}
              label="Wenn eine Konvertierung fertig ist"
            />
            <Toggle
              checked={draft.notifications.notify_on_job_failed}
              onChange={(notify_on_job_failed) => update("notifications", { notify_on_job_failed })}
              label="Wenn eine Konvertierung fehlschlägt oder verworfen wird"
            />
            <Toggle
              checked={draft.notifications.notify_on_scan_done}
              onChange={(notify_on_scan_done) => update("notifications", { notify_on_scan_done })}
              label="Wenn ein Bibliotheks-Scan abgeschlossen ist"
            />
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-ink-800 pt-5">
            <button
              className="btn-ghost"
              onClick={() => test.mutate()}
              disabled={test.isPending || !testable}
            >
              {test.isPending ? <Spinner className="size-4" /> : <Send className="size-4" aria-hidden="true" />}
              Test senden
            </button>
            <span className="text-xs text-ink-500">
              {!testable
                ? "Zuerst eine Adresse eintragen."
                : url === SECRET_MASK
                  ? "Schickt eine Testnachricht an die gespeicherte Adresse."
                  : unsaved
                    ? "Schickt eine Testnachricht an die eingetragene Adresse – gespeichert wird dabei nichts."
                    : "Schickt eine Testnachricht an die eingetragene Adresse."}
            </span>
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Security                                                                   */
/* -------------------------------------------------------------------------- */

function SecurityTab({
  draft,
  saved,
  update,
}: {
  draft: Settings;
  saved: Settings;
  update: UpdateFn;
}) {
  const stored = saved.security.password === SECRET_MASK;
  const hasPassword = draft.security.password !== "";
  return (
    <div className="space-y-4">
      {!saved.security.auth_enabled && (
        <Callout tone="warn" icon={<AlertTriangle className="size-4" />}>
          Die Anmeldung ist aus: Jeder, der den Server im Netzwerk erreicht, kann Optimizarr
          bedienen, Dateien konvertieren und Einstellungen ändern.
        </Callout>
      )}
      <Panel
        title="Anmeldung"
        subtitle="Benutzername und Passwort für die Oberfläche und die API"
      >
        <div className="space-y-5">
          <Toggle
            checked={draft.security.auth_enabled}
            onChange={(auth_enabled) => update("security", { auth_enabled })}
            label="Anmeldung verlangen"
            hint="Der Browser fragt dann einmal nach Benutzername und Passwort. Nur die Statusabfrage /api/health bleibt offen."
          />
          <div className="grid gap-5 md:grid-cols-2">
            <Field label="Benutzername">
              <input
                className="field"
                value={draft.security.username}
                onChange={(e) => update("security", { username: e.target.value })}
                autoComplete="username"
                aria-label="Benutzername"
                spellCheck={false}
              />
            </Field>
            <Field
              label="Passwort"
              hint="Wird nur als Hash gespeichert. Leer lassen, um das bisherige zu behalten."
            >
              <SecretField
                value={draft.security.password}
                stored={stored}
                onChange={(password) => update("security", { password })}
                placeholder="Neues Passwort"
                ariaLabel="Passwort"
              />
            </Field>
          </div>
          {draft.security.auth_enabled && !hasPassword && (
            <Callout tone="danger">
              Ohne Passwort lässt sich die Anmeldung nicht einschalten. Bitte ein Passwort
              eintragen.
            </Callout>
          )}
          <p className="text-xs leading-relaxed text-ink-400">
            Über reines HTTP werden Benutzername und Passwort nur kodiert, nicht verschlüsselt
            übertragen. Für den Zugriff von außerhalb des Heimnetzes gehört Optimizarr hinter
            einen Reverse-Proxy mit HTTPS.
          </p>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* System                                                                     */
/* -------------------------------------------------------------------------- */

function SystemTab({
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
