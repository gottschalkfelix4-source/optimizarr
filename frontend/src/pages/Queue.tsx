/** The encode queue: what runs now, what is waiting, what happened. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Layers,
  Pause,
  RotateCcw,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { endpoints, type Job } from "../lib/api";
import {
  bytes,
  dateTime,
  humanDuration,
  JOB_STATE_LABELS,
  number,
  percent,
  relativeTime,
} from "../lib/format";
import { useLive, useToast } from "../lib/live";
import {
  Callout,
  EmptyState,
  Modal,
  Panel,
  ProgressBar,
  Skeleton,
  Spinner,
  StateBadge,
  cn,
} from "../components/ui";

export default function Queue() {
  const { push } = useToast();
  const { jobProgress } = useLive();
  const queryClient = useQueryClient();
  const [logJobId, setLogJobId] = useState<number | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["jobs"],
    queryFn: () => endpoints.jobs(),
    refetchInterval: 8000,
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
      push(`${result.removed} Eintraege entfernt.`, "success");
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const items = data?.items ?? [];
  const running = items.filter((j) => j.state === "running");
  const queued = items.filter((j) => j.state === "queued");
  const finished = items.filter(
    (j) => !["running", "queued"].includes(j.state),
  );
  const worker = data?.worker;

  const queuedSaving = queued.reduce(
    (sum, j) => sum + Math.max(0, j.input_size - j.predicted_size),
    0,
  );
  const queuedEta = queued.reduce((sum, j) => sum + (j.eta_seconds || 0), 0);

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {worker?.paused && (
        <Callout tone="warn" icon={<Pause className="size-4" />}>
          Die Warteschlange ist pausiert. Laufende Jobs werden zu Ende gefuehrt, neue starten nicht.
        </Callout>
      )}
      {worker && !worker.paused && !worker.schedule_ok && (
        <Callout tone="info" icon={<Clock className="size-4" />}>
          {worker.blocked_reason} Jobs starten automatisch, sobald das Zeitfenster erreicht ist.
        </Callout>
      )}
      {worker?.blocked_reason && worker.schedule_ok && !worker.paused && (
        <Callout tone="info">{worker.blocked_reason}</Callout>
      )}

      {/* ---------------- running ---------------- */}
      <Panel
        title={`Laeuft gerade (${running.length})`}
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
              queued.length
                ? "Der naechste Job startet gleich."
                : "Fuege in der Bibliothek Dateien zur Warteschlange hinzu."
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
        title={`Warteschlange (${queued.length})`}
        subtitle={
          queued.length
            ? `${bytes(queuedSaving)} erwartete Ersparnis · geschaetzt ${humanDuration(queuedEta)} Rechenzeit`
            : undefined
        }
        bodyClassName="p-0"
      >
        {queued.length === 0 ? (
          <EmptyState icon={<Clock className="size-8" />} title="Nichts in der Warteschlange" />
        ) : (
          <ul className="divide-y divide-ink-800/80">
            {queued.map((job, index) => (
              <li key={job.id} className="flex items-center gap-3 px-5 py-3">
                <span className="w-6 shrink-0 text-right font-mono text-xs text-ink-600">
                  {index + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-ink-100">{job.name}</p>
                  <p className="mt-0.5 text-xs text-ink-500">
                    {job.resolution} · {bytes(job.input_size)}
                    {job.predicted_size > 0 && (
                      <span className="text-save-400">
                        {" "}
                        → {bytes(job.predicted_size)} (-
                        {percent(((job.input_size - job.predicted_size) / job.input_size) * 100)})
                      </span>
                    )}
                  </p>
                </div>
                <button
                  onClick={() => cancel.mutate(job.id)}
                  className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-danger-500/15 hover:text-danger-400"
                  title="Aus der Warteschlange nehmen"
                >
                  <X className="size-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {/* ---------------- finished ---------------- */}
      <Panel
        title="Abgeschlossen"
        subtitle={`${number(data?.counts.done ?? 0)} erfolgreich · ${number(
          (data?.counts.rejected ?? 0) + (data?.counts.failed ?? 0),
        )} ohne Ergebnis`}
        actions={
          finished.length > 0 && (
            <button className="btn-ghost btn-sm" onClick={() => clear.mutate()}>
              <Trash2 className="size-3.5" />
              Liste leeren
            </button>
          )
        }
        bodyClassName="p-0"
      >
        {finished.length === 0 ? (
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
  live?: { progress: number; fps: number; speed: number; eta_seconds: number; current_size: number };
  onCancel: () => void;
  onShowLog: () => void;
}) {
  const progress = live?.progress ?? job.progress;
  const currentSize = live?.current_size ?? job.current_size;
  const projected = progress > 0.02 && currentSize > 0 ? currentSize / progress : job.predicted_size;
  const onTrack = projected > 0 && projected < job.input_size;

  return (
    <div className="rounded-lg border border-brand-600/25 bg-brand-600/5 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium text-ink-100">{job.name}</p>
          <p className="mt-0.5 truncate text-xs text-ink-500">
            {job.resolution} · {job.plan?.encoder} · CRF {job.plan?.crf}
            {job.plan?.film_grain ? ` · Filmkorn ${job.plan.film_grain}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-lg font-semibold text-brand-400">
            {(progress * 100).toFixed(1)}%
          </span>
          <button
            onClick={onShowLog}
            className="btn-ghost btn-sm"
            title="Log ansehen"
          >
            Log
          </button>
          <button onClick={onCancel} className="btn-danger btn-sm" title="Abbrechen">
            <X className="size-3.5" />
          </button>
        </div>
      </div>

      <ProgressBar value={progress} className="mt-3 h-2.5" />

      <div className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <Metric label="Tempo" value={`${(live?.speed ?? job.speed).toFixed(2)}x`} />
        <Metric label="Bilder/s" value={number(Math.round(live?.fps ?? job.fps))} />
        <Metric label="Restzeit" value={humanDuration(live?.eta_seconds ?? job.eta_seconds)} />
        <Metric
          label="Hochrechnung"
          value={projected > 0 ? bytes(projected) : "-"}
          tone={onTrack ? "save" : projected > 0 ? "warn" : undefined}
        />
      </div>

      {projected > 0 && !onTrack && (
        <p className="mt-2 text-[11px] text-warn-400">
          Das Ergebnis koennte groesser werden als das Original - Optimizarr verwirft es dann
          automatisch und laesst die Datei unveraendert.
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
  const saved = job.input_size - job.output_size;
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
          <StateBadge state={job.state} label={JOB_STATE_LABELS[job.state] ?? job.state} />
        </div>
        {job.state === "done" && saved > 0 ? (
          <p className="mt-0.5 text-xs text-ink-500">
            {bytes(job.input_size)} → {bytes(job.output_size)}{" "}
            <span className="text-save-400">
              (-{bytes(saved)}, {percent((saved / job.input_size) * 100)})
            </span>
            {job.vmaf && ` · VMAF ${job.vmaf.toFixed(1)}`}
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
          title="Log ansehen"
        >
          <Layers className="size-3.5" />
        </button>
        {(job.state === "failed" || job.state === "cancelled") && (
          <button
            onClick={onRetry}
            className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-brand-600/20 hover:text-brand-400"
            title="Erneut versuchen"
          >
            <RotateCcw className="size-3.5" />
          </button>
        )}
      </div>
    </li>
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
  const { data: job, isLoading } = useQuery({
    queryKey: ["jobs", "detail", jobId],
    queryFn: () => endpoints.job(jobId!),
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data?.state === "running" ? 4000 : false),
  });

  return (
    <Modal open={jobId !== null} onClose={onClose} wide title={job?.name ?? "Job"} subtitle={job?.path}>
      {isLoading || !job ? (
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
                {job.plan.encoder} · CRF {job.plan.crf} · Preset {job.plan.preset} ·{" "}
                {job.plan.pix_fmt}
                {job.plan.film_grain ? ` · Filmkorn ${job.plan.film_grain}` : ""}
                {job.plan.hw_decode ? " · GPU-Decoding" : ""}
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
