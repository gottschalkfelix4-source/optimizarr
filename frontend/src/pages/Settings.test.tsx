import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Settings } from "../lib/api";
import SettingsPage, { DirectoryPicker, renderDeviceOptions } from "./Settings";

const SAVED = {
  library: {
    extensions: ["mkv"],
    min_file_size_mb: 50,
    min_duration_seconds: 60,
    exclude_patterns: [],
    follow_symlinks: false,
    scan_on_start: true,
    scan_interval_hours: 24,
    rescan_changed_only: true,
    reanalyze_after_days: 90,
  },
  queue: { paused: false, schedule_days: [0, 1, 2, 3, 4, 5, 6] },
  advisor: { api_key: "********" },
  ui: { size_unit: "binary", dashboard_refresh_seconds: 3 },
} as unknown as Settings;

type Handler = (url: string, init?: RequestInit) => { status?: number; body: unknown } | undefined;

function routeFetch(handler: Handler) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const r = handler(url, init) ?? { status: 404, body: { detail: "nicht gemockt" } };
    return new Response(JSON.stringify(r.body), {
      status: r.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function withClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  return client;
}

afterEach(() => vi.unstubAllGlobals());

describe("SettingsPage", () => {
  it("rebases edits onto server changes and saves only the difference", async () => {
    const user = userEvent.setup();
    const fetchMock = routeFetch((url, init) => {
      if (url === "/api/settings" && (!init?.method || init.method === "GET")) return { body: SAVED };
      if (url === "/api/library/paths") return { body: [] };
      if (url === "/api/settings" && init?.method === "PUT") return { body: SAVED };
      return undefined;
    });
    const client = withClient(<SettingsPage />);

    const field = await screen.findByLabelText("Mindestgröße");
    await user.clear(field);
    await user.type(field, "70");
    await user.tab();

    // Someone pauses the queue from the header while the edit is pending.
    act(() => {
      client.setQueryData(["settings"], {
        ...SAVED,
        queue: { ...SAVED.queue, paused: true },
      });
    });

    await user.click(await screen.findByRole("button", { name: /Speichern/ }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true),
    );
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
    // Only the edit - not the stale "paused: false", not the masked key.
    expect(JSON.parse(String(put[1]?.body))).toEqual({ library: { min_file_size_mb: 70 } });
  });

  it("shows an error with a retry instead of an endless skeleton", async () => {
    routeFetch(() => ({ status: 500, body: { detail: "Interner Fehler" } }));
    withClient(<SettingsPage />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("Interner Fehler")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Erneut versuchen/ })).toBeTruthy();
  });

  it("shows the security tab even if the server sends no security group", async () => {
    const user = userEvent.setup();
    routeFetch((url) => {
      if (url === "/api/settings") return { body: SAVED };
      if (url === "/api/library/paths") return { body: [] };
      return undefined;
    });
    withClient(<SettingsPage />);
    await user.click(await screen.findByRole("tab", { name: /Sicherheit/ }));
    expect(screen.getByLabelText("Benutzername")).toHaveProperty("value", "admin");
    expect(screen.getByText(/Die Anmeldung ist aus/)).toBeTruthy();
  });

  it("marks the tabs up as a tab list", async () => {
    routeFetch((url) => {
      if (url === "/api/settings") return { body: SAVED };
      if (url === "/api/library/paths") return { body: [] };
      return undefined;
    });
    withClient(<SettingsPage />);
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toContain("Sicherheit");
    expect(tabs.filter((t) => t.getAttribute("aria-selected") === "true")).toHaveLength(1);
  });
});

describe("DirectoryPicker", () => {
  it("only confirms a folder that is shown and exists", async () => {
    const user = userEvent.setup();
    routeFetch((url) => {
      const path = new URL(url, "http://x").searchParams.get("path");
      if (path === "/media") {
        return {
          body: { path: "/media", parent: "/", entries: [{ name: "movies", path: "/media/movies", readable: true }] },
        };
      }
      if (path === "/media/movies") return { body: { path: "/media/movies", parent: "/media", entries: [] } };
      return { status: 404, body: { detail: "Diesen Ordner gibt es im Container nicht." } };
    });
    const onSelect = vi.fn();
    withClient(<DirectoryPicker open onClose={() => {}} onSelect={onSelect} busy={false} />);

    const confirm = screen.getByRole("button", { name: /Diesen Ordner verwenden/ });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));

    const input = screen.getByLabelText("Pfad im Container");
    await user.clear(input);
    await user.type(input, "/nope");
    // Differs from what is listed: locked at once, before the debounce ends.
    expect(confirm).toHaveProperty("disabled", true);
    expect(await screen.findByText("Diesen Ordner gibt es im Container nicht.")).toBeTruthy();
    expect(confirm).toHaveProperty("disabled", true);

    await user.clear(input);
    await user.type(input, "/media");
    await user.click(await screen.findByRole("button", { name: /movies/ }));
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    await user.click(confirm);
    expect(onSelect).toHaveBeenCalledWith("/media/movies");
  });
});

describe("renderDeviceOptions", () => {
  it("lists card devices and keeps an unknown stored device", () => {
    const options = renderDeviceOptions(
      [
        { path: "/dev/dri/card0", writable: true, is_render_node: false },
        { path: "/dev/dri/renderD128", writable: true, is_render_node: true },
      ],
      "/dev/dri/renderD129",
    );
    expect(options.map((o) => o.value)).toEqual([
      "/dev/dri/renderD128",
      "/dev/dri/card0",
      "/dev/dri/renderD129",
    ]);
    expect(options[1].label).toMatch(/kein Render-Node/);
    expect(options[2].label).toMatch(/nicht gefunden/);
  });
});
