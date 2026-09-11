/** Movie overview: every film in the library and how far it is converted, Radarr style. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Film, Play, Search, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  BucketBar,
  FileAction,
  ForceConfirm,
  Legend,
  Stat,
  fileReason,
  pick,
} from "../components/groups";
import { EmptyState, Panel, Select, Skeleton, StateBadge } from "../components/ui";
import {
  endpoints,
  type EnqueueResult,
  type MovieFile,
  type MovieSummary,
  type SeriesTally,
} from "../lib/api";
import { bytes, number, resolutionLabel, STATE_LABELS } from "../lib/format";
import { useToast } from "../lib/live";

const FILTERS = [
  { value: "all", label: "Alle Filme" },
  { value: "open", label: "Noch nicht in AV1" },
  { value: "complete", label: "In AV1" },
  { value: "candidates", label: "Kandidaten" },
  { value: "excluded", label: "Ausgeschlossen" },
  { value: "active", label: "In Arbeit" },
  { value: "failed", label: "Fehler" },
];

const SORTS = [
  { value: "title", label: "Titel" },
  { value: "year", label: "Jahr (neueste zuerst)" },
  { value: "size", label: "Groesse" },
  { value: "saved", label: "Gespart" },
  { value: "potential", label: "Noch moeglich" },
];

/** Rows rendered at once - big movie libraries run into the thousands. */
const PAGE = 200;

const complete = (t: SeriesTally) => t.episodes > 0 && t.in_av1 === t.episodes;

const fileCount = (n: number) => `${number(n)} ${n === 1 ? "Datei" : "Dateien"}`;

