/** How well the prediction model is doing, and what the hardware can do. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, Cpu, RefreshCw, Target, TrendingUp } from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { endpoints } from "../lib/api";
import { number, percent, relativeTime } from "../lib/format";
import { useToast } from "../lib/live";
import { Callout, EmptyState, Panel, ProgressBar, Skeleton, cn } from "../components/ui";

const FEATURE_LABELS: Record<string, string> = {
  log_pixels: "Aufloesung x Bildrate",
  crf: "CRF-Wert",
  preset: "Encoder-Preset",
  log_source_bpp: "Bits pro Pixel der Quelle",
  codec_eff: "Effizienz des Quell-Codecs",
  is_hdr: "HDR",
  is_hw_encoder: "Hardware-Encoder",
  grain: "Filmkorn",
  has_sample: "Testkodierung vorhanden",
};

export default function Insights() {
  const { push } = useToast();
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["model"],
    queryFn: endpoints.modelStats,
    refetchInterval: 30000,
  });
  const { data: info } = useQuery({ queryKey: ["system"], queryFn: endpoints.systemInfo });

  const refit = useMutation({
    mutationFn: () => endpoints.refitModel(),
    onSuccess: (stats) => {
      push(
        stats.trained
          ? `Modell neu trainiert auf ${stats.samples} Jobs.`
          : "Noch zu wenige Daten zum Trainieren.",
        "success",
      );
      queryClient.invalidateQueries({ queryKey: ["model"] });
    },
  });

  const detect = useMutation({
    mutationFn: () => endpoints.detectHardware(),
    onSuccess: (report) => {
      push(report.summary, "success");
      queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const stats = data?.stats;
  const samples = data?.samples ?? [];
  const hw = info?.hardware;

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-32" />
        <Skeleton className="h-72" />
      </div>
    );
  }

  const scatterData = samples.map((s) => ({
    predicted: s.predicted_kbps,
    actual: s.actual_kbps,
    codec: s.source_codec,
    error: s.error_pct,
  }));
  const maxKbps = Math.max(1000, ...scatterData.flatMap((d) => [d.predicted, d.actual]));

  return (
    <div className="space-y-5">
      {/* ---------------- model state ---------------- */}
      <Panel
        title="Lernmodell"
        subtitle="Korrigiert die Groessenvorhersage anhand deiner bisherigen Konvertierungen"
        actions={
          <button
            className="btn-ghost btn-sm"
            onClick={() => refit.mutate()}
            disabled={refit.isPending}
          >
            <RefreshCw className={cn("size-3.5", refit.isPending && "animate-spin")} />
            Neu trainieren
          </button>
        }
      >
        <div className="grid gap-5 lg:grid-cols-3">
          <div className="space-y-4 lg:col-span-1">
            <div>
              <div className="flex items-baseline justify-between">
                <span className="text-sm text-ink-400">Reifegrad</span>
                <span className="font-mono text-sm text-ink-100">
                  {stats?.samples ?? 0} / {stats?.trust_threshold ?? 15} Jobs
                </span>
              </div>
              <ProgressBar value={stats?.maturity ?? 0} tone="warn" className="mt-2" />
              <p className="hint">
                {stats?.trained && (stats?.maturity ?? 0) >= 1
                  ? "Das Modell greift voll - Schaetzungen werden aktiv korrigiert."
                  : "Bis zur vollen Reife mischt Optimizarr die gelernte Korrektur nur anteilig bei. So kann ein halb trainiertes Modell keinen Unsinn produzieren."}
              </p>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <StatBox
                icon={<Target className="size-4" />}
                label="Mittlerer Fehler"
                value={stats?.trained ? `±${stats.mean_abs_error_pct.toFixed(1)} %` : "-"}
              />
              <StatBox
                icon={<Brain className="size-4" />}
                label="Trainingsdaten"
                value={number(stats?.samples ?? 0)}
              />
            </div>

            {stats?.top_signals?.length ? (
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">
                  Wichtigste Einflussgroessen
                </p>
                <ul className="space-y-1.5">
                  {stats.top_signals.map((signal) => (
                    <li key={signal.feature} className="flex items-center gap-2 text-xs">
                      <span className="min-w-0 flex-1 truncate text-ink-300">
                        {FEATURE_LABELS[signal.feature] ?? signal.feature}
                      </span>
                      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-ink-800">
                        <div
                          className={cn(
                            "h-full rounded-full",
                            signal.weight >= 0 ? "bg-save-500" : "bg-danger-500",
                          )}
                          style={{
                            width: `${Math.min(100, Math.abs(signal.weight) * 400)}%`,
                          }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>

          <div className="lg:col-span-2">
            {scatterData.length >= 3 ? (
              <>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">
                  Vorhersage gegen Wirklichkeit
                </p>
                <ResponsiveContainer width="100%" height={280}>
                  <ScatterChart margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                    <CartesianGrid stroke="#212a45" />
                    <XAxis
                      type="number"
                      dataKey="predicted"
                      name="vorhergesagt"
                      unit=" kbit/s"
                      domain={[0, maxKbps]}
                      tick={{ fill: "#6b7ba0", fontSize: 11 }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      type="number"
                      dataKey="actual"
                      name="tatsaechlich"
                      unit=" kbit/s"
                      domain={[0, maxKbps]}
                      tick={{ fill: "#6b7ba0", fontSize: 11 }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <ZAxis range={[45, 45]} />
                    <ReferenceLine
                      segment={[
                        { x: 0, y: 0 },
                        { x: maxKbps, y: maxKbps },
                      ]}
                      stroke="#4a5779"
                      strokeDasharray="4 4"
                    />
                    <Tooltip
                      cursor={{ strokeDasharray: "3 3" }}
                      contentStyle={tooltipStyle}
                      formatter={(value, name) => [
                        `${number(Number(value ?? 0))} kbit/s`,
                        String(name ?? ""),
                      ]}
                    />
                    <Scatter data={scatterData} fill="#8b6df0" fillOpacity={0.75} />
                  </ScatterChart>
                </ResponsiveContainer>
                <p className="hint text-center">
                  Punkte auf der gestrichelten Linie bedeuten: die Schaetzung lag genau richtig.
                  Oberhalb wurde die Datei groesser als erwartet, unterhalb kleiner.
                </p>
              </>
            ) : (
              <EmptyState
                icon={<TrendingUp className="size-8" />}
                title="Noch zu wenig Daten"
                description="Nach den ersten Konvertierungen zeigt dieser Bereich, wie genau die Groessenvorhersage trifft - und das Modell korrigiert sich selbst."
              />
            )}
          </div>
        </div>
      </Panel>

      {/* ---------------- error over time ---------------- */}
      {samples.length >= 5 && (
        <Panel title="Schaetzfehler im Zeitverlauf" subtitle="je naeher an 0 %, desto besser">
          <ResponsiveContainer width="100%" height={220}>
            <LineChart
              data={samples.map((s, i) => ({ ...s, index: i + 1 }))}
              margin={{ left: -16, right: 12, top: 8 }}
            >
              <CartesianGrid stroke="#212a45" vertical={false} />
              <XAxis
                dataKey="index"
                tick={{ fill: "#6b7ba0", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: "#6b7ba0", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v) => `${v} %`}
              />
              <ReferenceLine y={0} stroke="#4a5779" />
              <Tooltip
                contentStyle={tooltipStyle}
                formatter={(value) => [`${Number(value ?? 0).toFixed(1)} %`, "Abweichung"]}
                labelFormatter={(v) => `Job ${v}`}
              />
              <Legend wrapperStyle={{ fontSize: 11, color: "#94a1c2" }} />
              <Line
                type="monotone"
                dataKey="error_pct"
                name="Abweichung Vorhersage"
                stroke="#8b6df0"
                strokeWidth={2}
                dot={{ r: 2 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </Panel>
      )}

      {/* ---------------- hardware ---------------- */}
      <Panel
        title="Hardware"
        subtitle="Was diese Maschine wirklich kann - per Testkodierung geprueft"
        actions={
          <button
            className="btn-ghost btn-sm"
            onClick={() => detect.mutate()}
            disabled={detect.isPending}
          >
            <RefreshCw className={cn("size-3.5", detect.isPending && "animate-spin")} />
            Neu erkennen
          </button>
        }
      >
        {hw ? (
          <div className="space-y-4">
            <div className="flex items-start gap-3">
              <Cpu className="mt-0.5 size-5 shrink-0 text-brand-400" />
              <div>
                <p className="font-medium text-ink-100">{hw.gpu_name}</p>
                <p className="mt-0.5 text-sm text-ink-400">{hw.summary}</p>
                {hw.driver && <p className="mt-1 font-mono text-[11px] text-ink-600">{hw.driver}</p>}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
                <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-400">
                  Encoder
                </p>
                <ul className="space-y-2">
                  {Object.values(hw.encoders ?? {}).map((enc) => (
                    <li key={enc.name} className="flex items-start gap-2 text-sm">
                      <span
                        className={cn(
                          "mt-1.5 size-2 shrink-0 rounded-full",
                          enc.verified
                            ? "bg-save-500"
                            : enc.available
                              ? "bg-warn-500"
                              : "bg-ink-600",
                        )}
                      />
                      <span className="min-w-0">
                        <span className="font-mono text-ink-200">{enc.name}</span>
                        <span className="ml-2 text-xs text-ink-500">
                          {enc.verified
                            ? "getestet und einsatzbereit"
                            : enc.reason || "nicht verfuegbar"}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
                <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-ink-400">
                  GPU-Decoding
                </p>
                <ul className="space-y-2 text-sm">
                  {[
                    ["H.264", hw.decode_h264],
                    ["HEVC / H.265", hw.decode_hevc],
                    ["VP9", hw.decode_vp9],
                    ["AV1", hw.decode_av1],
                  ].map(([label, ok]) => (
                    <li key={String(label)} className="flex items-center gap-2">
                      <span
                        className={cn(
                          "size-2 shrink-0 rounded-full",
                          ok ? "bg-save-500" : "bg-ink-600",
                        )}
                      />
                      <span className="text-ink-200">{label}</span>
                      <span className="ml-auto text-xs text-ink-500">
                        {ok ? "beschleunigt" : "auf der CPU"}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="mt-3 border-t border-ink-800 pt-3 text-xs text-ink-500">
                  Qualitaetsmessung:{" "}
                  {hw.quality_metric === "vmaf"
                    ? "VMAF (libvmaf vorhanden)"
                    : hw.quality_metric === "ssim"
                      ? "SSIM, auf die VMAF-Skala umgerechnet"
                      : "nicht verfuegbar"}
                </p>
              </div>
            </div>

            {hw.notes.map((note, i) => (
              <Callout key={i} tone="info">
                {note}
              </Callout>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={<Cpu className="size-8" />}
            title="Hardware noch nicht erkannt"
            action={
              <button className="btn-primary btn-sm" onClick={() => detect.mutate()}>
                Jetzt erkennen
              </button>
            }
          />
        )}
      </Panel>

      {/* ---------------- recent samples table ---------------- */}
      {samples.length > 0 && (
        <Panel title="Letzte Konvertierungen im Detail" bodyClassName="p-0">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-ink-800 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-3 font-medium">Zeitpunkt</th>
                  <th className="px-3 py-3 font-medium">Quelle</th>
                  <th className="px-3 py-3 font-medium">Encoder</th>
                  <th className="px-3 py-3 text-right font-medium">Vorhergesagt</th>
                  <th className="px-3 py-3 text-right font-medium">Tatsaechlich</th>
                  <th className="px-5 py-3 text-right font-medium">Abweichung</th>
                </tr>
              </thead>
              <tbody>
                {[...samples].reverse().slice(0, 25).map((sample, i) => (
                  <tr key={i} className="table-row">
                    <td className="px-5 py-2.5 text-ink-400">{relativeTime(sample.created_at)}</td>
                    <td className="px-3 py-2.5 uppercase text-ink-300">{sample.source_codec}</td>
                    <td className="px-3 py-2.5 font-mono text-xs text-ink-400">
                      {sample.encoder} @ {sample.crf}
                    </td>
                    <td className="px-3 py-2.5 text-right text-ink-400">
                      {number(sample.predicted_kbps)} kbit/s
                    </td>
                    <td className="px-3 py-2.5 text-right text-ink-200">
                      {number(sample.actual_kbps)} kbit/s
                    </td>
                    <td
                      className={cn(
                        "px-5 py-2.5 text-right font-medium",
                        Math.abs(sample.error_pct) < 10
                          ? "text-save-400"
                          : Math.abs(sample.error_pct) < 25
                            ? "text-warn-400"
                            : "text-danger-400",
                      )}
                    >
                      {sample.error_pct > 0 ? "+" : ""}
                      {percent(sample.error_pct, 1)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}

function StatBox({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-3">
      <div className="flex items-center gap-2 text-ink-500">
        {icon}
        <span className="text-xs">{label}</span>
      </div>
      <p className="mt-1 text-lg font-semibold text-ink-100">{value}</p>
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
