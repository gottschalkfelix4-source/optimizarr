export function GroupPagination({ page, pages, total, onPage }: { page: number; pages: number; total: number; onPage: (page: number) => void }) {
  return <nav aria-label="Seitennavigation" className="flex items-center justify-between gap-3 border-t border-ink-800 p-4 text-sm">
    <button className="btn-ghost btn-sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>Zurück</button>
    <span>Seite {page} von {pages} · {total} Treffer</span>
    <button className="btn-ghost btn-sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>Weiter</button>
  </nav>;
}
