/** Typed access to the Optimizarr backend. */

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** Header the backend requires on every state-changing request (CSRF guard). */
export const CSRF_HEADER = "X-Optimizarr";

/** What the backend sends instead of a stored secret. */
export const SECRET_MASK = "********";

/** Request options the query functions pass through - mainly the abort signal. */
export interface RequestOpts {
  signal?: AbortSignal;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Merge caller options with the defaults.  Headers are merged, never replaced:
 *  spreading ``init`` after the default headers used to drop them again, and the
 *  CSRF marker must survive whatever a caller passes. */
export function buildInit(init: RequestInit = {}): RequestInit {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body !== undefined && init.body !== null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!SAFE_METHODS.has(method)) headers.set(CSRF_HEADER, "1");
  return { ...init, method, headers };
}

/* ---- validation errors in German ------------------------------------------ */

const FIELD_LABELS: Record<string, string> = {
  min_file_size_mb: "Mindestgröße",
  min_duration_seconds: "Mindestlaufzeit",
  scan_interval_hours: "Automatischer Scan",
  reanalyze_after_days: "Analyse neu aufrollen nach",
  min_saving_mb: "Mindestersparnis absolut",
  min_saving_percent: "Mindestersparnis",
  analysis_workers: "Parallele Analysen",
  trust_learning_after_samples: "Volles Vertrauen ab",
  crf: "Basis-Qualität (CRF)",
  crf_min: "CRF-Untergrenze",
  crf_max: "CRF-Obergrenze",
  max_width: "Maximale Breite",
  keyframe_interval_seconds: "Keyframe-Abstand",
  max_encode_hours: "Abbruch nach",
  opus_bitrate_per_channel: "Opus-Bitrate je Kanal",
  bloat_threshold_kbps_per_channel: "Schwelle für aufgeblähte Spuren",
  max_duration_drift_seconds: "Erlaubte Laufzeit-Abweichung",
  trash_retention_days: "Aufbewahrung",
  uid: "Benutzer-ID (UID)",
  gid: "Gruppen-ID (GID)",
  file_mode: "Dateirechte",
  max_concurrent_jobs: "Gleichzeitige Konvertierungen",
  cpu_threads: "CPU-Threads",
  nice_level: "Prozesspriorität",
  min_free_disk_gb: "Mindestens freier Speicher",
  schedule_start: "Beginn des Zeitfensters",
  schedule_end: "Ende des Zeitfensters",
  schedule_days: "Wochentage",
  openai_max_tokens: "Maximale Antwortlänge",
  max_calls_per_scan: "Maximale Anfragen pro Scan",
  max_crf_delta: "Maximale CRF-Verschiebung",
  timeout_seconds: "Zeitlimit pro Anfrage",
  username: "Benutzername",
  password: "Passwort",
  webhook_url: "Webhook-Adresse",
  dashboard_refresh_seconds: "Aktualisierung der Übersicht",
};

/** Whether a message is one of pydantic's English rule texts. */
const PYDANTIC_TEXT = /^(Input should|String should|Field required|Value error|Extra inputs)/;

/** Translate one pydantic rule ("Input should be ...") into German. */
function germanRule(message: string, type = ""): string {
  const num = (re: RegExp) => message.match(re)?.[1];
  let n: string | undefined;
  if ((n = num(/less than or equal to (-?[\d.]+)/))) return `darf höchstens ${n} sein`;
  if ((n = num(/greater than or equal to (-?[\d.]+)/))) return `muss mindestens ${n} sein`;
  if ((n = num(/less than (-?[\d.]+)/))) return `muss kleiner als ${n} sein`;
  if ((n = num(/greater than (-?[\d.]+)/))) return `muss größer als ${n} sein`;
  if (/valid integer/.test(message) || type.startsWith("int_")) return "muss eine ganze Zahl sein";
  if (/valid number/.test(message) || type.startsWith("float_")) return "muss eine Zahl sein";
  if (/match pattern/.test(message) || type === "string_pattern_mismatch") {
    return "hat ein ungültiges Format";
  }
  if (/at least \d+ character/.test(message) || type === "string_too_short") return "ist zu kurz";
  if (/at most \d+ character/.test(message) || type === "string_too_long") return "ist zu lang";
  if (/Field required/.test(message) || type === "missing") return "fehlt";
  if (type === "literal_error" || /^Input should be '/.test(message)) {
    return "ist kein erlaubter Wert";
  }
  return "ist ungültig";
}

