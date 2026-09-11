/** Building blocks shared by the series and movie overviews. */
import { Play, Zap } from "lucide-react";
import type { ReactNode } from "react";
import type { SeriesBucket, SeriesTally } from "../lib/api";
import { Modal, Spinner, cn } from "./ui";

/** Bar segments, left to right. */
export const BUCKETS: { key: SeriesBucket; label: string; className: string }[] = [
  { key: "converted", label: "Konvertiert", className: "bg-save-500" },
  { key: "av1", label: "War schon AV1", className: "bg-save-500/45" },
  { key: "active", label: "In Arbeit", className: "bg-brand-500" },
  { key: "pending", label: "Kandidat", className: "bg-info-500/70" },
  { key: "excluded", label: "Ausgeschlossen / uebersprungen", className: "bg-ink-400" },
  { key: "failed", label: "Fehler", className: "bg-danger-500" },
  { key: "other", label: "Noch offen", className: "bg-ink-600" },
];

/** Same rule as the backend: forcing in bulk leaves files that are AV1 already alone. */
export const FORCEABLE: SeriesBucket[] = ["pending", "excluded", "failed", "other"];

type Pickable = { bucket: SeriesBucket; state: string };

export const share = (t: SeriesTally) => (t.episodes ? t.in_av1 / t.episodes : 0);

/** Files a bulk action takes: candidates, or with ``force`` everything forceable. */
export const pick = <T extends Pickable>(files: T[], force: boolean): T[] =>
  files.filter((f) =>
    force ? FORCEABLE.includes(f.bucket) && f.state !== "missing" : f.bucket === "pending",
  );

export const countFor = (files: Pickable[], force: boolean) => pick(files, force).length;

/** Why a file is where it is - only worth a line when it is excluded or failed. */
export const fileReason = (f: { bucket: SeriesBucket; error: string; decision_reason: string }) =>
  f.bucket === "failed" ? f.error : f.bucket === "excluded" ? f.decision_reason : "";

export function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "save";
}) {
  return (
    <div className="panel px-4 py-3">
      <p className="text-xs text-ink-400">{label}</p>
      <p
        className={cn(
          "mt-1 text-xl font-semibold tracking-tight",
          tone === "save" ? "text-save-400" : "text-ink-100",
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-[11px] text-ink-500">{hint}</p>}
    </div>
  );
}

export function Legend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-ink-800/80 px-5 py-2.5 text-[11px] text-ink-400">
      {BUCKETS.map((b) => (
        <span key={b.key} className="flex items-center gap-1.5">
          <span className={cn("size-2 rounded-full", b.className)} />
          {b.label}
        </span>
      ))}
    </div>
  );
}

export function BucketBar({ tally, className }: { tally: SeriesTally; className?: string }) {
  const title = BUCKETS.filter((b) => tally.counts[b.key])
    .map((b) => `${b.label}: ${tally.counts[b.key]}`)
    .join(" · ");
  return (
    <div className={cn("flex h-2 overflow-hidden rounded-full bg-ink-800", className)} title={title}>
      {tally.episodes > 0 &&
        BUCKETS.map((b) =>
          tally.counts[b.key] ? (
            <div
              key={b.key}
              className={b.className}
              style={{ width: `${(tally.counts[b.key] / tally.episodes) * 100}%` }}
            />
          ) : null,
        )}
    </div>
  );
}

export function IconButton({
  title,
  onClick,
  disabled,
  tone = "brand",
  children,
}: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  tone?: "brand" | "warn";
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "rounded-md p-1.5 text-ink-400 transition-colors disabled:cursor-not-allowed disabled:opacity-30",
        tone === "warn"
          ? "hover:bg-warn-500/15 hover:text-warn-400"
          : "hover:bg-brand-600/20 hover:text-brand-400",
      )}
    >
      {children}
    </button>
  );
}

/** Queue a candidate, or force anything else that is not done, on its way or gone. */
export function FileAction({
  file,
  busy,
  onQueue,
  onForce,
}: {
  file: Pickable;
  busy: boolean;
  onQueue: () => void;
  onForce: () => void;
}) {
  if (file.bucket === "pending") {
    return (
      <IconButton title="Zur Warteschlange hinzufuegen" disabled={busy} onClick={onQueue}>
        <Play className="size-3.5" />
      </IconButton>
    );
  }
  const forceable =
    (FORCEABLE.includes(file.bucket) || file.bucket === "av1") && file.state !== "missing";
  if (!forceable) return null;
  return (
    <IconButton
      tone="warn"
      title={
        file.bucket === "av1" ? "Ist bereits AV1 - trotzdem neu kodieren" : "Trotz Ausschluss konvertieren"
      }
      disabled={busy}
      onClick={onForce}
    >
      <Zap className="size-3.5" />
    </IconButton>
  );
}

export function ForceConfirm({
  open,
  count,
  noun,
  subtitle,
  pending,
  onClose,
  onConfirm,
}: {
  open: boolean;
  count: number;
  /** Plural, e.g. "Folgen" or "Dateien". */
  noun: string;
  subtitle?: string;
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Trotz Ausschluss konvertieren?"
      subtitle={subtitle}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Abbrechen
          </button>
          <button className="btn-primary" disabled={pending} onClick={onConfirm}>
            {pending ? <Spinner className="size-4" /> : <Zap className="size-4" />}
            {count} {noun} einreihen
          </button>
        </>
      }
    >
      <div className="space-y-3 text-sm leading-relaxed text-ink-300">
        <p>
          {count} {noun} kommen in die Warteschlange - auch solche, deren Codec ausgeschlossen ist,
          die ignoriert werden oder die als nicht lohnend eingestuft wurden.
        </p>
        <ul className="list-disc space-y-1 pl-5 text-ink-400">
          <li>
            {noun}, die schon AV1 sind, bleiben unberuehrt. Einzelne lassen sich in ihrer Zeile
            trotzdem erzwingen.
          </li>
          <li>
            Die Mindestersparnis gilt fuer diese Jobs nicht. Waere ein Ergebnis groesser als das
            Original, wird es wie gewohnt verworfen.
          </li>
          <li>
            {noun}, die vor der Analyse ausgeschlossen wurden, bekommen ihren Plan erst beim Start -
            mit den Werten des Profils statt einer Testkodierung.
          </li>
        </ul>
      </div>
    </Modal>
  );
}
