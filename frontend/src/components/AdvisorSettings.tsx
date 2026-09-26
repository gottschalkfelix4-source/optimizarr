/** The AI advisor section: pick a backend, configure it, sign in where needed. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Brain,
  Check,
  Copy,
  ExternalLink,
  KeyRound,
  LogOut,
  RefreshCw,
  Server,
  Sparkles,
  UserCheck,
} from "lucide-react";
import { useRef, useState } from "react";
import {
  endpoints,
  SECRET_MASK,
  type AdvisorOverview,
  type AdvisorProviderId,
  type AdvisorTestResult,
  type CodexStatus,
  type Settings,
} from "../lib/api";
import { dateTime, relativeTime } from "../lib/format";
import { useToast } from "../lib/live";
import {
  Callout,
  Field,
  Modal,
  NumberField,
  Panel,
  SecretField,
  Select,
  SliderField,
  Spinner,
  Toggle,
  cn,
  withCurrent,
} from "./ui";

type UpdateFn = <K extends keyof Settings>(group: K, patch: Partial<Settings[K]>) => void;

const PROVIDER_ICONS: Record<AdvisorProviderId, typeof Brain> = {
  anthropic: Sparkles,
  openai_codex: UserCheck,
  openai_compatible: Server,
};

export function AdvisorSettings({
  draft,
  saved,
  update,
}: {
  draft: Settings;
  saved: Settings;
  update: UpdateFn;
}) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [signInOpen, setSignInOpen] = useState(false);
  const [testResult, setTestResult] = useState<AdvisorTestResult | null>(null);

  const { data: overview } = useQuery({
    queryKey: ["advisor"],
    queryFn: endpoints.advisorOverview,
    refetchInterval: 30000,
  });

  const test = useMutation({
    mutationFn: () => {
      const cfg = draft.advisor;
      const payload: Record<string, string> = { provider: cfg.provider };
      if (cfg.provider === "anthropic") {
        payload.api_key = cfg.api_key;
        payload.model = cfg.model;
      } else if (cfg.provider === "openai_compatible") {
        payload.openai_base_url = cfg.openai_base_url;
        payload.openai_api_key = cfg.openai_api_key;
        payload.openai_model = cfg.openai_model;
      } else {
        payload.codex_model = cfg.codex_model;
      }
      return endpoints.advisorTest(payload);
    },
    onSuccess: (result) => {
      setTestResult(result);
      push(result.message, result.ok ? "success" : "error");
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const logout = useMutation({
    mutationFn: () => endpoints.codexLogout(),
    onSuccess: (r) => {
      push(r.message, "info");
      queryClient.invalidateQueries({ queryKey: ["advisor"] });
      queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const provider = draft.advisor.provider;
  const codex = overview?.codex;

  return (
    <div className="space-y-4">
      <Callout tone="info" icon={<Sparkles className="size-4" />}>
        Die lokale Analyse entscheidet auch ohne diese Funktion vollständig eigenständig. Der
        KI-Berater ergänzt das, was Messwerte nicht sehen können: ob ein Film ein körniger
        Klassiker ist, ein flächiger Anime oder eine dunkle Konzertaufnahme – und passt CRF und
        Filmkorn entsprechend an. Jede Änderung wird auf den eingestellten Rahmen begrenzt, und
        fällt der Dienst aus, gilt einfach die lokale Entscheidung.
      </Callout>

      <Panel title="Anbieter" subtitle="Es ist immer genau einer aktiv">
        <div className="space-y-4">
          <Toggle
            checked={draft.advisor.enabled}
            onChange={(enabled) => update("advisor", { enabled })}
            label="KI-Berater verwenden"
            hint="Ohne Aktivierung werden keinerlei Daten nach außen gesendet."
          />

          {draft.advisor.enabled && (
            <div className="grid gap-3 md:grid-cols-3">
              {(overview?.providers ?? []).map((entry) => {
                const Icon = PROVIDER_ICONS[entry.id] ?? Brain;
                const active = provider === entry.id;
                const signedIn = entry.id === "openai_codex" && codex?.signed_in;
                return (
                  <button
                    key={entry.id}
                    onClick={() => {
                      update("advisor", { provider: entry.id });
                      setTestResult(null);
                    }}
                    className={cn(
                      "rounded-lg border p-4 text-left transition-colors",
                      active
                        ? "border-brand-500 bg-brand-600/10"
                        : "border-ink-700 bg-ink-850/40 hover:border-ink-600",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <Icon
                        className={cn("size-4 shrink-0", active ? "text-brand-400" : "text-ink-500")}
                      />
                      <span className="font-medium text-ink-100">{entry.label}</span>
                      {signedIn && (
                        <span className="ml-auto shrink-0 rounded-full bg-save-500/15 px-1.5 py-0.5 text-[10px] font-medium text-save-400">
                          angemeldet
                        </span>
                      )}
                    </div>
                    <p className="mt-2 text-xs leading-relaxed text-ink-400">{entry.hint}</p>
                    {!entry.sdk_installed && (
                      <p className="mt-2 text-xs text-warn-400">
                        Benötigtes Paket fehlt im Container.
                      </p>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </Panel>

      {draft.advisor.enabled && provider === "anthropic" && (
        <AnthropicPanel
          draft={draft}
          keyStored={saved.advisor.api_key === SECRET_MASK}
          update={update}
          onTest={() => test.mutate()}
          testing={test.isPending}
        />
      )}

      {draft.advisor.enabled && provider === "openai_compatible" && (
        <OpenAIPanel
          draft={draft}
          keyStored={saved.advisor.openai_api_key === SECRET_MASK}
          update={update}
          onTest={() => test.mutate()}
          testing={test.isPending}
          result={testResult}
        />
      )}

      {draft.advisor.enabled && provider === "openai_codex" && (
        <CodexPanel
          draft={draft}
          update={update}
          status={codex}
          onSignIn={() => setSignInOpen(true)}
          onSignOut={() => logout.mutate()}
          onTest={() => test.mutate()}
          testing={test.isPending}
        />
      )}

      {draft.advisor.enabled && <SharedBehaviour draft={draft} update={update} overview={overview} />}

      <CodexSignInModal open={signInOpen} onClose={() => setSignInOpen(false)} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Anthropic                                                                  */
