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
  lastEvent: LiveEvent | null;
}

const LiveContext = createContext<LiveContextValue>({
  connected: false,
  scan: null,
  jobProgress: {},
  lastEvent: null,
});

/** Which queries to refresh when a given event arrives. */
const INVALIDATION_MAP: Record<string, string[]> = {
  "scan.started": ["scan", "system"],
  "scan.finished": ["scan", "files", "stats", "system", "history"],
  "file.analyzed": ["files", "stats"],
  "job.started": ["jobs", "files", "system"],
  "job.finished": ["jobs", "files", "stats", "history", "system", "model"],
  "queue.changed": ["jobs", "files", "system"],
  "settings.changed": ["settings", "system"],
  "library.changed": ["library", "files", "stats"],
  "hardware.detected": ["system"],
  "model.updated": ["model", "system"],
  history: ["history"],
};

export function LiveProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [scan, setScan] = useState<ScanState | null>(null);
  const [jobProgress, setJobProgress] = useState<Record<number, JobProgress>>({});
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<number | undefined>(undefined);

  const handleEvent = useCallback(
    (event: LiveEvent) => {
      if (event.type === "ping") return;

      // Progress arrives several times a second.  Storing it in `lastEvent`
      // too would re-render every consumer of the context for a value only the
      // progress bars care about.
      if (event.type === "job.progress") {
        const d = event.data as unknown as { job_id: number } & JobProgress;
        setJobProgress((prev) => ({ ...prev, [d.job_id]: d }));
        return;
      }

      setLastEvent(event);

      if (event.type === "hello") {
        const payload = event.data as { scan?: ScanState };
        if (payload.scan) setScan(payload.scan);
        return;
      }
      if (event.type === "scan.progress") {
        setScan(event.data as unknown as ScanState);
        return;
      }
      if (event.type === "scan.started") {
        setScan((prev) => (prev ? { ...prev, running: true } : prev));
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
      if (keys) {
        keys.forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
      }
    },
    [queryClient],
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
  }, [handleEvent]);

  const value = useMemo(
    () => ({ connected, scan, jobProgress, lastEvent }),
    [connected, scan, jobProgress, lastEvent],
  );

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
