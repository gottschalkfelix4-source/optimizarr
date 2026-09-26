import { afterEach, describe, expect, it } from "vitest";
import {
  blockedKind,
  bytes,
  clampPage,
  hdrChip,
  hdrLabel,
  humanDuration,
  ignoreAction,
  IGNORABLE_STATES,
  joinParts,
  nextScanText,
  parseLocalDate,
  relativeTime,
  resolutionLabel,
  setSizeUnit,
  sizeChange,
} from "./format";
import type { FileState, QueueStatus } from "./api";

afterEach(() => setSizeUnit("binary"));

describe("bytes", () => {
  it("uses binary units by default", () => {
    expect(bytes(1536)).toBe("1,5 KiB");
    expect(bytes(5 * 1024 ** 3)).toBe("5,0 GiB");
  });

  it("uses decimal units when asked", () => {
    expect(bytes(1500, 1, "decimal")).toBe("1,5 kB");
    expect(bytes(5e9, 1, "decimal")).toBe("5,0 GB");
  });

  it("follows the global unit from ui.size_unit", () => {
    setSizeUnit("decimal");
    expect(bytes(2_000_000)).toBe("2,0 MB");
    setSizeUnit("binary");
    expect(bytes(2 * 1024 * 1024)).toBe("2,0 MiB");
  });

  it("treats empty and invalid input as 0 B", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(-5)).toBe("0 B");
    expect(bytes(null)).toBe("0 B");
    expect(bytes(Number.NaN)).toBe("0 B");
  });
});

describe("resolutionLabel (same classes as the backend)", () => {
  it.each([
    [3840, 2160, "2160p"],
    [3840, 1600, "2160p"], // letterboxed UHD
    [0, 1800, "2160p"],
    [2560, 1440, "1440p"],
    [1920, 1080, "1080p"],
    [1920, 800, "1080p"], // cinemascope 1080p
    [1440, 1080, "1080p"], // 4:3 pillarbox
    [1280, 720, "720p"],
    [1280, 536, "720p"],
    [720, 576, "SD"],
    [640, 480, "SD"],
  ])("%ix%i -> %s", (w, h, label) => {
    expect(resolutionLabel(w, h)).toBe(label);
  });

  it("has no class without dimensions", () => {
    expect(resolutionLabel(0, 0)).toBe("-");
  });
});

describe("hdrLabel", () => {
  it("names Dolby Vision profiles", () => {
    expect(hdrLabel("dolby_vision_p5")).toBe("Dolby Vision P5");
    expect(hdrLabel("dolby_vision_p8")).toBe("Dolby Vision P8");
    expect(hdrLabel("dolby_vision")).toBe("Dolby Vision");
    expect(hdrChip("dolby_vision_p7")).toBe("DV P7");
  });

  it("names the other HDR formats", () => {
    expect(hdrLabel("hdr10")).toBe("HDR10");
    expect(hdrLabel("hdr10plus")).toBe("HDR10+");
    expect(hdrLabel("hlg")).toBe("HLG");
  });
});

describe("durations", () => {
  it("writes days out", () => {
    expect(humanDuration(86400 + 3600 * 3)).toBe("1 Tag 3 Std");
    expect(humanDuration(3 * 86400)).toBe("3 Tage 0 Std");
  });

  it("says Tagen in relative times", () => {
    const now = Date.parse("2026-09-26T12:00:00Z");
    expect(relativeTime("2026-09-23T12:00:00Z", now)).toBe("vor 3 Tagen");
    expect(relativeTime("2026-09-25T12:00:00Z", now)).toBe("vor 1 Tag");
  });

  it("announces the next scan or says it is overdue", () => {
    const now = Date.parse("2026-09-26T12:00:00Z");
    expect(nextScanText("2026-09-26T14:30:00Z", now)).toBe("Nächster Scan in 2 Std 30 Min");
    expect(nextScanText("2026-09-26T11:00:00Z", now)).toBe("Nächster Scan überfällig");
  });
});

describe("parseLocalDate", () => {
  it("keeps the calendar day in every time zone", () => {
    const d = parseLocalDate("2026-09-20");
    expect(d.getFullYear()).toBe(2026);
    expect(d.getMonth()).toBe(8);
    expect(d.getDate()).toBe(20);
    expect(d.getHours()).toBe(0);
  });
});