export default function MoviesPage() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("title");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(PAGE);
  const [confirm, setConfirm] = useState<number[] | null>(null);

  useEffect(() => setLimit(PAGE), [search, filter, sort]);

  const { data, isLoading } = useQuery({
    queryKey: ["movies"],
    queryFn: endpoints.movies,
    refetchInterval: 30000,
  });

  const items = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const list = (data?.items ?? []).filter((m) => {
      if (needle && !`${m.title} ${m.year ?? ""} ${m.name}`.toLowerCase().includes(needle)) {
        return false;
      }
      switch (filter) {
        case "open":
          return !complete(m);
        case "complete":
          return complete(m);
        case "candidates":
          return m.counts.pending > 0;
        case "excluded":
          return m.counts.excluded > 0;
        case "active":
          return m.counts.active > 0;
        case "failed":
          return m.counts.failed > 0;
        default:
          return true;
      }
    });
    const byTitle = (a: MovieSummary, b: MovieSummary) =>
      a.title.localeCompare(b.title, "de") || (a.year ?? 0) - (b.year ?? 0);
    const descending: Record<string, (m: MovieSummary) => number> = {
      year: (m) => m.year ?? 0,
      size: (m) => m.total_size,
      saved: (m) => m.saved_bytes,
      potential: (m) => m.potential_saving,
    };
    const key = descending[sort];
    return key ? [...list].sort((a, b) => key(b) - key(a) || byTitle(a, b)) : list;
  }, [data, search, filter, sort]);

  const enqueue = useMutation({
    mutationFn: (p: { ids: number[]; force: boolean }) =>
      endpoints.enqueue({ file_ids: p.ids, force: p.force }),
    onSuccess: (result: EnqueueResult) => {
      push(result.message, result.added ? "success" : "info");
      setConfirm(null);
      ["movies", "series", "jobs", "files"].forEach((key) =>
        queryClient.invalidateQueries({ queryKey: [key] }),
      );
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  // Bulk actions work on everything the filter shows, not just the rendered page.
  const inFilter = items.flatMap((m) => m.files);
  const pendingIds = pick(inFilter, false).map((f) => f.id);
  const forceIds = pick(inFilter, true).map((f) => f.id);

  const totals = data?.totals;
  const movieCount = data?.items.length ?? 0;
  const completeCount = (data?.items ?? []).filter(complete).length;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Filme"
          value={number(movieCount)}
          hint={totals ? fileCount(totals.episodes) : undefined}
        />
        <Stat
          label="In AV1"
          value={movieCount ? `${Math.floor((completeCount / movieCount) * 100)} %` : "-"}
          hint={`${number(completeCount)} von ${number(movieCount)} Filmen`}
          tone="save"
        />
        <Stat
          label="Gespart"
          value={bytes(totals?.saved_bytes)}
          hint={`${fileCount(totals?.counts.converted ?? 0)} konvertiert`}
          tone="save"
        />
        <Stat
          label="Noch moeglich"
          value={bytes(totals?.potential_saving)}
          hint={`${number(totals?.counts.pending ?? 0)} Kandidaten`}
        />
      </div>

      <Panel
        title="Filme"
        subtitle="Jeder Ordner ohne Staffeln und Folgen gilt als Film"
        actions={
          <>
            <div className="relative w-56">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-500" />
              <input
                className="field pl-9"
                placeholder="Film oder Jahr suchen..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <div className="w-44">
              <Select value={filter} onChange={setFilter} options={FILTERS} />
            </div>
            <div className="w-52">
              <Select value={sort} onChange={setSort} options={SORTS} />
            </div>
          </>
        }
        bodyClassName="p-0"
      >
        <Legend />
        {!isLoading && items.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-ink-800/80 px-5 py-2.5">
            <span className="mr-auto text-xs text-ink-500">{number(items.length)} Filme im Filter</span>
            <button
              className="btn-ghost btn-sm"
              disabled={!pendingIds.length || enqueue.isPending}
              onClick={() => enqueue.mutate({ ids: pendingIds, force: false })}
            >
              <Play className="size-3.5" />
              Kandidaten einreihen ({pendingIds.length})
            </button>
            <button
              className="btn-ghost btn-sm border-warn-500/40 text-warn-400"
              disabled={!forceIds.length || enqueue.isPending}
              onClick={() => setConfirm(forceIds)}
            >
              <Zap className="size-3.5" />
              Alle im Filter trotzdem konvertieren ({forceIds.length})
            </button>
          </div>
        )}

        {isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-12" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={<Film className="size-8" />}
            title={movieCount ? "Kein Film passt zum Filter" : "Noch keine Filme gefunden"}
            description={
              movieCount
                ? undefined
                : "Als Film gilt jeder Ordner einer Bibliothek ohne Staffeln oder Folgen, " +
                  "z.B. Filme/Dune (2021)/Dune (2021).mkv."
            }
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-ink-800/80 text-left text-xs text-ink-500">
                  <tr>
                    <th className="w-8 px-3 py-2" />
                    <th className="px-2 py-2 font-medium">Titel</th>
                    <th className="hidden px-3 py-2 font-medium md:table-cell">Datei</th>
                    <th className="px-3 py-2 text-right font-medium">Groesse</th>
                    <th className="hidden px-3 py-2 text-right font-medium sm:table-cell">Ersparnis</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="w-12 px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {items.slice(0, limit).map((m) => (
                    <MovieRows
                      key={m.key}
                      movie={m}
                      open={open.has(m.key)}
                      onToggle={() => toggle(m.key)}
                      busy={enqueue.isPending}
                      onFile={(id, force) => enqueue.mutate({ ids: [id], force })}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            {items.length > limit && (
              <div className="border-t border-ink-800/80 px-5 py-3 text-center">
                <button className="btn-ghost btn-sm" onClick={() => setLimit((l) => l + PAGE)}>
                  Weitere {number(Math.min(PAGE, items.length - limit))} anzeigen
                </button>
              </div>
            )}
          </>
        )}
      </Panel>

      <ForceConfirm
        open={confirm !== null}
        count={confirm?.length ?? 0}
        noun="Dateien"
        subtitle={`Filme · ${FILTERS.find((f) => f.value === filter)?.label ?? ""}`}
        pending={enqueue.isPending}
        onClose={() => setConfirm(null)}
        onConfirm={() => confirm && enqueue.mutate({ ids: confirm, force: true })}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function MovieRows({
  movie,
  open,
  onToggle,
  busy,
  onFile,
}: {
  movie: MovieSummary;
  open: boolean;
  onToggle: () => void;
  busy: boolean;
  onFile: (id: number, force: boolean) => void;
}) {
  const main = movie.files[0];
  const multi = movie.files.length > 1;
  const reason = fileReason(main);

  return (
    <>
      <tr className="table-row">
        <td className="px-3 py-2.5">
          {multi && (
            <button
              onClick={onToggle}
              aria-expanded={open}
              aria-label={open ? "Dateien einklappen" : "Dateien anzeigen"}
              className="rounded p-0.5 text-ink-500 hover:text-ink-200"
            >
              {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
            </button>
          )}
        </td>
        <td className="w-full max-w-0 px-2 py-2.5">
          <Link
            to={`/library?file=${main.id}`}
            className="block truncate font-medium text-ink-100 hover:text-brand-400"
            title={movie.path}
          >
            {movie.title}
            {movie.year && <span className="ml-1.5 font-normal text-ink-500">({movie.year})</span>}
          </Link>
          <span className="block truncate text-xs text-ink-500" title={reason || movie.path}>
            {multi && `${movie.files.length} Dateien · `}
            {reason || movie.library}
          </span>
        </td>
        <td className="hidden whitespace-nowrap px-3 py-2.5 text-xs text-ink-300 md:table-cell">
          <span className="uppercase">{main.video_codec || "?"}</span>
          <span className="mx-1 text-ink-600">·</span>
          {resolutionLabel(main.width, main.height)}
        </td>
        <td className="whitespace-nowrap px-3 py-2.5 text-right text-ink-200">
          {bytes(movie.total_size)}
        </td>
        <td className="hidden whitespace-nowrap px-3 py-2.5 text-right text-xs sm:table-cell">
          {movie.saved_bytes > 0 ? (
            <span className="font-medium text-save-400">-{bytes(movie.saved_bytes)}</span>
          ) : movie.potential_saving > 0 ? (
            <span className="text-ink-500">~ -{bytes(movie.potential_saving)}</span>
          ) : (
            <span className="text-ink-600">-</span>
          )}
        </td>
        <td className="whitespace-nowrap px-3 py-2.5">
          {multi ? (
            <div className="w-28">
              <BucketBar tally={movie} />
              <span className="mt-1 block text-[11px] text-ink-500">
                {movie.in_av1} / {movie.episodes} in AV1
              </span>
            </div>
          ) : (
            <StateBadge state={main.state} label={STATE_LABELS[main.state] ?? main.state} />
          )}
        </td>
        <td className="whitespace-nowrap px-3 py-2.5 text-right">
          {!multi && (
            <FileAction
              file={main}
              busy={busy}
              onQueue={() => onFile(main.id, false)}
              onForce={() => onFile(main.id, true)}
            />
          )}
        </td>
      </tr>
      {multi &&
        open &&
        movie.files.map((file) => (
          <FileRow
            key={file.id}
            file={file}
            busy={busy}
            onQueue={() => onFile(file.id, false)}
            onForce={() => onFile(file.id, true)}
          />
        ))}
    </>
  );
}

function FileRow({
  file,
  busy,
  onQueue,
  onForce,
}: {
  file: MovieFile;
  busy: boolean;
  onQueue: () => void;
  onForce: () => void;
}) {
  const saved =
    file.state === "done" && file.original_size > file.size ? file.original_size - file.size : 0;
  const reason = fileReason(file);
  return (
    <tr className="table-row bg-ink-950/30">
      <td />
      <td className="w-full max-w-0 py-2 pl-5 pr-2">
        <Link
          to={`/library?file=${file.id}`}
          className="block truncate text-ink-200 hover:text-brand-400"
          title={file.path}
        >
          {file.name}
        </Link>
        {reason && (
          <span className="block truncate text-xs text-ink-500" title={reason}>
            {reason}
          </span>
        )}
      </td>
      <td className="hidden whitespace-nowrap px-3 py-2 text-xs text-ink-300 md:table-cell">
        <span className="uppercase">{file.video_codec || "?"}</span>
        <span className="mx-1 text-ink-600">·</span>
        {resolutionLabel(file.width, file.height)}
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right text-xs text-ink-200">
        {bytes(file.size)}
      </td>
      <td className="hidden whitespace-nowrap px-3 py-2 text-right text-xs sm:table-cell">
        {saved > 0 ? (
          <span className="text-save-400">-{bytes(saved)}</span>
        ) : file.bucket === "pending" && file.estimated_saving_bytes > 0 ? (
          <span className="text-ink-500">~ -{bytes(file.estimated_saving_bytes)}</span>
        ) : (
          <span className="text-ink-600">-</span>
        )}
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        <StateBadge state={file.state} label={STATE_LABELS[file.state] ?? file.state} />
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-right">
        <FileAction file={file} busy={busy} onQueue={onQueue} onForce={onForce} />
      </td>
    </tr>
  );
}
