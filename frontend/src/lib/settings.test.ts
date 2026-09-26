import { describe, expect, it } from "vitest";
import { SECRET_MASK, type Settings } from "./api";
import {
  canUseBrowsedPath,
  diffSettings,
  normalizeDirPath,
  normalizeSettings,
  rebase,
  toggleWeekday,
} from "./settings";

type Mini = { queue: { paused: boolean; days: number[] }; library: { size: number; name: string } };

const base: Mini = { queue: { paused: false, days: [0, 1] }, library: { size: 50, name: "a" } };

describe("rebase", () => {
  it("takes the new server state when nothing was edited", () => {
    const next = { ...base, queue: { ...base.queue, paused: true } };
    expect(rebase(base, structuredClone(base), next)).toEqual(next);
  });

  it("keeps edits and takes server changes in untouched fields", () => {
    const draft = { ...base, library: { ...base.library, size: 70 } };
    const next = { ...base, queue: { ...base.queue, paused: true } };
    expect(rebase(base, draft, next)).toEqual({
      queue: { paused: true, days: [0, 1] },
      library: { size: 70, name: "a" },
    });
  });

  it("treats arrays as values", () => {
    const draft = { ...base, queue: { ...base.queue, days: [0, 1, 2] } };
    const next = { ...base, queue: { ...base.queue, days: [5] } };
    expect(rebase(base, draft, next).queue.days).toEqual([0, 1, 2]);
  });

  it("lets the server win conflicts when asked (applying a profile)", () => {
    const draft = { ...base, library: { size: 70, name: "b" } };
    const next = { ...base, library: { size: 30, name: "a" } };
    expect(rebase(base, draft, next, true).library).toEqual({ size: 30, name: "b" });
    expect(rebase(base, draft, next, false).library).toEqual({ size: 70, name: "b" });
  });

  it("with the new state as base keeps a stale value - why the old base must be captured", () => {
    // This is what happened when the updater read base.current after it had
    // already been moved on: the draft's stale "paused: false" won.
    const draft = { ...base, library: { ...base.library, size: 70 } };
    const next = { ...base, queue: { ...base.queue, paused: true } };
    expect(rebase(next, draft, next).queue.paused).toBe(false);
    expect(rebase(base, draft, next).queue.paused).toBe(true);
  });
});

describe("diffSettings", () => {
  const saved = {
    library: { min_file_size_mb: 50, extensions: ["mkv"] },
    advisor: { api_key: SECRET_MASK, model: "claude-opus-5" },
    queue: { paused: false },
  } as unknown as Settings;

  it("sends nothing when nothing changed", () => {
    expect(diffSettings(saved, structuredClone(saved))).toEqual({});
  });

  it("sends only the changed fields, grouped", () => {
    const draft = structuredClone(saved);
    draft.library.min_file_size_mb = 70;
    draft.library.extensions = ["mkv", "mp4"];
    expect(diffSettings(saved, draft)).toEqual({
      library: { min_file_size_mb: 70, extensions: ["mkv", "mp4"] },
    });
  });

  it("leaves an untouched masked secret out and sends a cleared one", () => {
    const draft = structuredClone(saved);
    draft.advisor.model = "claude-opus-5-5";
    expect(diffSettings(saved, draft)).toEqual({ advisor: { model: "claude-opus-5-5" } });
    draft.advisor.api_key = "";
    expect(diffSettings(saved, draft)).toEqual({
      advisor: { model: "claude-opus-5-5", api_key: "" },
    });
  });
});

describe("normalizeSettings", () => {
  it("fills groups an older server does not send", () => {
    const old = { library: { min_file_size_mb: 50 }, ui: { size_unit: "decimal" } } as unknown as Settings;
    const n = normalizeSettings(old);
    expect(n.security).toEqual({ auth_enabled: false, username: "admin", password: "" });
    expect(n.analysis.dolby_vision).toBe("skip");
    expect(n.ui).toEqual({ size_unit: "decimal", dashboard_refresh_seconds: 3 });
    // Normalising both sides keeps the diff empty.
    expect(diffSettings(n, normalizeSettings(old))).toEqual({});
  });
});

describe("toggleWeekday", () => {
  it("adds and removes days in order", () => {
    expect(toggleWeekday([3, 1], 0)).toEqual([0, 1, 3]);
    expect(toggleWeekday([0, 1, 3], 1)).toEqual([0, 3]);
  });

  it("refuses to remove the last day", () => {
    expect(toggleWeekday([4], 4)).toBeNull();
  });

  it("sorts numerically", () => {
    expect(toggleWeekday([6, 10], 2)).toEqual([2, 6, 10]);
  });
});

describe("directory picker", () => {
  it("normalises paths", () => {
    expect(normalizeDirPath(" /media/movies/ ")).toBe("/media/movies");
    expect(normalizeDirPath("/")).toBe("/");
    expect(normalizeDirPath("///")).toBe("/");
    expect(normalizeDirPath("")).toBe("");
  });

  it("only allows the folder the server actually listed", () => {
    const ok = { fetching: false, error: false };
    expect(canUseBrowsedPath("/media/movies", "/media/movies", ok)).toBe(true);
    expect(canUseBrowsedPath("/media/movies/", "/media/movies", ok)).toBe(true);
    // Stale listing of the previous input.
    expect(canUseBrowsedPath("/media/movies", "/media", ok)).toBe(false);
    // Still loading, or the path does not exist.
    expect(canUseBrowsedPath("/media", "/media", { fetching: true, error: false })).toBe(false);
    expect(canUseBrowsedPath("/nope", "/nope", { fetching: false, error: true })).toBe(false);
    expect(canUseBrowsedPath("/media", undefined, ok)).toBe(false);
  });
});
