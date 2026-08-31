/** Small, shared presentational building blocks. */
import { AlertTriangle, CheckCircle2, Info, Loader2, X, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect } from "react";
import { STATE_STYLES } from "../lib/format";
import { useToast } from "../lib/live";

export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* -------------------------------------------------------------------------- */

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn("panel", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-800/80 px-5 py-4">
          <div className="min-w-0">
            {title && <h2 className="text-base font-semibold text-ink-100">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-sm text-ink-400">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn("px-5 py-4", bodyClassName)}>{children}</div>
    </section>
  );
}

export function StateBadge({ state, label }: { state: string; label: string }) {
  return (
    <span className={cn("chip", STATE_STYLES[state] ?? "bg-ink-700/60 text-ink-300")}>{label}</span>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-4 animate-spin", className)} />;
}

export function ProgressBar({
  value,
  className,
  tone = "brand",
  indeterminate = false,
}: {
  value: number;
  className?: string;
  tone?: "brand" | "save" | "warn";
  indeterminate?: boolean;
}) {
  const toneClass =
    tone === "save" ? "bg-save-500" : tone === "warn" ? "bg-warn-500" : "bg-brand-500";
  return (
    <div className={cn("relative h-2 overflow-hidden rounded-full bg-ink-800", className)}>
      {indeterminate ? (
        <div className={cn("shimmer absolute inset-0 overflow-hidden", toneClass, "opacity-40")} />
      ) : (
        <div
          className={cn("h-full rounded-full transition-[width] duration-500 ease-out", toneClass)}
          style={{ width: `${Math.min(100, Math.max(0, value * 100))}%` }}
        />
      )}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      {icon && <div className="text-ink-500">{icon}</div>}
      <div>
        <p className="font-medium text-ink-200">{title}</p>
        {description && (
          <p className="mx-auto mt-1 max-w-md text-sm leading-relaxed text-ink-400">{description}</p>
        )}
      </div>
      {action}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("shimmer relative overflow-hidden rounded bg-ink-800/70", className)} />;
}

/* -------------------------------------------------------------------------- */

export function Modal({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink-950/80 p-4 backdrop-blur-sm sm:p-8">
      <div
        className={cn(
          "panel my-auto w-full shadow-2xl shadow-black/50",
          wide ? "max-w-4xl" : "max-w-xl",
        )}
        role="dialog"
        aria-modal="true"
      >
        <header className="flex items-start justify-between gap-4 border-b border-ink-800 px-5 py-4">
          <div className="min-w-0">
            <h3 className="truncate text-base font-semibold text-ink-100">{title}</h3>
            {subtitle && <p className="mt-0.5 truncate text-sm text-ink-400">{subtitle}</p>}
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-ink-400 transition-colors hover:bg-ink-800 hover:text-ink-100"
            aria-label="Schliessen"
          >
            <X className="size-4" />
          </button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <footer className="flex flex-wrap justify-end gap-2 border-t border-ink-800 px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-3",
        disabled && "cursor-not-allowed opacity-50",
      )}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className={cn(
          "relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors",
          checked ? "bg-brand-600" : "bg-ink-600",
        )}
      >
        <span
          className={cn(
            "absolute left-0.5 top-0.5 size-4 rounded-full bg-white shadow transition-transform",
            checked ? "translate-x-4" : "translate-x-0",
          )}
        />
      </button>
      <span className="min-w-0">
        <span className="label block">{label}</span>
        {hint && <span className="hint block">{hint}</span>}
      </span>
    </label>
  );
}

export function Field({
  label,
  hint,
  children,
  htmlFor,
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div>
      <label className="label block" htmlFor={htmlFor}>
        {label}
      </label>
      <div className="mt-1.5">{children}</div>
      {hint && <p className="hint">{hint}</p>}
    </div>
  );
}

export function NumberField({
  value,
  onChange,
  min,
  max,
  step = 1,
  suffix,
  id,
}: {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  id?: string;
}) {
  return (
    <div className="relative">
      <input
        id={id}
        type="number"
        className="field pr-14"
        value={Number.isFinite(value) ? value : 0}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const next = e.target.value === "" ? 0 : Number(e.target.value);
          if (Number.isFinite(next)) onChange(next);
        }}
      />
      {suffix && (
        <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-ink-400">
          {suffix}
        </span>
      )}
    </div>
  );
}