function fieldLabel(path: string): string {
  const parts = path.split(".").filter((p) => p && p !== "body");
  const last = parts[parts.length - 1] ?? path;
  return FIELD_LABELS[last] ?? parts.join(".");
}

/** Turn an error ``detail`` into a German sentence.  Handles FastAPI's list of
 *  validation errors and the raw pydantic text the settings endpoint embeds. */
export function describeErrorDetail(detail: unknown): string {
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      const entry = item as { loc?: unknown[]; msg?: string; type?: string };
      const path = (entry.loc ?? []).map(String).join(".");
      return `${fieldLabel(path)}: ${germanRule(entry.msg ?? "", entry.type)}`;
    });
    return parts.length ? `Ungültige Eingabe – ${parts.join("; ")}` : "Ungültige Eingabe.";
  }
  if (typeof detail !== "string") return JSON.stringify(detail);

  // Raw pydantic text: "1 validation error for X\nfield\n  Input should ... [type=...]".
  if (/validation errors? for /.test(detail)) {
    const parts: string[] = [];
    const re = /^(\S[^\n]*)\n\s+([^\n]*?)\s*\[type=(\w+)/gm;
    let m: RegExpExecArray | null;
    while ((m = re.exec(detail)) !== null) {
      parts.push(`${fieldLabel(m[1].trim())}: ${germanRule(m[2], m[3])}`);
    }
    return parts.length ? `Ungültige Einstellungen – ${parts.join("; ")}` : detail;
  }

  // Summarised: "Ungueltige Einstellungen: queue.nice_level: Input should ...; ...".
  const summary = /^Ung(?:ue|ü)ltige Einstellungen:\s*(.+)$/s.exec(detail);
  if (summary) {
    const parts = summary[1].split(/;\s*/).map((part) => {
      const m = /^([\w.]+):\s*(.+)$/s.exec(part.trim());
      if (!m) return part.trim();
      const message = m[2].trim();
      // German messages from the server's own validators stay as they are.
      const rule = PYDANTIC_TEXT.test(message) ? germanRule(message) : message;
      return `${fieldLabel(m[1])}: ${rule}`;
    });
    return `Ungültige Einstellungen – ${parts.join("; ")}`;
  }
  return detail;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, buildInit(init));
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail !== undefined) detail = describeErrorDetail(body.detail);
    } catch {
      /* keep the status line */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (body: unknown) => (body === undefined ? undefined : JSON.stringify(body));

export const api = {
  get: <T,>(path: string, opts?: RequestOpts) => request<T>(path, { signal: opts?.signal }),
  post: <T,>(path: string, body?: unknown, opts?: RequestOpts) =>
    request<T>(path, { method: "POST", body: json(body), signal: opts?.signal }),
  put: <T,>(path: string, body: unknown) => request<T>(path, { method: "PUT", body: json(body) }),
  patch: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: json(body) }),
  del: <T,>(path: string) => request<T>(path, { method: "DELETE" }),
};

/* -------------------------------------------------------------------------- */
/* Types                                                                      */
/* -------------------------------------------------------------------------- */

export type FileState =
  | "new" | "probed" | "analyzing" | "candidate" | "skipped" | "queued"
  | "encoding" | "done" | "failed" | "missing" | "ignored";

export type JobState = "queued" | "running" | "done" | "failed" | "cancelled" | "rejected";

export interface MediaFile {
  id: number;
  path: string;
  name: string;
  folder: string;
  library_id: number | null;
  size: number;
  container: string;
  video_codec: string;
  width: number;
  height: number;
  fps: number;
  duration: number;
  video_bitrate: number;
  bit_depth: number;
  is_hdr: boolean;
  /** "hdr10", "hdr10plus", "hlg", "dolby_vision" or "dolby_vision_p<N>". */
  hdr_format: string;
  interlaced: boolean;
  state: FileState;
  ignored: boolean;
  error: string;
  estimated_size: number;
  estimated_saving_bytes: number;
  estimated_saving_pct: number;
  confidence: number;
  decision_reason: string;
  advisor_note: string;
  analysis_depth: string;
  analyzed_at: string | null;
  original_size: number;
  converted_at: string | null;
  measured_vmaf: number | null;
  audio_count: number;
  subtitle_count: number;
  audio_streams?: AudioStream[];
  subtitle_streams?: SubtitleStream[];
  plan?: EncodePlan | null;
  jobs?: Job[];
  exists?: boolean;
  analysis?: AnalysisResult;
}

