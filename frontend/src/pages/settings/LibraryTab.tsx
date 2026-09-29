import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, FolderOpen, FolderPlus, Trash2 } from "lucide-react";
import { useState } from "react";
import { endpoints, type Settings } from "../../lib/api";
import { bytes, number } from "../../lib/format";
import { useDebouncedValue } from "../../lib/hooks";
import { useToast } from "../../lib/live";
import { canUseBrowsedPath, normalizeDirPath } from "../../lib/settings";
import { Callout, ErrorState, Field, Modal, NumberField, Panel, Spinner, TagListField, Toggle, cn } from "../../components/ui";
import type { UpdateFn } from "./types";

export function LibraryTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
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
