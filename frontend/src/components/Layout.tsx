/** App shell: sidebar navigation, global actions, live status footer. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArchiveRestore,
  Cpu,
  Film,
  Gauge,
  HardDrive,
  History,
  Layers,
  ListVideo,
  Menu,
  Pause,
  Play,
  RadioTower,
  ScanLine,
  Settings2,
  Sparkles,
  Tv,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { endpoints, type SystemInfo } from "../lib/api";
import { bytes, nextScanText } from "../lib/format";
import { useMediaQuery, useNow } from "../lib/hooks";
import { useLive, useToast } from "../lib/live";
import { cn, ProgressBar, Spinner } from "./ui";

const NAV = [
  { to: "/", label: "Übersicht", icon: Gauge, end: true },
  { to: "/library", label: "Bibliothek", icon: ListVideo },
  { to: "/series", label: "Serien", icon: Tv },
  { to: "/movies", label: "Filme", icon: Film },
  { to: "/queue", label: "Warteschlange", icon: Layers },
  { to: "/insights", label: "Analyse & Modell", icon: Activity },
  { to: "/trash", label: "Papierkorb", icon: ArchiveRestore },
  { to: "/history", label: "Verlauf", icon: History },
  { to: "/settings", label: "Einstellungen", icon: Settings2 },
];

export function Layout({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const { connected, scan } = useLive();
  const { push } = useToast();
  const queryClient = useQueryClient();
  const desktop = useMediaQuery("(min-width: 1024px)");
  // Off-canvas on small screens: while closed it must not be reachable by Tab
  // or by a screen reader either, not just be pushed out of sight.
  const sidebarHidden = !desktop && !mobileOpen;

  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMobileOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobileOpen]);

  const { data: info } = useQuery({
    queryKey: ["system"],
    queryFn: endpoints.systemInfo,
    refetchInterval: 15000,
  });

  const startScan = useMutation({
    mutationFn: () => endpoints.startScan(),
    onSuccess: () => push("Scan gestartet.", "success"),
    onError: (error: Error) => push(error.message, "error"),
  });

  const cancelScan = useMutation({
    mutationFn: () => endpoints.cancelScan(),
    onSuccess: () => push("Scan wird abgebrochen …", "info"),
    onError: (error: Error) => push(error.message, "error"),
  });

  const togglePause = useMutation({
    mutationFn: (paused: boolean) => endpoints.pauseQueue(paused),
    onSuccess: (data) => {
      push(data.paused ? "Warteschlange pausiert." : "Warteschlange läuft.", "info");
      queryClient.invalidateQueries({ queryKey: ["system"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (error: Error) => push(error.message, "error"),
  });

  const scanning = scan?.running ?? info?.scan.running ?? false;
  const paused = info?.queue.paused ?? false;
  const activeJobs = info?.queue.running_jobs.length ?? 0;

  return (
    <div className="flex min-h-screen">
      {/* ---------------- sidebar ---------------- */}
      <aside
        id="sidebar"
        aria-label="Hauptnavigation"
        inert={sidebarHidden}
        aria-hidden={sidebarHidden || undefined}
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 flex-col border-r border-ink-800 bg-ink-900/95 backdrop-blur-md transition-transform lg:sticky lg:top-0 lg:h-screen lg:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center gap-3 border-b border-ink-800 px-5 py-4">
          <div className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 shadow-lg shadow-brand-700/30">
            <Sparkles className="size-5 text-white" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <p className="font-semibold tracking-tight text-ink-100">Optimizarr</p>
            <p className="truncate text-[11px] text-ink-500">
              AV1-Optimierung {info?.version ? `v${info.version}` : ""}
            </p>
          </div>
          <button
            className="ml-auto rounded-lg p-1.5 text-ink-400 hover:bg-ink-800 lg:hidden"
            onClick={() => setMobileOpen(false)}
            aria-label="Menü schließen"
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        </div>

        <nav className="flex-1 space-y-1 overflow-y-auto p-3">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setMobileOpen(false)}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-brand-600/15 text-brand-400"
                    : "text-ink-300 hover:bg-ink-800/70 hover:text-ink-100",
                )
              }
            >
              <Icon className="size-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{label}</span>
              {to === "/queue" && activeJobs > 0 && (
                <span
                  aria-label={`${activeJobs} laufend`}
                  className="ml-auto rounded-full bg-brand-600/25 px-1.5 py-0.5 text-[10px] font-semibold text-brand-400">
                  {activeJobs}
                </span>
              )}
            </NavLink>
          ))}
        </nav>

        <SidebarStatus info={info} connected={connected} />
      </aside>

      {mobileOpen && (
        <div
          className="fixed inset-0 z-30 bg-ink-950/70 backdrop-blur-sm lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* ---------------- main ---------------- */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-ink-800 bg-ink-950/80 backdrop-blur-md">
          <div className="flex flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
            <button
              className="rounded-lg p-2 text-ink-300 hover:bg-ink-800 lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Menü öffnen"
              aria-expanded={mobileOpen}
              aria-controls="sidebar"
            >
              <Menu className="size-5" aria-hidden="true" />
            </button>
            <h1 className="text-lg font-semibold tracking-tight text-ink-100">
              {NAV.find((n) => (n.end ? location.pathname === n.to : location.pathname.startsWith(n.to)))
                ?.label ?? "Optimizarr"}
            </h1>

            <div className="ml-auto flex flex-wrap items-center gap-2">
              <button
                className={cn("btn-ghost btn-sm", paused && "border-warn-500/40 text-warn-400")}
                onClick={() => togglePause.mutate(!paused)}
                disabled={togglePause.isPending}
              >
                {paused ? (
                  <Play className="size-3.5" aria-hidden="true" />
                ) : (
                  <Pause className="size-3.5" aria-hidden="true" />
                )}
                {paused ? "Fortsetzen" : "Pausieren"}
              </button>
              {scanning ? (
                <button
                  className="btn-ghost btn-sm border-warn-500/40 text-warn-400"
                  onClick={() => cancelScan.mutate()}
                >
                  <X className="size-3.5" aria-hidden="true" />
                  Scan abbrechen
                </button>
              ) : (
                <button
                  className="btn-primary btn-sm"
                  onClick={() => startScan.mutate()}
                  disabled={startScan.isPending}
                >
                  {startScan.isPending ? <Spinner className="size-3.5" /> : <ScanLine className="size-3.5" aria-hidden="true" />}
                  Bibliothek scannen
                </button>
              )}
            </div>
          </div>

          {scanning && scan && <ScanBanner scan={scan} />}
        </header>

        <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-6 sm:px-6">{children}</main>
      </div>
    </div>
  );
}

