import { GroupPagination } from "../components/GroupPagination";
import { useDebouncedValue } from "../lib/hooks";
/** Movie overview: every film in the library and how far it is converted, Radarr style. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Film, Play, Search, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  BucketBar,
  FileAction,
  ForceConfirm,
  Legend,
  Stat,
  fileReason,
} from "../components/groups";
import { EmptyState, ErrorState, Panel, Select, Skeleton, StateBadge } from "../components/ui";
import {
  endpoints,
  type EnqueueResult,
  type MovieFile,
  type MovieSummary,
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
  { value: "size", label: "Größe" },
  { value: "saved", label: "Gespart" },
  { value: "potential", label: "Noch möglich" },
];



const fileCount = (n: number) => `${number(n)} ${n === 1 ? "Datei" : "Dateien"}`;

export default function MoviesPage() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("title");
  const [open, setOpen] = useState<Set<string>>(new Set());

  const [confirm, setConfirm] = useState<boolean>(false);



  const [page, setPage] = useState(1);
  const debouncedSearch = useDebouncedValue(search, 250);
  useEffect(() => setPage(1), [debouncedSearch, filter, sort]);
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["movies", debouncedSearch, filter, sort, page],
    queryFn: ({ signal }) => endpoints.movies({ search: debouncedSearch, filter, sort, page, page_size: 50 }, { signal }),
    refetchInterval: 30000,
  });
  const items = data?.items ?? [];

  const enqueue = useMutation({
    mutationFn: (p: { ids?: number[]; force: boolean }) =>
      p.ids ? endpoints.enqueue({ file_ids: p.ids, force: p.force }) : endpoints.enqueueMovies({ search: debouncedSearch, filter, force: p.force }),
    onSuccess: (result: EnqueueResult) => {
      push(result.message, result.added ? "success" : "info");
      setConfirm(false);
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

  // The server applies bulk actions to every match, including other pages.
  const pendingCount = data?.pending_count ?? 0;
  const forceCount = data?.force_count ?? 0;

  const totals = data?.totals;
  const movieCount = data?.all_count ?? 0;
  const completeCount = data?.complete_count ?? 0;

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
          label="Noch möglich"
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
                placeholder="Film oder Jahr suchen …"
                aria-label="Film oder Jahr suchen"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <div className="w-44">
              <Select value={filter} onChange={setFilter} options={FILTERS} ariaLabel="Filter" />
            </div>
            <div className="w-52">
              <Select value={sort} onChange={setSort} options={SORTS} ariaLabel="Sortierung" />
            </div>
          </>
        }
        bodyClassName="p-0"
      >
        <Legend />
        {!isLoading && items.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-ink-800/80 px-5 py-2.5">
            <span className="mr-auto text-xs text-ink-500">{number(data?.total ?? 0)} {data?.total === 1 ? "Film" : "Filme"} im Filter</span>
            <button
              className="btn-ghost btn-sm"
              disabled={!pendingCount || enqueue.isPending}
              onClick={() => enqueue.mutate({ force: false })}
            >
              <Play className="size-3.5" />
              Kandidaten einreihen ({pendingCount})
            </button>
            <button
              className="btn-ghost btn-sm border-warn-500/40 text-warn-400"
              disabled={!forceCount || enqueue.isPending}
              onClick={() => setConfirm(true)}
            >
              <Zap className="size-3.5" />
              Alle im Filter trotzdem konvertieren ({forceCount})
            </button>
          </div>
        )}

        {isError && !data ? (
          <ErrorState error={error} onRetry={() => refetch()} title="Filme konnten nicht geladen werden" />
        ) : isLoading ? (
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
                  "z. B. Filme/Dune (2021)/Dune (2021).mkv."
            }
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-ink-800/80 text-left text-xs text-ink-500">
                  <tr>
                    <th className="w-8 px-3 py-2">
                      <span className="sr-only">Aufklappen</span>
                    </th>
                    <th className="px-2 py-2 font-medium">Titel</th>
                    <th className="hidden px-3 py-2 font-medium md:table-cell">Datei</th>
                    <th className="px-3 py-2 text-right font-medium">Größe</th>
                    <th className="hidden px-3 py-2 text-right font-medium sm:table-cell">Ersparnis</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="w-12 px-3 py-2">
                      <span className="sr-only">Aktionen</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((m) => (
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
          </>
        )}
        {data && <GroupPagination page={data.page} pages={data.pages} total={data.total} onPage={setPage} />}
      </Panel>

      <ForceConfirm
        open={confirm}
        count={forceCount}
        noun="Dateien"
        subtitle={`Filme · ${FILTERS.find((f) => f.value === filter)?.label ?? ""}`}
        pending={enqueue.isPending}
        onClose={() => setConfirm(false)}
        onConfirm={() => enqueue.mutate({ force: true })}
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