export interface AudioStream {
  index: number;
  codec: string;
  channels: number;
  channel_layout: string;
  bitrate: number;
  language: string;
  title: string;
  default: boolean;
  commentary: boolean;
}

export interface SubtitleStream {
  index: number;
  codec: string;
  language: string;
  title: string;
  forced: boolean;
  default: boolean;
  text: boolean;
}

export interface EncodePlan {
  encoder: string;
  crf: number;
  preset: number;
  pix_fmt: string;
  film_grain: number;
  target_height: number;
  deinterlace: boolean;
  hw_decode: boolean;
  container: string;
  keyint_frames: number;
  audio: { index: number; action: string; codec: string; channels: number; bitrate: number; language: string; reason: string }[];
  subtitles: { index: number; action: string; codec: string; language: string }[];
  estimated_size: number;
  estimated_saving_bytes: number;
  estimated_saving_pct: number;
  predicted_video_bitrate: number;
  notes: string[];
}

export interface AnalysisResult {
  decision: "convert" | "skip" | "error";
  reason: string;
  reasons: string[];
  depth: string;
  estimated_size: number;
  estimated_saving_bytes: number;
  estimated_saving_pct: number;
  confidence: number;
  eta_seconds: number;
  plan: EncodePlan | null;
  prediction: {
    video_bitrate: number;
    size_bytes: number;
    saving_pct: number;
    confidence: number;
    source: string;
    complexity: number;
    learned_correction: number;
    notes: string[];
  } | null;
  sample: {
    measured_bitrate: number;
    spread: number;
    segments: number;
    grain_level: number;
    vmaf: number | null;
    speed_factor: number;
    ok: boolean;
    error: string;
  } | null;
  advice: {
    content_type: string;
    grain_assessment: string;
    crf_delta: number;
    reasoning: string;
    warnings: string[];
    confidence: number;
    ok: boolean;
    error: string;
    model: string;
    tokens: { input: number; output: number };
  } | null;
}

export interface Job {
  id: number;
  file_id: number;
  state: JobState;
  priority: number;
  progress: number;
  speed: number;
  fps: number;
  eta_seconds: number;
  current_size: number;
  input_size: number;
  output_size: number;
  predicted_size: number;
  vmaf: number | null;
  error: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  plan: EncodePlan | null;
  /** Queued by hand despite an exclusion or a skip verdict. */
  forced?: boolean;
  path?: string;
  name?: string;
  duration?: number;
  resolution?: string;
  log?: string;
}

export interface LibraryPathEntry {
  id: number;
  path: string;
  name: string;
  enabled: boolean;
  file_count?: number;
  total_size?: number;
  candidates?: number;
  converted?: number;
  exists?: boolean;
}

export interface HardwareReport {
  device: string;
  device_present: boolean;
  readable: boolean;
  gpu_name: string;
  driver: string;
  vainfo_ok: boolean;
  decode_av1: boolean;
  decode_hevc: boolean;
  decode_h264: boolean;
  decode_vp9: boolean;
  svt_av1: boolean;
  libvmaf: boolean;
  quality_metric: "vmaf" | "ssim" | "none";
  recommended_encoder: string;
  summary: string;
  notes: string[];
  encoders: Record<string, { name: string; available: boolean; verified: boolean; reason: string }>;
}

export interface ModelStats {
  trained: boolean;
  samples: number;
  trust_threshold: number;
  maturity: number;
  residual_std: number;
  mean_abs_error_pct: number;
  top_signals: { feature: string; weight: number }[];
}

export interface SystemInfo {
  version: string;
  python: string;
  platform: string;
  cpu_count: number;
  ffmpeg: { binary: string; version: string; encoders: string[] };
  hardware: HardwareReport | null;
  learning_model: ModelStats;
  advisor: {
    sdk_installed: boolean;
    enabled: boolean;
    provider: AdvisorProviderId;
    configured: boolean;
    reason: string;
    model: string;
    calls_used: number;
  };
  scan: ScanState;
  queue: QueueStatus;
  next_scan: string | null;
  paths: { config: string; transcode: string; transcode_free_gb: number };
}

export interface ScanState {
  run_id: number | null;
  running: boolean;
  phase: string;
  total: number;
  done: number;
  current: string;
  progress: number;
  started_at: string | null;
  candidates?: number;
  /** Walk phase only: files found so far / new ones among them. */
  seen?: number;
  new?: number;
}

