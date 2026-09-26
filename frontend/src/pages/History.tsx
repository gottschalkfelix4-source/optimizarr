/** Activity log. */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, History as HistoryIcon, Info, XCircle } from "lucide-react";
import { useState } from "react";
import { endpoints, type HistoryItem } from "../lib/api";
import { bytes, dateTime, relativeTime } from "../lib/format";
import { EmptyState, ErrorState, Panel, Select, Skeleton, cn } from "../components/ui";

const LEVEL_META = {
  success: { icon: CheckCircle2, className: "text-save-400" },
  info: { icon: Info, className: "text-info-400" },
  warning: { icon: AlertTriangle, className: "text-warn-400" },
  error: { icon: XCircle, className: "text-danger-400" },
} as const;

const LEVEL_OPTIONS = [
  { value: "all", label: "Alle Meldungen" },
  { value: "success", label: "Erfolge" },
  { value: "warning", label: "Warnungen" },
  { value: "error", label: "Fehler" },
  { value: "info", label: "Infos" },
];

const CATEGORY_LABELS: Record<string, string> = {
  scan: "Scan",
  encode: "Konvertierung",
  queue: "Warteschlange",
  system: "System",
};

export default function HistoryPage() {
  const [level, setLevel] = useState("all");

  // Filtered by the server: filtering the last 150 entries here left rare
  // levels (errors) empty although older ones existed.
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["history", level],
    queryFn: ({ signal }) => endpoints.history({ limit: 150, level }, { signal }),
    refetchInterval: 20000,
    placeholderData: (prev) => prev,
  });

  const items = data ?? [];
  const levelLabel = LEVEL_OPTIONS.find((o) => o.value === level)?.label ?? "";

  return (
    <Panel
      title="Verlauf"
      subtitle="Alles, was Optimizarr getan hat"
      actions={
        <div className="w-40">
          <Select value={level} onChange={setLevel} options={LEVEL_OPTIONS} ariaLabel="Art der Meldung" />
        </div>
      }
      bodyClassName="p-0"
    >
      {isError && !data ? (
        <ErrorState error={error} onRetry={() => refetch()} title="Verlauf konnte nicht geladen werden" />
      ) : isLoading ? (
        <div className="space-y-2 p-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-14" />
          ))}
        </div>
      ) : items.length === 0 ? (
        level === "all" ? (
          <EmptyState
            icon={<HistoryIcon className="size-8" />}
            title="Noch nichts passiert"
            description="Sobald ein Scan läuft oder eine Datei konvertiert wird, erscheint es hier."
          />
        ) : (
          <EmptyState
            icon={<HistoryIcon className="size-8" />}
            title={`Keine Einträge unter „${levelLabel}“`}
            description="Unter „Alle Meldungen“ steht, was sonst passiert ist."
          />
        )
      ) : (
        <ul className="divide-y divide-ink-800/80">
          {items.map((item) => (
            <HistoryRow key={item.id} item={item} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function HistoryRow({ item }: { item: HistoryItem }) {
  const meta = LEVEL_META[item.level] ?? LEVEL_META.info;
  const Icon = meta.icon;
  const detail = item.detail as
    | { input_size?: number; output_size?: number; vmaf?: number; encoder?: string; crf?: number; seconds?: number }
    | null;

  return (
    <li className="flex items-start gap-3 px-5 py-3">
      <Icon className={cn("mt-0.5 size-4 shrink-0", meta.className)} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-sm leading-relaxed text-ink-100">{item.message}</p>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-ink-500">
          <span>{CATEGORY_LABELS[item.category] ?? item.category}</span>
          <span title={dateTime(item.created_at)}>{relativeTime(item.created_at)}</span>
          {detail?.encoder && (
            <span className="font-mono">
              {detail.encoder}
              {detail.crf !== undefined && ` @ CRF ${detail.crf}`}
            </span>
          )}
          {!!detail?.input_size && !!detail?.output_size && (
            <span className="text-save-400">
              {bytes(detail.input_size)} → {bytes(detail.output_size)}
            </span>
          )}
          {detail?.vmaf != null && detail.vmaf > 0 && <span>VMAF {detail.vmaf.toFixed(1)}</span>}
          {detail?.seconds != null && detail.seconds > 0 && (
            <span>{Math.max(1, Math.round(detail.seconds / 60))} Min Rechenzeit</span>
          )}
        </div>
      </div>
    </li>
  );
}
