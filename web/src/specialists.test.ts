import { describe, expect, it } from "vitest";
import { agentLabel } from "./chat/trace";
import { specialistStatuses } from "./components/systemStatus";
import {
  joinLabels,
  resolveSpecialists,
  setActiveSpecialists,
  specialistFor,
  specialistLabel,
  systemLabels,
} from "./specialists";

const custom = [
  { key: "kafka", name: "kafka_health_agent" },
  { key: "billing", name: "billing_ledger_agent", label: "Billing ledger" },
  { key: "ops_journal", name: "ops_journal_agent" },
];

describe("specialists", () => {
  it("falls back to the stock set when the server doesn't list any", () => {
    expect(resolveSpecialists(undefined).map((s) => s.key)).toEqual([
      "kafka",
      "k8s",
      "elasticsearch",
      "observability",
      "docker",
      "ops_journal",
    ]);
    expect(resolveSpecialists([]).length).toBe(0);
  });

  it("labels by the server's label, then the stock label, then the name", () => {
    expect(specialistLabel(custom[0])).toBe("Kafka");
    expect(specialistLabel(custom[1])).toBe("Billing ledger");
    expect(specialistLabel({ key: "x", name: "warehouse_agent" })).toBe("Warehouse");
  });

  it("leaves the ops journal out of the systems a sweep covers", () => {
    expect(systemLabels(custom)).toEqual(["Kafka", "Billing ledger"]);
    expect(joinLabels(["A", "B", "C"], "or")).toBe("A, B or C");
    expect(joinLabels(["A"])).toBe("A");
  });

  it("matches agents by name, or a checker node by key", () => {
    expect(specialistFor("billing_ledger_agent", custom)?.key).toBe("billing");
    expect(specialistFor("kafka_health_checker", custom)?.key).toBe("kafka");
    expect(specialistFor("k8s_health_agent", custom)).toBeNull();
  });

  it("names a plugin specialist by its label in traces and the triage rollup", () => {
    setActiveSpecialists(custom);
    expect(agentLabel("billing_ledger_agent")).toBe("Billing ledger");
    // Stock names still read well from a server whose list omits them.
    expect(agentLabel("k8s_health_agent")).toBe("Kubernetes");

    const rows = specialistStatuses([
      {
        operation: "get_balance",
        details: "[billing_ledger_agent] account=1 → success",
        timestamp: "2026-07-25T10:00:00Z",
      },
      {
        operation: "list_pods",
        details: "[k8s_health_agent] ns=default → success",
        timestamp: "2026-07-25T10:00:00Z",
      },
    ]);
    // Only systems this deployment runs get a row.
    expect(rows.map((r) => r.label)).toEqual(["Billing ledger"]);
  });
});
