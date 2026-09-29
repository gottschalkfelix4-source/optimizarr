import { AlertTriangle } from "lucide-react";
import { SECRET_MASK, type Settings } from "../../lib/api";
import { Callout, Field, Panel, SecretField, Toggle } from "../../components/ui";
import type { UpdateFn } from "./types";

export function SecurityTab({
  draft,
  saved,
  update,
}: {
  draft: Settings;
  saved: Settings;
  update: UpdateFn;
}) {
  const stored = saved.security.password === SECRET_MASK;
  const hasPassword = draft.security.password !== "";
  return (
    <div className="space-y-4">
      {!saved.security.auth_enabled && (
        <Callout tone="warn" icon={<AlertTriangle className="size-4" />}>
          Die Anmeldung ist aus: Jeder, der den Server im Netzwerk erreicht, kann Optimizarr
          bedienen, Dateien konvertieren und Einstellungen ändern.
        </Callout>
      )}
      <Panel
        title="Anmeldung"
        subtitle="Benutzername und Passwort für die Oberfläche und die API"
      >
        <div className="space-y-5">
          <Toggle
            checked={draft.security.auth_enabled}
            onChange={(auth_enabled) => update("security", { auth_enabled })}
            label="Anmeldung verlangen"
            hint="Der Browser fragt dann einmal nach Benutzername und Passwort. Nur die Statusabfrage /api/health bleibt offen."
          />
          <div className="grid gap-5 md:grid-cols-2">
            <Field label="Benutzername">
              <input
                className="field"
                value={draft.security.username}
                onChange={(e) => update("security", { username: e.target.value })}
                autoComplete="username"
                aria-label="Benutzername"
                spellCheck={false}
              />
            </Field>
            <Field
              label="Passwort"
              hint="Wird nur als Hash gespeichert. Leer lassen, um das bisherige zu behalten."
            >
              <SecretField
                value={draft.security.password}
                stored={stored}
                onChange={(password) => update("security", { password })}
                placeholder="Neues Passwort"
                ariaLabel="Passwort"
              />
            </Field>
          </div>
          {draft.security.auth_enabled && !hasPassword && (
            <Callout tone="danger">
              Ohne Passwort lässt sich die Anmeldung nicht einschalten. Bitte ein Passwort
              eintragen.
            </Callout>
          )}
          <p className="text-xs leading-relaxed text-ink-400">
            Über reines HTTP werden Benutzername und Passwort nur kodiert, nicht verschlüsselt
            übertragen. Für den Zugriff von außerhalb des Heimnetzes gehört Optimizarr hinter
            einen Reverse-Proxy mit HTTPS.
          </p>
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* System                                                                     */
/* -------------------------------------------------------------------------- */
