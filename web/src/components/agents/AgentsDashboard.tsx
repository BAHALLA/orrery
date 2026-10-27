import { useEffect, useState } from "react";
import type { CatalogUpdate } from "../../api/types";
import type { SystemController } from "../../system/useSystem";
import { Dashboard } from "./Dashboard";

interface Props {
  system: SystemController;
  /** False while the console shows the chat: the page stays mounted, hidden,
   * so a draft survives navigating away (or a triage verdict pulling the
   * console back to the chat). */
  active: boolean;
  /** Told whenever the draft gains or loses unsaved changes. */
  onDirtyChange: (dirty: boolean) => void;
}

/**
 * What the coordinator can reach, for admins, as a full page: what is running
 * (versions, model), each agent with its tools grouped by guard level, and —
 * when the deployment allows editing — a switch per specialist and the
 * composed agents, saved together.
 *
 * Served agents come from the running tree (`/admin/agents`), so a disabled
 * specialist is absent there because it is absent from what runs. The catalog
 * adds what is *not* served: switched-off specialists (tools taken from the
 * catalog's tool list) and the ones the deployment's env excludes.
 */
export function AgentsDashboard({ system, active, onDirtyChange }: Props) {
  const { agents, deployment, agentsError, loadAgents, loadCatalog, saveCatalog } = system;
  // Only a well-formed catalog counts: an older server answers with something else.
  const catalog = system.catalog?.catalog ? system.catalog : null;
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  // The draft of a save that failed. A conflict reloads the catalog, and the new
  // version remounts the dashboard: without this the admin's edits would vanish.
  const [carried, setCarried] = useState<CatalogUpdate | null>(null);

  useEffect(() => {
    if (agents === null) void loadAgents();
  }, [agents, loadAgents]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  if (agentsError) {
    return (
      <div className="p-6">
        <p
          role="alert"
          className="mx-auto max-w-6xl rounded-lg bg-crit-bg px-3 py-2 text-sm text-crit-fg"
        >
          {agentsError}
        </p>
      </div>
    );
  }
  if (agents === null) {
    return <p className="p-6 text-sm text-slate-500 dark:text-slate-400">Loading agents…</p>;
  }

  return (
    <Dashboard
      // A successful save re-seeds the draft from the new version.
      key={catalog?.catalog.version ?? "read-only"}
      agents={agents}
      deployment={deployment}
      catalog={catalog}
      active={active}
      onDirtyChange={onDirtyChange}
      savedVersion={savedVersion}
      carried={carried}
      saveError={saveError}
      onDiscard={() => {
        setSaveError(null);
        setCarried(null);
      }}
      onSave={async (update) => {
        // Cleared first: a successful save remounts the dashboard *during*
        // saveCatalog, and a draft from an earlier failure must not seed it.
        setCarried(null);
        const error = await saveCatalog(update);
        setSaveError(error);
        if (error) setCarried(update);
        if (!error) setSavedVersion(update.version + 1);
      }}
    />
  );
}
