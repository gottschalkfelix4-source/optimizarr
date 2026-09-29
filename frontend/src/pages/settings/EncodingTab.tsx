import { type Settings } from "../../lib/api";
import { Field, NumberField, Panel, Select, SliderField, Toggle, cn } from "../../components/ui";
import type { UpdateFn } from "./types";

export function EncodingTab({
  draft,
  update,
  onProfile,
  applying,
}: {
  draft: Settings;
  update: UpdateFn;
  onProfile: (name: string) => void;
  applying: boolean;
}) {
  const profiles = [
    {
      id: "archive",
      title: "Archiv",
      desc: "Praktisch nicht unterscheidbar vom Original. Weniger Ersparnis, langsamster Encode.",
      crf: 24,
    },
    {
      id: "balanced",
      title: "Ausgewogen",
      desc: "Empfohlen. Deutliche Ersparnis bei kaum sichtbarem Unterschied.",
      crf: 30,
    },
    {
      id: "space",
      title: "Platz sparen",
      desc: "Maximale Ersparnis, schneller Encode. Auf großen TVs sichtbar weicher.",
      crf: 35,
    },
  ];

  return (
    <div className="space-y-4">
      <Panel
        title="Qualitätsprofil"
        subtitle="Setzt CRF, Preset und Qualitätsziel in einem Rutsch und speichert sofort"
      >
        <div className="grid gap-3 md:grid-cols-3">
          {profiles.map((profile) => (
            <button
              key={profile.id}
              onClick={() => onProfile(profile.id)}
              disabled={applying}
              aria-pressed={draft.encoding.profile === profile.id}
              className={cn(
                "rounded-lg border p-4 text-left transition-colors disabled:opacity-60",
                draft.encoding.profile === profile.id
                  ? "border-brand-500 bg-brand-600/10"
                  : "border-ink-700 bg-ink-850/40 hover:border-ink-600",
              )}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-ink-100">{profile.title}</span>
                <span className="font-mono text-xs text-ink-500">CRF {profile.crf}</span>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-ink-400">{profile.desc}</p>
            </button>
          ))}
        </div>
      </Panel>

      <Panel title="Encoder">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Encoder-Auswahl"
            hint="„Automatisch“ nimmt den GPU-Encoder, sobald ein Testlauf beweist, dass er funktioniert."
          >
            <Select
              value={draft.encoding.encoder}
              onChange={(encoder) => update("encoding", { encoder })}
              ariaLabel="Encoder-Auswahl"
              options={[
                { value: "auto", label: "Automatisch (empfohlen)" },
                { value: "svt_av1", label: "SVT-AV1 (CPU)" },
                { value: "av1_qsv", label: "Intel QSV AV1 (GPU)" },
                { value: "av1_vaapi", label: "Intel VAAPI AV1 (GPU)" },
              ]}
            />
          </Field>
          <Field label="Container">
            <Select
              value={draft.encoding.container}
              onChange={(container) => update("encoding", { container })}
              ariaLabel="Container"
              options={[
                { value: "mkv", label: "MKV (empfohlen – kann alles)" },
                { value: "mp4", label: "MP4 (kompatibler, verliert Bild-Untertitel)" },
              ]}
            />
          </Field>

          <Field
            label="Basis-Qualität (CRF)"
            hint="Niedriger = besser und größer. Die Analyse verschiebt diesen Wert pro Datei."
          >
            <SliderField
              value={draft.encoding.crf}
              onChange={(crf) => update("encoding", { crf })}
              min={16}
              max={50}
              ariaLabel="Basis-Qualität (CRF)"
              marks={[
                { value: 16, label: "16 · sehr gut" },
                { value: 32, label: "32" },
                { value: 50, label: "50 · klein" },
              ]}
            />
          </Field>
          <Field
            label="Preset (nur SVT-AV1)"
            hint="Niedriger = langsamer, aber kleinere Dateien bei gleicher Qualität. 6 ist ein guter Alltagswert."
          >
            <SliderField
              value={draft.encoding.preset}
              onChange={(preset) => update("encoding", { preset })}
              min={0}
              max={13}
              ariaLabel="Preset (nur SVT-AV1)"
              marks={[
                { value: 0, label: "0 · sehr langsam" },
                { value: 6, label: "6" },
                { value: 13, label: "13 · sehr schnell" },
              ]}
            />
          </Field>

          <Field label="CRF-Untergrenze" hint="Die Analyse darf nicht unter diesen Wert gehen.">
            <NumberField
              value={draft.encoding.crf_min}
              onChange={(crf_min) => update("encoding", { crf_min })}
              min={1}
              max={63}
              ariaLabel="CRF-Untergrenze"
            />
          </Field>
          <Field label="CRF-Obergrenze">
            <NumberField
              value={draft.encoding.crf_max}
              onChange={(crf_max) => update("encoding", { crf_max })}
              min={1}
              max={63}
              ariaLabel="CRF-Obergrenze"
            />
          </Field>
        </div>

        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.encoding.allow_crf_adjust}
            onChange={(allow_crf_adjust) => update("encoding", { allow_crf_adjust })}
            label="CRF pro Datei automatisch anpassen"
            hint="Die Analyse sucht den höchsten CRF, der das Qualitätsziel noch hält."
          />
          <Toggle
            checked={draft.encoding.force_10bit}
            onChange={(force_10bit) => update("encoding", { force_10bit })}
            label="Immer in 10 Bit kodieren"
            hint="AV1 komprimiert auch 8-Bit-Quellen in 10 Bit effizienter und vermeidet Farbstufen in Verläufen."
          />
          <Toggle
            checked={draft.encoding.auto_film_grain}
            onChange={(auto_film_grain) => update("encoding", { auto_film_grain })}
            label="Filmkorn automatisch erkennen und synthetisieren"
            hint="Bei körnigem Material wird das Korn vor dem Encoden entfernt und bei der Wiedergabe neu erzeugt – das spart sehr viel Bitrate. Auf sauberem Material bleibt die Funktion aus."
          />
          <Toggle
            checked={draft.encoding.deinterlace}
            onChange={(deinterlace) => update("encoding", { deinterlace })}
            label="Interlaced-Material deinterlacen"
          />
          <Toggle
            checked={draft.encoding.copy_chapters}
            onChange={(copy_chapters) => update("encoding", { copy_chapters })}
            label="Kapitelmarken übernehmen"
          />
          <Toggle
            checked={draft.encoding.copy_attachments}
            onChange={(copy_attachments) => update("encoding", { copy_attachments })}
            label="Anhänge übernehmen (Schriftarten für ASS-Untertitel)"
          />
        </div>
      </Panel>

      <Panel title="Erweitert">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Maximale Breite"
            hint="0 behält die Auflösung bei. Sonst wird proportional herunterskaliert."
          >
            <NumberField
              value={draft.encoding.max_width}
              onChange={(max_width) => update("encoding", { max_width })}
              min={0}
              suffix="px"
              ariaLabel="Maximale Breite"
            />
          </Field>
          <Field label="Keyframe-Abstand">
            <NumberField
              value={draft.encoding.keyframe_interval_seconds}
              onChange={(keyframe_interval_seconds) =>
                update("encoding", { keyframe_interval_seconds })
              }
              min={1}
              max={30}
              suffix="Sek"
              ariaLabel="Keyframe-Abstand"
            />
          </Field>
          <Field
            label="Filmkorn-Synthese fest"
            hint="0 lässt die automatische Erkennung entscheiden. Sonst gilt dieser Wert für alle Dateien."
          >
            <SliderField
              value={draft.encoding.film_grain_synthesis}
              onChange={(film_grain_synthesis) => update("encoding", { film_grain_synthesis })}
              min={0}
              max={50}
              format={(v) => (v === 0 ? "auto" : String(v))}
              ariaLabel="Filmkorn-Synthese fest"
            />
          </Field>
          <Field label="Abbruch nach" hint="Sicherheitsnetz gegen hängende Encodes.">
            <NumberField
              value={draft.encoding.max_encode_hours}
              onChange={(max_encode_hours) => update("encoding", { max_encode_hours })}
              min={1}
              max={72}
              suffix="Std"
              ariaLabel="Abbruch nach"
            />
          </Field>
          <div className="md:col-span-2">
            <Field
              label="Zusätzliche ffmpeg-Parameter"
              hint="Werden unverändert an ffmpeg angehängt. Nur für Leute, die wissen, was sie tun."
            >
              <input
                className="field font-mono text-sm"
                value={draft.encoding.extra_ffmpeg_args}
                onChange={(e) => update("encoding", { extra_ffmpeg_args: e.target.value })}
                placeholder="-svtav1-params enable-overlays=1"
                aria-label="Zusätzliche ffmpeg-Parameter"
                spellCheck={false}
              />
            </Field>
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Audio & subtitles                                                          */
/* -------------------------------------------------------------------------- */
