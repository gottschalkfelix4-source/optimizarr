/** Series overview: the library grouped by series and season, Sonarr style. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Play, Search, Tv, Zap } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  endpoints,
  type EnqueueResult,
  type SeriesBucket,
  type SeriesEpisode,
  type SeriesSeason,
  type SeriesSummary,
  type SeriesTally,
} from "../lib/api";
import { bytes, number, relativeTime, resolutionLabel, STATE_LABELS } from "../lib/format";
import { useToast } from "../lib/live";
import { EmptyState, Modal, Panel, Select, Skeleton, Spinner, StateBadge, cn } from "../components/ui";

/** Bar segments, left to right. */
const BUCKETS: { key: SeriesBucket; label: string; className: string }[] = [
  { key: "converted", label: "Konvertiert", className: "bg-save-500" },
  { key: "av1", label: "War schon AV1", className: "bg-save-500/45" },
  { key: "active", label: "In Arbeit", className: "bg-brand-500" },
  { key: "pending", label: "Kandidat", className: "bg-info-500/70" },
  { key: "excluded", label: "Ausgeschlossen / uebersprungen", className: "bg-ink-400" },
  { key: "failed", label: "Fehler", className: "bg-danger-500" },
  { key: "other", label: "Noch offen", className: "bg-ink-600" },
];

/** Same rule as the backend: a forced series or season run leaves AV1 files alone. */
const FORCEABLE: SeriesBucket[] = ["pending", "excluded", "failed", "other"];

const FILTERS = [
  { value: "all", label: "Alle Serien" },
  { value: "open", label: "Noch nicht fertig" },
  { value: "complete", label: "Komplett in AV1" },
  { value: "candidates", label: "Mit Kandidaten" },
  { value: "excluded", label: "Mit Ausschluessen" },
  { value: "failed", label: "Mit Fehlern" },
];

const SORTS = [
  { value: "name", label: "Name" },
  { value: "progress", label: "Fortschritt (offen zuerst)" },
  { value: "size", label: "Groesse" },
  { value: "saved", label: "Gespart" },
  { value: "potential", label: "Noch moeglich" },
];

const share = (t: SeriesTally) => (t.episodes ? t.in_av1 / t.episodes : 0);

const countFor = (episodes: SeriesEpisode[], force: boolean) =>
  episodes.filter((e) =>
    force ? FORCEABLE.includes(e.bucket) && e.state !== "missing" : e.bucket === "pending",
  ).length;

