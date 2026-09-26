import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InvalidationBatcher } from "./invalidation";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("InvalidationBatcher", () => {
  it("collects a burst of events into one flush", () => {
    const flush = vi.fn();
    const batcher = new InvalidationBatcher(flush, 1500);
    for (let i = 0; i < 500; i++) batcher.add(["files", "stats"]);
    batcher.add(["jobs"]);
    expect(flush).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1500);
    expect(flush).toHaveBeenCalledTimes(1);
    expect(flush.mock.calls[0][0].sort()).toEqual(["files", "jobs", "stats"]);
  });

  it("throttles rather than debounces: a steady stream still refreshes", () => {
    const flush = vi.fn();
    const batcher = new InvalidationBatcher(flush, 1000);
    for (let t = 0; t < 3500; t += 100) {
      batcher.add(["files"]);
      vi.advanceTimersByTime(100);
    }
    // One flush per window, not zero until the stream stops.
    expect(flush.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(flush.mock.calls.length).toBeLessThanOrEqual(4);
  });

  it("flushes everything after addAll", () => {
    const flush = vi.fn();
    const batcher = new InvalidationBatcher(flush, 500);
    batcher.add(["files"]);
    batcher.addAll();
    vi.advanceTimersByTime(500);
    expect(flush).toHaveBeenCalledWith("all");
    expect(flush).toHaveBeenCalledTimes(1);
  });

  it("does nothing without keys and nothing after dispose", () => {
    const flush = vi.fn();
    const batcher = new InvalidationBatcher(flush, 500);
    batcher.add([]);
    expect(batcher.pending).toBe(false);
    batcher.add(["files"]);
    batcher.dispose();
    vi.advanceTimersByTime(1000);
    expect(flush).not.toHaveBeenCalled();
  });
});
