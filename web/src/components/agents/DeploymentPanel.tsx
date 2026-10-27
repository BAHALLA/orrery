import type { CatalogResponse, DeploymentInfo } from "../../api/types";
import { ageLabel } from "../agentDraft";
import { panel } from "./styles";

/** What is running: versions, model, autonomy, uptime and the catalog's version. */
export function DeploymentPanel({
  deployment,
  catalog,
}: {
  deployment: DeploymentInfo | null;
  catalog: CatalogResponse | null;
}) {
  if (!deployment && !catalog) return null;
  const rows: [string, string, (string | undefined)?][] = [];
  if (deployment) {
    rows.push(["Orrery", deployment.orrery_version || "unknown"]);
    rows.push(["Model", deployment.model || deployment.model_provider, deployment.model_provider]);
    rows.push(["Autonomy", deployment.autonomy_level ?? "not set"]);
    rows.push(["ADK", deployment.adk_version || "unknown"]);
    rows.push(["Python", deployment.python_version]);
    rows.push([
      "Up for",
      ageLabel(deployment.started_at).replace(" ago", ""),
      new Date(deployment.started_at * 1000).toLocaleString(),
    ]);
    if (deployment.enabled_specialists || deployment.disabled_specialists) {
      rows.push([
        "Selected by env",
        [
          deployment.enabled_specialists ? `only ${deployment.enabled_specialists}` : "",
          deployment.disabled_specialists ? `not ${deployment.disabled_specialists}` : "",
        ]
          .filter(Boolean)
          .join(" · "),
      ]);
    }
  }
  if (catalog) {
    const c = catalog.catalog;
    rows.push([
      "Catalog",
      `v${c.version}${c.updated_by ? ` · ${c.updated_by}` : ""}${c.updated_at ? ` · ${ageLabel(c.updated_at)}` : ""}`,
      c.updated_at ? new Date(c.updated_at * 1000).toLocaleString() : undefined,
    ]);
    if (catalog.applied_version !== c.version) {
      rows.push(["Serving", `v${catalog.applied_version} (catching up)`]);
    }
  }

  return (
    <section aria-label="Deployment" className={panel}>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
        {rows.map(([label, value, title]) => (
          <div key={label} className="min-w-0">
            <dt className="text-[11px] font-medium tracking-wide text-slate-500 uppercase dark:text-slate-400">
              {label}
            </dt>
            <dd
              className="truncate font-mono text-[13px] text-slate-800 dark:text-slate-100"
              title={title ?? value}
            >
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
