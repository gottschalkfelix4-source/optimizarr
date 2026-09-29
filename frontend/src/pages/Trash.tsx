import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ArchiveRestore } from "lucide-react";
import { endpoints, type TrashItem } from "../lib/api";
import { bytes } from "../lib/format";
import { useToast } from "../lib/live";
import { Callout, EmptyState, ErrorState, Modal, Panel, Skeleton } from "../components/ui";

export default function TrashPage() {
  const client = useQueryClient();
  const { push } = useToast();
  const [selected, setSelected] = useState<TrashItem | null>(null);
  const query = useQuery({ queryKey: ["trash"], queryFn: ({ signal }) => endpoints.trash({ signal }), refetchInterval: 30000 });
  const restore = useMutation({
    mutationFn: (id: string) => endpoints.restoreTrash(id),
    onSuccess: (result) => {
      setSelected(null);
      push(`Original wiederhergestellt.${result.kept_output ? ` Konvertierte Version aufbewahrt: ${result.kept_output}` : ""}`, "success");
      ["trash", "files", "movies", "series", "stats", "history"].forEach(key => client.invalidateQueries({ queryKey: [key] }));
    },
    onError: (error: Error) => { push(error.message, "error"); query.refetch(); },
  });
  return <div className="space-y-5">
    <Callout tone="info">Hier stehen protokollierte Originale. Ältere Papierkorb-Dateien ohne Protokoll bleiben erhalten und müssen manuell verwaltet werden. Beim Wiederherstellen bleibt die konvertierte Version als zusätzliche Datei erhalten.</Callout>
    <Panel title="Papierkorb" subtitle={`${bytes(query.data?.total_size)} belegter Speicher`}>
      {query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : query.isLoading ? <Skeleton className="h-32" /> : !query.data?.items.length ? <EmptyState title="Keine protokollierten Originale im Papierkorb" /> :
        <ul className="divide-y divide-ink-800">{query.data.items.map(item => <li key={item.id} className="flex flex-wrap items-center justify-between gap-4 py-4">
          <div className="min-w-0 flex-1 text-sm">
            <p className="break-all font-medium text-ink-100">{item.source}</p>
            <p className="mt-1 text-ink-400">{bytes(item.size)} · Verschoben: {new Date(item.trashed_at).toLocaleString("de-DE")}</p>
            <p className="text-ink-400">{item.expires_at ? `Automatische Löschung ab ${new Date(item.expires_at).toLocaleString("de-DE")}` : "Automatische Löschung deaktiviert"}</p>
            <p className="mt-1 break-all text-xs text-ink-500">{item.path}</p>
            {item.conflict && <p role="status" className="mt-2 text-warn-400">{item.conflict}</p>}
          </div>
          <button className="btn-ghost" disabled={!!item.conflict || restore.isPending} onClick={() => setSelected(item)}><ArchiveRestore className="size-4" />Wiederherstellen</button>
        </li>)}</ul>}
    </Panel>
    <Modal open={selected !== null} title="Original wiederherstellen?" onClose={() => { if (!restore.isPending) setSelected(null); }} footer={<>
      <button className="btn-ghost" disabled={restore.isPending} onClick={() => setSelected(null)}>Abbrechen</button>
      <button className="btn-primary" disabled={restore.isPending} onClick={() => selected && restore.mutate(selected.id)}>{restore.isPending ? "Wird wiederhergestellt …" : "Original wiederherstellen"}</button>
    </>}>
      <p className="break-all text-sm text-ink-200">{selected?.source}</p>
      <p className="mt-3 text-sm text-ink-400">Die konvertierte Version wird unter einem zusätzlichen Namen aufbewahrt. Die Datei wird anschließend ignoriert, damit sie nicht automatisch erneut konvertiert wird. Laufende Scans und wartende oder laufende Jobs für diese Datei müssen vorher beendet werden.</p>
    </Modal>
  </div>;
}
