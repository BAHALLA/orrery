import { tonePill, type StatusTone } from "../components/severity";
import { formatMs, panelCard, sectionTitle } from "./format";
import type { RunView } from "./runState";
import type { StudioRegistry } from "./types";

const outcomeTone: Record<string, StatusTone> = {
  success: "ok",
  failure: "crit",
  refused: "refused",
};

/**
 * MCP servers in the registry (their tools and how each is classified) and
 * the MCP calls of the run on show. Read-only is the only usable class: a
 * Studio run is held to autonomy L2. There is no try-it call yet, because the
 * API has none.
 */
export function McpInspector({
  registry,
  view,
}: {
  registry: StudioRegistry | null;
  view: RunView | null;
}) {
  const servers = registry?.mcp ?? [];
  const calls = view?.mcp ?? [];
  const catalog = registry?.catalog ?? [];
  return (
    <div className="flex flex-col gap-4">
      <section aria-labelledby="studio-mcp-calls">
        <h3 id="studio-mcp-calls" className={sectionTitle}>
          Calls in this run {calls.length ? `(${calls.length})` : ""}
        </h3>
        {calls.length === 0 ? (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {view
              ? "This run made no MCP calls."
              : "Run a composition with an mcp: tool to see its calls here."}
          </p>
        ) : (
          <div className="orrery-scroll mt-1.5 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-[10px] tracking-wide text-slate-500 uppercase dark:text-slate-400">
                <tr>
                  <th scope="col" className="py-1 pr-2 font-medium">
                    Node
                  </th>
                  <th scope="col" className="py-1 pr-2 font-medium">
                    Tool
                  </th>
                  <th scope="col" className="py-1 pr-2 font-medium">
                    Outcome
                  </th>
                  <th scope="col" className="py-1 text-right font-medium">
                    Time
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {calls.map((c) => (
                  <tr key={c.seq}>
                    <td className="py-1.5 pr-2 font-mono text-slate-700 dark:text-slate-300">
                      {c.node_id}
                    </td>
                    <td className="py-1.5 pr-2 font-mono text-slate-800 dark:text-slate-200">
                      {c.entry}.{c.tool}
                      <span className="block font-sans text-[10px] text-slate-500 dark:text-slate-400">
                        {c.classification}
                      </span>
                    </td>
                    <td className="py-1.5 pr-2">
                      <span
                        className={`rounded px-1.5 py-px text-[10px] font-semibold ${tonePill[outcomeTone[c.outcome] ?? "neutral"]}`}
                      >
                        {c.outcome}
                      </span>
                      {c.status && c.status !== "success" ? (
                        <span className="block font-mono text-[10px] text-slate-500 dark:text-slate-400">
                          {c.status}
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1.5 text-right font-mono text-slate-500 tabular-nums dark:text-slate-400">
                      {formatMs(c.elapsed_ms)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-labelledby="studio-mcp-servers">
        <h3 id="studio-mcp-servers" className={sectionTitle}>
          MCP servers {servers.length ? `(${servers.length})` : ""}
        </h3>
        {registry === null ? (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Loading the registry…</p>
        ) : servers.length === 0 ? (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            The registry lists no MCP servers.
          </p>
        ) : (
          <ul className="mt-1.5 flex flex-col gap-2">
            {servers.map((s) => (
              <li key={s.id} className={`${panelCard} p-3`}>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
                    {s.id}
                  </span>
                  <span className={`rounded px-1 py-px text-[10px] ${tonePill.neutral}`}>
                    {s.transport.replace("_", " ")}
                  </span>
                  {s.private_network ? (
                    <span className={`rounded px-1 py-px text-[10px] ${tonePill.neutral}`}>
                      private network
                    </span>
                  ) : null}
                </div>
                <p
                  className="mt-1 truncate font-mono text-[11px] text-slate-500 dark:text-slate-400"
                  title={s.url}
                >
                  {s.url}
                </p>
                <ul className="mt-2 flex flex-col gap-1">
                  {s.tools.map((t) => {
                    const ro = t.classification === "read_only";
                    return (
                      <li key={t.name} className="flex items-center gap-2 text-xs">
                        <span className="min-w-0 flex-1 truncate font-mono text-slate-800 dark:text-slate-200">
                          {t.name}
                        </span>
                        {t.pinned ? (
                          <span className={`rounded px-1 text-[10px] ${tonePill.neutral}`}>
                            pinned
                          </span>
                        ) : null}
                        <span
                          className={`rounded px-1.5 py-px text-[10px] font-semibold ${ro ? tonePill.ok : tonePill.crit}`}
                          title={
                            ro
                              ? "Usable in a Studio run"
                              : "Not usable: Studio runs are read-only (L2)"
                          }
                        >
                          {ro ? "read-only" : t.classification}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-[11px] text-slate-500 dark:text-slate-400">
          Tool schemas and a try-it call need an API the Studio does not have yet.
        </p>
      </section>

      {catalog.length ? (
        <section aria-labelledby="studio-catalog">
          <h3 id="studio-catalog" className={sectionTitle}>
            Catalogue tools ({catalog.filter((t) => t.read_only).length} usable of {catalog.length})
          </h3>
          <ul className="mt-1.5 flex flex-wrap gap-1">
            {catalog.map((t) => (
              <li
                key={t.ref}
                className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${t.read_only ? tonePill.neutral : `${tonePill.refused} line-through`}`}
                title={t.read_only ? "read-only" : "changes state: not usable in a Studio run"}
              >
                {t.ref}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
