import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { endpoints, type TrashItem } from "../lib/api";
import TrashPage from "./Trash";

const item: TrashItem = { id: "a".repeat(32), source: "/media/Film.mkv", path: "/media/.optimizarr-trash/Film.mkv", size: 1024, trashed_at: "2026-09-29T10:00:00Z", expires_at: "2026-10-13T10:00:00Z", conflict: "" };
function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><TrashPage /></QueryClientProvider>);
}

describe("Trash", () => {
  it("confirms restoration and then removes the completed entry", async () => {
    const user = userEvent.setup();
    const list = vi.spyOn(endpoints, "trash").mockResolvedValue({ items: [item], total_size: 1024 });
    const restore = vi.spyOn(endpoints, "restoreTrash").mockImplementation(async () => {
      list.mockResolvedValue({ items: [], total_size: 0 });
      return { ok: true, source: item.source, kept_output: "/media/Film.original.mkv" };
    });
    show();
    await user.click(await screen.findByRole("button", { name: "Wiederherstellen" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(restore).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Original wiederherstellen" }));
    await screen.findByText("Keine protokollierten Originale im Papierkorb");
    expect(restore).toHaveBeenCalledWith(item.id);
  });
  it("disables restoration of a changed file", async () => {
    vi.spyOn(endpoints, "trash").mockResolvedValue({ items: [{ ...item, conflict: "Datei wurde verändert" }], total_size: 1024 });
    show();
    const button = await screen.findByRole("button", { name: "Wiederherstellen" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Datei wurde verändert")).toBeTruthy();
  });
  it("keeps the dialog open and refreshes conflicts on failure", async () => {
    const list = vi.spyOn(endpoints, "trash").mockResolvedValue({ items: [item], total_size: 1024 });
    vi.spyOn(endpoints, "restoreTrash").mockRejectedValue(new Error("Job läuft"));
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Wiederherstellen" }));
    await user.click(screen.getByRole("button", { name: "Original wiederherstellen" }));
    await waitFor(() => expect(list.mock.calls.length).toBeGreaterThan(1));
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});