/* -------------------------------------------------------------------------- */

const ANTHROPIC_MODELS = [
  { value: "claude-opus-5-5", label: "Claude Opus 5.5 (beste Einschätzung)" },
  { value: "claude-opus-5", label: "Claude Opus 5" },
  { value: "claude-sonnet-5", label: "Claude Sonnet 5 (günstiger)" },
  { value: "claude-haiku-4-5", label: "Claude Haiku 4.5 (am günstigsten)" },
];

function AnthropicPanel({
  draft,
  keyStored,
  update,
  onTest,
  testing,
}: {
  draft: Settings;
  keyStored: boolean;
  update: UpdateFn;
  onTest: () => void;
  testing: boolean;
}) {
  return (
    <Panel title="Claude (Anthropic API)">
      <div className="grid gap-5 md:grid-cols-2">
        <Field
          label="API-Schlüssel"
          hint="Von console.anthropic.com. Wird lokal gespeichert und nur an die Anthropic-API gesendet."
        >
          <div className="flex gap-2">
            <SecretField
              value={draft.advisor.api_key}
              stored={keyStored}
              onChange={(api_key) => update("advisor", { api_key })}
              placeholder="sk-ant-…"
              ariaLabel="Anthropic-API-Schlüssel"
            />
            <button className="btn-ghost shrink-0" onClick={onTest} disabled={testing}>
              {testing ? <Spinner className="size-4" /> : <Check className="size-4" aria-hidden="true" />}
              Testen
            </button>
          </div>
        </Field>
        <Field label="Modell">
          <Select
            value={draft.advisor.model}
            onChange={(model) => update("advisor", { model })}
            ariaLabel="Claude-Modell"
            options={withCurrent(ANTHROPIC_MODELS, draft.advisor.model)}
          />
        </Field>
      </div>
    </Panel>
  );
}

/* -------------------------------------------------------------------------- */
/* OpenAI-compatible                                                          */
/* -------------------------------------------------------------------------- */

