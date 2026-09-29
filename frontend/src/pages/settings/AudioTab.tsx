import { type Settings } from "../../lib/api";
import { Field, NumberField, Panel, Select, TagListField, Toggle } from "../../components/ui";
import type { UpdateFn } from "./types";

export function AudioTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  return (
    <div className="space-y-4">
      <Panel
        title="Tonspuren"
        subtitle="Bei einem 4-GB-Film können 1,5 GB auf eine unkomprimierte Tonspur entfallen"
      >
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Umgang mit Audio">
            <Select
              value={draft.audio.mode}
              onChange={(mode) => update("audio", { mode })}
              ariaLabel="Umgang mit Audio"
              options={[
                { value: "opus_if_bloated", label: "Nur aufgeblähte Spuren umwandeln (empfohlen)" },
                { value: "copy", label: "Immer unverändert kopieren" },
                { value: "opus", label: "Alles nach Opus umwandeln" },
              ]}
            />
          </Field>
          <Field
            label="Opus-Bitrate je Kanal"
            hint="48 kbit/s pro Kanal sind bei Opus transparent – 5.1 landet bei rund 288 kbit/s."
          >
            <NumberField
              value={draft.audio.opus_bitrate_per_channel}
              onChange={(opus_bitrate_per_channel) => update("audio", { opus_bitrate_per_channel })}
              min={24}
              max={128}
              suffix="kbit/s"
              ariaLabel="Opus-Bitrate je Kanal"
            />
          </Field>
          {draft.audio.mode === "opus_if_bloated" && (
            <Field
              label="Ab wann gilt eine Spur als aufgebläht"
              hint="Spuren darunter werden unverändert kopiert. Verlustfreie Formate wie TrueHD werden immer umgewandelt."
            >
              <NumberField
                value={draft.audio.bloat_threshold_kbps_per_channel}
                onChange={(bloat_threshold_kbps_per_channel) =>
                  update("audio", { bloat_threshold_kbps_per_channel })
                }
                min={32}
                max={512}
                suffix="kbit/s je Kanal"
                ariaLabel="Ab wann gilt eine Spur als aufgebläht"
              />
            </Field>
          )}
          <Field
            label="Sprachen behalten"
            hint="Leer lässt alle Spuren drin. Sonst ISO-Codes wie deu, eng – durch Kommas getrennt."
          >
            <TagListField
              values={draft.audio.keep_languages}
              onChange={(keep_languages) => update("audio", { keep_languages })}
              placeholder="deu, eng"
              ariaLabel="Audio-Sprachen behalten"
            />
          </Field>
        </div>
        <div className="mt-5 space-y-3 border-t border-ink-800 pt-5">
          <Toggle
            checked={draft.audio.drop_commentary}
            onChange={(drop_commentary) => update("audio", { drop_commentary })}
            label="Kommentarspuren entfernen"
          />
          <Toggle
            checked={draft.audio.keep_default_track_always}
            onChange={(keep_default_track_always) => update("audio", { keep_default_track_always })}
            label="Standardspur nie entfernen"
            hint="Schutz davor, aus Versehen eine Datei ohne Ton zu erzeugen."
          />
        </div>
      </Panel>

      <Panel title="Untertitel">
        <div className="grid gap-5 md:grid-cols-2">
          <Field label="Umgang mit Untertiteln">
            <Select
              value={draft.subtitles.mode}
              onChange={(mode) => update("subtitles", { mode })}
              ariaLabel="Umgang mit Untertiteln"
              options={[
                { value: "copy", label: "Alle übernehmen (empfohlen)" },
                { value: "text_only", label: "Nur Text-Untertitel (SRT/ASS)" },
                { value: "drop", label: "Alle entfernen" },
              ]}
            />
          </Field>
          <Field label="Sprachen behalten" hint="Erzwungene Untertitel bleiben immer erhalten.">
            <TagListField
              values={draft.subtitles.keep_languages}
              onChange={(keep_languages) => update("subtitles", { keep_languages })}
              placeholder="deu, eng"
              ariaLabel="Untertitel-Sprachen behalten"
            />
          </Field>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Output & checks                                                            */
/* -------------------------------------------------------------------------- */