export function SliderField({
  value,
  onChange,
  min,
  max,
  step = 1,
  format,
  marks,
}: {
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  format?: (value: number) => string;
  marks?: { value: number; label: string }[];
}) {
  return (
    <div>
      <div className="flex items-center gap-3">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-ink-700
                     [&::-webkit-slider-thumb]:size-4 [&::-webkit-slider-thumb]:appearance-none
                     [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-brand-500
                     [&::-webkit-slider-thumb]:shadow-md [&::-moz-range-thumb]:size-4
                     [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0
                     [&::-moz-range-thumb]:bg-brand-500"
        />
        <span className="w-20 shrink-0 text-right font-mono text-sm text-ink-100">
          {format ? format(value) : value}
        </span>
      </div>
      {marks && (
        <div className="mt-1.5 flex justify-between text-[11px] text-ink-500">
          {marks.map((m) => (
            <span key={m.value}>{m.label}</span>
          ))}
        </div>
      )}
    </div>
  );
}

export function Select<T extends string>({
  value,
  onChange,
  options,
  id,
}: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  id?: string;
}) {
  return (
    <select
      id={id}
      className="field appearance-none bg-[length:1rem] bg-[right_0.6rem_center] bg-no-repeat pr-9"
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%236b7ba0' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
      }}
      value={value}
      onChange={(e) => onChange(e.target.value as T)}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value} className="bg-ink-850">
          {o.label}
        </option>
      ))}
    </select>
  );
}

/** Comma-separated list editor, used for languages and file extensions. */
export function TagListField({
  values,
  onChange,
  placeholder,
  id,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  id?: string;
}) {
  return (
    <input
      id={id}
      className="field"
      placeholder={placeholder}
      value={values.join(", ")}
      onChange={(e) =>
        onChange(
          e.target.value
            .split(",")
            .map((v) => v.trim())
            .filter(Boolean),
        )
      }
    />
  );
}

/* -------------------------------------------------------------------------- */

export function Toaster() {
  const { toasts, dismiss } = useToast();
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-full max-w-sm flex-col gap-2">
      {toasts.map((toast) => {
        const Icon =
          toast.tone === "success" ? CheckCircle2 : toast.tone === "error" ? XCircle : Info;
        const tone =
          toast.tone === "success"
            ? "border-save-500/40 text-save-400"
            : toast.tone === "error"
              ? "border-danger-500/40 text-danger-400"
              : "border-ink-600 text-info-400";
        return (
          <div
            key={toast.id}
            className={cn(
              "panel pointer-events-auto flex items-start gap-3 px-4 py-3 shadow-xl shadow-black/40",
              tone,
            )}
          >
            <Icon className="mt-0.5 size-4 shrink-0" />
            <p className="flex-1 text-sm leading-snug text-ink-100">{toast.message}</p>
            <button
              onClick={() => dismiss(toast.id)}
              className="text-ink-500 transition-colors hover:text-ink-200"
              aria-label="Schliessen"
            >
              <X className="size-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}

export function Callout({
  tone = "info",
  children,
  icon,
}: {
  tone?: "info" | "warn" | "danger" | "success";
  children: ReactNode;
  icon?: ReactNode;
}) {
  const styles = {
    info: "border-info-500/30 bg-info-500/8 text-info-400",
    warn: "border-warn-500/30 bg-warn-500/8 text-warn-400",
    danger: "border-danger-500/30 bg-danger-500/8 text-danger-400",
    success: "border-save-500/30 bg-save-500/8 text-save-400",
  }[tone];
  const DefaultIcon = tone === "info" ? Info : tone === "success" ? CheckCircle2 : AlertTriangle;
  return (
    <div className={cn("flex items-start gap-3 rounded-lg border px-4 py-3", styles)}>
      <span className="mt-0.5 shrink-0">{icon ?? <DefaultIcon className="size-4" />}</span>
      <div className="min-w-0 flex-1 text-sm leading-relaxed text-ink-200">{children}</div>
    </div>
  );
}
