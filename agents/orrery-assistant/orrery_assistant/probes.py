"""Read-only reachability probes for the console's first-run self-test.

Each specialist gets its single cheapest read-only tool. The question these
answer is not "is the cluster healthy" — the agent does that — but "is this
integration wired at all", which is the failure a new user actually hits and the
one the platform is worst at reporting: today a missing kubeconfig surfaces as a
stack trace inside a tool result, several turns into a conversation.

Every probe here **must** stay read-only. They run on an authenticated but
otherwise ungated path (the self-test does not consult RBAC), so anything with
side effects does not belong.

The list lives in the agent package rather than in core because core has no
dependency on any agent — the console stays a transport over ``AgentGateway``.
"""

from __future__ import annotations

from orrery_core.serving.onboarding import IntegrationProbe


def default_probes() -> list[IntegrationProbe]:
    """The probes for the specialists this assistant composes.

    Imports are deferred to call time so that a missing optional dependency
    surfaces as one red row in the console rather than preventing the server
    from starting.
    """

    async def kafka() -> dict:
        from kafka_health_agent.tools import get_kafka_cluster_health

        return await get_kafka_cluster_health()

    async def kubernetes() -> dict:
        from k8s_health_agent.tools import get_cluster_info

        return await get_cluster_info()

    async def elasticsearch() -> dict:
        from elasticsearch_agent.tools import get_cluster_health

        return await get_cluster_health()

    async def prometheus() -> dict:
        from observability_agent.tools import get_prometheus_targets

        return await get_prometheus_targets()

    async def docker() -> dict:
        from docker_agent.tools import list_containers

        return await list_containers()

    return [
        IntegrationProbe(
            name="kafka",
            label="Kafka",
            hint=(
                "Set KAFKA_BOOTSTRAP_SERVERS to a reachable broker list; for a secured "
                "cluster also KAFKA_SECURITY_PROTOCOL and its TLS/SASL settings."
            ),
            run=kafka,
        ),
        IntegrationProbe(
            name="kubernetes",
            label="Kubernetes",
            hint="Provide a kubeconfig (KUBECONFIG_PATH) or run inside a cluster.",
            run=kubernetes,
        ),
        IntegrationProbe(
            name="elasticsearch",
            label="Elasticsearch",
            hint="Set ELASTICSEARCH_URL (and credentials, if the cluster needs them).",
            run=elasticsearch,
        ),
        IntegrationProbe(
            name="prometheus",
            label="Prometheus",
            hint="Set PROMETHEUS_URL to a reachable Prometheus instance.",
            run=prometheus,
        ),
        IntegrationProbe(
            name="docker",
            label="Docker",
            hint="Ensure the docker CLI is on PATH and the daemon socket is reachable.",
            run=docker,
        ),
    ]
