import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Bell, Brain, Cpu, FolderOpen, Gauge, Layers, Lock, Microscope, Music4, Save, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { AdvisorSettings } from "../components/AdvisorSettings";
import { endpoints, type Settings, type SettingsPatch, type SettingsSaveResult } from "../lib/api";
import { useToast } from "../lib/live";
import { diffSettings, normalizeSettings, rebase } from "../lib/settings";
import { ErrorState, Panel, Skeleton, Spinner, cn } from "../components/ui";
import { LibraryTab } from "./settings/LibraryTab";
import { AnalysisTab } from "./settings/AnalysisTab";
import { EncodingTab } from "./settings/EncodingTab";
import { AudioTab } from "./settings/AudioTab";
import { OutputTab } from "./settings/OutputTab";
import { QueueTab } from "./settings/QueueTab";
import { HardwareTab } from "./settings/HardwareTab";
import { NotificationsTab } from "./settings/NotificationsTab";
import { SecurityTab } from "./settings/SecurityTab";
import { SystemTab } from "./settings/SystemTab";
export { DirectoryPicker } from "./settings/LibraryTab";
export { renderDeviceOptions } from "./settings/HardwareTab";

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
