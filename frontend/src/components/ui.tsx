/** Small, shared presentational building blocks. */
import { AlertTriangle, CheckCircle2, Info, Loader2, RotateCcw, X, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useId, useRef, useState } from "react";
import { SECRET_MASK } from "../lib/api";
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
          {actions && <div className="flex max-w-full flex-wrap items-center gap-2">{actions}</div>}
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
  return <Loader2 className={cn("size-4 animate-spin", className)} aria-hidden="true" />;
}

export function ProgressBar({
  value,
  className,
  tone = "brand",
  indeterminate = false,
  smooth = false,
}: {
  value: number;
  className?: string;
  tone?: "brand" | "save" | "warn";
  indeterminate?: boolean;
  /** For values updated many times a second: a short linear transition, so the
   *  bar glides instead of easing into every tiny step. */
  smooth?: boolean;
}) {
  const toneClass =
    tone === "save" ? "bg-save-500" : tone === "warn" ? "bg-warn-500" : "bg-brand-500";
  const safe = Number.isFinite(value) ? value : 0;
  return (
    <div className={cn("relative h-2 overflow-hidden rounded-full bg-ink-800", className)}>
      {indeterminate ? (
        <div className={cn("shimmer absolute inset-0 overflow-hidden", toneClass, "opacity-40")} />
      ) : (
        <div
          className={cn(
            "h-full rounded-full",
            smooth
              ? "transition-[width] duration-150 ease-linear"
              : "transition-[width] duration-500 ease-out",
            toneClass,
          )}
          style={{ width: `${Math.min(100, Math.max(0, safe * 100))}%` }}
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

/** A query that failed - says so and offers a retry instead of looking empty. */
export function ErrorState({
  error,
  onRetry,
  title = "Laden fehlgeschlagen",
  compact = false,
}: {
  error: unknown;
  onRetry: () => void;
  title?: string;
  compact?: boolean;
}) {
  const message = error instanceof Error ? error.message : error ? String(error) : "";
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center gap-3 px-6 text-center",
        compact ? "py-6" : "py-14",
      )}
    >
      <AlertTriangle className="size-8 text-danger-400" aria-hidden="true" />
      <div>
        <p className="font-medium text-ink-200">{title}</p>
        {message && (
          <p className="mx-auto mt-1 max-w-md text-sm leading-relaxed text-ink-400">{message}</p>
        )}
      </div>
      <button className="btn-ghost btn-sm" onClick={onRetry}>
        <RotateCcw className="size-3.5" aria-hidden="true" />
        Erneut versuchen
      </button>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), summary';

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.closest("[inert]") && el.getAttribute("aria-hidden") !== "true",
  );
}

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
  const dialogRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const subtitleId = useId();
  // Callers pass inline arrows; keeping the latest one in a ref stops the
  // effect below from re-running - and re-focusing - on every render.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previous = document.activeElement as HTMLElement | null;

    // Initial focus: an explicit [data-autofocus], else the first control in
    // the body, else the dialog itself.
    const initial =
      dialog.querySelector<HTMLElement>("[data-autofocus]") ??
      (bodyRef.current ? focusables(bodyRef.current)[0] : undefined) ??
      dialog;
    initial.focus({ preventScroll: true });

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusables(dialog);
      if (!items.length) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (!active || !dialog.contains(active)) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && (active === first || active === dialog)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      if (previous && document.contains(previous)) previous.focus({ preventScroll: true });
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink-950/80 p-4 backdrop-blur-sm sm:p-8">
      <div
        ref={dialogRef}
        className={cn(
          "panel my-auto w-full shadow-2xl shadow-black/50 outline-none",
          wide ? "max-w-4xl" : "max-w-xl",
        )}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={subtitle ? subtitleId : undefined}
        tabIndex={-1}
      >
        <header className="flex items-start justify-between gap-4 border-b border-ink-800 px-5 py-4">
          <div className="min-w-0">
            <h3 id={titleId} className="truncate text-base font-semibold text-ink-100">
              {title}
            </h3>
            {subtitle && (
              <p id={subtitleId} className="mt-0.5 truncate text-sm text-ink-400">
                {subtitle}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-ink-400 transition-colors hover:bg-ink-800 hover:text-ink-100"
            aria-label="Schließen"
            title="Schließen"
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        </header>
        <div ref={bodyRef} className="max-h-[70vh] overflow-y-auto px-5 py-4">
          {children}
        </div>
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

/** Parse what someone typed: German decimal comma allowed, "" is not 0. */
export function parseNumberInput(text: string): number | null {
  const t = text.trim().replace(",", ".");
  if (t === "" || t === "-" || t === "." || t === "-.") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** Round to whole numbers for integer steps and keep within min/max. */
export function clampNumber(
  value: number,
  { min, max, integer }: { min?: number; max?: number; integer?: boolean },
): number {
  let v = integer ? Math.round(value) : value;
  if (min !== undefined) v = Math.max(min, v);
  if (max !== undefined) v = Math.min(max, v);
  return v;
}

const showNumber = (value: number) =>
  Number.isFinite(value) ? String(value).replace(".", ",") : "";

/** Numeric input that keeps what is typed until it makes sense.
 *
 * A plain controlled ``type=number`` turned an emptied field into 0 and made a
 * leading minus impossible to type.  Here the raw text stays local, valid
 * in-range values are passed on while typing, and leaving the field clamps to
 * min/max (or restores the last value when it is empty). */
export function NumberField({
  value,
  onChange,
  min,
  max,
  step = 1,
  suffix,
  id,
  ariaLabel,
}: {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  id?: string;
  ariaLabel?: string;
}) {
  const [text, setText] = useState(showNumber(value));
  const [focused, setFocused] = useState(false);
  const hintId = useId();
  const integer = Number.isInteger(step);
  const bounds = { min, max, integer };

  useEffect(() => {
    if (!focused) setText(showNumber(value));
  }, [value, focused]);

  const parsed = parseNumberInput(text);
  const invalid = parsed === null || clampNumber(parsed, bounds) !== parsed;

  const commit = () => {
    const next = parsed === null ? value : clampNumber(parsed, bounds);
    if (next !== value) onChange(next);
    setText(showNumber(next));
  };

  const range =
    min !== undefined && max !== undefined
      ? `Erlaubt: ${showNumber(min)} bis ${showNumber(max)}`
      : min !== undefined
        ? `Erlaubt: ab ${showNumber(min)}`
        : max !== undefined
          ? `Erlaubt: bis ${showNumber(max)}`
          : "";

  return (
    <div>
      <div className="relative">
        <input
          id={id}
          type="text"
          inputMode={(min ?? 0) >= 0 ? (integer ? "numeric" : "decimal") : "text"}
          className={cn("field pr-14", focused && invalid && "border-danger-500/70")}
          value={text}
          aria-label={ariaLabel}
          aria-invalid={focused && invalid ? true : undefined}
          aria-describedby={focused && invalid ? hintId : undefined}
          onFocus={() => setFocused(true)}
          onChange={(e) => {
            const next = e.target.value;
            if (!/^\s*-?\d*(?:[.,]\d*)?\s*$/.test(next)) return;
            setText(next);
            const n = parseNumberInput(next);
            if (n !== null && clampNumber(n, bounds) === n && n !== value) onChange(n);
          }}
          onBlur={() => {
            setFocused(false);
            commit();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
        />
        {suffix && (
          <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-ink-400">
            {suffix}
          </span>
        )}
      </div>
      {focused && invalid && (
        <p id={hintId} className="mt-1 text-[11px] text-danger-400">
          {parsed === null
            ? "Bitte eine Zahl eingeben."
            : integer && !Number.isInteger(parsed)
              ? `Nur ganze Zahlen. ${range}`.trim()
              : range}
        </p>
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
  ariaLabel,
}: {
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  format?: (value: number) => string;
  marks?: { value: number; label: string }[];
  ariaLabel?: string;
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
          aria-label={ariaLabel}
          aria-valuetext={format ? format(value) : undefined}
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
  ariaLabel,
}: {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  id?: string;
  ariaLabel?: string;
}) {
  return (
    <select
      id={id}
      aria-label={ariaLabel}
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

/** Options plus the current value if it is not among them - a stored value the
 *  list does not know must stay visible instead of silently showing option 1. */
export function withCurrent<T extends string>(
  options: { value: T; label: string }[],
  current: T,
  label: (value: T) => string = (v) => `${v} (gespeichert)`,
): { value: T; label: string }[] {
  if (!current || options.some((o) => o.value === current)) return options;
  return [...options, { value: current, label: label(current) }];
}

/** "a, b c ,, d" -> ["a", "b c", "d"].  Only commas separate entries, so
 *  patterns with spaces ("*\/Season 1\/*") stay whole. */
export function parseTagList(text: string): string[] {
  return text
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

/** Comma-separated list editor, used for languages, extensions and patterns.
 *
 * The raw text is kept while typing and only parsed on blur or Enter -
 * parsing on every keystroke swallowed the comma and the space before the next
 * entry could be typed. */
export function TagListField({
  values,
  onChange,
  placeholder,
  id,
  ariaLabel,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  id?: string;
  ariaLabel?: string;
}) {
  const joined = values.join(", ");
  const [text, setText] = useState(joined);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!editing) setText(joined);
  }, [joined, editing]);

  const commit = () => {
    const parsed = parseTagList(text);
    if (parsed.length !== values.length || parsed.some((v, i) => v !== values[i])) {
      onChange(parsed);
    }
    setText(parsed.join(", "));
  };

  return (
    <input
      id={id}
      className="field"
      placeholder={placeholder}
      aria-label={ariaLabel}
      value={text}
      onFocus={() => setEditing(true)}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        setEditing(false);
        commit();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
        }
      }}
    />
  );
}

/** Input for a secret the server only ever returns masked.
 *
 * ``SECRET_MASK`` in the draft means "keep what is stored": the field stays
 * empty and says so.  Typing replaces the secret; "Entfernen" sets it to "",
 * which deletes it on save. */
export function SecretField({
  value,
  stored,
  onChange,
  placeholder,
  id,
  type = "password",
  ariaLabel,
}: {
  value: string;
  /** Whether the server has a value stored right now. */
  stored: boolean;
  onChange: (value: string) => void;
  placeholder?: string;
  id?: string;
  type?: "password" | "text";
  ariaLabel?: string;
}) {
  const keep = value === SECRET_MASK;
  const clearing = stored && value === "";
  return (
    <div className="flex min-w-0 flex-1 gap-2">
      <input
        id={id}
        type={type}
        className="field font-mono text-sm"
        value={keep ? "" : value}
        aria-label={ariaLabel}
        onChange={(e) => {
          const next = e.target.value;
          // Emptying a fresh entry goes back to "keep the stored one".
          onChange(next === "" && stored ? SECRET_MASK : next);
        }}
        placeholder={
          keep
            ? "Gespeichert – leer lassen, um ihn zu behalten"
            : clearing
              ? "Wird beim Speichern entfernt"
              : placeholder
        }
        autoComplete={type === "password" ? "new-password" : "off"}
        spellCheck={false}
      />
      {stored &&
        (clearing ? (
          <button type="button" className="btn-ghost shrink-0" onClick={() => onChange(SECRET_MASK)}>
            Behalten
          </button>
        ) : (
          <button type="button" className="btn-ghost shrink-0" onClick={() => onChange("")}>
            Entfernen
          </button>
        ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

export function Toaster() {
  const { toasts, dismiss } = useToast();
  return (
    <div
      className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-full max-w-sm flex-col gap-2"
      role="status"
      aria-live="polite"
    >
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
            <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <p className="flex-1 text-sm leading-snug text-ink-100">{toast.message}</p>
            <button
              onClick={() => dismiss(toast.id)}
              className="text-ink-500 transition-colors hover:text-ink-200"
              aria-label="Meldung schließen"
            >
              <X className="size-3.5" aria-hidden="true" />
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
      <span className="mt-0.5 shrink-0" aria-hidden="true">
        {icon ?? <DefaultIcon className="size-4" />}
      </span>
      <div className="min-w-0 flex-1 text-sm leading-relaxed text-ink-200">{children}</div>
    </div>
  );
}