export default function SeriesPage() {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("name");
  const [open, setOpen] = useState<Set<string>>(new Set());

  const { data, isLoading } = useQuery({
    queryKey: ["series"],
    queryFn: endpoints.series,
    refetchInterval: 30000,
  });

  const items = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const list = (data?.items ?? []).filter((s) => {
      if (needle && !s.name.toLowerCase().includes(needle)) return false;
      switch (filter) {
        case "open":
          return s.in_av1 < s.episodes;
        case "complete":
          return s.episodes > 0 && s.in_av1 === s.episodes;
        case "candidates":
          return s.counts.pending > 0;
        case "excluded":
          return s.counts.excluded > 0;
        case "failed":
          return s.counts.failed > 0;
        default:
          return true;
      }
    });
    const byName = (a: SeriesSummary, b: SeriesSummary) => a.name.localeCompare(b.name, "de");
    const descending: Record<string, (s: SeriesSummary) => number> = {
      size: (s) => s.total_size,
      saved: (s) => s.saved_bytes,
      potential: (s) => s.potential_saving,
    };
    if (sort === "progress") return [...list].sort((a, b) => share(a) - share(b) || byName(a, b));
    const key = descending[sort];
    return key ? [...list].sort((a, b) => key(b) - key(a) || byName(a, b)) : list;
  }, [data, search, filter, sort]);

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  const totals = data?.totals;
  const hasSeries = (data?.items.length ?? 0) > 0;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Serien"
          value={number(data?.items.length ?? 0)}
          hint={totals ? `${number(totals.episodes)} Folgen` : undefined}
        />
        <Stat
          label="In AV1"
          value={totals ? `${Math.floor(share(totals) * 100)} %` : "-"}
          hint={totals ? `${number(totals.in_av1)} von ${number(totals.episodes)} Folgen` : undefined}
          tone="save"
        />
        <Stat
          label="Gespart"
          value={bytes(totals?.saved_bytes)}
          hint={`${number(totals?.counts.converted ?? 0)} Folgen konvertiert`}
          tone="save"
        />
        <Stat
          label="Noch moeglich"
          value={bytes(totals?.potential_saving)}
          hint={`${number(totals?.counts.pending ?? 0)} Kandidaten`}
        />
      </div>

      <Panel
        title="Serien"
        subtitle="Nach Serie und Staffel gruppiert - erkannt an der Ordnerstruktur"
        actions={
          <>
            <div className="relative w-56">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-500" />
              <input
                className="field pl-9"
                placeholder="Serie suchen..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <div className="w-44">
              <Select value={filter} onChange={setFilter} options={FILTERS} />
            </div>
            <div className="w-56">
              <Select value={sort} onChange={setSort} options={SORTS} />
            </div>
          </>
        }
        bodyClassName="p-0"
      >
        <Legend />
        {isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={<Tv className="size-8" />}
            title={hasSeries ? "Keine Serie passt zum Filter" : "Noch keine Serien erkannt"}
            description={
              hasSeries
                ? undefined
                : "Serien werden an der Ordnerstruktur erkannt, z.B. Serie/Staffel 01/Serie - S01E01.mkv. " +
                  "Sobald ein Scan solche Dateien findet, erscheinen sie hier."
            }
          />
        ) : (
          <ul className="divide-y divide-ink-800/80">
            {items.map((s) => (
              <SeriesRow key={s.key} series={s} open={open.has(s.key)} onToggle={() => toggle(s.key)} />
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "save";
}) {
  return (
    <div className="panel px-4 py-3">
      <p className="text-xs text-ink-400">{label}</p>
      <p
        className={cn(
          "mt-1 text-xl font-semibold tracking-tight",
          tone === "save" ? "text-save-400" : "text-ink-100",
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-[11px] text-ink-500">{hint}</p>}
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-ink-800/80 px-5 py-2.5 text-[11px] text-ink-400">
      {BUCKETS.map((b) => (
        <span key={b.key} className="flex items-center gap-1.5">
          <span className={cn("size-2 rounded-full", b.className)} />
          {b.label}
        </span>
      ))}
    </div>
  );
}

function BucketBar({ tally, className }: { tally: SeriesTally; className?: string }) {
  const title = BUCKETS.filter((b) => tally.counts[b.key])
    .map((b) => `${b.label}: ${tally.counts[b.key]}`)
    .join(" · ");
  return (
    <div className={cn("flex h-2 overflow-hidden rounded-full bg-ink-800", className)} title={title}>
      {tally.episodes > 0 &&
        BUCKETS.map((b) =>
          tally.counts[b.key] ? (
            <div
              key={b.key}
              className={b.className}
              style={{ width: `${(tally.counts[b.key] / tally.episodes) * 100}%` }}
            />
          ) : null,
        )}
    </div>
  );
}

function IconButton({
  title,
  onClick,
  disabled,
  tone = "brand",
  children,
}: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  tone?: "brand" | "warn";
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "rounded-md p-1.5 text-ink-400 transition-colors disabled:cursor-not-allowed disabled:opacity-30",
        tone === "warn"
          ? "hover:bg-warn-500/15 hover:text-warn-400"
          : "hover:bg-brand-600/20 hover:text-brand-400",
      )}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------------- */

function SeriesRow({
  series,
  open,
  onToggle,
}: {
  series: SeriesSummary;
  open: boolean;
  onToggle: () => void;
}) {
  const seasons =
    series.season_count === 1 ? "1 Staffel" : `${series.season_count} Staffeln`;
  return (
    <li>
      <button
        onClick={onToggle}
        aria-expanded={open}
        className="grid w-full grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-2 px-5 py-3 text-left transition-colors hover:bg-ink-800/40 md:grid-cols-[auto_minmax(0,2fr)_minmax(0,1.5fr)_7rem_8rem]"
      >
        {open ? (
          <ChevronDown className="size-4 text-ink-500" />
        ) : (
          <ChevronRight className="size-4 text-ink-500" />
        )}
        <span className="min-w-0">
          <span className="block truncate font-medium text-ink-100">{series.name}</span>
          <span className="block truncate text-xs text-ink-500">
            {series.library} · {seasons} · {series.episodes} Folgen
          </span>
        </span>
        <span className="col-span-2 min-w-0 md:col-span-1">
          <span className="mb-1 flex items-baseline justify-between gap-2 text-xs">
            <span className="font-medium text-ink-200">
              {series.in_av1} / {series.episodes}
            </span>
            <span className="text-ink-500">{Math.floor(share(series) * 100)} % in AV1</span>
          </span>
          <BucketBar tally={series} />
        </span>
        <span className="hidden text-right text-sm text-ink-200 md:block">
          {bytes(series.total_size)}
        </span>
        <span className="hidden text-right text-xs md:block">
          {series.saved_bytes > 0 ? (
            <span className="block font-medium text-save-400">-{bytes(series.saved_bytes)}</span>
          ) : (
            <span className="block text-ink-600">-</span>
          )}
          {series.potential_saving > 0 && (
            <span className="block text-ink-500">~ -{bytes(series.potential_saving)} moeglich</span>
          )}
        </span>
      </button>
      {open && <SeriesDetailView seriesKey={series.key} />}
    </li>
  );
}

function SeriesDetailView({ seriesKey }: { seriesKey: string }) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<{ season: SeriesSeason | null; count: number } | null>(
    null,
  );

  const { data, isLoading } = useQuery({
    queryKey: ["series", "detail", seriesKey],
    queryFn: () => endpoints.seriesDetail(seriesKey),
  });

  const onResult = (result: EnqueueResult) => {
    push(result.message, result.added ? "success" : "info");
    ["series", "jobs", "files"].forEach((key) =>
      queryClient.invalidateQueries({ queryKey: [key] }),
    );
  };

  const enqueueGroup = useMutation({
    mutationFn: (p: { season?: number; force: boolean }) =>
      endpoints.enqueueSeries({ key: seriesKey, ...p }),
    onSuccess: (result) => {
      onResult(result);
      setConfirm(null);
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const enqueueFile = useMutation({
    mutationFn: (p: { id: number; force: boolean }) =>
      endpoints.enqueue({ file_ids: [p.id], force: p.force }),
    onSuccess: onResult,
    onError: (e: Error) => push(e.message, "error"),
  });

  if (isLoading || !data) {
    return (
      <div className="space-y-2 border-t border-ink-800/80 bg-ink-950/30 px-5 py-4">
        <Skeleton className="h-10" />
        <Skeleton className="h-24" />
      </div>
    );
  }

  const all = data.seasons.flatMap((s) => s.files);
  const pendingAll = countFor(all, false);
  const forceAll = countFor(all, true);
  const busy = enqueueGroup.isPending || enqueueFile.isPending;

  return (
    <div className="border-t border-ink-800/80 bg-ink-950/30 px-3 pb-4 pt-3 sm:px-5">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="mr-auto min-w-0 truncate font-mono text-[11px] text-ink-500" title={data.path}>
          {data.path}
          {data.last_converted && (
            <span className="ml-3 font-sans">Zuletzt konvertiert {relativeTime(data.last_converted)}</span>
          )}
        </span>
        <button
          className="btn-ghost btn-sm"
          disabled={!pendingAll || busy}
          onClick={() => enqueueGroup.mutate({ force: false })}
        >
          <Play className="size-3.5" />
          Kandidaten einreihen ({pendingAll})
        </button>
        <button
          className="btn-ghost btn-sm border-warn-500/40 text-warn-400"
          disabled={!forceAll || busy}
          onClick={() => setConfirm({ season: null, count: forceAll })}
        >
          <Zap className="size-3.5" />
          Alles trotzdem konvertieren ({forceAll})
        </button>
      </div>

      <div className="space-y-3">
        {data.seasons.map((season) => (
          <SeasonBlock
            key={String(season.season)}
            season={season}
            busy={busy}
            onQueue={() => enqueueGroup.mutate({ season: season.season ?? undefined, force: false })}
            onForce={() => setConfirm({ season, count: countFor(season.files, true) })}
            onEpisode={(id, force) => enqueueFile.mutate({ id, force })}
          />
        ))}
      </div>

      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        title="Trotz Ausschluss konvertieren?"
        subtitle={confirm?.season ? `${data.name} · ${confirm.season.label}` : data.name}
        footer={
          <>
            <button className="btn-ghost" onClick={() => setConfirm(null)}>
              Abbrechen
            </button>
            <button
              className="btn-primary"
              disabled={enqueueGroup.isPending}
              onClick={() =>
                confirm &&
                enqueueGroup.mutate({ season: confirm.season?.season ?? undefined, force: true })
              }
            >
              {enqueueGroup.isPending ? <Spinner className="size-4" /> : <Zap className="size-4" />}
              {confirm?.count} Folgen einreihen
            </button>
          </>
        }
      >
        <div className="space-y-3 text-sm leading-relaxed text-ink-300">
          <p>
            {confirm?.count} Folgen kommen in die Warteschlange - auch solche, deren Codec
            ausgeschlossen ist, die ignoriert werden oder die als nicht lohnend eingestuft wurden.
          </p>
          <ul className="list-disc space-y-1 pl-5 text-ink-400">
            <li>
              Folgen, die schon AV1 sind, bleiben unberuehrt. Einzelne lassen sich in ihrer Zeile
              trotzdem erzwingen.
            </li>
            <li>
              Die Mindestersparnis gilt fuer diese Jobs nicht. Waere ein Ergebnis groesser als das
              Original, wird es wie gewohnt verworfen.
            </li>
            <li>
              Folgen, die vor der Analyse ausgeschlossen wurden, bekommen ihren Plan erst beim
              Start - mit den Werten des Profils statt einer Testkodierung.
            </li>
          </ul>
        </div>
      </Modal>
    </div>
  );
}

function SeasonBlock({
  season,
  busy,
  onQueue,
  onForce,
  onEpisode,
}: {
  season: SeriesSeason;
  busy: boolean;
  onQueue: () => void;
  onForce: () => void;
  onEpisode: (id: number, force: boolean) => void;
}) {
  // Finished seasons start folded, like Sonarr - what is left to do stays in view.
  const [open, setOpen] = useState(season.in_av1 < season.episodes);
  const pending = countFor(season.files, false);
  const forceable = countFor(season.files, true);

  return (
    <div className="overflow-hidden rounded-lg border border-ink-800 bg-ink-900/60">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5">
        <button
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-2 text-left"
        >
          {open ? (
            <ChevronDown className="size-4 text-ink-500" />
          ) : (
            <ChevronRight className="size-4 text-ink-500" />
          )}
          <span className="font-medium text-ink-100">{season.label}</span>
          <span className="whitespace-nowrap text-xs text-ink-500">
            {season.in_av1} / {season.episodes} in AV1
          </span>
        </button>
        <BucketBar tally={season} className="min-w-24 flex-1" />
        <span className="whitespace-nowrap text-xs text-ink-400">
          {bytes(season.total_size)}
          {season.saved_bytes > 0 && (
            <span className="text-save-400"> · -{bytes(season.saved_bytes)}</span>
          )}
        </span>
        {/* Loose files have no season to address; they are reachable per row. */}
        {season.season !== null && (
          <div className="flex gap-1">
            <IconButton
              title={`Kandidaten dieser Staffel einreihen (${pending})`}
              disabled={!pending || busy}
              onClick={onQueue}
            >
              <Play className="size-3.5" />
            </IconButton>
            <IconButton
              tone="warn"
              title={`Staffel trotzdem konvertieren (${forceable})`}
              disabled={!forceable || busy}
              onClick={onForce}
            >
              <Zap className="size-3.5" />
            </IconButton>
          </div>
        )}
      </div>
      {open && (
        <div className="overflow-x-auto border-t border-ink-800">
          <table className="w-full text-sm">
            <tbody>
              {season.files.map((ep) => (
                <EpisodeRow
                  key={ep.id}
                  episode={ep}
                  busy={busy}
                  onQueue={() => onEpisode(ep.id, false)}
                  onForce={() => onEpisode(ep.id, true)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function episodeCode(ep: SeriesEpisode): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  if (ep.season !== null && ep.episode !== null) return `S${pad(ep.season)}E${pad(ep.episode)}`;
  if (ep.episode !== null) return `E${pad(ep.episode)}`;
  return "-";
}

function EpisodeRow({
  episode: ep,
  busy,
  onQueue,
  onForce,
}: {
  episode: SeriesEpisode;
  busy: boolean;
  onQueue: () => void;
  onForce: () => void;
}) {
  const saved = ep.state === "done" && ep.original_size > ep.size ? ep.original_size - ep.size : 0;
  const reason =
    ep.bucket === "failed" ? ep.error : ep.bucket === "excluded" ? ep.decision_reason : "";
  const canForce =
    (FORCEABLE.includes(ep.bucket) || ep.bucket === "av1") &&
    ep.bucket !== "pending" &&
    ep.state !== "missing";

  return (
    <tr className="table-row last:border-0">
      <td className="w-20 whitespace-nowrap px-4 py-2 font-mono text-xs text-ink-400">
        {episodeCode(ep)}
      </td>
      <td className="w-full max-w-0 px-2 py-2">
        <Link
          to={`/library?file=${ep.id}`}
          className="block truncate text-ink-100 hover:text-brand-400"
          title={ep.path}
        >
          {ep.name}
        </Link>
        {reason && (
          <span className="block truncate text-xs text-ink-500" title={reason}>
            {reason}
          </span>
        )}
      </td>
      <td className="hidden whitespace-nowrap px-3 py-2 text-xs text-ink-300 sm:table-cell">
        <span className="uppercase">{ep.video_codec || "?"}</span>
        <span className="mx-1 text-ink-600">·</span>
        {resolutionLabel(ep.width, ep.height)}
      </td>
      <td className="hidden whitespace-nowrap px-3 py-2 text-right text-xs sm:table-cell">
        <span className="text-ink-200">{bytes(ep.size)}</span>
        {saved > 0 ? (
          <span className="block text-save-400">-{bytes(saved)}</span>
        ) : ep.bucket === "pending" && ep.estimated_saving_bytes > 0 ? (
          <span className="block text-ink-500">~ -{bytes(ep.estimated_saving_bytes)}</span>
        ) : null}
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        <StateBadge state={ep.state} label={STATE_LABELS[ep.state] ?? ep.state} />
      </td>
      <td className="w-12 whitespace-nowrap px-3 py-2 text-right">
        {ep.bucket === "pending" ? (
          <IconButton title="Zur Warteschlange hinzufuegen" disabled={busy} onClick={onQueue}>
            <Play className="size-3.5" />
          </IconButton>
        ) : canForce ? (
          <IconButton
            tone="warn"
            title={
              ep.bucket === "av1"
                ? "Ist bereits AV1 - trotzdem neu kodieren"
                : "Trotz Ausschluss konvertieren"
            }
            disabled={busy}
            onClick={onForce}
          >
            <Zap className="size-3.5" />
          </IconButton>
        ) : null}
      </td>
    </tr>
  );
}