describe("sizeChange", () => {
  it("shows a shrink with a minus", () => {
    const c = sizeChange(10 * 1024 ** 3, 6 * 1024 ** 3);
    expect(c).toEqual({ text: "-4,0 GiB (-40 %)", smaller: true });
  });

  it("shows growth with a plus, not as a double minus", () => {
    const c = sizeChange(1000 * 1024 ** 2, 1100 * 1024 ** 2);
    expect(c?.text).toBe("+100,0 MiB (+10 %)");
    expect(c?.smaller).toBe(false);
  });

  it("does not divide by zero", () => {
    expect(sizeChange(0, 500)).toBeNull();
    expect(sizeChange(500, 0)).toBeNull();
  });
});

describe("clampPage", () => {
  it("keeps a valid page", () => expect(clampPage(3, 5)).toBe(3));
  it("steps back when the last page vanished", () => expect(clampPage(7, 4)).toBe(4));
  it("never goes below 1", () => {
    expect(clampPage(0, 3)).toBe(1);
    expect(clampPage(2, 0)).toBe(1);
    expect(clampPage(Number.NaN, 3)).toBe(1);
  });
});

describe("joinParts", () => {
  it("skips empty plan fields of forced jobs", () => {
    expect(joinParts("1080p", undefined, null)).toBe("1080p");
    expect(joinParts(undefined, "", null)).toBe("");
    expect(joinParts("1080p", "av1_vaapi", "CRF 30")).toBe("1080p · av1_vaapi · CRF 30");
  });
});

describe("blockedKind", () => {
  const base: QueueStatus = {
    running_jobs: [],
    paused: false,
    schedule_ok: true,
    blocked_reason: "",
    max_concurrent: 1,
  };

  it("uses blocked_kind when the server sends it", () => {
    expect(blockedKind({ ...base, blocked_kind: "disk", blocked_reason: "Zu wenig Platz" })).toBe(
      "disk",
    );
    expect(blockedKind({ ...base, blocked_kind: "schedule", schedule_ok: false })).toBe("schedule");
    expect(blockedKind({ ...base, blocked_kind: "" })).toBe("");
  });

  it("does not call a full disk a schedule problem", () => {
    // Old UI logic appended "sobald das Zeitfenster erreicht ist" to any reason.
    expect(
      blockedKind({ ...base, blocked_kind: "disk", schedule_ok: true, blocked_reason: "x" }),
    ).not.toBe("schedule");
  });

  it("falls back for backends without blocked_kind", () => {
    expect(blockedKind({ ...base, paused: true })).toBe("paused");
    expect(blockedKind({ ...base, schedule_ok: false, blocked_reason: "Außerhalb" })).toBe("schedule");
    expect(blockedKind({ ...base, blocked_reason: "Scan läuft" })).toBe("other");
    expect(blockedKind(base)).toBe("");
  });
});

describe("ignore button visibility", () => {
  const all: FileState[] = [
    "new", "probed", "analyzing", "candidate", "skipped", "queued",
    "encoding", "done", "failed", "missing", "ignored",
  ];

  it("is offered exactly in the states the server allows", () => {
    const allowed = all.filter((s) => ignoreAction({ state: s, ignored: false }) !== null);
    expect(allowed.sort()).toEqual(
      ["candidate", "failed", "ignored", "missing", "new", "probed", "skipped"].sort(),
    );
    expect([...IGNORABLE_STATES].sort()).toEqual(allowed.sort());
  });

  it("is hidden while a file is queued, encoding, analysing or done", () => {
    for (const state of ["queued", "encoding", "analyzing", "done"] as FileState[]) {
      expect(ignoreAction({ state, ignored: false })).toBeNull();
    }
  });

  it("distinguishes ignoring from un-ignoring", () => {
    expect(ignoreAction({ state: "candidate", ignored: false })).toEqual({
      ignored: true,
      label: "Datei ignorieren",
    });
    expect(ignoreAction({ state: "ignored", ignored: true })).toEqual({
      ignored: false,
      label: "Nicht mehr ignorieren",
    });
  });
});
