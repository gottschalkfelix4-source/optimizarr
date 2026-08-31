/** Display formatting helpers - German locale throughout. */

export function bytes(value: number | null | undefined, digits = 1): string {
  if (!value || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toLocaleString("de-DE", {
    minimumFractionDigits: i === 0 ? 0 : digits,
    maximumFractionDigits: i === 0 ? 0 : digits,
  })} ${units[i]}`;
}

/** Signed byte delta, e.g. "-4,2 GiB". */
export function bytesDelta(value: number): string {
  if (value === 0) return "0 B";
  return `${value > 0 ? "-" : "+"}${bytes(Math.abs(value))}`;
}

export function duration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "-";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

/** Coarse duration for ETAs: "2 Std 14 Min". */
export function humanDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "-";
  const s = Math.round(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} Tg ${h} Std`;
  if (h > 0) return `${h} Std ${m} Min`;
  if (m > 0) return `${m} Min`;
  return `${s} Sek`;
}

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "-";
  return `${value.toLocaleString("de-DE", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} %`;
}

export function number(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "-";
  return value.toLocaleString("de-DE", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function bitrate(bitsPerSecond: number | null | undefined): string {
  if (!bitsPerSecond || bitsPerSecond <= 0) return "-";
  if (bitsPerSecond >= 1_000_000) return `${(bitsPerSecond / 1_000_000).toFixed(1)} Mbit/s`;
  return `${Math.round(bitsPerSecond / 1000)} kbit/s`;
}

export function resolutionLabel(width: number, height: number): string {
  if (!width || !height) return "-";
  if (height >= 2000) return "4K";
  if (height >= 1400) return "1440p";
  if (height >= 1000) return "1080p";
  if (height >= 700) return "720p";
  if (height >= 500) return "576p";
  return `${height}p`;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "-";
  const diff = Date.now() - then;
  const minutes = Math.round(diff / 60000);
  if (minutes < 1) return "gerade eben";
  if (minutes < 60) return `vor ${minutes} Min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `vor ${hours} Std`;
  const days = Math.round(hours / 24);
  if (days < 30) return `vor ${days} Tg`;
  return new Date(iso).toLocaleDateString("de-DE");
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export const STATE_LABELS: Record<string, string> = {
  new: "Neu",
  probed: "Eingelesen",
  analyzing: "Wird analysiert",
  candidate: "Kandidat",
  skipped: "Uebersprungen",
  queued: "In Warteschlange",
  encoding: "Wird konvertiert",
  done: "Konvertiert",
  failed: "Fehler",
  missing: "Fehlt",
  ignored: "Ignoriert",
};

export const JOB_STATE_LABELS: Record<string, string> = {
  queued: "Wartet",
  running: "Laeuft",
  done: "Fertig",
  failed: "Fehlgeschlagen",
  cancelled: "Abgebrochen",
  rejected: "Verworfen",
};

export const STATE_STYLES: Record<string, string> = {
  new: "bg-ink-700/60 text-ink-300",
  probed: "bg-ink-700/60 text-ink-300",
  analyzing: "bg-info-500/15 text-info-400",
  candidate: "bg-save-500/15 text-save-400",
  skipped: "bg-ink-700/60 text-ink-400",
  queued: "bg-brand-500/15 text-brand-400",
  encoding: "bg-brand-500/20 text-brand-400",
  done: "bg-save-500/20 text-save-400",
  failed: "bg-danger-500/15 text-danger-400",
  missing: "bg-warn-500/15 text-warn-400",
  ignored: "bg-ink-700/60 text-ink-500",
  running: "bg-brand-500/20 text-brand-400",
  cancelled: "bg-ink-700/60 text-ink-400",
  rejected: "bg-warn-500/15 text-warn-400",
};

/** Confidence 0..1 -> label + colour. */
export function confidenceLabel(value: number): { label: string; className: string } {
  if (value >= 0.75) return { label: "hoch", className: "text-save-400" };
  if (value >= 0.55) return { label: "mittel", className: "text-warn-400" };
  return { label: "niedrig", className: "text-danger-400" };
}
