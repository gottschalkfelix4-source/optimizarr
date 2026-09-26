import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveProvider } from "./live";

class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  open() {
    this.onopen?.();
  }
  send(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
  close() {
    this.onclose?.();
  }
}

function setup() {
  const client = new QueryClient();
  const spy = vi.spyOn(client, "invalidateQueries").mockResolvedValue(undefined);
  render(
    <QueryClientProvider client={client}>
      <LiveProvider>
        <div />
      </LiveProvider>
    </QueryClientProvider>,
  );
  return spy;
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const current = () => FakeSocket.instances[FakeSocket.instances.length - 1];

describe("LiveProvider", () => {
  it("batches invalidations from a storm of events and keeps running fetches", () => {
    const spy = setup();
    act(() => current().open());
    act(() => {
      for (let i = 0; i < 200; i++) {
        current().send({ type: "file.analyzed", ts: "", data: { file_id: i } });
      }
    });
    expect(spy).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(2000));
    const keys = spy.mock.calls.map(([filters]) => (filters as { queryKey: string[] }).queryKey[0]);
    expect(keys.sort()).toEqual(["files", "stats"]);
    for (const call of spy.mock.calls) expect(call[1]).toEqual({ cancelRefetch: false });
  });

  it("refreshes everything after a reconnect, but not on the first connect", () => {
    const spy = setup();
    act(() => current().open());
    act(() => vi.advanceTimersByTime(5000));
    expect(spy).not.toHaveBeenCalled();

    act(() => current().close());
    act(() => vi.advanceTimersByTime(3000)); // reconnect back-off
    act(() => current().open());
    act(() => vi.advanceTimersByTime(2000));
    expect(spy).toHaveBeenCalledWith(undefined, { cancelRefetch: false });
  });

  it("ignores progress and ping frames for invalidation", () => {
    const spy = setup();
    act(() => current().open());
    act(() => {
      current().send({ type: "ping", ts: "", data: {} });
      current().send({ type: "job.progress", ts: "", data: { job_id: 1, progress: 0.5 } });
    });
    act(() => vi.advanceTimersByTime(2000));
    expect(spy).not.toHaveBeenCalled();
  });
});