/** Why the queue does not start anything right now ("" = it would). */
export type BlockedKind = "" | "paused" | "schedule" | "disk" | "scan";

export interface QueueStatus {
  running_jobs: number[];
  paused: boolean;
  schedule_ok: boolean;
  /** "Jetzt starten": the schedule is ignored until the queue has run dry. */
  schedule_override?: boolean;
  blocked_reason: string;
  /** Missing on backends that predate it - see ``blockedKind`` in format.ts. */
  blocked_kind?: BlockedKind;
  max_concurrent: number;
}

export interface Stats {
  files: {
    total: number;
    total_size: number;
    total_duration: number;
    by_state: Record<string, number>;
  };
  potential: { saving_bytes: number; candidate_count: number };
  realised: { saved_bytes: number; converted_count: number; average_vmaf: number | null };
  codecs: { codec: string; count: number; size: number }[];
  resolutions: { label: string; count: number; size: number }[];
  daily: { date: string; saved: number; count: number }[];
  top_candidates: MediaFile[];
  model: ModelStats;
}

export type HistoryLevel = "info" | "success" | "warning" | "error";

export interface HistoryItem {
  id: number;
  level: HistoryLevel;
  category: string;
  message: string;
  file_id: number | null;
  detail: Record<string, unknown> | null;
  created_at: string;
}

export interface DryRunResult {
  ok: boolean;
  returncode: number;
  seconds: number;
  encoder: string;
  hw_decode: boolean;
  pix_fmt: string;
  command: string;
  error_line: string;
  video_at_fault: boolean | null;
  output: string;
}

