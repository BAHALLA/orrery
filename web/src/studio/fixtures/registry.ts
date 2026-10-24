import type { StudioRegistry } from "../types";

/** `deploy/studio-demo/registry.yaml` as `GET /studio/registry` lists it, plus two tools a run may not use. */
export const DEMO_REGISTRY: StudioRegistry = {
  mcp: [
    {
      id: "tirraflow",
      url: "https://tirraflow.com/mcp",
      transport: "streamable_http",
      private_network: false,
      tools: [
        { name: "search_articles", classification: "read_only", pinned: false },
        { name: "get_article", classification: "read_only", pinned: false },
        { name: "publish", classification: "destructive", pinned: false },
      ],
    },
  ],
  a2a: [
    {
      id: "demo-writer",
      card_url: "https://demo-writer.studio.internal:8443/.well-known/agent-card.json",
      skills: ["draft"],
      effect: "read_only",
      private_network: true,
      authenticated: false,
      description: "Drafts release notes.",
      card: {
        digest: null,
        pinned_by: null,
        resolved: false,
        fetched_at: null,
        error: null,
        skills: [],
      },
    },
  ],
  catalog: [
    { ref: "get_consumer_lag", read_only: true },
    { ref: "k8s.restart_deployment", read_only: false },
  ],
};