function ScanBanner({ scan }: { scan: NonNullable<ReturnType<typeof useLive>["scan"]> }) {
  const phaseLabel: Record<string, string> = {
    walk: "Dateien werden gesucht",
    probe: "Metadaten werden gelesen",
    analyze: "Dateien werden analysiert",
    idle: "Scan läuft",
  };
  return (
    <div className="border-t border-ink-800/70 bg-ink-900/60 px-4 py-2.5 sm:px-6">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="flex items-center gap-2 font-medium text-brand-400">
          <Spinner className="size-3.5" />
          {phaseLabel[scan.phase] ?? "Scan läuft"}
        </span>
        {scan.phase === "walk" && typeof scan.seen === "number" && scan.seen > 0 ? (
          <span className="text-ink-400">
            {scan.seen} Dateien gefunden
            {typeof scan.new === "number" && scan.new > 0 && ` · ${scan.new} neu`}
          </span>
        ) : (
          scan.total > 0 && (
            <span className="text-ink-400">
              {scan.done} / {scan.total}
            </span>
          )
        )}
        {typeof scan.candidates === "number" && scan.candidates > 0 && (
          <span className="text-save-400">{scan.candidates} Kandidaten</span>
        )}
        {scan.current && (
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-500">
            {scan.current}
          </span>
        )}
      </div>
      {scan.total > 0 && <ProgressBar value={scan.progress} className="mt-2 h-1" />}
    </div>
  );
}

function SidebarStatus({ info, connected }: { info: SystemInfo | undefined; connected: boolean }) {
  const now = useNow(30000);
  const hw = info?.hardware;
  const hwAv1 =
    hw && Object.values(hw.encoders ?? {}).some((e) => e.verified && e.name.startsWith("av1_"));

  return (
    <div className="space-y-2.5 border-t border-ink-800 px-4 py-3 text-[11px]">
      <div className="flex items-center gap-2">
        <RadioTower
          className={cn("size-3.5 shrink-0", connected ? "text-save-400" : "text-danger-400")}
        />
        <span className={connected ? "text-ink-400" : "text-danger-400"}>
          {connected ? "Live verbunden" : "Keine Verbindung"}
        </span>
      </div>

      <div className="flex items-start gap-2">
        <Cpu className="mt-0.5 size-3.5 shrink-0 text-ink-500" />
        <span className="min-w-0 text-ink-400">
          {hw ? (
            <>
              <span className="block truncate text-ink-300">{hw.gpu_name}</span>
              <span className={hwAv1 ? "text-save-400" : "text-ink-500"}>
                {hwAv1 ? "AV1 in Hardware" : "AV1 auf der CPU"}
              </span>
            </>
          ) : (
            "Hardware wird geprüft …"
          )}
        </span>
      </div>

      {info?.paths && (
        <div className="flex items-center gap-2 text-ink-500">
          <HardDrive className="size-3.5 shrink-0" />
          <span>{bytes(info.paths.transcode_free_gb * 1024 ** 3, 0)} frei</span>
        </div>
      )}

      {info?.next_scan && (
        <div className="flex items-center gap-2 text-ink-500">
          <ScanLine className="size-3.5 shrink-0" />
          <span>{nextScanText(info.next_scan, now)}</span>
        </div>
      )}
    </div>
  );
}