/** Hosted services get their real address.  Local ones only get a template:
 *  the IP of the machine running them is not something a preset can know, and
 *  an example address that looks real would just time out. */
const PRESET_ENDPOINTS: { label: string; url?: string; template?: string; port?: string; model: string }[] = [
  { label: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-5" },
  { label: "OpenRouter", url: "https://openrouter.ai/api/v1", model: "openai/gpt-5" },
  { label: "Groq", url: "https://api.groq.com/openai/v1", model: "llama-3.3-70b-versatile" },
  { label: "DeepSeek", url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { label: "Ollama (lokal)", template: "http://<IP-des-Rechners>:11434/v1", port: ":11434", model: "qwen2.5:14b" },
  { label: "LM Studio (lokal)", template: "http://<IP-des-Rechners>:1234/v1", port: ":1234", model: "local-model" },
];

function OpenAIPanel({
  draft,
  keyStored,
  update,
  onTest,
  testing,
  result,
}: {
  draft: Settings;
  keyStored: boolean;
  update: UpdateFn;
  onTest: () => void;
  testing: boolean;
  result: AdvisorTestResult | null;
}) {
  const { push } = useToast();
  const [models, setModels] = useState<string[]>([]);
  const [urlTemplate, setUrlTemplate] = useState("");
  const urlRef = useRef<HTMLInputElement>(null);
  const baseUrl = draft.advisor.openai_base_url;

  const applyPreset = (preset: (typeof PRESET_ENDPOINTS)[number]) => {
    const model = draft.advisor.openai_model || preset.model;
    if (preset.url) {
      setUrlTemplate("");
      update("advisor", { openai_base_url: preset.url, openai_model: model });
      return;
    }
    // Local service: keep an address that already points at that port,
    // otherwise empty the field and show what to fill in.
    const keep = preset.port && baseUrl.includes(preset.port);
    setUrlTemplate(preset.template ?? "");
    update("advisor", { openai_base_url: keep ? baseUrl : "", openai_model: model });
    urlRef.current?.focus();
  };

  const presetActive = (preset: (typeof PRESET_ENDPOINTS)[number]) =>
    preset.url ? baseUrl === preset.url : !!preset.port && baseUrl.includes(preset.port);

  const fetchModels = useMutation({
    mutationFn: () =>
      endpoints.advisorOpenAIModels(draft.advisor.openai_base_url, draft.advisor.openai_api_key),
    onSuccess: (data) => {
      setModels(data.models);
      push(data.message, data.ok ? "success" : "error");
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  return (
    <Panel
      title="OpenAI-kompatibler Endpunkt"
      subtitle="Funktioniert mit allem, was die OpenAI-Chat-API spricht"
    >
      <div className="space-y-5">
        <div>
          <p className="label mb-2">Schnellauswahl</p>
          <div className="flex flex-wrap gap-2">
            {PRESET_ENDPOINTS.map((preset) => (
              <button
                key={preset.label}
                onClick={() => applyPreset(preset)}
                aria-pressed={presetActive(preset)}
                className={cn(
                  "rounded-lg border px-3 py-1.5 text-xs transition-colors",
                  presetActive(preset)
                    ? "border-brand-500 bg-brand-600/15 text-brand-400"
                    : "border-ink-700 text-ink-400 hover:border-ink-600 hover:text-ink-200",
                )}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <p className="hint">
            Setzt die Adresse und – falls das Feld noch leer ist – ein Beispielmodell. Bei lokalen
            Diensten trägst du die IP des Rechners selbst ein; den Schlüssel gibst du darunter an.
          </p>
        </div>

        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Endpunkt-URL"
            hint="Ohne /v1 am Ende wird es automatisch ergänzt. Lokale Dienste brauchen die IP des Rechners, nicht localhost – der Container hat sein eigenes localhost."
          >
            <input
              ref={urlRef}
              className="field font-mono text-sm"
              value={baseUrl}
              onChange={(e) => update("advisor", { openai_base_url: e.target.value })}
              placeholder={urlTemplate || "http://<IP-des-Rechners>:11434/v1"}
              aria-label="Endpunkt-URL"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>

          <Field
            label="API-Schlüssel"
            hint="Bei lokalen Diensten ohne Authentifizierung einfach leer lassen. Ein gespeicherter Schlüssel wird nur an die gespeicherte Adresse geschickt."
          >
            <SecretField
              value={draft.advisor.openai_api_key}
              stored={keyStored}
              onChange={(openai_api_key) => update("advisor", { openai_api_key })}
              placeholder="sk-… (optional)"
              ariaLabel="API-Schlüssel des Endpunkts"
            />
          </Field>

          <Field label="Modell" hint="Exakt so schreiben, wie der Dienst es erwartet.">
            <div className="flex gap-2">
              <input
                className="field font-mono text-sm"
                value={draft.advisor.openai_model}
                onChange={(e) => update("advisor", { openai_model: e.target.value })}
                placeholder="gpt-5"
                list="openai-model-list"
                autoComplete="off"
              />
              <datalist id="openai-model-list">
                {models.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              <button
                className="btn-ghost shrink-0"
                onClick={() => fetchModels.mutate()}
                disabled={fetchModels.isPending || !draft.advisor.openai_base_url}
                title="Modelle vom Endpunkt abrufen"
                aria-label="Modelle vom Endpunkt abrufen"
              >
                {fetchModels.isPending ? (
                  <Spinner className="size-4" />
                ) : (
                  <RefreshCw className="size-4" />
                )}
              </button>
            </div>
          </Field>

          <Field
            label="JSON-Erzwingung"
            hint="Automatisch probiert der Reihe nach Schema, JSON-Modus und Prompt-Anweisung und merkt sich, was funktioniert hat."
          >
            <Select
              value={draft.advisor.openai_structured_mode}
              onChange={(openai_structured_mode) =>
                update("advisor", { openai_structured_mode })
              }
              options={[
                { value: "auto", label: "Automatisch aushandeln (empfohlen)" },
                { value: "json_schema", label: "JSON-Schema erzwingen" },
                { value: "json_object", label: "Nur JSON-Modus" },
                { value: "prompt", label: "Nur über Prompt-Anweisung" },
              ]}
            />
          </Field>
        </div>

        <div className="flex flex-wrap items-center gap-3 border-t border-ink-800 pt-5">
          <button className="btn-primary" onClick={onTest} disabled={testing}>
            {testing ? <Spinner className="size-4" /> : <Check className="size-4" />}
            Verbindung testen
          </button>
          {result?.capabilities && (
            <span className="text-xs text-ink-400">
              Ausgehandelt: {describeCapabilities(result.capabilities)}
            </span>
          )}
        </div>

        <details className="rounded-lg border border-ink-700/70 bg-ink-850/40 p-4">
          <summary className="cursor-pointer text-sm font-medium text-ink-200">
            Feineinstellungen
          </summary>
          <div className="mt-4 grid gap-5 md:grid-cols-2">
            <Field label="Maximale Antwortlänge">
              <NumberField
                value={draft.advisor.openai_max_tokens}
                onChange={(openai_max_tokens) => update("advisor", { openai_max_tokens })}
                min={256}
                max={32000}
                suffix="Token"
              />
            </Field>
            <Field
              label="Temperature"
              hint="Niedrig heißt berechenbar. Manche Reasoning-Modelle ignorieren den Wert."
            >
              <SliderField
                value={draft.advisor.openai_temperature}
                onChange={(openai_temperature) => update("advisor", { openai_temperature })}
                min={0}
                max={2}
                step={0.1}
                format={(v) => v.toFixed(1)}
              />
            </Field>
            <div className="md:col-span-2">
              <Toggle
                checked={draft.advisor.openai_send_system_role}
                onChange={(openai_send_system_role) =>
                  update("advisor", { openai_send_system_role })
                }
                label="System-Nachricht separat senden"
                hint="Ausschalten, wenn der Dienst die Rolle „system“ nicht kennt – der Text wandert dann in die Nutzernachricht."
              />
            </div>
          </div>
        </details>
      </div>
    </Panel>
  );
}

function describeCapabilities(caps: NonNullable<AdvisorTestResult["capabilities"]>): string {
  const parts = [caps.structured_label ?? caps.structured];
  if (caps.token_field !== "max_tokens") parts.push(caps.token_field);
  if (!caps.send_temperature) parts.push("ohne temperature");
  if (!caps.send_system_role) parts.push("ohne system-Rolle");
  return parts.join(", ");
}

/* -------------------------------------------------------------------------- */
/* ChatGPT sign-in                                                            */
/* -------------------------------------------------------------------------- */

function CodexPanel({
  draft,
  update,
  status,
  onSignIn,
  onSignOut,
  onTest,
  testing,
}: {
  draft: Settings;
  update: UpdateFn;
  status: CodexStatus | undefined;
  onSignIn: () => void;
  onSignOut: () => void;
  onTest: () => void;
  testing: boolean;
}) {
  const { push } = useToast();
  const [models, setModels] = useState<string[]>([]);
  const [modelNote, setModelNote] = useState("");
  const signedIn = status?.signed_in;

  const fetchModels = useMutation({
    mutationFn: () => endpoints.codexModels(true),
    onSuccess: (data) => {
      setModels(data.models);
      setModelNote(data.message);
      push(data.message, data.ok ? "success" : "info");
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  return (
    <Panel
      title="ChatGPT-Anmeldung"
      subtitle="Nutzt das bestehende ChatGPT-Abo statt separater API-Guthaben"
    >
      <div className="space-y-5">
        <Callout tone="warn">
          Dieser Weg meldet sich so an wie das Codex-Kommandozeilenwerkzeug von OpenAI.
          Vorgesehen ist er für OpenAIs eigene Anwendungen – für Drittprogramme wie
          Optimizarr ist das eine Grauzone, und OpenAI kann den Zugang jederzeit
          einschränken. Wenn du das vermeiden möchtest, nimm einen Platform-API-Key über
          den Punkt <strong className="text-ink-100">OpenAI-kompatibler Endpunkt</strong>.
        </Callout>

        {signedIn ? (
          <div className="rounded-lg border border-save-500/30 bg-save-500/8 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex items-start gap-3">
                <UserCheck className="mt-0.5 size-5 shrink-0 text-save-400" />
                <div>
                  <p className="font-medium text-ink-100">
                    Angemeldet
                    {status?.account_label ? ` als ${status.account_label}` : ""}
                    {status?.plan_type ? ` (${status.plan_type})` : ""}
                  </p>
                  <div className="mt-1 space-y-0.5 text-xs text-ink-400">
                    {status?.expires_at && (
                      <p>
                        Zugang gültig bis {dateTime(status.expires_at)}
                        {status.can_refresh
                          ? " – wird automatisch erneuert."
                          : " – danach ist eine neue Anmeldung nötig."}
                      </p>
                    )}
                    {status?.last_refresh && <p>Zuletzt erneuert {relativeTime(status.last_refresh)}</p>}
                    {!status?.account_id_present && (
                      <p className="text-warn-400">
                        Keine Konto-Kennung gefunden – falls Anfragen abgelehnt werden, hilft
                        eine erneute Anmeldung über den Browser.
                      </p>
                    )}
                    {status?.last_error && <p className="text-danger-400">{status.last_error}</p>}
                  </div>
                </div>
              </div>
              <div className="flex gap-2">
                <button className="btn-ghost btn-sm" onClick={onTest} disabled={testing}>
                  {testing ? <Spinner className="size-3.5" /> : <Check className="size-3.5" />}
                  Testen
                </button>
                <button className="btn-danger btn-sm" onClick={onSignOut}>
                  <LogOut className="size-3.5" />
                  Abmelden
                </button>
              </div>
            </div>
          </div>
        ) : (
          <div className="rounded-lg border border-ink-700 bg-ink-850/40 p-5 text-center">
            <KeyRound className="mx-auto size-8 text-ink-500" />
            <p className="mt-3 font-medium text-ink-100">Noch nicht angemeldet</p>
            <p className="mx-auto mt-1 max-w-md text-sm leading-relaxed text-ink-400">
              Die Anmeldung läuft über deinen Browser. Weil Optimizarr im Container läuft,
              kopierst du dabei einmal eine Adresse hin und her – der Assistent führt dich
              durch die drei Schritte.
            </p>
            <button className="btn-primary mt-4" onClick={onSignIn}>
              <ExternalLink className="size-4" />
              Mit ChatGPT anmelden
            </button>
          </div>
        )}

        <div className="grid gap-5 border-t border-ink-800 pt-5 md:grid-cols-2">
          <Field
            label="Modell"
            hint={
              modelNote ||
              "Welche Modelle erreichbar sind, gibt OpenAI je nach Konto und Client vor – Liste abrufen."
            }
          >
            <div className="flex gap-2">
              <input
                className="field font-mono text-sm"
                value={draft.advisor.codex_model}
                onChange={(e) => update("advisor", { codex_model: e.target.value })}
                placeholder="gpt-6-astra"
                list="codex-model-list"
              />
              <datalist id="codex-model-list">
                {(models.length ? models : status?.known_models ?? []).map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              <button
                className="btn-ghost shrink-0"
                onClick={() => fetchModels.mutate()}
                disabled={fetchModels.isPending || !signedIn}
                title="Verfügbare Modelle vom Konto abrufen"
                aria-label="Verfügbare Modelle vom Konto abrufen"
              >
                {fetchModels.isPending ? (
                  <Spinner className="size-4" />
                ) : (
                  <RefreshCw className="size-4" />
                )}
              </button>
            </div>
            {draft.advisor.codex_model !== "gpt-6-astra" && (
              <button
                type="button"
                className="mt-2 text-xs text-brand-400 hover:underline"
                onClick={() => update("advisor", { codex_model: "gpt-6-astra" })}
              >
                GPT-6 Astra verwenden
              </button>
            )}
          </Field>
          <Field
            label="Denktiefe"
            hint="Für diese Aufgabe reicht die niedrigste Stufe – sie ist schneller und schont das Kontingent."
          >
            <Select
              value={draft.advisor.codex_reasoning_effort}
              onChange={(codex_reasoning_effort) =>
                update("advisor", { codex_reasoning_effort })
              }
              options={[
                { value: "low", label: "Niedrig (empfohlen)" },
                { value: "medium", label: "Mittel" },
                { value: "high", label: "Hoch" },
              ]}
            />
          </Field>
        </div>
      </div>
    </Panel>
  );
}

function CodexSignInModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<"browser" | "file">("browser");
  const [pasted, setPasted] = useState("");
  const [authJson, setAuthJson] = useState("");
  const [copied, setCopied] = useState(false);

  const start = useMutation({
    mutationFn: () => endpoints.codexStart(),
    onError: (e: Error) => push(e.message, "error"),
  });

  // Closing - by any route - starts the next attempt from scratch: a pasted
  // callback address or a half-used login link must not linger.
  const close = () => {
    setPasted("");
    setAuthJson("");
    setCopied(false);
    setTab("browser");
    start.reset();
    onClose();
  };

  const finish = () => {
    queryClient.invalidateQueries({ queryKey: ["advisor"] });
    queryClient.invalidateQueries({ queryKey: ["settings"] });
    queryClient.invalidateQueries({ queryKey: ["system"] });
    close();
  };

  const complete = useMutation({
    mutationFn: () => endpoints.codexComplete(pasted, start.data?.state),
    onSuccess: (r) => {
      push(r.message, "success");
      finish();
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const importJson = useMutation({
    mutationFn: () => endpoints.codexImport(authJson),
    onSuccess: (r) => {
      push(r.message, "success");
      finish();
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const copyLink = async () => {
    if (!start.data?.authorize_url) return;
    try {
      await navigator.clipboard.writeText(start.data.authorize_url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      push("Kopieren nicht möglich – bitte den Link von Hand markieren.", "error");
    }
  };

  return (
    <Modal
      open={open}
      onClose={close}
      wide
      title="Mit ChatGPT anmelden"
      subtitle="Einmalig – danach erneuert sich der Zugang von selbst"
    >
      <div className="space-y-5">
        <div className="flex gap-1 rounded-lg border border-ink-700 bg-ink-950/50 p-1">
          {[
            { id: "browser" as const, label: "Über den Browser" },
            { id: "file" as const, label: "auth.json einfügen" },
          ].map((entry) => (
            <button
              key={entry.id}
              onClick={() => setTab(entry.id)}
              className={cn(
                "flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                tab === entry.id
                  ? "bg-brand-600/20 text-brand-400"
                  : "text-ink-400 hover:text-ink-200",
              )}
            >
              {entry.label}
            </button>
          ))}
        </div>

        {tab === "browser" ? (
          <div className="space-y-4">
            <Step
              number={1}
              title="Anmeldung starten"
              done={!!start.data}
              body={
                start.data ? (
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <a
                        href={start.data.authorize_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="btn-primary btn-sm"
                      >
                        <ExternalLink className="size-3.5" />
                        Anmeldeseite öffnen
                      </a>
                      <button className="btn-ghost btn-sm" onClick={copyLink}>
                        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                        {copied ? "Kopiert" : "Link kopieren"}
                      </button>
                    </div>
                    <p className="text-xs text-ink-500">
                      Der Link ist 30 Minuten gültig.
                    </p>
                  </div>
                ) : (
                  <button
                    className="btn-primary btn-sm"
                    onClick={() => start.mutate()}
                    disabled={start.isPending}
                  >
                    {start.isPending ? <Spinner className="size-3.5" /> : null}
                    Link erzeugen
                  </button>
                )
              }
            />

            <Step
              number={2}
              title="Anmelden und Adresse kopieren"
              disabled={!start.data}
              body={
                <p className="text-sm leading-relaxed text-ink-400">
                  Melde dich mit deinem ChatGPT-Konto an. Danach landet der Browser auf einer
                  Seite, die <strong className="text-ink-200">nicht geladen werden kann</strong> –
                  genau so soll es sein. Kopiere die komplette Adresse aus der Adresszeile; sie
                  beginnt mit{" "}
                  <code className="rounded bg-ink-950 px-1 py-0.5 text-[11px] text-ink-300">
                    {start.data?.redirect_uri ?? "http://localhost:1455/auth/callback"}?code=
                  </code>
                </p>
              }
            />

            <Step
              number={3}
              title="Adresse hier einfügen"
              disabled={!start.data}
              body={
                <div className="space-y-2">
                  <textarea
                    className="field h-24 resize-none font-mono text-xs"
                    value={pasted}
                    onChange={(e) => setPasted(e.target.value)}
                    placeholder="http://localhost:1455/auth/callback?code=…"
                    spellCheck={false}
                  />
                  <button
                    className="btn-primary btn-sm"
                    onClick={() => complete.mutate()}
                    disabled={complete.isPending || pasted.trim().length < 8}
                  >
                    {complete.isPending ? (
                      <Spinner className="size-3.5" />
                    ) : (
                      <Check className="size-3.5" />
                    )}
                    Anmeldung abschließen
                  </button>
                </div>
              }
            />
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-sm leading-relaxed text-ink-400">
              Wenn du das Codex-Kommandozeilenwerkzeug schon auf einem Rechner eingerichtet hast,
              kannst du dessen Zugangsdaten direkt übernehmen. Die Datei liegt unter{" "}
              <code className="rounded bg-ink-950 px-1 py-0.5 text-[11px] text-ink-300">
                ~/.codex/auth.json
              </code>{" "}
              beziehungsweise{" "}
              <code className="rounded bg-ink-950 px-1 py-0.5 text-[11px] text-ink-300">
                %USERPROFILE%\.codex\auth.json
              </code>
              .
            </p>
            <textarea
              className="field h-40 resize-none font-mono text-xs"
              value={authJson}
              onChange={(e) => setAuthJson(e.target.value)}
              placeholder='{"tokens": {"access_token": "...", "refresh_token": "..."}}'
              spellCheck={false}
            />
            <button
              className="btn-primary btn-sm"
              onClick={() => importJson.mutate()}
              disabled={importJson.isPending || authJson.trim().length < 20}
            >
              {importJson.isPending ? <Spinner className="size-3.5" /> : <Check className="size-3.5" />}
              Zugangsdaten übernehmen
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
}

function Step({
  number,
  title,
  body,
  done,
  disabled,
}: {
  number: number;
  title: string;
  body: React.ReactNode;
  done?: boolean;
  disabled?: boolean;
}) {
  return (
    <div className={cn("flex gap-3", disabled && "opacity-45")}>
      <span
        className={cn(
          "grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold",
          done ? "bg-save-500/20 text-save-400" : "bg-ink-700 text-ink-300",
        )}
      >
        {done ? <Check className="size-3.5" /> : number}
      </span>
      <div className="min-w-0 flex-1">
        <p className="mb-2 font-medium text-ink-100">{title}</p>
        {body}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Shared behaviour                                                           */
/* -------------------------------------------------------------------------- */

function SharedBehaviour({
  draft,
  update,
  overview,
}: {
  draft: Settings;
  update: UpdateFn;
  overview: AdvisorOverview | undefined;
}) {
  return (
    <Panel title="Verhalten" subtitle="Gilt für jeden Anbieter">
      <div className="space-y-5">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Wann gefragt wird"
            hint="Jede Anfrage kostet – entweder Geld oder Kontingent. „Nur bei unsicherer Einschätzung“ fragt genau dann, wenn es etwas bringt."
          >
            <Select
              value={draft.advisor.mode}
              onChange={(mode) => update("advisor", { mode })}
              options={[
                { value: "uncertain_only", label: "Nur bei unsicherer Einschätzung (empfohlen)" },
                { value: "all_candidates", label: "Bei jedem Kandidaten" },
                { value: "explain_only", label: "Nur erklären, nichts ändern" },
              ]}
            />
          </Field>
          <Field label="Maximale Anfragen pro Scan" hint="Harte Obergrenze.">
            <NumberField
              value={draft.advisor.max_calls_per_scan}
              onChange={(max_calls_per_scan) => update("advisor", { max_calls_per_scan })}
              min={0}
              max={5000}
            />
          </Field>
          {draft.advisor.mode === "uncertain_only" && (
            <Field
              label="Unsicher unterhalb von"
              hint="Bezieht sich auf die Sicherheit der lokalen Schätzung."
            >
              <SliderField
                value={draft.advisor.uncertain_below_confidence}
                onChange={(uncertain_below_confidence) =>
                  update("advisor", { uncertain_below_confidence })
                }
                min={0}
                max={1}
                step={0.05}
                format={(v) => `${Math.round(v * 100)} %`}
              />
            </Field>
          )}
          <Field
            label="Maximale CRF-Verschiebung"
            hint="Begrenzt, wie stark die KI die Qualitätseinstellung verändern darf."
          >
            <NumberField
              value={draft.advisor.max_crf_delta}
              onChange={(max_crf_delta) => update("advisor", { max_crf_delta })}
              min={0}
              max={15}
            />
          </Field>
          <Field label="Zeitlimit pro Anfrage">
            <NumberField
              value={draft.advisor.timeout_seconds}
              onChange={(timeout_seconds) => update("advisor", { timeout_seconds })}
              min={5}
              max={300}
              suffix="Sek"
            />
          </Field>
        </div>

        <div className="space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.advisor.allow_setting_changes}
            onChange={(allow_setting_changes) => update("advisor", { allow_setting_changes })}
            label="Einstellungen anpassen lassen"
            hint="Aus: die KI liefert nur eine Begründung, ändert aber keine Werte."
          />
          <Toggle
            checked={draft.advisor.include_filename}
            onChange={(include_filename) => update("advisor", { include_filename })}
            label="Dateinamen mitsenden"
            hint="Hilft beim Erkennen von Anime, Klassikern oder Handyaufnahmen. Aus, wenn dir das zu viel Information ist."
          />
        </div>

        {overview && (
          <p className="border-t border-ink-800 pt-4 text-xs text-ink-500">
            {overview.ready ? (
              <>
                Bereit. In diesem Scan wurden {overview.calls_used} Anfragen gestellt,{" "}
                {overview.budget_left} bleiben übrig.
              </>
            ) : (
              <span className="text-warn-400">{overview.reason}</span>
            )}
          </p>
        )}
      </div>
    </Panel>
  );
}
