/** WebSocket connection to the backend, plus a tiny toast system. */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { ScanState } from "./api";
import { InvalidationBatcher, type FlushTarget } from "./invalidation";

export interface LiveEvent {
  type: string;
  ts: string;
  data: Record<string, unknown>;
}

export interface JobProgress {
  progress: number;
  fps: number;
  speed: number;
  eta_seconds: number;
  current_size: number;
  /** Seconds of source encoded so far, and the total - used to extrapolate. */
  out_time?: number;
  duration?: number;
}

interface LiveContextValue {
  connected: boolean;
  scan: ScanState | null;
  jobProgress: Record<number, JobProgress>;
}

const LiveContext = createContext<LiveContextValue>({
  connected: false,
  scan: null,
  jobProgress: {},
});

/** Which queries to refresh when a given event arrives. */
export const INVALIDATION_MAP: Record<string, string[]> = {
  "scan.started": ["scan", "system"],
  "scan.finished": ["scan", "files", "series", "movies", "stats", "system", "history"],
  "file.analyzed": ["files", "stats", "series", "movies"],
  "job.started": ["jobs", "files", "series", "movies", "system"],
  "job.finished": ["trash", "jobs", "files", "series", "movies", "stats", "history", "system", "model"],
  "queue.changed": ["jobs", "files", "series", "movies", "system", "settings"],
  "settings.changed": ["settings", "system"],
  "library.changed": ["library", "files", "series", "movies", "stats"],
  "hardware.detected": ["system"],
  "model.updated": ["model", "system"],
  "trash.changed": ["trash", "files", "movies", "series", "stats", "history"],
  history: ["history"],
};

/** Scan state at the start of a run, before any progress has arrived. */
function freshScan(): ScanState {
  return {
    run_id: null,
    running: true,
    phase: "walk",
    total: 0,
    done: 0,
    current: "",
    progress: 0,
    started_at: new Date().toISOString(),
    seen: 0,
  };
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [scan, setScan] = useState<ScanState | null>(null);
  const [jobProgress, setJobProgress] = useState<Record<number, JobProgress>>({});
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | undefined>(undefined);
  // Set after the first successful connect: every later open is a reconnect.
  const connectedBefore = useRef(false);

  // Events only mark queries stale; the refetches run once per window.  A
  // refetch already in flight is left alone instead of being restarted.
  const batcher = useMemo(
    () =>
      new InvalidationBatcher((target: FlushTarget) => {
        if (target === "all") {
          queryClient.invalidateQueries(undefined, { cancelRefetch: false });
          return;
        }
        target.forEach((key) =>
          queryClient.invalidateQueries({ queryKey: [key] }, { cancelRefetch: false }),
        );
      }),
    [queryClient],
  );
  useEffect(() => () => batcher.dispose(), [batcher]);

  const handleEvent = useCallback(
    (event: LiveEvent) => {
      if (event.type === "ping") return;

      // Progress arrives several times a second; only the progress bars care.
      if (event.type === "job.progress") {
        const d = event.data as unknown as { job_id: number } & JobProgress;
        setJobProgress((prev) => ({ ...prev, [d.job_id]: d }));
        return;
      }

      if (event.type === "hello") {
        const payload = event.data as { scan?: ScanState };
        if (payload.scan) setScan(payload.scan);
        // A (re)connect starts from scratch: progress kept from before a server
        // restart belongs to runs that no longer exist.
        setJobProgress({});
        return;
      }
      if (event.type === "scan.progress") {
        // The walk phase only sends {phase, seen, new, current}; later phases
        // send a full snapshot.  Merge so running/total/done survive either way.
        const d = event.data as unknown as Partial<ScanState>;
        setScan((prev) => ({ ...(prev ?? freshScan()), running: true, ...d }));
        return;
      }
      if (event.type === "scan.started") {
        // A new run: counters of the previous run must not leak into this one.
        const d = event.data as { run_id?: number };
        setScan({ ...freshScan(), run_id: d.run_id ?? null });
      }
      if (event.type === "scan.finished") {
        setScan((prev) => (prev ? { ...prev, running: false, phase: "idle" } : prev));
      }
      if (event.type === "job.finished") {
        const d = event.data as unknown as { job_id: number };
        setJobProgress((prev) => {
          const next = { ...prev };
          delete next[d.job_id];
          return next;
        });
      }

      const keys = INVALIDATION_MAP[event.type];
      if (keys) batcher.add(keys);
    },
    [batcher],
  );

  useEffect(() => {
    let closed = false;

    const connect = () => {
      if (closed) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(`${protocol}//${window.location.host}/api/ws`);
      socketRef.current = socket;

      socket.onopen = () => {
        setConnected(true);
        retryRef.current = 0;
        // Whatever happened while the connection was down arrived nowhere, so
        // after a reconnect every cached answer is suspect.  The first connect
        // needs nothing: the queries have only just been fetched.
        if (connectedBefore.current) batcher.addAll();
        connectedBefore.current = true;
      };
      socket.onmessage = (message) => {
        try {
          handleEvent(JSON.parse(message.data) as LiveEvent);
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        // Back off, but keep trying - the container may just be restarting.
        retryRef.current = Math.min(retryRef.current + 1, 6);
        const delay = Math.min(1000 * 2 ** retryRef.current, 20000);
        timerRef.current = window.setTimeout(connect, delay);
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      closed = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
      socketRef.current?.close();
    };
  }, [handleEvent, batcher]);

  const value = useMemo(() => ({ connected, scan, jobProgress }), [connected, scan, jobProgress]);

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export const useLive = () => useContext(LiveContext);

/* -------------------------------------------------------------------------- */
/* Toasts                                                                     */
/* -------------------------------------------------------------------------- */

export interface Toast {
  id: number;
  message: string;
  tone: "success" | "error" | "info";
}

interface ToastContextValue {
  toasts: Toast[];
  push: (message: string, tone?: Toast["tone"]) => void;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastContextValue>({
  toasts: [],
  push: () => {},
  dismiss: () => {},
});

let toastId = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (message: string, tone: Toast["tone"] = "info") => {
      const id = ++toastId;
      setToasts((prev) => [...prev.slice(-3), { id, message, tone }]);
      window.setTimeout(() => dismiss(id), tone === "error" ? 8000 : 4500);
    },
    [dismiss],
  );

  const value = useMemo(() => ({ toasts, push, dismiss }), [toasts, push, dismiss]);
  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>;
}

export const useToast = () => useContext(ToastContext);
