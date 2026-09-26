import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, buildInit, CSRF_HEADER, describeErrorDetail, endpoints } from "./api";

function mockFetch(body: unknown = {}, status = 200) {
  const fn = vi.fn(async (_url: string, _init?: RequestInit) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("buildInit", () => {
  it("adds the CSRF header to every state-changing method", () => {
    for (const method of ["POST", "PUT", "PATCH", "DELETE", "post"]) {
      const headers = buildInit({ method }).headers as Headers;
      expect(headers.get(CSRF_HEADER)).toBe("1");
    }
  });

  it("leaves safe methods alone", () => {
    for (const method of [undefined, "GET", "HEAD", "OPTIONS"]) {
      const headers = buildInit({ method }).headers as Headers;
      expect(headers.get(CSRF_HEADER)).toBeNull();
    }
  });

  it("merges caller headers instead of letting them replace the defaults", () => {
    const init = buildInit({
      method: "POST",
      body: "{}",
      headers: { "X-Extra": "yes" },
    });
    const headers = init.headers as Headers;
    expect(headers.get("X-Extra")).toBe("yes");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get(CSRF_HEADER)).toBe("1");
  });

  it("keeps the CSRF header even if a caller tries to override it", () => {
    const headers = buildInit({ method: "DELETE", headers: { [CSRF_HEADER]: "0" } })
      .headers as Headers;
    expect(headers.get(CSRF_HEADER)).toBe("1");
  });
});

describe("requests", () => {
  it("sends the header on real POST/PUT/DELETE calls", async () => {
    const fetchMock = mockFetch({ ok: true });
    await api.post("/scan");
    await api.put("/settings", { a: 1 });
    await api.del("/jobs/finished");
    await api.get("/settings");
    const sent = fetchMock.mock.calls.map(([, init]) => (init?.headers as Headers).get(CSRF_HEADER));
    expect(sent).toEqual(["1", "1", "1", null]);
  });

  it("passes the abort signal through", async () => {
    const fetchMock = mockFetch({});
    const controller = new AbortController();
    await endpoints.stats({ signal: controller.signal });
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it("asks for OpenAI models by POST, never with the key in the URL", async () => {
    const fetchMock = mockFetch({ ok: true, models: [], message: "" });
    await endpoints.advisorOpenAIModels("https://api.openai.com/v1", "sk-secret");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/advisor/openai/models");
    expect(url).not.toContain("sk-secret");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      base_url: "https://api.openai.com/v1",
      api_key: "sk-secret",
    });
  });

  it("sends null for an empty key so the server may use the stored one", async () => {
    const fetchMock = mockFetch({ ok: true, models: [], message: "" });
    await endpoints.advisorOpenAIModels("http://x/v1", "");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).api_key).toBeNull();
  });

  it("filters the history by level on the server", async () => {
    const fetchMock = mockFetch([]);
    await endpoints.history({ limit: 150, level: "error" });
    await endpoints.history({ limit: 150, level: "all" });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/history?limit=150&level=error");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/history?limit=150");
  });

  it("asks for active and finished jobs separately", async () => {
    const fetchMock = mockFetch({ items: [], counts: {}, worker: {} });
    await endpoints.jobs({ state: "active", limit: 1000 });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs?state=active&limit=1000");
  });

  it("turns error details into ApiError messages", async () => {
    mockFetch({ detail: "Diesen Ordner gibt es nicht." }, 404);
    const error = await endpoints.browse("/nope").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(404);
    expect(error.message).toBe("Diesen Ordner gibt es nicht.");
  });
});

describe("describeErrorDetail", () => {
  it("translates FastAPI's validation list", () => {
    const msg = describeErrorDetail([
      {
        loc: ["body", "queue", "nice_level"],
        msg: "Input should be less than or equal to 19",
        type: "less_than_equal",
      },
    ]);
    expect(msg).toBe("Ungültige Eingabe – Prozesspriorität: darf höchstens 19 sein");
  });

  it("translates the raw pydantic text an old server embeds", () => {
    const raw =
      "Ungueltige Einstellungen: 1 validation error for QueueSettings\n" +
      "nice_level\n" +
      "  Input should be greater than or equal to -20 [type=greater_than_equal, input_value=-25, input_type=int]\n" +
      "    For further information visit https://errors.pydantic.dev/2/v/greater_than_equal";
    expect(describeErrorDetail(raw)).toBe(
      "Ungültige Einstellungen – Prozesspriorität: muss mindestens -20 sein",
    );
  });

  it("translates the summarised form and keeps German messages", () => {
    const text =
      "Ungueltige Einstellungen: queue.max_concurrent_jobs: Input should be less than or equal to 8; " +
      "output.file_mode: Ungültige Dateirechte";
    expect(describeErrorDetail(text)).toBe(
      "Ungültige Einstellungen – Gleichzeitige Konvertierungen: darf höchstens 8 sein; " +
        "Dateirechte: Ungültige Dateirechte",
    );
  });

  it("passes plain messages through", () => {
    expect(describeErrorDetail("Job nicht gefunden")).toBe("Job nicht gefunden");
  });
});
