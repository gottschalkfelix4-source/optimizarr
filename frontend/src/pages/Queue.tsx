import { qualityLabel } from "../lib/format";
/** The encode queue: what runs now, what is waiting, what happened. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileText,
  HardDrive,
  Layers,
  Pause,
  Play,
  RotateCcw,
  ScanLine,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { endpoints, type Job } from "../lib/api";
import type { JobProgress } from "../lib/live";
import {
  blockedKind,
  bytes,
  dateTime,
  humanDuration,
  JOB_STATE_LABELS,
  joinParts,
  number,
  relativeTime,
  sizeChange,
} from "../lib/format";
import { useLive, useToast } from "../lib/live";
import { useSmoothEta, useSmoothProgress } from "../lib/progress";
import {
  Callout,
  EmptyState,
  ErrorState,
  Modal,
  Panel,
  ProgressBar,
  Skeleton,
  Spinner,
  StateBadge,
  cn,
} from "../components/ui";

/** The server's maximum page - everything that is still to do. */
const ACTIVE_LIMIT = 1000;
const FINISHED_LIMIT = 100;

export default function Queue() {
  const { push } = useToast();
  const { jobProgress } = useLive();
  const queryClient = useQueryClient();
  const [logJobId, setLogJobId] = useState<number | null>(null);

  // Two lists: everything that is still to do (up to the server's maximum),
  // and the most recent finished jobs.  Headings count from ``counts``, which
  // covers all jobs - a single mixed list with a limit hid queued jobs behind
  // hundreds of finished ones.
  const active = useQuery({
    queryKey: ["jobs", "active"],
    queryFn: ({ signal }) => endpoints.jobs({ state: "active", limit: ACTIVE_LIMIT }, { signal }),
    refetchInterval: 8000,
  });
  const finishedQuery = useQuery({
    queryKey: ["jobs", "finished"],
    queryFn: ({ signal }) => endpoints.jobs({ state: "finished", limit: FINISHED_LIMIT }, { signal }),
    refetchInterval: 30000,
  });

  const cancel = useMutation({
    mutationFn: (id: number) => endpoints.cancelJob(id),
    onSuccess: (result) => {
      push(result.message, "info");
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const retry = useMutation({
    mutationFn: (id: number) => endpoints.retryJob(id),
    onSuccess: (result) => {
      push(result.message, "success");
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const clear = useMutation({
    mutationFn: () => endpoints.clearFinished(),
    onSuccess: (result) => {
      push(`${result.removed} Einträge entfernt.`, "success");
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const startNow = useMutation({
    mutationFn: (on: boolean) => endpoints.startNow(on),
    onSuccess: (result) => {
      push(
        result.active
          ? "Konvertierung startet jetzt - bis die Warteschlange leer ist."
          : "Zeitfenster gilt wieder.",
        "info",
      );
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const items = active.data?.items ?? [];
  const running = items.filter((j) => j.state === "running");
  const queued = items.filter((j) => j.state === "queued");
  const finished = finishedQuery.data?.items ?? [];
  const counts = active.data?.counts ?? finishedQuery.data?.counts ?? {};
  const worker = active.data?.worker ?? finishedQuery.data?.worker;
  const kind = blockedKind(worker);
  const runningCount = counts.running ?? running.length;
  const queuedCount = counts.queued ?? queued.length;
  const finishedCount =
    (counts.done ?? 0) + (counts.failed ?? 0) + (counts.rejected ?? 0) + (counts.cancelled ?? 0);

  // Forced jobs for files that were never analysed have no prediction; counting
  // them as "input minus 0" would promise their whole size as a saving.
  const queuedSaving = queued.reduce(
    (sum, j) => sum + (j.predicted_size > 0 ? Math.max(0, j.input_size - j.predicted_size) : 0),
    0,
  );
  // No compute-time total here: eta_seconds only exists for a running encode,
  // so for waiting jobs it is 0 or stale and the sum would be made up.

  if (active.isError && !active.data) {
    return (
      <Panel>
        <ErrorState
          error={active.error}
          onRetry={() => active.refetch()}
          title="Warteschlange konnte nicht geladen werden"
        />
      </Panel>
    );
  }

  // Without the active list, "nothing queued" would be a claim, not a fact.
  if (!active.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {kind === "paused" && (
        <Callout tone="warn" icon={<Pause className="size-4" />}>
          Die Warteschlange ist pausiert. Laufende Jobs werden zu Ende geführt, neue starten nicht.
        </Callout>
      )}
      {kind === "schedule" && (
        <Callout tone="info" icon={<Clock className="size-4" />}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span>
              {worker?.blocked_reason} Jobs starten automatisch, sobald das Zeitfenster erreicht
              ist.
            </span>
            {queuedCount > 0 && (
              <button
                className="btn-primary btn-sm"
                onClick={() => startNow.mutate(true)}
                disabled={startNow.isPending}
                title="Zeitfenster ignorieren, bis die Warteschlange leer ist"
              >
                <Play className="size-3.5" aria-hidden="true" />
                Jetzt starten
              </button>
            )}
          </div>
        </Callout>
      )}
      {worker?.schedule_override && !worker.schedule_ok && kind !== "paused" && (
        <Callout tone="info" icon={<Play className="size-4" />}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Zeitfenster übersprungen: Die Warteschlange läuft jetzt, bis sie leer ist. Danach gilt
              das Zeitfenster wieder.
            </span>
            <button
              className="btn-ghost btn-sm"
              onClick={() => startNow.mutate(false)}
              disabled={startNow.isPending}
            >
              <Clock className="size-3.5" aria-hidden="true" />
              Zeitfenster wieder beachten
            </button>
          </div>
        </Callout>
      )}
      {kind === "disk" && (
        <Callout tone="warn" icon={<HardDrive className="size-4" />}>
          {worker?.blocked_reason}
        </Callout>
      )}
      {kind === "scan" && (
        <Callout tone="info" icon={<ScanLine className="size-4" />}>
          {worker?.blocked_reason}
        </Callout>
      )}
      {kind === "other" && <Callout tone="info">{worker?.blocked_reason}</Callout>}

      {/* ---------------- running ---------------- */}
      <Panel
        title={`Läuft gerade (${number(runningCount)})`}
        subtitle={
          worker ? `${worker.max_concurrent} gleichzeitige Konvertierung(en) erlaubt` : undefined
        }
        bodyClassName={running.length ? "space-y-3" : "p-0"}
      >
        {running.length === 0 ? (
          <EmptyState
            icon={<Layers className="size-8" />}
            title="Keine aktive Konvertierung"
            description={
              queuedCount
                ? kind
                  ? "Neue Jobs starten, sobald der Hinweis oben nicht mehr zutrifft."
                  : "Der nächste Job startet gleich."
                : "Füge in der Bibliothek Dateien zur Warteschlange hinzu."
            }
          />
        ) : (
          running.map((job) => (
            <RunningJob
              key={job.id}
              job={job}
              live={jobProgress[job.id]}
              onCancel={() => cancel.mutate(job.id)}
              onShowLog={() => setLogJobId(job.id)}
            />
          ))
        )}
      </Panel>

      {/* ---------------- waiting ---------------- */}
      <Panel
        title={`Warteschlange (${number(queuedCount)})`}
        subtitle={
          queued.length
            ? `${bytes(queuedSaving)} erwartete Ersparnis` +
              (queuedCount > queued.length ? ` (für die ersten ${number(queued.length)})` : "")
            : undefined
        }
        bodyClassName="p-0"
      >
        {queued.length === 0 ? (
          <EmptyState icon={<Clock className="size-8" />} title="Nichts in der Warteschlange" />
        ) : (
          <ul className="divide-y divide-ink-800/80">
            {queued.map((job, index) => {
              const change = job.predicted_size > 0 ? sizeChange(job.input_size, job.predicted_size) : null;
              return (
              <li key={job.id} className="flex items-center gap-3 px-5 py-3">
                <span className="w-6 shrink-0 text-right font-mono text-xs text-ink-600">
                  {index + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="flex min-w-0 items-center gap-2 text-sm text-ink-100">
                    <span className="truncate">{job.name}</span>
                    {job.forced && <ForcedChip />}
                  </p>
                  <p className="mt-0.5 text-xs text-ink-500">
                    {joinParts(job.resolution, bytes(job.input_size))}
                    {change && (
                      <span className={change.smaller ? "text-save-400" : "text-warn-400"}>
                        {" "}
                        → {bytes(job.predicted_size)} ({change.text})
                      </span>
                    )}
                  </p>
                </div>
                <button
                  onClick={() => cancel.mutate(job.id)}
                  className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-danger-500/15 hover:text-danger-400"
                  title="Aus der Warteschlange nehmen"
                  aria-label={`${job.name ?? "Job"} aus der Warteschlange nehmen`}
                >
                  <X className="size-4" aria-hidden="true" />
                </button>
              </li>
              );
            })}
          </ul>
        )}
        {queuedCount > queued.length && (
          <p className="border-t border-ink-800 px-5 py-3 text-xs text-ink-500">
            … und {number(queuedCount - queued.length)} weitere.
          </p>
        )}
      </Panel>

      {/* ---------------- finished ---------------- */}
      <Panel
        title={`Abgeschlossen (${number(finishedCount)})`}
        subtitle={
          `${number(counts.done ?? 0)} erfolgreich · ${number(
            (counts.rejected ?? 0) + (counts.failed ?? 0),
          )} ohne Ergebnis` +
          (counts.cancelled ? ` · ${number(counts.cancelled)} abgebrochen` : "") +
          (finishedCount > finished.length ? ` · die letzten ${number(finished.length)} angezeigt` : "")
        }
        actions={
          finishedCount > 0 && (
            <button
              className="btn-ghost btn-sm"
              onClick={() => clear.mutate()}
              disabled={clear.isPending}
            >
              <Trash2 className="size-3.5" aria-hidden="true" />
              Liste leeren
            </button>
          )
        }
        bodyClassName="p-0"
      >
        {finishedQuery.isError && !finishedQuery.data ? (
          <ErrorState compact error={finishedQuery.error} onRetry={() => finishedQuery.refetch()} />
        ) : finishedQuery.isLoading ? (
          <div className="space-y-2 p-4">
            <Skeleton className="h-10" />
            <Skeleton className="h-10" />
          </div>
        ) : finished.length === 0 ? (
          <EmptyState icon={<CheckCircle2 className="size-8" />} title="Noch nichts abgeschlossen" />
        ) : (
          <ul className="divide-y divide-ink-800/80">
            {finished.map((job) => (
              <FinishedJob
                key={job.id}
                job={job}
                onRetry={() => retry.mutate(job.id)}
                onShowLog={() => setLogJobId(job.id)}
              />
            ))}
          </ul>
        )}
      </Panel>

      <JobLogModal jobId={logJobId} onClose={() => setLogJobId(null)} />
    </div>
  );
}

function RunningJob({
  job,
  live,
  onCancel,
  onShowLog,
}: {
  job: Job;
  live?: JobProgress;
  onCancel: () => void;
  onShowLog: () => void;
}) {
  // The bar keeps moving between updates instead of stepping on each one.
  const progress = useSmoothProgress(
    live ?? { progress: job.progress, speed: job.speed, duration: job.duration },
  );
  const eta = useSmoothEta(live?.eta_seconds ?? job.eta_seconds);
  const currentSize = live?.current_size ?? job.current_size;
  const projected = progress > 0.02 && currentSize > 0 ? currentSize / progress : job.predicted_size;
  const onTrack = projected > 0 && projected < job.input_size;

  return (
    <div className="rounded-lg border border-brand-600/25 bg-brand-600/5 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="flex min-w-0 items-center gap-2 font-medium text-ink-100">
            <span className="truncate">{job.name}</span>
            {job.forced && <ForcedChip />}
          </p>
          <p className="mt-0.5 truncate text-xs text-ink-500">
            {joinParts(
              job.resolution,
              job.plan?.encoder,
              job.plan ? `CRF ${job.plan.crf}` : null,
              job.plan?.film_grain ? `Filmkorn ${job.plan.film_grain}` : null,
            ) || "Plan entsteht beim Start"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-lg font-semibold text-brand-400">
            {(progress * 100).toFixed(1)}%
          </span>
          <button onClick={onShowLog} className="btn-ghost btn-sm" title="Protokoll ansehen">
            Protokoll
          </button>
          <button
            onClick={onCancel}
            className="btn-danger btn-sm"
            title="Konvertierung abbrechen"
            aria-label={`Konvertierung von ${job.name ?? "Job"} abbrechen`}
          >
            <X className="size-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>

      <ProgressBar value={progress} className="mt-3 h-2.5" smooth />

      <div className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <Metric
          label="Tempo"
          value={`${(live?.speed ?? job.speed).toLocaleString("de-DE", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}x`}
        />
        <Metric label="Bilder/s" value={number(Math.round(live?.fps ?? job.fps))} />
        <Metric label="Restzeit" value={humanDuration(eta)} />
        <Metric
          label="Hochrechnung"
          value={projected > 0 ? bytes(projected) : "-"}
          tone={onTrack ? "save" : projected > 0 ? "warn" : undefined}
        />
      </div>

      {projected > 0 && !onTrack && (
        <p className="mt-2 text-[11px] text-warn-400">
          {job.forced
            ? "Das Ergebnis könnte größer werden als das Original. Bei „Trotzdem konvertieren“ wird es nach bestandenen Prüfungen dennoch übernommen."
            : "Das Ergebnis könnte größer werden als das Original. Ob es übernommen wird, hängt von den eingestellten Ersparnisregeln und dem H.264-Umstellungsmodus ab."}
        </p>
      )}
    </div>
  );
}

function FinishedJob({
  job,
  onRetry,
  onShowLog,
}: {
  job: Job;
  onRetry: () => void;
  onShowLog: () => void;
}) {
  const change = sizeChange(job.input_size, job.output_size);
  const Icon =
    job.state === "done"
      ? CheckCircle2
      : job.state === "rejected"
        ? AlertTriangle
        : job.state === "failed"
          ? XCircle
          : X;
  const iconTone =
    job.state === "done"
      ? "text-save-400"
      : job.state === "rejected"
        ? "text-warn-400"
        : job.state === "failed"
          ? "text-danger-400"
          : "text-ink-500";

  return (
    <li className="flex items-start gap-3 px-5 py-3">
      <Icon className={cn("mt-0.5 size-4 shrink-0", iconTone)} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <p className="min-w-0 flex-1 truncate text-sm text-ink-100">{job.name}</p>
          {job.forced && <ForcedChip />}
          <StateBadge state={job.state} label={JOB_STATE_LABELS[job.state] ?? job.state} />
        </div>
        {job.state === "done" && change ? (
          <p className="mt-0.5 text-xs text-ink-500">
            {bytes(job.input_size)} → {bytes(job.output_size)}{" "}
            <span className={change.smaller ? "text-save-400" : "text-warn-400"}>
              ({change.text})
            </span>
            {job.vmaf != null && job.vmaf > 0 && ` · ${qualityLabel(job)}`}
            {" · "}
            {relativeTime(job.finished_at)}
          </p>
        ) : (
          <p className="mt-0.5 line-clamp-2 text-xs text-ink-500">
            {job.error || relativeTime(job.finished_at)}
          </p>
        )}
      </div>
      <div className="flex shrink-0 gap-1">
        <button
          onClick={onShowLog}
          className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-ink-700 hover:text-ink-200"
          title="Protokoll ansehen"
          aria-label={`Protokoll von ${job.name ?? "Job"} ansehen`}
        >
          <FileText className="size-3.5" aria-hidden="true" />
        </button>
        {(job.state === "failed" || job.state === "cancelled") && (
          <button
            onClick={onRetry}
            className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-brand-600/20 hover:text-brand-400"
            title="Erneut versuchen"
            aria-label={`${job.name ?? "Job"} erneut versuchen`}
          >
            <RotateCcw className="size-3.5" aria-hidden="true" />
          </button>
        )}
      </div>
    </li>
  );
}

function ForcedChip() {
  return (
    <span
      className="chip shrink-0 bg-warn-500/15 text-warn-400"
      title="Trotz Ausschluss oder „lohnt sich nicht“ von Hand eingereiht – die Mindestersparnis gilt hier nicht"
    >
      Erzwungen
    </span>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "save" | "warn";
}) {
  return (
    <div>
      <p className="text-ink-500">{label}</p>
      <p
        className={cn(
          "mt-0.5 font-medium",
          tone === "save" ? "text-save-400" : tone === "warn" ? "text-warn-400" : "text-ink-200",
        )}
      >
        {value}
      </p>
    </div>
  );
}

function JobLogModal({ jobId, onClose }: { jobId: number | null; onClose: () => void }) {
  const { data: job, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["jobs", "detail", jobId],
    queryFn: ({ signal }) => endpoints.job(jobId!, { signal }),
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data?.state === "running" ? 4000 : false),
  });

  return (
    <Modal open={jobId !== null} onClose={onClose} wide title={job?.name ?? "Job"} subtitle={job?.path}>
      {isError && !job ? (
        <ErrorState compact error={error} onRetry={() => refetch()} />
      ) : isLoading || !job ? (
        <Spinner />
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Metric label="Status" value={JOB_STATE_LABELS[job.state] ?? job.state} />
            <Metric label="Eingang" value={bytes(job.input_size)} />
            <Metric
              label="Ergebnis"
              value={job.output_size ? bytes(job.output_size) : "-"}
              tone={job.output_size && job.output_size < job.input_size ? "save" : undefined}
            />
            <Metric label="Gestartet" value={dateTime(job.started_at)} />
          </div>

          {job.plan && (
            <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4 text-sm">
              <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">
                Encoding-Plan
              </h4>
              <p className="text-ink-300">
                {joinParts(
                  job.plan.encoder,
                  `CRF ${job.plan.crf}`,
                  job.plan.encoder === "libsvtav1" ? `Preset ${job.plan.preset}` : null,
                  job.plan.pix_fmt,
                  job.plan.film_grain ? `Filmkorn ${job.plan.film_grain}` : null,
                  job.plan.hw_decode ? "GPU-Decoding" : null,
                )}
              </p>
            </div>
          )}

          {job.error && <Callout tone={job.state === "rejected" ? "warn" : "danger"}>{job.error}</Callout>}

          <div>
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">
              Protokoll
            </h4>
            <pre className="max-h-80 overflow-auto rounded-lg border border-ink-700/70 bg-ink-950/70 p-3 font-mono text-[11px] leading-relaxed text-ink-300">
              {job.log?.trim() || "Kein Protokoll vorhanden."}
            </pre>
          </div>
        </div>
      )}
    </Modal>
  );
}
