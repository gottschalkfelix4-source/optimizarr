import { useMutation } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { endpoints, SECRET_MASK, type Settings } from "../../lib/api";
import { useToast } from "../../lib/live";
import { Field, Panel, SecretField, Spinner, Toggle } from "../../components/ui";
import type { UpdateFn } from "./types";

export function NotificationsTab({
  draft,
  saved,
  update,
  unsaved,
}: {
  draft: Settings;
  saved: Settings;
  update: UpdateFn;
  unsaved: boolean;
}) {
  const { push } = useToast();
  const stored = saved.notifications.webhook_url === SECRET_MASK;
  const url = draft.notifications.webhook_url;
  // The masked value tells the server to use the stored address; a typed one
  // is tested as it is, before saving.
  const testable = url !== "" && (url !== SECRET_MASK || stored);

  const test = useMutation({
    mutationFn: () => endpoints.testNotification(url),
    onSuccess: (result) => {
      const ok = result?.ok !== false;
      push(
        result?.message || (ok ? "Testnachricht gesendet." : "Senden fehlgeschlagen."),
        ok ? "success" : "error",
      );
    },
    onError: (e: Error) => push(e.message, "error"),
  });

  return (
    <div className="space-y-4">
      <Panel
        title="Webhook"
        subtitle="Optimizarr schickt bei den gewählten Ereignissen eine JSON-Nachricht per POST"
      >
        <div className="space-y-5">
          <Field
            label="Webhook-Adresse"
            hint={
              <>
                Zum Beispiel ein Home-Assistant-Webhook, ntfy, Gotify oder ein eigener Dienst. Der
                Inhalt ist <code className="text-ink-300">{"{event, title, message, data}"}</code>.
                Leer = keine Benachrichtigungen.
              </>
            }
          >
            <SecretField
              type="text"
              value={draft.notifications.webhook_url}
              stored={stored}
              onChange={(webhook_url) => update("notifications", { webhook_url })}
              placeholder="https://…"
              ariaLabel="Webhook-Adresse"
            />
          </Field>
          <div className="space-y-3 border-t border-ink-800 pt-5">
            <Toggle
              checked={draft.notifications.notify_on_job_done}
              onChange={(notify_on_job_done) => update("notifications", { notify_on_job_done })}
              label="Wenn eine Konvertierung fertig ist"
            />
            <Toggle
              checked={draft.notifications.notify_on_job_failed}
              onChange={(notify_on_job_failed) => update("notifications", { notify_on_job_failed })}
              label="Wenn eine Konvertierung fehlschlägt oder verworfen wird"
            />
            <Toggle
              checked={draft.notifications.notify_on_scan_done}
              onChange={(notify_on_scan_done) => update("notifications", { notify_on_scan_done })}
              label="Wenn ein Bibliotheks-Scan abgeschlossen ist"
            />
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-ink-800 pt-5">
            <button
              className="btn-ghost"
              onClick={() => test.mutate()}
              disabled={test.isPending || !testable}
            >
              {test.isPending ? <Spinner className="size-4" /> : <Send className="size-4" aria-hidden="true" />}
              Test senden
            </button>
            <span className="text-xs text-ink-500">
              {!testable
                ? "Zuerst eine Adresse eintragen."
                : url === SECRET_MASK
                  ? "Schickt eine Testnachricht an die gespeicherte Adresse."
                  : unsaved
                    ? "Schickt eine Testnachricht an die eingetragene Adresse – gespeichert wird dabei nichts."
                    : "Schickt eine Testnachricht an die eingetragene Adresse."}
            </span>
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Security                                                                   */
/* -------------------------------------------------------------------------- */
