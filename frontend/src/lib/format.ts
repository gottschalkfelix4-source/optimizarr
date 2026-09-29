/** Display formatting helpers - German locale throughout. */
import type { BlockedKind, FileState, QueueStatus } from "./api";

export type SizeUnit = "binary" | "decimal";

/** Set from ``ui.size_unit`` once the settings are loaded (see App). */
let sizeUnit: SizeUnit = "binary";

export function setSizeUnit(unit: SizeUnit | undefined): void {
  sizeUnit = unit === "decimal" ? "decimal" : "binary";
}

export function getSizeUnit(): SizeUnit {
  return sizeUnit;
}

const UNITS: Record<SizeUnit, { base: number; names: string[] }> = {
  binary: { base: 1024, names: ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] },
  decimal: { base: 1000, names: ["B", "kB", "MB", "GB", "TB", "PB"] },
};

/** File size in binary (GiB) or decimal (GB) units, per ``ui.size_unit``. */
export function bytes(value: number | null | undefined, digits = 1, unit: SizeUnit = sizeUnit): string {
  if (!value || value <= 0 || !Number.isFinite(value)) return "0 B";
  const { base, names } = UNITS[unit];
  let v = value;
  let i = 0;
  while (v >= base && i < names.length - 1) {
    v /= base;
    i += 1;
  }
  return `${v.toLocaleString("de-DE", {
    minimumFractionDigits: i === 0 ? 0 : digits,
    maximumFractionDigits: i === 0 ? 0 : digits,
  })} ${names[i]}`;
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

/** Coarse duration for ETAs: "2 Std 14 Min", "3 Tage 4 Std". */
export function humanDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "-";
  const s = Math.round(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} ${d === 1 ? "Tag" : "Tage"} ${h} Std`;
  if (h > 0) return `${h} Std ${m} Min`;
  if (m > 0) return `${m} Min`;
  return `${s} Sek`;
}

export function percent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return `${value.toLocaleString("de-DE", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} %`;
}

export function number(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return value.toLocaleString("de-DE", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function bitrate(bitsPerSecond: number | null | undefined): string {
  if (!bitsPerSecond || bitsPerSecond <= 0) return "-";
  if (bitsPerSecond >= 1_000_000) {
    return `${(bitsPerSecond / 1_000_000).toLocaleString("de-DE", {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    })} Mbit/s`;
  }
  return `${Math.round(bitsPerSecond / 1000)} kbit/s`;
}

/** Resolution class - identical to the backend: by width, height as fallback,
 *  so letterboxed films (1920x800) still count as 1080p. */
export function resolutionLabel(width: number, height: number): string {
  const w = width || 0;
  const h = height || 0;
  if (!w && !h) return "-";
  if (w >= 3200 || h >= 1800) return "2160p";
  if (w >= 2200 || h >= 1260) return "1440p";
  if (w >= 1700 || h >= 900) return "1080p";
  if (w >= 1100 || h >= 620) return "720p";
  return "SD";
}

/** "dolby_vision_p8" -> "Dolby Vision P8", "hdr10" -> "HDR10". */
export function hdrLabel(format: string | null | undefined): string {
  const f = (format ?? "").toLowerCase();
  if (!f) return "HDR";
  const dv = f.match(/^dolby_vision(?:_p(\d+))?$/);
  if (dv) return dv[1] ? `Dolby Vision P${dv[1]}` : "Dolby Vision";
  if (f === "hdr10plus") return "HDR10+";
  if (f === "hdr10") return "HDR10";
  if (f === "hlg") return "HLG";
  return f.toUpperCase();
}

/** Short chip text: "DV P5", "HDR10", ... */
export function hdrChip(format: string | null | undefined): string {
  const label = hdrLabel(format);
  return label.startsWith("Dolby Vision") ? label.replace("Dolby Vision", "DV") : label;
}

export function isDolbyVision(format: string | null | undefined): boolean {
  return (format ?? "").toLowerCase().startsWith("dolby_vision");
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "-";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "-";
  const diff = now - then;
  const minutes = Math.round(diff / 60000);
  if (minutes < 1) return "gerade eben";
  if (minutes < 60) return `vor ${minutes} Min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `vor ${hours} Std`;
  const days = Math.round(hours / 24);
  if (days < 30) return days === 1 ? "vor 1 Tag" : `vor ${days} Tagen`;
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

/** A plain "YYYY-MM-DD" as a local date.  ``new Date("2026-09-20")`` is UTC
 *  midnight, which west of Greenwich is still the day before. */
export function parseLocalDate(value: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return new Date(value);
}

/** "in 2 Std 14 Min" or "überfällig" for the sidebar. */
export function nextScanText(iso: string, now = Date.now()): string {
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return "";
  const seconds = (at - now) / 1000;
  if (seconds <= 30) return "Nächster Scan überfällig";
  return `Nächster Scan in ${humanDuration(seconds)}`;
}

/** Size change from ``input`` to ``output``, signed from the file's view:
 *  "-1,2 GiB (-40 %)" when it shrank, "+300 MiB (+5 %)" when it grew. */
export function sizeChange(
  input: number,
  output: number,
): { text: string; smaller: boolean } | null {
  if (!(input > 0) || !(output > 0)) return null;
  const delta = output - input;
  const pct = (delta / input) * 100;
  const sign = delta > 0 ? "+" : delta < 0 ? "-" : "±";
  const pctText = percent(Math.abs(pct));
  return {
    text: `${sign}${bytes(Math.abs(delta))} (${sign}${pctText})`,
    smaller: delta < 0,
  };
}

/** Keep a page number inside 1..pages after the result set shrank. */
export function clampPage(page: number, pages: number | undefined): number {
  const last = Math.max(1, Math.floor(pages ?? 1));
  if (!Number.isFinite(page) || page < 1) return 1;
  return Math.min(Math.floor(page), last);
}

/** "1080p · av1_vaapi · CRF 30" without empty parts - forced jobs that were
 *  never analysed have no plan yet. */
export function joinParts(...parts: (string | number | null | undefined | false)[]): string {
  return parts
    .filter((p) => p !== null && p !== undefined && p !== false && String(p).trim() !== "")
    .join(" · ");
}

/** The reason category, also for backends that do not send ``blocked_kind``. */
export function blockedKind(status: QueueStatus | undefined): BlockedKind | "other" {
  if (!status) return "";
  if (status.blocked_kind !== undefined) {
    if (status.blocked_kind === "" && status.blocked_reason) return "other";
    return status.blocked_kind;
  }
  if (status.paused) return "paused";
  if (!status.schedule_ok) return "schedule";
  return status.blocked_reason ? "other" : "";
}

/** States in which a file may be ignored or un-ignored (the server answers 409
 *  for the others: queued, encoding, analyzing, done). */
export const IGNORABLE_STATES: ReadonlySet<FileState> = new Set<FileState>([
  "new", "probed", "candidate", "skipped", "failed", "ignored", "missing",
]);

export function ignoreAction(file: { state: FileState; ignored: boolean }):
  | { ignored: boolean; label: string }
  | null {
  if (!IGNORABLE_STATES.has(file.state)) return null;
  const isIgnored = file.ignored || file.state === "ignored";
  return isIgnored
    ? { ignored: false, label: "Nicht mehr ignorieren" }
    : { ignored: true, label: "Datei ignorieren" };
}

export const STATE_LABELS: Record<string, string> = {
  new: "Neu",
  probed: "Eingelesen",
  analyzing: "Wird analysiert",
  candidate: "Kandidat",
  skipped: "Übersprungen",
  queued: "In Warteschlange",
  encoding: "Wird konvertiert",
  done: "Konvertiert",
  failed: "Fehler",
  missing: "Fehlt",
  ignored: "Ignoriert",
};

export const JOB_STATE_LABELS: Record<string, string> = {
  queued: "Wartet",
  running: "Läuft",
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

/** Historic scores without a metric must never claim to be measured VMAF. */
export function qualityLabel(score: { vmaf: number | null; quality_metric?: string | null; quality_value?: number | null }): string {
  if (score.quality_metric === "vmaf" && score.quality_value != null) return `VMAF ${score.quality_value.toFixed(1)}`;
  if (score.quality_metric === "ssim" && score.quality_value != null) return `SSIM ${score.quality_value.toFixed(4)}${score.vmaf != null ? ` · VMAF-Schätzung ${score.vmaf.toFixed(1)}` : ""}`;
  return score.vmaf != null ? `Qualitätswert ${score.vmaf.toFixed(1)} (Messverfahren unbekannt)` : "Nicht gemessen";
}
