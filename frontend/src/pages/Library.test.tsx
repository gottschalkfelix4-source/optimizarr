import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { endpoints, type MediaFile } from "../lib/api";
import Library from "./Library";

const file = (id: number, state: MediaFile["state"]) => ({ id, state, name: `Film-${id}.mkv`, path: `/media/Film-${id}.mkv`, folder: "/media", size: 2000, video_codec: "hevc", width: 1920, height: 1080, estimated_size: 1000, estimated_saving_bytes: 1000, estimated_saving_pct: 50, confidence: .8, ignored: state === "ignored" }) as MediaFile;

function setup() {
  const page = [file(1,"candidate"), file(2,"ignored"), file(3,"done"), file(4,"encoding")];
  vi.spyOn(endpoints,"libraryPaths").mockResolvedValue([]);
  vi.spyOn(endpoints,"files").mockImplementation(async params => ({ items: params?.page === 2 ? [file(5,"candidate")] : page, total: 5, page: params?.page ?? 1, pages: 2, page_size: 50, aggregate: { potential_saving: 3000 } }) as Awaited<ReturnType<typeof endpoints.files>>);
  const enqueue = vi.spyOn(endpoints,"enqueue").mockResolvedValue({ added: 1, skipped: [], message: "Eingereiht" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<MemoryRouter><QueryClientProvider client={client}><Library /></QueryClientProvider></MemoryRouter>);
  return { enqueue, user: userEvent.setup() };
}

describe("library bulk selection", () => {
  it("retains selections across pages and excludes ignored files from normal conversion", async () => {
    const { enqueue, user } = setup();
    await user.click(await screen.findByRole("checkbox", { name: "Alle auf dieser Seite auswählen" }));
    expect((screen.getByRole("checkbox", { name: "Film-3.mkv auswählen" }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("checkbox", { name: "Film-4.mkv auswählen" }) as HTMLInputElement).disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "Weiter" }));
    await user.click(await screen.findByRole("checkbox", { name: "Film-5.mkv auswählen" }));
    expect(screen.getByText(/davon 2 auf anderen Seiten/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Kandidaten konvertieren (2)" }));
    await waitFor(() => expect(enqueue).toHaveBeenCalledWith({ file_ids: [1,5], force: false }));
  });

  it("requires the explicit force action for ignored files", async () => {
    const { enqueue, user } = setup();
    await user.click(await screen.findByRole("checkbox", { name: "Film-2.mkv auswählen" }));
    expect(screen.queryByRole("button", { name: "Konvertieren" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Konvertieren erzwingen (1)" }));
    await waitFor(() => expect(enqueue).toHaveBeenCalledWith({ file_ids: [2], force: true }));
  });
});
