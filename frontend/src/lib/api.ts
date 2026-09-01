/** Typed access to the Optimizarr backend. */

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep the status line */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
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
  profile: string | null;
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
}

export interface QueueStatus {
  running_jobs: number[];
  paused: boolean;
  schedule_ok: boolean;
  blocked_reason: string;
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

export interface HistoryItem {
  id: number;
  level: "info" | "success" | "warning" | "error";
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
    api_key: string;
    model: string;
    openai_base_url: string;
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
    webhook_url: string;
    notify_on_job_done: boolean;
    notify_on_job_failed: boolean;
    notify_on_scan_done: boolean;
  };
  ui: {
    theme: "dark" | "light" | "system";
    language: "de" | "en";
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
  applied?: { codec_exclusions?: CodecExclusionResult };
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

/* -------------------------------------------------------------------------- */
/* Endpoints                                                                  */
/* -------------------------------------------------------------------------- */

export const endpoints = {
  systemInfo: () => api.get<SystemInfo>("/system/info"),
  detectHardware: () => api.post<HardwareReport>("/system/detect-hardware"),
  renderDevices: () =>
    api.get<{ devices: { path: string; writable: boolean; is_render_node: boolean }[]; dri_present: boolean }>(
      "/system/render-devices",
    ),
  refitModel: () => api.post<ModelStats>("/system/refit-model"),

  advisorOverview: () => api.get<AdvisorOverview>("/advisor/providers"),
  advisorTest: (payload: Record<string, string>) =>
    api.post<AdvisorTestResult>("/advisor/test", payload),
  advisorOpenAIModels: (baseUrl: string, apiKey: string) =>
    api.get<{ ok: boolean; models: string[]; message: string }>(
      `/advisor/openai/models?base_url=${encodeURIComponent(baseUrl)}&api_key=${encodeURIComponent(apiKey)}`,
    ),
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

  settings: () => api.get<Settings>("/settings"),
  saveSettings: (patch: SettingsPatch) => api.put<SettingsSaveResult>("/settings", patch),
  applyProfile: (name: string) => api.post<Settings>(`/settings/profile/${name}`),
  testAdvisor: (payload: { api_key?: string; model?: string }) =>
    api.post<{ ok: boolean; message: string }>("/settings/test-advisor", payload),
  resetSettings: () => api.post<SettingsSaveResult>("/settings/reset"),

  libraryPaths: () => api.get<LibraryPathEntry[]>("/library/paths"),
  libraryCodecs: () => api.get<LibraryCodecs>("/library/codecs"),
  addLibraryPath: (payload: { path: string; name?: string }) =>
    api.post<LibraryPathEntry>("/library/paths", payload),
  updateLibraryPath: (id: number, payload: Partial<LibraryPathEntry>) =>
    api.patch<LibraryPathEntry>(`/library/paths/${id}`, payload),
  deleteLibraryPath: (id: number) => api.del<{ ok: boolean }>(`/library/paths/${id}`),
  browse: (path: string) =>
    api.get<{ path: string; parent: string | null; entries: { name: string; path: string; readable: boolean }[] }>(
      `/library/browse?path=${encodeURIComponent(path)}`,
    ),

  files: (params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "") query.set(k, String(v));
    });
    return api.get<FileListResponse>(`/files?${query.toString()}`);
  },
  file: (id: number) => api.get<MediaFile>(`/files/${id}`),
  analyzeFile: (id: number, depth?: string) =>
    api.post<MediaFile>(`/files/${id}/analyze${depth ? `?depth=${depth}` : ""}`),
  dryRun: (id: number, seconds = 15) =>
    api.post<DryRunResult>(`/files/${id}/dry-run`, { seconds }),
  ignoreFile: (id: number, ignored: boolean) =>
    api.post<MediaFile>(`/files/${id}/ignore?ignored=${ignored}`),

  startScan: (payload?: { depth?: string; file_ids?: number[] }) =>
    api.post<{ ok: boolean; status: ScanState }>("/scan", payload ?? {}),
  cancelScan: () => api.post<{ ok: boolean }>("/scan/cancel"),
  scanStatus: () => api.get<{ live: ScanState; last_run: unknown }>("/scan/status"),

  jobs: (state?: string) => api.get<{ items: Job[]; counts: Record<string, number>; worker: QueueStatus }>(
    `/jobs${state ? `?state=${state}` : ""}`,
  ),
  job: (id: number) => api.get<Job>(`/jobs/${id}`),
  enqueue: (payload: {
    file_ids?: number[];
    all_candidates?: boolean;
    min_saving_pct?: number;
    limit?: number;
  }) => api.post<{ added: number; skipped: string[]; message: string }>("/jobs", payload),
  cancelJob: (id: number) => api.post<{ ok: boolean; message: string }>(`/jobs/${id}/cancel`),
  retryJob: (id: number) => api.post<{ ok: boolean; message: string }>(`/jobs/${id}/retry`),
  clearFinished: () => api.del<{ removed: number }>("/jobs/finished"),
  pauseQueue: (paused: boolean) => api.post<{ paused: boolean }>("/queue/pause", { paused }),

  stats: () => api.get<Stats>("/stats"),
  modelStats: () =>
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
    }>("/stats/model"),
  history: (limit = 60) => api.get<HistoryItem[]>(`/history?limit=${limit}`),
};
