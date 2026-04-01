# AEP-005: Agent-to-Agent (A2A) Protocol Support

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P1 |
| **Effort** | High (5-7 days) |
| **Impact** | High |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
All agents run **in-process** within the same Python runtime:
- Sub-agents communicate via shared `session.state` and `output_key`
- The devops-assistant orchestrator uses `AgentTool` for LLM-routed delegation
- No network boundary exists between agents

This works for a single deployment but doesn't support:
- Agents running on different machines or in different containers
- Teams owning and deploying agents independently
- Integrating with third-party agent systems
- Scaling individual agents independently

### What ADK Provides
ADK supports the **Agent-to-Agent (A2A) Protocol** for inter-agent communication:

1. **Exposing agents**: Wrap any ADK agent in an `A2AServer` to make it network-accessible
2. **Consuming agents**: Use `RemoteA2aAgent` as a client proxy that looks like a local sub-agent
3. **Standard protocol**: Based on the [A2A Protocol](https://a2a-protocol.org) specification
4. **Cross-language**: A2A agents can be in Python, Go, Java, or TypeScript

### Gap
For an enterprise DevOps platform:
- The Kafka agent team might want to deploy and scale their agent independently
- A security team might want to add a vulnerability scanning agent without modifying the core
- Multiple DevOps platforms might want to share agents (e.g., a central Kubernetes agent)
- The Slack bot and the web UI should be able to reach agents over the network

## Proposed Solution

### Step 1: Expose Each Agent as an A2A Server

Each agent package becomes independently deployable:

```python
# agents/kafka-health/serve.py
from google.adk.a2a import A2AServer
from kafka_health_agent import agent

server = A2AServer(agent=agent.root_agent)
server.run(port=8001)
```

### Step 2: Consume Remote Agents in the Orchestrator

The devops-assistant uses `RemoteA2aAgent` instead of importing agents directly:

```python
from google.adk.a2a import RemoteA2aAgent

kafka_remote = RemoteA2aAgent(
    name="kafka_health_agent",
    description="Kafka cluster health monitoring",
    url="http://kafka-agent:8001",
)

root_agent = create_agent(
    name="devops_assistant",
    sub_agents=[kafka_remote, k8s_remote, ...],
)
```

### Step 3: Agent Discovery

Create an agent registry for service discovery:

```yaml
# infra/agent-registry.yml
agents:
  kafka-health:
    url: http://kafka-agent:8001
    description: Kafka cluster health monitoring
    capabilities: [health_check, topic_management, consumer_groups]
  k8s-health:
    url: http://k8s-agent:8002
    description: Kubernetes cluster management
    capabilities: [pod_management, deployment_scaling, log_retrieval]
```

### Step 4: Docker Compose for Multi-Agent Deployment

```yaml
# docker-compose.a2a.yml
services:
  kafka-agent:
    build: agents/kafka-health
    ports: ["8001:8001"]
    command: python serve.py

  k8s-agent:
    build: agents/k8s-health
    ports: ["8002:8002"]
    command: python serve.py

  devops-assistant:
    build: agents/devops-assistant
    ports: ["8000:8000"]
    environment:
      KAFKA_AGENT_URL: http://kafka-agent:8001
      K8S_AGENT_URL: http://k8s-agent:8002
    depends_on: [kafka-agent, k8s-agent]
```

### Step 5: Hybrid Mode

Support both local and remote agents:

```python
def create_devops_assistant():
    kafka_agent_url = os.getenv("KAFKA_AGENT_URL")

    if kafka_agent_url:
        kafka = RemoteA2aAgent(name="kafka_health_agent", url=kafka_agent_url)
    else:
        from kafka_health_agent.agent import root_agent as kafka
        kafka = AgentTool(agent=kafka)

    return create_agent(name="devops_assistant", sub_agents=[kafka, ...])
```

## Affected Files

| File | Change |
|------|--------|
| `agents/kafka-health/serve.py` | New: A2A server entrypoint |
| `agents/k8s-health/serve.py` | New: A2A server entrypoint |
| `agents/observability/serve.py` | New: A2A server entrypoint |
| `agents/devops-assistant/devops_assistant/agent.py` | Support remote agents via env vars |
| `infra/agent-registry.yml` | New: agent discovery config |
| `docker-compose.a2a.yml` | New: multi-agent deployment |
| `Makefile` | Add `make run-a2a` target |

## Acceptance Criteria

- [ ] Each agent can be deployed as an independent A2A server
- [ ] Devops-assistant can consume agents locally or remotely (env var toggle)
- [ ] Docker Compose config for multi-agent deployment
- [ ] Agent discovery via registry config
- [ ] Inter-agent communication works over HTTP
- [ ] RBAC and plugins still apply when agents are remote
- [ ] Health checks work across agent boundaries
- [ ] Latency impact documented (local vs remote)

## Notes

- A2A is best suited for **microservices-style deployment** where teams independently own agents. For a single-team project, local sub-agents are simpler and faster.
- Start with optional A2A support (env var toggle) so the project works both ways.
- Consider authentication between agents (mTLS or API keys) for production deployments.
- A2A adds network latency; for the incident triage parallel health check, this could slow down the response. Benchmark before committing to A2A for latency-sensitive workflows.
