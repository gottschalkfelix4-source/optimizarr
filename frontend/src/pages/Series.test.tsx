import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { endpoints, type GroupPage, type SeriesSummary } from "../lib/api";
import SeriesPage from "./Series";

describe("server-side series browsing", () => {
  it("requests the next page and sends search terms to the server", async () => {
    const response = { items: [], totals: { episodes: 120, in_av1: 0, saved_bytes: 0, potential_saving: 0, counts: { converted: 0, pending: 0 } }, all_count: 120, total: 120, page: 1, pages: 3, page_size: 50, complete_count: 0 } as unknown as GroupPage<SeriesSummary>;
    const api = vi.spyOn(endpoints, "series").mockImplementation(async params => ({ ...response, page: params?.page ?? 1 }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><SeriesPage /></QueryClientProvider>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Weiter" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith(expect.objectContaining({ page: 2, page_size: 50 }), expect.anything()));
    await user.type(screen.getByRole("textbox", { name: "Serie suchen" }), "Dune");
    await waitFor(() => expect(api).toHaveBeenCalledWith(expect.objectContaining({ page: 1, search: "Dune" }), expect.anything()));
  });
});
