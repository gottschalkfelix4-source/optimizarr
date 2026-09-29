import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu } from "lucide-react";
import { endpoints, type Settings } from "../../lib/api";
import { useToast } from "../../lib/live";
import { Callout, Field, Panel, Select, Spinner, Toggle, withCurrent } from "../../components/ui";
import type { UpdateFn } from "./types";

export function renderDeviceOptions(
  devices: { path: string; writable: boolean; is_render_node: boolean }[],
  current: string,
): { value: string; label: string }[] {
  const sorted = [...devices].sort(
    (a, b) => Number(b.is_render_node) - Number(a.is_render_node) || a.path.localeCompare(b.path),
  );
  const options = sorted.map((d) => ({
    value: d.path,
    label:
      `${d.path}${d.is_render_node ? "" : " (kein Render-Node)"}` +
      `${d.writable ? "" : " (kein Schreibzugriff)"}`,
  }));
  return withCurrent(options, current, (v) => `${v} (im Container nicht gefunden)`);
}

export function HardwareTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const { push } = useToast();
  const queryClient = useQueryClient();

  const { data: devices } = useQuery({
    queryKey: ["render-devices"],
    queryFn: endpoints.renderDevices,
  });
  const { data: info } = useQuery({ queryKey: ["system"], queryFn: endpoints.systemInfo });

  const detect = useMutation({
    mutationFn: () => endpoints.detectHardware(),
    onSuccess: (report) => {
      push(report.summary, "success");
      queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  const hw = info?.hardware;

  return (
    <div className="space-y-4">
      {hw && (
        <Callout tone={hw.device_present ? "info" : "warn"} icon={<Cpu className="size-4" />}>
          {hw.summary}
        </Callout>
      )}

      <Panel
        title="Intel-Grafik"
        actions={
          <button
            className="btn-ghost btn-sm"
            onClick={() => detect.mutate()}
            disabled={detect.isPending}
          >
            {detect.isPending ? <Spinner className="size-3.5" /> : <Cpu className="size-3.5" aria-hidden="true" />}
            Erneut erkennen
          </button>
        }
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Render-Gerät"
            hint={
              devices?.dri_present
                ? "Gefundene Geräte im Container. Für die Arc/iGPU wird ein renderD-Gerät gebraucht."
                : "/dev/dri ist nicht sichtbar – im Unraid-Template als Device hinzufügen."
            }
          >
            {devices?.devices.length ? (
              <Select
                value={draft.hardware.render_device}
                onChange={(render_device) => update("hardware", { render_device })}
                ariaLabel="Render-Gerät"
                options={renderDeviceOptions(devices.devices, draft.hardware.render_device)}
              />
            ) : (
              <input
                className="field font-mono text-sm"
                value={draft.hardware.render_device}
                onChange={(e) => update("hardware", { render_device: e.target.value })}
                aria-label="Render-Gerät"
              />
            )}
          </Field>
        </div>

        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.hardware.hw_encode}
            onChange={(hw_encode) => update("hardware", { hw_encode })}
            label="AV1 auf der GPU kodieren, wenn möglich"
            hint="Nur Intel Arc und Core Ultra können das. Ältere iGPUs fallen automatisch auf die CPU zurück."
          />
          <Toggle
            checked={draft.hardware.hw_decode}
            onChange={(hw_decode) => update("hardware", { hw_decode })}
            label="Quellmaterial auf der GPU dekodieren"
            hint="Entlastet die CPU spürbar. Wird nur für Codecs genutzt, die die GPU nachweislich beherrscht."
          />
          <Toggle
            checked={draft.hardware.qsv_low_power}
            onChange={(qsv_low_power) => update("hardware", { qsv_low_power })}
            label="QSV Low-Power-Modus (VDENC)"
            hint="Auf den meisten Intel-Chips zwingend erforderlich. Nur abschalten, wenn das Hardware-Encoding sonst nicht startet."
          />
          <Toggle
            checked={draft.hardware.fallback_to_cpu}
            onChange={(fallback_to_cpu) => update("hardware", { fallback_to_cpu })}
            label="Bei Hardware-Fehlern auf die CPU ausweichen"
            hint="Ein abgebrochener GPU-Encode wird automatisch mit SVT-AV1 wiederholt, statt als Fehler zu enden."
          />
          <Toggle
            checked={draft.hardware.detect_on_start}
            onChange={(detect_on_start) => update("hardware", { detect_on_start })}
            label="Hardware beim Start prüfen"
          />
        </div>
      </Panel>

      {info?.ffmpeg && (
        <Panel title="ffmpeg">
          <dl className="space-y-2 text-sm">
            <div className="flex flex-wrap justify-between gap-2">
              <dt className="text-ink-500">Version</dt>
              <dd className="font-mono text-xs text-ink-300">{info.ffmpeg.version}</dd>
            </div>
            <div className="flex flex-wrap justify-between gap-2">
              <dt className="text-ink-500">Programm</dt>
              <dd className="font-mono text-xs text-ink-300">{info.ffmpeg.binary}</dd>
            </div>
            <div className="flex flex-wrap justify-between gap-2">
              <dt className="text-ink-500">Relevante Encoder</dt>
              <dd className="font-mono text-xs text-ink-300">
                {info.ffmpeg.encoders.join(", ") || "-"}
              </dd>
            </div>
          </dl>
        </Panel>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Notifications                                                              */
/* -------------------------------------------------------------------------- */
