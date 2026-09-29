import { type Settings } from "../../lib/api";
import { useToast } from "../../lib/live";
import { toggleWeekday } from "../../lib/settings";
import { Callout, Field, NumberField, Panel, SliderField, Toggle, cn } from "../../components/ui";
import type { UpdateFn } from "./types";

const WEEKDAYS = [
  { short: "Mo", long: "Montag" },
  { short: "Di", long: "Dienstag" },
  { short: "Mi", long: "Mittwoch" },
  { short: "Do", long: "Donnerstag" },
  { short: "Fr", long: "Freitag" },
  { short: "Sa", long: "Samstag" },
  { short: "So", long: "Sonntag" },
];

export function QueueTab({ draft, update }: { draft: Settings; update: UpdateFn }) {
  const { push } = useToast();
  const toggleDay = (day: number) => {
    const next = toggleWeekday(draft.queue.schedule_days, day);
    if (!next) {
      push("Mindestens ein Wochentag muss ausgewählt bleiben.", "info");
      return;
    }
    update("queue", { schedule_days: next });
  };

  return (
    <div className="space-y-4">
      <Panel title="Verarbeitung">
        <div className="grid gap-5 md:grid-cols-2">
          <Field
            label="Gleichzeitige Konvertierungen"
            hint="Auf einem Heimserver ist 1 fast immer richtig – AV1-Encoding nutzt ohnehin alle Kerne."
          >
            <NumberField
              value={draft.queue.max_concurrent_jobs}
              onChange={(max_concurrent_jobs) => update("queue", { max_concurrent_jobs })}
              min={1}
              max={8}
              ariaLabel="Gleichzeitige Konvertierungen"
            />
          </Field>
          <Field
            label="CPU-Threads"
            hint="0 nutzt alle Kerne. Begrenzt pro Job den SVT-AV1-Encoder und die Decoder-Threads – niedriger setzen, wenn parallel noch andere Dienste laufen."
          >
            <NumberField
              value={draft.queue.cpu_threads}
              onChange={(cpu_threads) => update("queue", { cpu_threads })}
              min={0}
              max={128}
              ariaLabel="CPU-Threads"
            />
          </Field>
          <Field
            label="Prozesspriorität (nice)"
            hint="Höher = freundlicher zu anderen Diensten. 10 ist ein guter Wert für einen NAS."
          >
            <SliderField
              value={draft.queue.nice_level}
              onChange={(nice_level) => update("queue", { nice_level })}
              min={-20}
              max={19}
              ariaLabel="Prozesspriorität (nice)"
            />
          </Field>
          <Field
            label="Mindestens freier Speicher"
            hint="Unterhalb dieser Grenze startet kein neuer Job."
          >
            <NumberField
              value={draft.queue.min_free_disk_gb}
              onChange={(min_free_disk_gb) => update("queue", { min_free_disk_gb })}
              min={0}
              suffix="GB"
              ariaLabel="Mindestens freier Speicher"
            />
          </Field>
        </div>
      </Panel>

      <Panel title="Automatik">
        <div className="space-y-4">
          <Toggle
            checked={draft.queue.auto_queue_candidates}
            onChange={(auto_queue_candidates) => update("queue", { auto_queue_candidates })}
            label="Kandidaten nach dem Scan automatisch einreihen"
            hint="Ohne diese Option entscheidest du in der Bibliothek selbst, was konvertiert wird."
          />
          {draft.queue.auto_queue_candidates && (
            <Field
              label="Nur ab dieser Ersparnis automatisch einreihen"
              hint="Schützt davor, dass Grenzfälle ungefragt Rechenzeit verbrauchen. H.264 im Umstellungsmodus wird unabhängig von dieser Schwelle eingereiht."
            >
              <SliderField
                value={draft.queue.auto_queue_min_saving_percent}
                onChange={(auto_queue_min_saving_percent) =>
                  update("queue", { auto_queue_min_saving_percent })
                }
                min={5}
                max={80}
                format={(v) => `${v} %`}
                ariaLabel="Nur ab dieser Ersparnis automatisch einreihen"
              />
            </Field>
          )}
        </div>
      </Panel>

      <Panel title="Zeitfenster" subtitle="Encoden nur dann, wenn der Server ohnehin nichts tut">
        <div className="space-y-4">
          <Toggle
            checked={draft.queue.schedule_enabled}
            onChange={(schedule_enabled) => update("queue", { schedule_enabled })}
            label="Nur innerhalb eines Zeitfensters konvertieren"
          />
          {draft.queue.schedule_enabled && (
            <>
              <div className="grid gap-5 sm:grid-cols-2">
                <Field label="Beginn">
                  <input
                    type="time"
                    className="field"
                    value={draft.queue.schedule_start}
                    onChange={(e) => update("queue", { schedule_start: e.target.value })}
                    aria-label="Beginn des Zeitfensters"
                  />
                </Field>
                <Field label="Ende" hint="Ein Fenster darf über Mitternacht laufen.">
                  <input
                    type="time"
                    className="field"
                    value={draft.queue.schedule_end}
                    onChange={(e) => update("queue", { schedule_end: e.target.value })}
                    aria-label="Ende des Zeitfensters"
                  />
                </Field>
              </div>
              <Field
                label="Wochentage"
                hint="Mindestens ein Tag bleibt ausgewählt – ohne Tag würde nie konvertiert."
              >
                <div className="flex flex-wrap gap-2" role="group" aria-label="Wochentage">
                  {WEEKDAYS.map((day, index) => {
                    const active = draft.queue.schedule_days.includes(index);
                    return (
                      <button
                        key={day.short}
                        onClick={() => toggleDay(index)}
                        aria-pressed={active}
                        aria-label={day.long}
                        title={day.long}
                        className={cn(
                          "rounded-lg border px-3 py-1.5 text-sm transition-colors",
                          active
                            ? "border-brand-500 bg-brand-600/15 text-brand-400"
                            : "border-ink-700 text-ink-400 hover:border-ink-600",
                        )}
                      >
                        {day.short}
                      </button>
                    );
                  })}
                </div>
              </Field>
              {draft.queue.schedule_days.length === 0 && (
                <Callout tone="danger">
                  Es ist kein Wochentag ausgewählt – so startet nie ein Job. Bitte mindestens
                  einen Tag wählen.
                </Callout>
              )}
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Hardware                                                                   */
/* -------------------------------------------------------------------------- */

/** Options for the render device: render nodes first, then card* nodes, and
 *  the stored value even when the container does not show it (any more). */
