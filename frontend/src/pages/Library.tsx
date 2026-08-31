/** The file browser: filter, inspect, and queue individual files. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  EyeOff,
  FileVideo,
  Microscope,
  Play,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { endpoints, type MediaFile } from "../lib/api";
import {
  bytes,
  bitrate,
  confidenceLabel,
  duration,
  percent,
  relativeTime,
  resolutionLabel,
  STATE_LABELS,
  number,
} from "../lib/format";
import { useToast } from "../lib/live";
import {
  Callout,
  EmptyState,
  Modal,
  Panel,
  Select,
  Skeleton,
  Spinner,
  StateBadge,
  cn,
} from "../components/ui";

const STATE_FILTERS = [
  { value: "candidate", label: "Kandidaten" },
  { value: "all", label: "Alle" },
  { value: "actionable", label: "In Arbeit" },
  { value: "done", label: "Konvertiert" },
  { value: "skipped", label: "Uebersprungen" },
  { value: "failed", label: "Fehler" },
  { value: "ignored", label: "Ignoriert" },
  { value: "new", label: "Noch nicht analysiert" },
];

const SORT_OPTIONS = [
  { value: "saving", label: "Ersparnis (absolut)" },
  { value: "saving_pct", label: "Ersparnis (Prozent)" },
  { value: "size", label: "Dateigroesse" },
  { value: "duration", label: "Laufzeit" },
  { value: "analyzed", label: "Zuletzt analysiert" },
  { value: "name", label: "Name" },
];

export default function Library() {
  const [params, setParams] = useSearchParams();
  const { push } = useToast();
  const queryClient = useQueryClient();

  const [state, setState] = useState("candidate");
  const [sort, setSort] = useState("saving");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [libraryId, setLibraryId] = useState<string>("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [detailId, setDetailId] = useState<number | null>(
    params.get("file") ? Number(params.get("file")) : null,
  );

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 350);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => setPage(1), [state, sort, debouncedSearch, libraryId]);

  const { data: libraries } = useQuery({
    queryKey: ["library", "paths"],
    queryFn: endpoints.libraryPaths,
  });

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ["files", state, sort, debouncedSearch, libraryId, page],
    queryFn: () =>
      endpoints.files({
        state,
        sort,
        search: debouncedSearch,
        library_id: libraryId || undefined,
        page,
        page_size: 50,
      }),
    placeholderData: (prev) => prev,
  });

  const enqueue = useMutation({
    mutationFn: (fileIds: number[]) => endpoints.enqueue({ file_ids: fileIds }),
    onSuccess: (result) => {
      push(result.message, result.added ? "success" : "info");
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const ignore = useMutation({
    mutationFn: ({ id, ignored }: { id: number; ignored: boolean }) =>
      endpoints.ignoreFile(id, ignored),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["files"] });
      push("Status geaendert.", "success");
    },
  });

  const items = data?.items ?? [];
  const allSelected = items.length > 0 && items.every((f) => selected.has(f.id));

  const toggleAll = () => {
    setSelected((prev) => {
      if (allSelected) {
        const next = new Set(prev);
        items.forEach((f) => next.delete(f.id));
        return next;
      }
      return new Set([...prev, ...items.map((f) => f.id)]);
    });
  };

  const toggleOne = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const selectedSaving = useMemo(
    () => items.filter((f) => selected.has(f.id)).reduce((sum, f) => sum + f.estimated_saving_bytes, 0),
    [items, selected],
  );

  return (
    <div className="space-y-4">
      {/* ---------------- filters ---------------- */}
      <div className="panel flex flex-wrap items-center gap-3 p-3">
        <div className="relative min-w-[200px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-500" />
          <input
            className="field pl-9"
            placeholder="Nach Dateiname oder Ordner suchen..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="w-44">
          <Select
            value={state}
            onChange={setState}
            options={STATE_FILTERS}
          />
        </div>
        <div className="w-52">
          <Select
            value={sort}
            onChange={setSort}
            options={SORT_OPTIONS}
          />
        </div>
        {(libraries?.length ?? 0) > 1 && (
          <div className="w-44">
            <Select
              value={libraryId}
              onChange={setLibraryId}
              options={[
                { value: "", label: "Alle Ordner" },
                ...(libraries ?? []).map((l) => ({ value: String(l.id), label: l.name })),
              ]}
            />
          </div>
        )}
      </div>

      {/* ---------------- summary / bulk bar ---------------- */}
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="text-ink-400">
          {number(data?.total ?? 0)} Dateien
          {data?.aggregate.potential_saving ? (
            <>
              {" · "}
              <span className="text-save-400">
                {bytes(data.aggregate.potential_saving)} Sparpotenzial
              </span>
            </>
          ) : null}
        </span>
        {isFetching && <Spinner className="size-3.5 text-ink-500" />}

        {selected.size > 0 && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-ink-300">
              {selected.size} ausgewaehlt
              {selectedSaving > 0 && (
                <span className="text-save-400"> · {bytes(selectedSaving)}</span>
              )}
            </span>
            <button className="btn-ghost btn-sm" onClick={() => setSelected(new Set())}>
              Auswahl aufheben
            </button>
            <button
              className="btn-primary btn-sm"
              onClick={() => enqueue.mutate([...selected])}
              disabled={enqueue.isPending}
            >
              {enqueue.isPending ? <Spinner className="size-3.5" /> : <Play className="size-3.5" />}
              Konvertieren
            </button>
          </div>
        )}
      </div>

      {/* ---------------- table ---------------- */}
      <Panel bodyClassName="p-0">
        {isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={<FileVideo className="size-8" />}
            title="Keine Dateien gefunden"
            description={
              state === "candidate"
                ? "Optimizarr hat noch keine lohnenden Kandidaten gefunden. Starte einen Scan oder senke die Mindestersparnis in den Einstellungen."
                : "Passe die Filter an oder starte einen Bibliotheks-Scan."
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-sm">
              <thead>
                <tr className="border-b border-ink-800 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="w-10 px-4 py-3">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      className="size-4 cursor-pointer rounded border-ink-600 bg-ink-800 accent-brand-500"
                      aria-label="Alle auswaehlen"
                    />
                  </th>
                  <th className="w-[38%] px-2 py-3 font-medium">Datei</th>
                  <th className="w-40 px-3 py-3 font-medium">Format</th>
                  <th className="w-24 px-3 py-3 text-right font-medium">Groesse</th>
                  <th className="w-24 px-3 py-3 text-right font-medium">Erwartet</th>
                  <th className="w-28 px-3 py-3 text-right font-medium">Ersparnis</th>
                  <th className="w-36 px-3 py-3 font-medium">Status</th>
                  <th className="w-24 px-3 py-3" />
                </tr>
              </thead>
              <tbody>
                {items.map((file) => (
                  <FileRow
                    key={file.id}
                    file={file}
                    selected={selected.has(file.id)}
                    onToggle={() => toggleOne(file.id)}
                    onOpen={() => setDetailId(file.id)}
                    onQueue={() => enqueue.mutate([file.id])}
                    onIgnore={() => ignore.mutate({ id: file.id, ignored: !file.ignored })}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {(data?.pages ?? 1) > 1 && (
          <div className="flex items-center justify-between border-t border-ink-800 px-4 py-3 text-sm">
            <span className="text-ink-400">
              Seite {data?.page} von {data?.pages}
            </span>
            <div className="flex gap-2">
              <button
                className="btn-ghost btn-sm"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
              >
                <ChevronLeft className="size-3.5" /> Zurueck
              </button>
              <button
                className="btn-ghost btn-sm"
                onClick={() => setPage((p) => Math.min(data?.pages ?? 1, p + 1))}
                disabled={page >= (data?.pages ?? 1)}
              >
                Weiter <ChevronRight className="size-3.5" />
              </button>
            </div>
          </div>
        )}
      </Panel>

      <FileDetail
        fileId={detailId}
        onClose={() => {
          setDetailId(null);
          if (params.get("file")) {
            params.delete("file");
            setParams(params, { replace: true });
          }
        }}
      />
    </div>
  );
}

function FileRow({
  file,
  selected,
  onToggle,
  onOpen,
  onQueue,
  onIgnore,
}: {
  file: MediaFile;
  selected: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onQueue: () => void;
  onIgnore: () => void;
}) {
  const canQueue = file.state === "candidate" && !file.ignored;
  return (
    <tr className={cn("table-row", selected && "bg-brand-600/8")}>
      <td className="px-4 py-2.5">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggle}
          className="size-4 cursor-pointer rounded border-ink-600 bg-ink-800 accent-brand-500"
          aria-label={`${file.name} auswaehlen`}
        />
      </td>
      <td className="max-w-0 px-2 py-2.5">
        <button onClick={onOpen} className="block w-full text-left">
          <span className="block truncate font-medium text-ink-100" title={file.path}>
            {file.name}
          </span>
          <span className="block truncate text-xs text-ink-500" title={file.folder}>
            {file.folder}
          </span>
        </button>
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-xs text-ink-300">
        <span className="uppercase">{file.video_codec || "?"}</span>
        <span className="mx-1 text-ink-600">·</span>
        {resolutionLabel(file.width, file.height)}
        {file.is_hdr && (
          <span className="ml-1.5 rounded bg-warn-500/15 px-1 text-[10px] font-medium text-warn-400">
            HDR
          </span>
        )}
        <span className="mt-0.5 block text-ink-500">
          {duration(file.duration)} · {bitrate(file.video_bitrate)}
        </span>
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right text-ink-200">{bytes(file.size)}</td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right text-ink-400">
        {file.estimated_size ? bytes(file.estimated_size) : "-"}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right">
        {file.estimated_saving_bytes > 0 ? (
          <>
            <span className="font-medium text-save-400">
              -{bytes(file.estimated_saving_bytes)}
            </span>
            <span className="mt-0.5 block text-xs text-ink-500">
              {percent(file.estimated_saving_pct)}
            </span>
          </>
        ) : (
          <span className="text-ink-600">-</span>
        )}
      </td>
      <td className="whitespace-nowrap px-3 py-2.5">
        <StateBadge state={file.state} label={STATE_LABELS[file.state] ?? file.state} />
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-right">
        <div className="flex justify-end gap-1">
          {canQueue && (
            <button
              onClick={onQueue}
              className="rounded-md p-1.5 text-ink-400 transition-colors hover:bg-brand-600/20 hover:text-brand-400"
              title="Zur Warteschlange hinzufuegen"
            >
              <Play className="size-3.5" />
            </button>
          )}
          <button
            onClick={onIgnore}
            className="rounded-md p-1.5 text-ink-400 transition-colors hover:bg-ink-700 hover:text-ink-200"
            title={file.ignored ? "Nicht mehr ignorieren" : "Datei ignorieren"}
          >
            <EyeOff className="size-3.5" />
          </button>
          <button
            onClick={onOpen}
            className="rounded-md p-1.5 text-ink-400 transition-colors hover:bg-ink-700 hover:text-ink-200"
            title="Details"
          >
            <SlidersHorizontal className="size-3.5" />
          </button>
        </div>
      </td>
    </tr>
  );
}

/* -------------------------------------------------------------------------- */
/* Detail modal                                                               */
/* -------------------------------------------------------------------------- */

function FileDetail({ fileId, onClose }: { fileId: number | null; onClose: () => void }) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [analysisDepth, setAnalysisDepth] = useState("sample");

  const { data: file, isLoading } = useQuery({
    queryKey: ["files", "detail", fileId],
    queryFn: () => endpoints.file(fileId!),
    enabled: fileId !== null,
  });

  const analyze = useMutation({
    mutationFn: () => endpoints.analyzeFile(fileId!, analysisDepth),
    onSuccess: (result) => {
      push(
        result.analysis?.decision === "convert"
          ? "Analyse fertig - lohnt sich."
          : "Analyse fertig - lohnt sich nicht.",
        result.analysis?.decision === "convert" ? "success" : "info",
      );
      queryClient.invalidateQueries({ queryKey: ["files"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const enqueue = useMutation({
    mutationFn: () => endpoints.enqueue({ file_ids: [fileId!] }),
    onSuccess: (result) => {
      push(result.message, result.added ? "success" : "info");
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      onClose();
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const plan = file?.plan;
  const conf = file ? confidenceLabel(file.confidence) : null;

  return (
    <Modal
      open={fileId !== null}
      onClose={onClose}
      wide
      title={file?.name ?? "Datei"}
      subtitle={file?.folder}
      footer={
        <>
          <div className="mr-auto w-40">
            <Select
              value={analysisDepth}
              onChange={setAnalysisDepth}
              options={[
                { value: "quick", label: "Schnell (Metadaten)" },
                { value: "sample", label: "Testkodierung" },
                { value: "vmaf", label: "Mit VMAF-Suche" },
              ]}
            />
          </div>
          <button
            className="btn-ghost"
            onClick={() => analyze.mutate()}
            disabled={analyze.isPending}
          >
            {analyze.isPending ? <Spinner className="size-4" /> : <Microscope className="size-4" />}
            Neu analysieren
          </button>
          <button
            className="btn-primary"
            onClick={() => enqueue.mutate()}
            disabled={enqueue.isPending || !plan}
          >
            <Play className="size-4" />
            Konvertieren
          </button>
        </>
      }
    >
      {isLoading || !file ? (
        <div className="space-y-3">
          <Skeleton className="h-20" />
          <Skeleton className="h-32" />
        </div>
      ) : (
        <div className="space-y-5">
          {/* --- verdict --- */}
          <div
            className={cn(
              "rounded-lg border p-4",
              file.estimated_saving_bytes > 0
                ? "border-save-500/30 bg-save-500/8"
                : "border-ink-700 bg-ink-850/60",
            )}
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="text-sm leading-relaxed text-ink-100">{file.decision_reason || "Noch nicht analysiert."}</p>
              {conf && (
                <span className={cn("shrink-0 text-xs font-medium", conf.className)}>
                  Sicherheit: {conf.label}
                </span>
              )}
            </div>
            {file.analyzed_at && (
              <p className="mt-2 text-[11px] text-ink-500">
                Analysiert {relativeTime(file.analyzed_at)}
                {file.analysis_depth && ` · Tiefe: ${DEPTH_LABELS[file.analysis_depth] ?? file.analysis_depth}`}
              </p>
            )}
          </div>

          {file.advisor_note && (
            <Callout tone="info">
              <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-info-400">
                KI-Berater
              </span>
              {file.advisor_note}
            </Callout>
          )}

          {file.error && <Callout tone="danger">{file.error}</Callout>}

          {/* --- source vs target --- */}
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
              <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-400">
                Quelle
              </h4>
              <dl className="space-y-1.5 text-sm">
                <Row label="Codec" value={file.video_codec.toUpperCase()} />
                <Row label="Aufloesung" value={`${file.width}x${file.height}`} />
                <Row label="Bildrate" value={`${file.fps.toFixed(3)} fps`} />
                <Row label="Bitrate" value={bitrate(file.video_bitrate)} />
                <Row label="Farbtiefe" value={`${file.bit_depth} Bit${file.is_hdr ? ` · ${file.hdr_format.toUpperCase()}` : ""}`} />
                <Row label="Laufzeit" value={duration(file.duration)} />
                <Row label="Groesse" value={bytes(file.size)} />
              </dl>
            </div>

            <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
              <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-400">
                Geplante Konvertierung
              </h4>
              {plan ? (
                <dl className="space-y-1.5 text-sm">
                  <Row label="Encoder" value={plan.encoder} />
                  <Row label="Qualitaet" value={`CRF ${plan.crf}${plan.encoder === "libsvtav1" ? ` · Preset ${plan.preset}` : ""}`} />
                  <Row label="Pixelformat" value={plan.pix_fmt} />
                  {plan.film_grain > 0 && <Row label="Filmkorn-Synthese" value={`Stufe ${plan.film_grain}`} />}
                  {plan.target_height > 0 && <Row label="Skalierung" value={`auf ${plan.target_height}p`} />}
                  <Row label="Container" value={plan.container.toUpperCase()} />
                  <Row
                    label="Erwartete Groesse"
                    value={bytes(file.estimated_size)}
                    valueClass="text-save-400"
                  />
                </dl>
              ) : (
                <p className="text-sm text-ink-400">Noch kein Plan - Datei zuerst analysieren.</p>
              )}
            </div>
          </div>

          {/* --- streams --- */}
          {(file.audio_streams?.length || file.subtitle_streams?.length) && (
            <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
              <h4 className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-400">
                Spuren
              </h4>
              <div className="space-y-2">
                {file.audio_streams?.map((stream) => {
                  const action = plan?.audio.find((a) => a.index === stream.index);
                  return (
                    <div
                      key={`a${stream.index}`}
                      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
                    >
                      <span className="w-14 shrink-0 text-ink-500">Audio</span>
                      <span className="text-ink-200">
                        {stream.codec.toUpperCase()} · {stream.channel_layout || `${stream.channels}ch`} ·{" "}
                        {stream.language}
                      </span>
                      {action && (
                        <span
                          className={cn(
                            "chip ml-auto",
                            action.action === "drop"
                              ? "bg-danger-500/12 text-danger-400"
                              : action.action === "opus"
                                ? "bg-brand-500/12 text-brand-400"
                                : "bg-ink-700/60 text-ink-300",
                          )}
                          title={action.reason}
                        >
                          {action.action === "drop"
                            ? "wird entfernt"
                            : action.action === "opus"
                              ? `Opus ${Math.round(action.bitrate / 1000)}k`
                              : "kopieren"}
                        </span>
                      )}
                    </div>
                  );
                })}
                {file.subtitle_streams?.map((stream) => {
                  const action = plan?.subtitles.find((s) => s.index === stream.index);
                  return (
                    <div
                      key={`s${stream.index}`}
                      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
                    >
                      <span className="w-14 shrink-0 text-ink-500">Unterti.</span>
                      <span className="text-ink-200">
                        {stream.codec} · {stream.language}
                        {stream.forced && " · forced"}
                      </span>
                      {action && (
                        <span
                          className={cn(
                            "chip ml-auto",
                            action.action === "drop"
                              ? "bg-danger-500/12 text-danger-400"
                              : "bg-ink-700/60 text-ink-300",
                          )}
                        >
                          {action.action === "drop" ? "wird entfernt" : "kopieren"}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* --- reasoning from a fresh analysis --- */}
          {file.analysis?.reasons?.length ? (
            <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
              <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">
                Wie Optimizarr zu dieser Einschaetzung kommt
              </h4>
              <ul className="space-y-1.5 text-sm text-ink-300">
                {file.analysis.reasons.map((reason, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="mt-1.5 size-1 shrink-0 rounded-full bg-ink-500" />
                    <span className="leading-relaxed">{reason}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : plan?.notes?.length ? (
            <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
              <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">
                Hinweise zum Plan
              </h4>
              <ul className="space-y-1.5 text-sm text-ink-300">
                {plan.notes.map((note, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="mt-1.5 size-1 shrink-0 rounded-full bg-ink-500" />
                    <span className="leading-relaxed">{note}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <p className="break-all font-mono text-[11px] text-ink-600">{file.path}</p>
        </div>
      )}
    </Modal>
  );
}

const DEPTH_LABELS: Record<string, string> = {
  quick: "Schnellschaetzung",
  sample: "Testkodierung",
  vmaf: "Testkodierung + VMAF",
};

function Row({
  label,
  value,
  valueClass,
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-ink-500">{label}</dt>
      <dd className={cn("text-right font-medium text-ink-200", valueClass)}>{value}</dd>
    </div>
  );
}