export interface FileListResponse {
  items: MediaFile[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
  aggregate: { count: number; total_size: number; potential_saving: number };
}

export interface JobListResponse {
  items: Job[];
  /** Per state, over all jobs - not just the ones in ``items``. */
  counts: Partial<Record<JobState, number>>;
  worker: QueueStatus;
}

export type AdvisorProviderId = "anthropic" | "openai_compatible" | "openai_codex";

export interface AdvisorProviderInfo {
  id: AdvisorProviderId;
  label: string;
  hint: string;
  needs: string[];
  sdk_installed: boolean;
}

export interface CodexStatus {
  signed_in: boolean;
  account_label?: string;
  plan_type?: string;
  account_id_present?: boolean;
  expires_at?: string | null;
  expired?: boolean;
  can_refresh?: boolean;
  last_refresh?: string | null;
  last_error?: string;
  known_models?: string[];
  redirect_uri?: string;
}

export interface AdvisorOverview {
  active: AdvisorProviderId;
  enabled: boolean;
  ready: boolean;
  reason: string;
  providers: AdvisorProviderInfo[];
  codex: CodexStatus;
  calls_used: number;
  budget_left: number;
}

export interface AdvisorTestResult {
  ok: boolean;
  message: string;
  provider: string;
  capabilities?: {
    structured: string;
    structured_label: string;
    token_field: string;
    send_temperature: boolean;
    send_system_role: boolean;
  };
}

export interface Settings {
  library: {
    extensions: string[];
    min_file_size_mb: number;
    min_duration_seconds: number;
    exclude_patterns: string[];
    follow_symlinks: boolean;
    scan_on_start: boolean;
    scan_interval_hours: number;
    rescan_changed_only: boolean;
    reanalyze_after_days: number;
  };
  analysis: {
    convert_all_h264: boolean;
    mode: "quick" | "sample" | "vmaf";
    sample_count: number;
    sample_duration: number;
    sample_skip_intro_pct: number;
    target_vmaf: number;
    vmaf_search_steps: number;
    min_saving_percent: number;
    min_saving_mb: number;
    skip_codecs: string[];
    skip_if_bitrate_below_kbps: number;
    analysis_workers: number;
    dolby_vision: "skip" | "hdr10_fallback";
    use_learning_model: boolean;
    trust_learning_after_samples: number;
  };
  encoding: {
    profile: "archive" | "balanced" | "space";
    encoder: "auto" | "svt_av1" | "av1_qsv" | "av1_vaapi";
    preset: number;
    crf: number;
    allow_crf_adjust: boolean;
    crf_min: number;
    crf_max: number;
    force_10bit: boolean;
    film_grain_synthesis: number;
    auto_film_grain: boolean;
    max_width: number;
    keyframe_interval_seconds: number;
    deinterlace: boolean;
    copy_chapters: boolean;
    copy_attachments: boolean;
    container: "mkv" | "mp4";
    extra_ffmpeg_args: string;
    max_encode_hours: number;
  };
  audio: {
    mode: "copy" | "opus" | "opus_if_bloated";
    opus_bitrate_per_channel: number;
    bloat_threshold_kbps_per_channel: number;
    keep_languages: string[];
    drop_commentary: boolean;
    keep_default_track_always: boolean;
  };
  subtitles: { mode: "copy" | "drop" | "text_only"; keep_languages: string[] };
  output: {
    mode: "replace" | "sidecar" | "separate_dir";
    output_dir: string;
    sidecar_suffix: string;
    original_action: "delete" | "trash" | "keep";
    /** Empty = ``<library root>/.optimizarr-trash``. */
    trash_dir: string;
    trash_retention_days: number;
    preserve_mtime: boolean;
    set_permissions: boolean;
    file_mode: string;
    uid: number;
    gid: number;
    require_smaller: boolean;
    min_accept_saving_percent: number;
    verify_output: boolean;
    max_duration_drift_seconds: number;
    verify_vmaf: boolean;
    min_accept_vmaf: number;
  };
  queue: {
    max_concurrent_jobs: number;
    auto_queue_candidates: boolean;
    auto_queue_min_saving_percent: number;
    paused: boolean;
    schedule_enabled: boolean;
    schedule_start: string;
    schedule_end: string;
    schedule_days: number[];
    cpu_threads: number;
    nice_level: number;
    min_free_disk_gb: number;
  };
  hardware: {
    render_device: string;
    hw_decode: boolean;
    hw_encode: boolean;
    qsv_low_power: boolean;
    fallback_to_cpu: boolean;
    detect_on_start: boolean;
  };
  advisor: {
    enabled: boolean;
    provider: AdvisorProviderId;
    /** Secret: ``SECRET_MASK`` when stored, "" when not. */
    api_key: string;
    model: string;
    openai_base_url: string;
    /** Secret: ``SECRET_MASK`` when stored, "" when not. */
    openai_api_key: string;
    openai_model: string;
    openai_structured_mode: "auto" | "json_schema" | "json_object" | "prompt";
    openai_max_tokens: number;
    openai_temperature: number;
    openai_send_system_role: boolean;
    codex_model: string;
    codex_reasoning_effort: "low" | "medium" | "high";
    mode: "uncertain_only" | "all_candidates" | "explain_only";
    allow_setting_changes: boolean;
    max_crf_delta: number;
    max_calls_per_scan: number;
    uncertain_below_confidence: number;
    timeout_seconds: number;
    include_filename: boolean;
  };
  notifications: {
    /** Secret: ``SECRET_MASK`` when stored, "" when not. */
    webhook_url: string;
    notify_on_job_done: boolean;
    notify_on_job_failed: boolean;
    notify_on_scan_done: boolean;
  };
  security: {
    auth_enabled: boolean;
    username: string;
    /** Secret: ``SECRET_MASK`` when set, "" when not.  Hashed by the server. */
    password: string;
  };
  ui: {
    size_unit: "binary" | "decimal";
    dashboard_refresh_seconds: number;
  };
}

export type SettingsPatch = {
  [K in keyof Settings]?: Partial<Settings[K]>;
};

/** What a settings change did to files that had already been analysed. */
export interface CodecExclusionResult {
  added: string[];
  removed: string[];
  excluded: number;
  restored: number;
  queued_untouched: number;
}

export type SettingsSaveResult = Settings & {
  applied?: { codec_exclusions?: CodecExclusionResult; h264_reanalysis?: number };
};

/** One video codec the library contains, or one that is excluded by hand. */
export interface LibraryCodec {
  codec: string;
  label: string;
  files: number;
  total_size: number;
  candidates: number;
  excluded: boolean;
}

export interface LibraryCodecs {
  items: LibraryCodec[];
  known: { codec: string; label: string }[];
}

/** Progress-bar segment of one episode, in display order. */
export type SeriesBucket =
  | "converted" | "av1" | "active" | "pending" | "excluded" | "failed" | "other";

export interface SeriesTally {
  episodes: number;
  /** Converted by Optimizarr plus files that were AV1 already. */
  in_av1: number;
  total_size: number;
  saved_bytes: number;
  potential_saving: number;
  counts: Record<SeriesBucket, number>;
  last_converted: string | null;
}

export interface SeriesSummary extends SeriesTally {
  key: string;
  library_id: number;
  library: string;
  name: string;
  path: string;
  season_count: number;
}

export type SeriesEpisode = MediaFile & {
  season: number | null;
  episode: number | null;
  bucket: SeriesBucket;
};

export interface SeriesSeason extends SeriesTally {
  season: number | null;
  label: string;
  files: SeriesEpisode[];
}

export interface SeriesDetail extends SeriesSummary {
  seasons: SeriesSeason[];
}

export interface MovieFile {
  id: number;
  path: string;
  name: string;
  state: FileState;
  bucket: SeriesBucket;
  ignored: boolean;
  video_codec: string;
  width: number;
  height: number;
  size: number;
  original_size: number;
  estimated_saving_bytes: number;
  decision_reason: string;
  error: string;
}

/** A movie folder; ``episodes`` from the tally is its file count. */
export interface MovieSummary extends SeriesTally {
  key: string;
  library_id: number;
  library: string;
  name: string;
  title: string;
  year: number | null;
  path: string;
  /** Largest first: the film itself, then other versions and extras. */
  files: MovieFile[];
}

export interface EnqueueResult {
  added: number;
  skipped: string[];
  message: string;
}

export interface BrowseResult {
  path: string;
  parent: string | null;
  entries: { name: string; path: string; readable: boolean }[];
}

/* -------------------------------------------------------------------------- */
/* Endpoints                                                                  */
/* -------------------------------------------------------------------------- */

/** Query string from the defined, non-empty values. */
function query(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== "") q.set(k, String(v));
  });
  const s = q.toString();
  return s ? `?${s}` : "";
}

