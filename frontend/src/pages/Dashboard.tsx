/** Overview: what is there, what could be saved, what is happening right now. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Brain,
  CheckCircle2,
  Cpu,
  Database,
  FileVideo,
  HardDriveDownload,
  Sparkles,
  TrendingDown,
  Zap,
} from "lucide-react";
import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { endpoints, type Job, type MediaFile } from "../lib/api";
import { bytes, humanDuration, number, percent, resolutionLabel } from "../lib/format";
import { useLive, useToast, type JobProgress } from "../lib/live";
import { useSmoothEta, useSmoothProgress } from "../lib/progress";
import { Callout, EmptyState, Panel, ProgressBar, Skeleton, cn } from "../components/ui";

const CODEC_COLORS: Record<string, string> = {
  h264: "#8b6df0",
  hevc: "#38bdf8",
  av1: "#10b981",
  vp9: "#fbbf24",
  mpeg2video: "#fb7185",
  vc1: "#f472b6",
};

export default function Dashboard() {
  const { push } = useToast();
  const { jobProgress } = useLive();
  const queryClient = useQueryClient();

  const { data: stats, isLoading } = useQuery({
    queryKey: ["stats"],
    queryFn: endpoints.stats,
    refetchInterval: 20000,
  });
  const { data: info } = useQuery({ queryKey: ["system"], queryFn: endpoints.systemInfo });
  const { data: jobs } = useQuery({
    queryKey: ["jobs", "active"],
    queryFn: () => endpoints.jobs("active"),
    refetchInterval: 10000,
  });

  const enqueueAll = useMutation({
    mutationFn: () => endpoints.enqueue({ all_candidates: true, limit: 250 }),
    onSuccess: (data) => {
      push(data.message, data.added ? "success" : "info");
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const running = jobs?.items.filter((j) => j.state === "running") ?? [];
  const queued = jobs?.items.filter((j) => j.state === "queued") ?? [];
  const hw = info?.hardware;
  const hwAv1 = hw && Object.values(hw.encoders ?? {}).some((e) => e.verified && e.name.startsWith("av1_"));

  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-28" />
        ))}
      </div>
    );
  }

  const potential = stats?.potential.saving_bytes ?? 0;
  const realised = stats?.realised.saved_bytes ?? 0;
  const totalSize = stats?.files.total_size ?? 0;

  return (
    <div className="space-y-5">
      {/* ---------------- headline numbers ---------------- */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={<TrendingDown className="size-5" />}
          tone="save"
          label="Einsparpotenzial"
          value={bytes(potential)}
          detail={
            stats?.potential.candidate_count
              ? `${number(stats.potential.candidate_count)} Kandidaten gefunden`
              : "Noch keine Kandidaten"
          }
          footer={
            totalSize > 0 ? (
              <div className="mt-3">
                <ProgressBar value={potential / totalSize} tone="save" className="h-1.5" />
                <p className="mt-1.5 text-[11px] text-ink-500">
                  {percent((potential / totalSize) * 100, 1)} der gesamten Bibliothek
                </p>
              </div>
            ) : null
          }
        />
        <StatCard
          icon={<HardDriveDownload className="size-5" />}
          tone="brand"
          label="Bereits gespart"
          value={bytes(realised)}
          detail={`${number(stats?.realised.converted_count ?? 0)} Dateien konvertiert`}
          footer={
            stats?.realised.average_vmaf ? (
              <p className="mt-3 text-[11px] text-ink-500">
                Durchschnittliche Qualitaet: VMAF {stats.realised.average_vmaf.toFixed(1)}
              </p>
            ) : null
          }
        />
        <StatCard
          icon={<Database className="size-5" />}
          tone="info"
          label="Bibliothek"
          value={bytes(totalSize)}
          detail={`${number(stats?.files.total ?? 0)} Videodateien`}
          footer={
            stats?.files.total_duration ? (
              <p className="mt-3 text-[11px] text-ink-500">
                {humanDuration(stats.files.total_duration)} Laufzeit insgesamt
              </p>
            ) : null
          }
        />
        <StatCard
          icon={<Brain className="size-5" />}
          tone="warn"
          label="Lernmodell"
          value={
            stats?.model.trained
              ? `±${stats.model.mean_abs_error_pct.toFixed(0)} %`
              : "lernt noch"
          }
          detail={
            stats?.model.samples
              ? `${stats.model.samples} abgeschlossene Jobs ausgewertet`
              : "Noch keine Trainingsdaten"
          }
          footer={
            <div className="mt-3">
              <ProgressBar value={stats?.model.maturity ?? 0} tone="warn" className="h-1.5" />
              <p className="mt-1.5 text-[11px] text-ink-500">
                {stats?.model.trained
                  ? "Schaetzgenauigkeit der Groessenvorhersage"
                  : `Ab ${stats?.model.trust_threshold ?? 15} Jobs greift die Korrektur voll`}
              </p>
            </div>
          }
        />
      </div>

      {/* ---------------- hardware note (one message, not two) ---------------- */}
      {hw && !hw.device_present ? (
        <Callout tone="warn">
          Es ist keine Intel-GPU sichtbar ({hw.device}). In Unraid muss <code>/dev/dri</code> als
          Device durchgereicht werden - sonst laeuft das Encoding komplett auf der CPU.
        </Callout>
      ) : hw && !hwAv1 ? (
        <Callout tone="info" icon={<Cpu className="size-4" />}>
          <strong className="text-ink-100">{hw.gpu_name}</strong> kann AV1 nicht in Hardware
          kodieren - das uebernimmt SVT-AV1 auf der CPU. Das ist langsamer, liefert aber die
          besseren Ergebnisse pro Megabyte.{" "}
          <Link to="/settings" className="text-brand-400 underline-offset-2 hover:underline">
            Encoder-Einstellungen
          </Link>
        </Callout>
      ) : null}

      <div className="grid gap-5 xl:grid-cols-3">
        {/* ---------------- active jobs ---------------- */}
        <Panel
          className="xl:col-span-2"
          title="Aktive Konvertierungen"
          subtitle={
            queued.length ? `${queued.length} weitere in der Warteschlange` : "Live-Fortschritt"
          }
          actions={
            <Link to="/queue" className="btn-ghost btn-sm">
              Warteschlange
            </Link>
          }
          bodyClassName={running.length ? "space-y-4" : ""}
        >
          {running.length === 0 ? (
            <EmptyState
              icon={<Zap className="size-8" />}
              title="Gerade laeuft nichts"
              description={
                stats?.potential.candidate_count
                  ? `${number(stats.potential.candidate_count)} Dateien warten darauf, konvertiert zu werden.`
                  : "Starte einen Scan, damit Optimizarr deine Bibliothek durchsieht."
              }
              action={
                stats?.potential.candidate_count ? (
                  <button
                    className="btn-primary btn-sm"
                    onClick={() => enqueueAll.mutate()}
                    disabled={enqueueAll.isPending}
                  >
                    Alle Kandidaten einreihen
                  </button>
                ) : undefined
              }
            />
          ) : (
            running.map((job) => (
              <ActiveJob key={job.id} job={job} live={jobProgress[job.id]} />
            ))
          )}
        </Panel>

        {/* ---------------- codec distribution ---------------- */}
        <Panel title="Codecs in der Bibliothek" subtitle="nach belegtem Speicher">
          {stats?.codecs.length ? (
            <div className="space-y-3">
              <ResponsiveContainer width="100%" height={170}>
                <BarChart
                  data={stats.codecs.slice(0, 6)}
                  layout="vertical"
                  margin={{ left: 0, right: 8, top: 4, bottom: 0 }}
                >
                  <XAxis type="number" hide />
                  <YAxis
                    type="category"
                    dataKey="codec"
                    width={68}
                    tick={{ fill: "#94a1c2", fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <Tooltip
                    cursor={{ fill: "rgba(255,255,255,0.03)" }}
                    contentStyle={tooltipStyle}
                    formatter={(value, _name, item) => [
                      `${bytes(Number(value ?? 0))} · ${number(
                        (item?.payload as { count?: number } | undefined)?.count ?? 0,
                      )} Dateien`,
                      "Speicher",
                    ]}
                  />
                  <Bar dataKey="size" radius={[0, 4, 4, 0]} barSize={16}>
                    {stats.codecs.slice(0, 6).map((entry) => (
                      <Cell key={entry.codec} fill={CODEC_COLORS[entry.codec] ?? "#4a5779"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
              <div className="grid grid-cols-2 gap-2 border-t border-ink-800 pt-3">
                {stats.resolutions.map((r) => (
                  <div key={r.label} className="flex items-baseline justify-between text-xs">
                    <span className="text-ink-400">{r.label}</span>
                    <span className="font-medium text-ink-200">{number(r.count)}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <EmptyState
              icon={<FileVideo className="size-8" />}
              title="Noch keine Daten"
              description="Nach dem ersten Scan siehst du hier, wie sich deine Bibliothek zusammensetzt."
            />
          )}
        </Panel>
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        {/* ---------------- savings over time ---------------- */}
        <Panel
          className="xl:col-span-2"
          title="Gesparter Speicher"
          subtitle="kumuliert ueber die letzten Konvertierungen"
        >
          {stats?.daily.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={cumulative(stats.daily)} margin={{ left: -18, right: 8, top: 8 }}>
                <defs>
                  <linearGradient id="savedGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#10b981" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#212a45" vertical={false} />
                <XAxis
                  dataKey="date"
                  tick={{ fill: "#6b7ba0", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v: string) =>
                    new Date(v).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })
                  }
                />
                <YAxis
                  tick={{ fill: "#6b7ba0", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => bytes(Number(v), 0)}
                />
                <Tooltip
                  contentStyle={tooltipStyle}
                  labelFormatter={(v) => new Date(String(v)).toLocaleDateString("de-DE")}
                  formatter={(value) => [bytes(Number(value ?? 0)), "gespart"]}
                />
                <Area
                  type="monotone"
                  dataKey="total"
                  stroke="#10b981"
                  strokeWidth={2}
                  fill="url(#savedGradient)"
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState
              icon={<CheckCircle2 className="size-8" />}
              title="Noch nichts konvertiert"
              description="Sobald die erste Datei fertig ist, waechst hier die Kurve."
            />
          )}
        </Panel>

        {/* ---------------- top candidates ---------------- */}
        <Panel
          title="Groesste Chancen"
          subtitle="Dateien mit dem meisten Sparpotenzial"
          actions={
            <Link to="/library" className="btn-ghost btn-sm">
              Alle
            </Link>
          }
          bodyClassName="p-0"
        >
          {stats?.top_candidates.length ? (
            <ul className="divide-y divide-ink-800/80">
              {stats.top_candidates.map((file) => (
                <TopCandidateRow key={file.id} file={file} />
              ))}
            </ul>
          ) : (
            <EmptyState
              icon={<Sparkles className="size-8" />}
              title="Keine Kandidaten"
              description="Entweder ist alles schon optimal, oder es fehlt noch ein Scan."
            />
          )}
        </Panel>
      </div>
    </div>
  );
}

function ActiveJob({ job, live }: { job: Job; live?: JobProgress }) {
  // Own component rather than inline in the map: the smoothing hooks cannot
  // run inside a loop.
  const progress = useSmoothProgress(
    live ?? { progress: job.progress, speed: job.speed, duration: job.duration },
  );
  const eta = useSmoothEta(live?.eta_seconds ?? job.eta_seconds);
  const currentSize = live?.current_size ?? job.current_size;
  const projected =
    progress > 0.02 && currentSize > 0 ? currentSize / progress : job.predicted_size;

  return (
    <div className="rounded-lg border border-ink-700/60 bg-ink-850/50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-ink-100">{job.name}</p>
          <p className="mt-0.5 truncate text-xs text-ink-500">
            {job.resolution} · {job.plan?.encoder} · CRF {job.plan?.crf}
          </p>
        </div>
        <span className="font-mono text-sm text-brand-400">{(progress * 100).toFixed(1)} %</span>
      </div>
      <ProgressBar value={progress} className="mt-3" smooth />
      <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-400">
        {(live?.speed ?? job.speed) > 0 && (
          <span>{(live?.speed ?? job.speed).toFixed(2)}x Echtzeit</span>
        )}
        {(live?.fps ?? job.fps) > 0 && <span>{Math.round(live?.fps ?? job.fps)} fps</span>}
        {eta > 0 && <span>noch {humanDuration(eta)}</span>}
        {projected > 0 && job.input_size > 0 && (
          <span className="text-save-400">
            voraussichtlich {bytes(projected)} statt {bytes(job.input_size)}
          </span>
        )}
      </div>
    </div>
  );
}

function TopCandidateRow({ file }: { file: MediaFile }) {
  return (
    <li className="px-5 py-3 transition-colors hover:bg-ink-800/40">
      <Link to={`/library?file=${file.id}`} className="block">
        <p className="truncate text-sm text-ink-100" title={file.path}>
          {file.name}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-ink-500">
          <span className="uppercase">{file.video_codec}</span>
          <span>{resolutionLabel(file.width, file.height)}</span>
          <span>{bytes(file.size)}</span>
          <span className="ml-auto font-medium text-save-400">
            -{bytes(file.estimated_saving_bytes)} ({percent(file.estimated_saving_pct)})
          </span>
        </div>
      </Link>
    </li>
  );
}

function StatCard({
  icon,
  label,
  value,
  detail,
  footer,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  detail?: string;
  footer?: React.ReactNode;
  tone: "save" | "brand" | "info" | "warn";
}) {
  const toneClass = {
    save: "bg-save-500/12 text-save-400",
    brand: "bg-brand-500/12 text-brand-400",
    info: "bg-info-500/12 text-info-400",
    warn: "bg-warn-500/12 text-warn-400",
  }[tone];

  return (
    <div className="panel panel-hover p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-400">{label}</p>
          <p className="mt-1.5 text-2xl font-semibold tracking-tight text-ink-100">{value}</p>
          {detail && <p className="mt-0.5 truncate text-xs text-ink-400">{detail}</p>}
        </div>
        <span className={cn("grid size-10 shrink-0 place-items-center rounded-xl", toneClass)}>
          {icon}
        </span>
      </div>
      {footer}
    </div>
  );
}

const tooltipStyle = {
  background: "#121829",
  border: "1px solid #2e3a5c",
  borderRadius: "0.5rem",
  fontSize: "12px",
  color: "#e6ecf8",
};

function cumulative(daily: { date: string; saved: number }[]) {
  let total = 0;
  return daily.map((d) => {
    total += d.saved;
    return { date: d.date, total };
  });
}
