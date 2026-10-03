"""Tests for name-based mutation detection."""

import pytest

from orrery_core import looks_mutating
from orrery_core.security.classify import mutating_tokens


@pytest.mark.parametrize(
    "name",
    [
        "delete_kafka_topic",
        "kafka_delete_topic",
        "scale_deployment",
        "set_preference",
        "uncordon_node",
    ],
)
def test_mutations_are_flagged_wherever_the_verb_sits(name):
    assert looks_mutating(name)


@pytest.mark.parametrize(
    "name",
    [
        "get_loki_label_values",  # "label" after a read verb is a noun
        "list_consumer_groups",
        "describe_topic",
        "check_cluster_health",
        "top_pods",
        "prometheus_query",
        "",
    ],
)
def test_reads_are_not(name):
    assert not looks_mutating(name)


def test_tokens_are_reported():
    assert mutating_tokens("increase_topic_partitions") == {"increase"}
