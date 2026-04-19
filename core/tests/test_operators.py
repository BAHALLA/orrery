"""Tests for the operator registry and built-in detectors."""

from __future__ import annotations

import pytest

from orrery_core import (
    CRDRef,
    ECKDetector,
    OperatorRegistry,
    OperatorStatus,
    StrimziDetector,
    default_registry,
)

# ── Registry mechanics ────────────────────────────────────────────────


class TestRegistry:
    def test_register_and_get_by_name(self):
        reg = OperatorRegistry()
        det = StrimziDetector()
        reg.register(det)
        assert reg.get_by_name("strimzi") is det

    def test_get_by_kind(self):
        reg = OperatorRegistry()
        reg.register(StrimziDetector())
        det = reg.get_by_kind("Kafka")
        assert det is not None and det.name == "strimzi"
        det = reg.get_by_kind("KafkaTopic")
        assert det is not None and det.name == "strimzi"
        assert reg.get_by_kind("Unknown") is None

    def test_get_by_group(self):
        reg = OperatorRegistry()
        reg.register(ECKDetector())
        det = reg.get_by_group("elasticsearch.k8s.elastic.co")
        assert det is not None and det.name == "eck"
        assert reg.get_by_group("some.other.io") is None

    def test_get_by_api_version(self):
        reg = OperatorRegistry()
        reg.register(ECKDetector())
        det = reg.get_by_api_version("elasticsearch.k8s.elastic.co/v1")
        assert det is not None and det.name == "eck"
        assert reg.get_by_api_version("v1") is None  # core kinds never match

    def test_resolve_returns_crd_ref(self):
        reg = OperatorRegistry()
        reg.register(StrimziDetector())
        result = reg.resolve("Kafka", "kafka.strimzi.io/v1beta2")
        assert result is not None
        det, crd = result
        assert det.name == "strimzi"
        assert crd.plural == "kafkas"

    def test_resolve_falls_back_across_versions(self):
        reg = OperatorRegistry()
        reg.register(StrimziDetector())
        # unlisted version still resolves because the kind matches
        result = reg.resolve("Kafka", "kafka.strimzi.io/v1beta3")
        assert result is not None

    def test_resolve_requires_group_version_form(self):
        reg = OperatorRegistry()
        reg.register(StrimziDetector())
        assert reg.resolve("Kafka", "v1") is None

    def test_later_registration_wins(self):
        reg = OperatorRegistry()

        class FakeStrimzi:
            name = "fake-strimzi"
            crd_groups = ("kafka.strimzi.io",)
            watched = (CRDRef("kafka.strimzi.io", "v1beta2", "kafkas", "Kafka"),)

            def interpret_status(self, kind, cr):  # pragma: no cover - unused
                return OperatorStatus(healthy=True, summary="")

        reg.register(StrimziDetector())
        reg.register(FakeStrimzi())
        det = reg.get_by_kind("Kafka")
        assert det is not None and det.name == "fake-strimzi"

    def test_unregister(self):
        reg = OperatorRegistry()
        reg.register(StrimziDetector())
        reg.unregister("strimzi")
        assert reg.get_by_name("strimzi") is None

    def test_clear(self):
        reg = OperatorRegistry()
        reg.register(StrimziDetector())
        reg.register(ECKDetector())
        reg.clear()
        assert reg.all() == []


# ── Strimzi status interpretation ─────────────────────────────────────


def _kafka_cr(name="my-kafka", conditions=None, kafka_metadata_state=None):
    return {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "Kafka",
        "metadata": {"name": name, "namespace": "kafka"},
        "spec": {},
        "status": {
            "conditions": conditions or [],
            **({"kafkaMetadataState": kafka_metadata_state} if kafka_metadata_state else {}),
        },
    }