// GET endpoints take ``opts`` last so they can be handed to useQuery as the
// query function directly: react-query passes its context, whose ``signal``
// aborts the request when the result is no longer wanted.
export const endpoints = {
  systemInfo: (opts?: RequestOpts) => api.get<SystemInfo>("/system/info", opts),
  detectHardware: () => api.post<HardwareReport>("/system/detect-hardware"),
  renderDevices: (opts?: RequestOpts) =>
    api.get<{ devices: { path: string; writable: boolean; is_render_node: boolean }[]; dri_present: boolean }>(
      "/system/render-devices",
      opts,
    ),
  refitModel: () => api.post<ModelStats>("/system/refit-model"),

  advisorOverview: (opts?: RequestOpts) => api.get<AdvisorOverview>("/advisor/providers", opts),
  advisorTest: (payload: Record<string, string>) =>
    api.post<AdvisorTestResult>("/advisor/test", payload),
  /** POST, so the key never ends up in a URL or an access log. An empty key
   *  lets the server use the stored one - only for the stored base URL. */
  advisorOpenAIModels: (baseUrl: string, apiKey: string) =>
    api.post<{ ok: boolean; models: string[]; message: string }>("/advisor/openai/models", {
      base_url: baseUrl,
      api_key: apiKey || null,
    }),
  codexStart: () =>
    api.post<{ authorize_url: string; state: string; redirect_uri: string; instructions: string }>(
      "/advisor/codex/start",
    ),
  codexComplete: (pasted: string, state?: string) =>
    api.post<{ ok: boolean; message: string; status: CodexStatus }>("/advisor/codex/complete", {
      pasted,
      state,
    }),
  codexImport: (authJson: string) =>
    api.post<{ ok: boolean; message: string; status: CodexStatus }>("/advisor/codex/import", {
      auth_json: authJson,
    }),
  codexLogout: () => api.post<{ ok: boolean; message: string }>("/advisor/codex/logout"),
  codexModels: (refresh = false) =>
    api.get<{ ok: boolean; models: string[]; message: string }>(
      `/advisor/codex/models?refresh=${refresh}`,
    ),

  settings: (opts?: RequestOpts) => api.get<Settings>("/settings", opts),
  saveSettings: (patch: SettingsPatch) => api.put<SettingsSaveResult>("/settings", patch),
  applyProfile: (name: string) => api.post<Settings>(`/settings/profile/${name}`),
  resetSettings: () => api.post<SettingsSaveResult>("/settings/reset"),
  /** An empty or masked URL tests the stored one. */
  testNotification: (webhookUrl?: string) =>
    api.post<{ ok: boolean; message: string }>(
      "/notifications/test",
      webhookUrl ? { webhook_url: webhookUrl } : {},
    ),

  libraryPaths: (opts?: RequestOpts) => api.get<LibraryPathEntry[]>("/library/paths", opts),
  libraryCodecs: (opts?: RequestOpts) => api.get<LibraryCodecs>("/library/codecs", opts),
  addLibraryPath: (payload: { path: string; name?: string }) =>
    api.post<LibraryPathEntry>("/library/paths", payload),
  updateLibraryPath: (id: number, payload: Partial<LibraryPathEntry>) =>
    api.patch<LibraryPathEntry>(`/library/paths/${id}`, payload),
  deleteLibraryPath: (id: number) =>
    api.del<{ ok: boolean; jobs_cancelled?: number; jobs_removed?: number }>(`/library/paths/${id}`),
  browse: (path: string, opts?: RequestOpts) =>
    api.get<BrowseResult>(`/library/browse${query({ path })}`, opts),

  files: (params: Record<string, string | number | undefined>, opts?: RequestOpts) =>
    api.get<FileListResponse>(`/files${query(params)}`, opts),
  file: (id: number, opts?: RequestOpts) => api.get<MediaFile>(`/files/${id}`, opts),
  analyzeFile: (id: number, depth?: string) =>
    api.post<MediaFile>(`/files/${id}/analyze${query({ depth })}`),
  dryRun: (id: number, seconds = 15) =>
    api.post<DryRunResult>(`/files/${id}/dry-run`, { seconds }),
  ignoreFile: (id: number, ignored: boolean) =>
    api.post<MediaFile>(`/files/${id}/ignore?ignored=${ignored}`),

  startScan: (payload?: { depth?: string; file_ids?: number[] }) =>
    api.post<{ ok: boolean; status: ScanState }>("/scan", payload ?? {}),
  cancelScan: () => api.post<{ ok: boolean }>("/scan/cancel"),

  /** ``state``: "active" (queued + running), "finished", or one job state. */
  jobs: (params: { state?: string; limit?: number } = {}, opts?: RequestOpts) =>
    api.get<JobListResponse>(`/jobs${query(params)}`, opts),
  job: (id: number, opts?: RequestOpts) => api.get<Job>(`/jobs/${id}`, opts),
  enqueue: (payload: {
    file_ids?: number[];
    all_candidates?: boolean;
    min_saving_pct?: number;
    limit?: number;
    force?: boolean;
  }) => api.post<EnqueueResult>("/jobs", payload),
  cancelJob: (id: number) => api.post<{ ok: boolean; message: string }>(`/jobs/${id}/cancel`),
  retryJob: (id: number) => api.post<{ ok: boolean; message: string }>(`/jobs/${id}/retry`),
  clearFinished: () => api.del<{ removed: number }>("/jobs/finished"),
  pauseQueue: (paused: boolean) => api.post<{ paused: boolean }>("/queue/pause", { paused }),
  startNow: (active: boolean) => api.post<{ active: boolean }>("/queue/start-now", { active }),

  series: (opts?: RequestOpts) =>
    api.get<{ items: SeriesSummary[]; totals: SeriesTally }>("/series", opts),
  seriesDetail: (key: string, opts?: RequestOpts) =>
    api.get<SeriesDetail>(`/series/detail${query({ key })}`, opts),
  enqueueSeries: (payload: { key: string; season?: number; force?: boolean }) =>
    api.post<EnqueueResult>("/series/enqueue", payload),
  movies: (opts?: RequestOpts) =>
    api.get<{ items: MovieSummary[]; totals: SeriesTally }>("/movies", opts),

  stats: (opts?: RequestOpts) => api.get<Stats>("/stats", opts),
  modelStats: (opts?: RequestOpts) =>
    api.get<{
      stats: ModelStats;
      samples: {
        created_at: string;
        predicted_kbps: number;
        actual_kbps: number;
        error_pct: number;
        encoder: string;
        crf: number;
        source_codec: string;
        vmaf: number | null;
      }[];
    }>("/stats/model", opts),
  /** ``level`` is filtered by the server, so the limit applies to that level. */
  history: (params: { limit?: number; level?: string } = {}, opts?: RequestOpts) =>
    api.get<HistoryItem[]>(
      `/history${query({ limit: params.limit ?? 60, level: params.level === "all" ? undefined : params.level })}`,
      opts,
    ),
};
