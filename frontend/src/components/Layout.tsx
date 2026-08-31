/** App shell: sidebar navigation, global actions, live status footer. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Cpu,
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
  X,
} from "lucide-react";
import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { endpoints, type SystemInfo } from "../lib/api";
import { bytes, humanDuration } from "../lib/format";
import { useLive, useToast } from "../lib/live";
import { cn, ProgressBar, Spinner } from "./ui";

const NAV = [
  { to: "/", label: "Uebersicht", icon: Gauge, end: true },
  { to: "/library", label: "Bibliothek", icon: ListVideo },
  { to: "/queue", label: "Warteschlange", icon: Layers },
  { to: "/insights", label: "Analyse & Modell", icon: Activity },
  { to: "/history", label: "Verlauf", icon: History },
  { to: "/settings", label: "Einstellungen", icon: Settings2 },
];

export function Layout({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const { connected, scan } = useLive();
  const { push } = useToast();
  const queryClient = useQueryClient();

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
    onSuccess: () => push("Scan wird abgebrochen...", "info"),
  });

  const togglePause = useMutation({
    mutationFn: (paused: boolean) => endpoints.pauseQueue(paused),
    onSuccess: (data) => {
      push(data.paused ? "Warteschlange pausiert." : "Warteschlange laeuft.", "info");
      queryClient.invalidateQueries({ queryKey: ["system"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const scanning = scan?.running ?? info?.scan.running ?? false;
  const paused = info?.queue.paused ?? false;
  const activeJobs = info?.queue.running_jobs.length ?? 0;

  return (
    <div className="flex min-h-screen">
      {/* ---------------- sidebar ---------------- */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-ink-800 bg-ink-900/95 backdrop-blur-md transition-transform lg:static lg:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center gap-3 border-b border-ink-800 px-5 py-4">
          <div className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 shadow-lg shadow-brand-700/30">
            <Sparkles className="size-5 text-white" />
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
            aria-label="Menue schliessen"
          >
            <X className="size-4" />
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
              <Icon className="size-4 shrink-0" />
              <span className="truncate">{label}</span>
              {to === "/queue" && activeJobs > 0 && (
                <span className="ml-auto rounded-full bg-brand-600/25 px-1.5 py-0.5 text-[10px] font-semibold text-brand-400">
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
              aria-label="Menue oeffnen"
            >
              <Menu className="size-5" />
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
                {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
                {paused ? "Fortsetzen" : "Pausieren"}
              </button>
              {scanning ? (
                <button
                  className="btn-ghost btn-sm border-warn-500/40 text-warn-400"
                  onClick={() => cancelScan.mutate()}
                >
                  <X className="size-3.5" />
                  Scan abbrechen
                </button>
              ) : (
                <button
                  className="btn-primary btn-sm"
                  onClick={() => startScan.mutate()}
                  disabled={startScan.isPending}
                >
                  {startScan.isPending ? <Spinner className="size-3.5" /> : <ScanLine className="size-3.5" />}
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
    idle: "Scan laeuft",
  };
  return (
    <div className="border-t border-ink-800/70 bg-ink-900/60 px-4 py-2.5 sm:px-6">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="flex items-center gap-2 font-medium text-brand-400">
          <Spinner className="size-3.5" />
          {phaseLabel[scan.phase] ?? "Scan laeuft"}
        </span>
        {scan.total > 0 && (
          <span className="text-ink-400">
            {scan.done} / {scan.total}
          </span>
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
            "Hardware wird geprueft..."
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
          <span>
            Naechster Scan{" "}
            {humanDuration((new Date(info.next_scan).getTime() - Date.now()) / 1000)}
          </span>
        </div>
      )}
    </div>
  );
}