class TestStrimziDetector:
    def test_healthy_when_ready_true(self):
        cr = _kafka_cr(
            conditions=[{"type": "Ready", "status": "True"}],
            kafka_metadata_state="KRaft",
        )
        status = StrimziDetector().interpret_status("Kafka", cr)
        assert status.healthy is True
        assert status.phase == "KRaft"
        assert "healthy" in status.summary

    def test_unhealthy_when_ready_false(self):
        cr = _kafka_cr(
            conditions=[
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "RollingUpdate",
                    "message": "Broker-0 is restarting",
                }
            ]
        )
        status = StrimziDetector().interpret_status("Kafka", cr)
        assert status.healthy is False
        assert any("RollingUpdate" in w or "restarting" in w for w in status.warnings)

    def test_kafka_connector_failed_tasks(self):
        cr = {
            "kind": "KafkaConnector",
            "metadata": {"name": "sink"},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "connectorStatus": {
                    "connector": {"state": "RUNNING"},
                    "tasks": [
                        {"id": 0, "state": "RUNNING"},
                        {"id": 1, "state": "FAILED"},
                    ],
                },
            },
        }
        status = StrimziDetector().interpret_status("KafkaConnector", cr)
        assert status.healthy is False
        assert status.phase == "RUNNING"
        assert any("FAILED" in w for w in status.warnings)

    def test_rebalance_state_as_phase(self):
        cr = {
            "kind": "KafkaRebalance",
            "metadata": {"name": "rb"},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "state": "ProposalReady",
            },
        }
        status = StrimziDetector().interpret_status("KafkaRebalance", cr)
        assert status.phase == "ProposalReady"

    def test_empty_status(self):
        cr = {"kind": "Kafka", "metadata": {"name": "k"}, "status": {}}
        status = StrimziDetector().interpret_status("Kafka", cr)
        assert status.healthy is False  # no Ready=True → unhealthy by default
        assert status.conditions == []


# ── ECK status interpretation ─────────────────────────────────────────


def _es_cr(name="es", health="green", phase="Ready", available=3):
    return {
        "apiVersion": "elasticsearch.k8s.elastic.co/v1",
        "kind": "Elasticsearch",
        "metadata": {"name": name, "namespace": "elastic"},
        "status": {
            "health": health,
            "phase": phase,
            "availableNodes": available,
            "conditions": [
                {"type": "ReconciliationComplete", "status": "True"},
                {"type": "ElasticsearchIsReachable", "status": "True"},
            ],
        },
    }


class TestECKDetector:
    def test_healthy_green(self):
        status = ECKDetector().interpret_status("Elasticsearch", _es_cr())
        assert status.healthy is True
        assert status.phase == "Ready"
        assert status.ready_replicas == 3

    def test_red_cluster_is_unhealthy(self):
        cr = _es_cr(health="red")
        status = ECKDetector().interpret_status("Elasticsearch", cr)
        assert status.healthy is False
        assert any("RED" in w for w in status.warnings)

    def test_yellow_cluster_is_warned(self):
        cr = _es_cr(health="yellow")
        status = ECKDetector().interpret_status("Elasticsearch", cr)
        assert status.healthy is False
        assert any("YELLOW" in w for w in status.warnings)

    def test_applying_changes_phase(self):
        cr = _es_cr(phase="ApplyingChanges")
        status = ECKDetector().interpret_status("Elasticsearch", cr)
        assert status.healthy is False
        assert any("ApplyingChanges" in w for w in status.warnings)

    def test_es_not_reachable_condition(self):
        cr = _es_cr()
        cr["status"]["conditions"] = [
            {
                "type": "ElasticsearchIsReachable",
                "status": "False",
                "message": "cert expired",
            }
        ]
        status = ECKDetector().interpret_status("Elasticsearch", cr)
        assert status.healthy is False
        assert any("ElasticsearchIsReachable" in w for w in status.warnings)


# ── Default registry sanity ───────────────────────────────────────────


class TestDefaultRegistry:
    def test_strimzi_registered(self):
        assert default_registry.get_by_name("strimzi") is not None

    def test_eck_registered(self):
        assert default_registry.get_by_name("eck") is not None

    @pytest.mark.parametrize("kind", ["Kafka", "KafkaTopic", "KafkaConnector"])
    def test_strimzi_kinds_resolvable(self, kind):
        det = default_registry.get_by_kind(kind)
        assert det is not None and det.name == "strimzi"

    @pytest.mark.parametrize("kind", ["Elasticsearch", "Kibana", "ApmServer", "Beat"])
    def test_eck_kinds_resolvable(self, kind):
        det = default_registry.get_by_kind(kind)
        assert det is not None and det.name == "eck"
