/** Pure helpers for the settings draft: merging server changes, building patches. */
import type { Settings, SettingsPatch } from "./api";

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Three-way merge: apply the edits made on top of ``base`` to ``next``.
 *
 * Fields the draft did not touch take the new server value; real edits
 * survive.  Where both sides changed the same field, the draft wins unless
 * ``preferNext`` is set (applying a profile must override those fields).
 * Arrays are values, not merged element by element. */
export function rebase<T>(base: T, draft: T, next: T, preferNext = false): T {
  if (same(draft, base)) return structuredClone(next);
  if (!isObject(draft) || !isObject(base) || !isObject(next)) {
    if (preferNext && !same(next, base)) return structuredClone(next);
    return draft;
  }
  const out: Record<string, unknown> = { ...next };
  for (const key of Object.keys(draft)) {
    out[key] = key in next ? rebase(base[key], draft[key], next[key], preferNext) : draft[key];
  }
  return out as T;
}

/** Only what differs from the saved settings, group by group.  The backend
 *  merges deeply, so untouched fields - and untouched masked secrets - are
 *  simply not sent and cannot overwrite anything. */
export function diffSettings(saved: Settings, draft: Settings): SettingsPatch {
  const patch: Record<string, Record<string, unknown>> = {};
  for (const group of Object.keys(draft) as (keyof Settings)[]) {
    const before = (saved[group] ?? {}) as Record<string, unknown>;
    const after = draft[group] as Record<string, unknown>;
    if (!isObject(after)) continue;
    for (const key of Object.keys(after)) {
      if (!same(before[key], after[key])) {
        (patch[group] ??= {})[key] = after[key];
      }
    }
  }
  return patch as SettingsPatch;
}

/** Defaults for groups and fields an older server may not send yet, so the
 *  page never reads a property of ``undefined``. */
const FALLBACKS: Partial<Record<keyof Settings, Record<string, unknown>>> = {
  analysis: { dolby_vision: "skip" },
  output: { verify_full_decode: false, min_quality_samples: 2 },
  notifications: {
    webhook_url: "",
    notify_on_job_done: false,
    notify_on_job_failed: true,
    notify_on_scan_done: false,
  },
  security: { auth_enabled: false, username: "admin", password: "" },
  ui: { size_unit: "binary", dashboard_refresh_seconds: 3 },
  maintenance: { history_retention_days: 90, job_retention_days: 365, scan_retention_days: 90, restored_manifest_retention_days: 180, max_learning_samples: 10000, max_backups: 7 },
};

export function normalizeSettings(settings: Settings): Settings {
  const out = { ...settings } as Record<string, Record<string, unknown>>;
  for (const [group, defaults] of Object.entries(FALLBACKS)) {
    out[group] = { ...defaults, ...(out[group] ?? {}) };
  }
  return out as unknown as Settings;
}

/** "/media/movies/ " -> "/media/movies"; "/" stays "/". */
export function normalizeDirPath(path: string): string {
  const trimmed = path.trim();
  if (!trimmed) return "";
  const stripped = trimmed.replace(/\/+$/, "");
  return stripped === "" ? "/" : stripped;
}

/** The folder picker may only confirm a folder the server has just listed -
 *  not a stale listing of the previous input and not a path that 404'd. */
export function canUseBrowsedPath(
  typed: string,
  listed: string | undefined,
  state: { fetching: boolean; error: boolean },
): boolean {
  if (!listed || state.fetching || state.error) return false;
  const want = normalizeDirPath(typed);
  return want !== "" && want === normalizeDirPath(listed);
}

/** Toggle a weekday (0 = Monday) but never leave the schedule without a day:
 *  an empty list would silently mean "never convert".  Returns null when the
 *  change is refused. */
export function toggleWeekday(days: number[], day: number): number[] | null {
  const set = new Set(days);
  if (set.has(day)) {
    if (set.size <= 1) return null;
    set.delete(day);
  } else {
    set.add(day);
  }
  return [...set].sort((a, b) => a - b);
}
