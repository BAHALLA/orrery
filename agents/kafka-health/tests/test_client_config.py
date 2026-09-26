"""Kafka connection settings: validation, and what librdkafka is handed.

Every generated configuration is also given to the **real librdkafka**
(``AdminClient(...)``), which rejects unknown properties and unsupported
feature combinations, and parses the CA, certificate and key files at
construction. A misspelled property name would pass a dict comparison; it
cannot pass this.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

import pytest
from confluent_kafka.admin import AdminClient
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from kafka_health_agent.client_config import (
    MANAGED_PROPERTIES,
    KafkaConfig,
    build_client_properties,
    describe_connection,
)

UNREACHABLE = "127.0.0.1:1"  # construction never needs a broker


@pytest.fixture(autouse=True)
def _no_ambient_kafka_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("KAFKA_"):
            monkeypatch.delenv(name)


def make(**fields) -> KafkaConfig:
    fields.setdefault("kafka_bootstrap_servers", UNREACHABLE)
    return KafkaConfig(_env_file=None, **fields)


def accepted_by_librdkafka(cfg: KafkaConfig) -> None:
    AdminClient(build_client_properties(cfg))


@pytest.fixture(scope="module")
def pki(tmp_path_factory) -> dict[str, str]:
    """A throwaway CA plus a client certificate and key, as PEM files."""
    root = tmp_path_factory.mktemp("pki")
    now = datetime.datetime.now(datetime.UTC)

    def cert(subject: str, key, issuer_name, issuer_key, ca: bool) -> x509.Certificate:
        return (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(issuer_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(issuer_key, hashes.SHA256())
        )

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca_cert = cert("test-ca", ca_key, ca_name, ca_key, ca=True)
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_cert = cert("orrery", client_key, ca_name, ca_key, ca=False)

    paths = {"ca": root / "ca.pem", "cert": root / "client.pem", "key": root / "client.key"}
    paths["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths["cert"].write_bytes(client_cert.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return {k: str(v) for k, v in paths.items()}


# ── Valid configurations librdkafka accepts ──────────────────────────


def test_default_is_plaintext_and_backwards_compatible():
    cfg = make()

    assert build_client_properties(cfg) == {
        "bootstrap.servers": UNREACHABLE,
        "security.protocol": "PLAINTEXT",
    }
    accepted_by_librdkafka(cfg)


@pytest.mark.parametrize("mechanism", ["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"])
def test_sasl_ssl_with_password(mechanism, pki):
    cfg = make(
        kafka_security_protocol="SASL_SSL",
        kafka_sasl_mechanism=mechanism,
        kafka_sasl_username="svc-orrery",
        kafka_sasl_password="p@ss",
        kafka_ssl_ca_location=pki["ca"],
    )
    props = build_client_properties(cfg)

    assert props["sasl.mechanism"] == mechanism
    assert props["sasl.username"] == "svc-orrery"
    assert props["sasl.password"] == "p@ss"
    assert props["ssl.endpoint.identification.algorithm"] == "https"
    assert props["ssl.ca.location"] == pki["ca"]
    accepted_by_librdkafka(cfg)


def test_mutual_tls(pki):
    cfg = make(
        kafka_security_protocol="SSL",
        kafka_ssl_ca_location=pki["ca"],
        kafka_ssl_certificate_location=pki["cert"],
        kafka_ssl_key_location=pki["key"],
    )
    props = build_client_properties(cfg)

    assert props["ssl.certificate.location"] == pki["cert"]
    assert props["ssl.key.location"] == pki["key"]
    assert "sasl.mechanism" not in props
    accepted_by_librdkafka(cfg)


def test_oauthbearer_oidc():
    cfg = make(
        kafka_security_protocol="SASL_SSL",
        kafka_sasl_mechanism="OAUTHBEARER",
        kafka_sasl_oauthbearer_client_id="orrery",
        kafka_sasl_oauthbearer_client_secret="shh",
        kafka_sasl_oauthbearer_token_endpoint_url="https://idp.example/token",
        kafka_sasl_oauthbearer_scope="kafka",
    )
    props = build_client_properties(cfg)

    assert props["sasl.oauthbearer.method"] == "oidc"
    assert props["sasl.oauthbearer.client.secret"] == "shh"
    assert props["sasl.oauthbearer.scope"] == "kafka"
    assert "sasl.username" not in props
    accepted_by_librdkafka(cfg)


def test_extra_properties_pass_through():
    cfg = make(kafka_client_properties='{"client.id": "orrery", "socket.keepalive.enable": true}')

    props = build_client_properties(cfg)

    assert props["client.id"] == "orrery"
    assert props["socket.keepalive.enable"] == "True"  # librdkafka accepts either case
    accepted_by_librdkafka(cfg)


def test_protocol_and_mechanism_are_case_insensitive():
    cfg = make(
        kafka_security_protocol=" sasl_ssl ",
        kafka_sasl_mechanism="scram-sha-512",
        kafka_sasl_username="u",
        kafka_sasl_password="p",
    )

    assert (cfg.kafka_security_protocol, cfg.kafka_sasl_mechanism) == ("SASL_SSL", "SCRAM-SHA-512")


def test_empty_values_mean_unset():
    cfg = make(kafka_security_protocol="", kafka_sasl_mechanism="")

    assert (cfg.kafka_security_protocol, cfg.kafka_sasl_mechanism) == ("PLAINTEXT", None)


def test_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9093")
    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
    monkeypatch.setenv("KAFKA_SASL_MECHANISM", "SCRAM-SHA-256")
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "svc")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", "from-env")

    props = build_client_properties(KafkaConfig(_env_file=None))

    assert props["bootstrap.servers"] == "broker:9093"
    assert props["sasl.password"] == "from-env"


# ── Misconfigurations are refused at startup ─────────────────────────


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"kafka_security_protocol": "SASL_SSL"}, "needs KAFKA_SASL_MECHANISM"),
        (
            {
                "kafka_sasl_mechanism": "PLAIN",
                "kafka_sasl_username": "u",
                "kafka_sasl_password": "p",
            },
            "does not use SASL",
        ),
        (
            {"kafka_security_protocol": "SASL_SSL", "kafka_sasl_mechanism": "SCRAM-SHA-256"},
            "needs KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD",
        ),
        (
            {
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "PLAIN",
                "kafka_sasl_username": "u",
                "kafka_sasl_password": "",
            },
            "KAFKA_SASL_PASSWORD",
        ),
        (
            {"kafka_security_protocol": "SASL_SSL", "kafka_sasl_mechanism": "OAUTHBEARER"},
            "KAFKA_SASL_OAUTHBEARER_CLIENT_ID, KAFKA_SASL_OAUTHBEARER_CLIENT_SECRET",
        ),
        (
            {
                "kafka_security_protocol": "SASL_SSL",
                "kafka_sasl_mechanism": "OAUTHBEARER",
                "kafka_sasl_oauthbearer_client_id": "c",
                "kafka_sasl_oauthbearer_client_secret": "s",
                "kafka_sasl_oauthbearer_token_endpoint_url": "http://idp.example/token",
            },
            "must be https://",
        ),
        ({"kafka_ssl_ca_location": "/etc/kafka/ca.pem"}, "does not use TLS"),
        (
            {"kafka_security_protocol": "SSL", "kafka_ssl_ca_location": "/nope/ca.pem"},
            "does not exist",
        ),
        ({"kafka_security_protocol": "KERBEROS"}, "kafka_security_protocol"),
        (
            {"kafka_security_protocol": "SASL_SSL", "kafka_sasl_mechanism": "GSSAPI"},
            "kafka_sasl_mechanism",
        ),
        ({"kafka_client_properties": "not json"}, "not valid JSON"),
        ({"kafka_client_properties": "[1, 2]"}, "must be a JSON object"),
    ],
)
def test_misconfiguration_is_refused(fields, message):
    with pytest.raises((ValidationError, ValueError), match=message):
        make(**fields)


def test_certificate_without_key_is_refused(pki):
    with pytest.raises(ValidationError, match="must be set together"):
        make(
            kafka_security_protocol="SSL",
            kafka_ssl_ca_location=pki["ca"],
            kafka_ssl_certificate_location=pki["cert"],
        )


@pytest.mark.parametrize("key", sorted(MANAGED_PROPERTIES))
def test_passthrough_cannot_override_a_managed_setting(key):
    """One source of truth per setting: KAFKA_CLIENT_PROPERTIES cannot, say,
    quietly turn off hostname verification or swap the password."""
    with pytest.raises(ValidationError, match="may not set"):
        make(kafka_client_properties={key: "x"})


def test_all_problems_are_reported_together():
    with pytest.raises(ValidationError) as info:
        make(kafka_security_protocol="SASL_SSL", kafka_ssl_certificate_location="/nope/cert")

    text = str(info.value)
    assert "needs KAFKA_SASL_MECHANISM" in text
    assert "must be set together" in text
    assert "does not exist" in text


# ── Secrets never leak ───────────────────────────────────────────────


def test_secrets_never_appear_in_repr_errors_or_descriptions(pki):
    cfg = make(
        kafka_security_protocol="SASL_SSL",
        kafka_sasl_mechanism="SCRAM-SHA-512",
        kafka_sasl_username="svc",
        kafka_sasl_password="hunter2-password",
        kafka_ssl_key_password="hunter2-keypass",
        kafka_ssl_ca_location=pki["ca"],
    )

    for rendered in (repr(cfg), str(cfg), describe_connection(cfg)):
        assert "hunter2" not in rendered

    with pytest.raises(ValidationError) as info:
        make(
            kafka_security_protocol="SASL_SSL",
            kafka_sasl_mechanism="SCRAM-SHA-512",
            kafka_sasl_password="hunter2-password",
        )
    assert "hunter2" not in str(info.value)


def test_describe_connection():
    cfg = make(
        kafka_security_protocol="SASL_SSL",
        kafka_sasl_mechanism="PLAIN",
        kafka_sasl_username="u",
        kafka_sasl_password="p",
    )

    assert describe_connection(cfg) == f"{UNREACHABLE} (SASL_SSL, PLAIN)"


# ── Risky-but-legal settings are loud ───────────────────────────────


def test_cleartext_sasl_plain_warns(caplog):
    caplog.set_level(logging.WARNING)
    make(
        kafka_security_protocol="SASL_PLAINTEXT",
        kafka_sasl_mechanism="PLAIN",
        kafka_sasl_username="u",
        kafka_sasl_password="p",
    )

    assert "without encryption" in caplog.text


def test_disabled_hostname_verification_warns(caplog, pki):
    caplog.set_level(logging.WARNING)
    cfg = make(
        kafka_security_protocol="SSL",
        kafka_ssl_ca_location=pki["ca"],
        kafka_ssl_endpoint_identification_algorithm="none",
    )

    assert "impersonate a broker" in caplog.text
    assert build_client_properties(cfg)["ssl.endpoint.identification.algorithm"] == "none"
    accepted_by_librdkafka(cfg)


def test_ssl_key_password_is_passed(pki, tmp_path):
    cfg = make(
        kafka_security_protocol="SSL",
        kafka_ssl_ca_location=pki["ca"],
        kafka_ssl_certificate_location=pki["cert"],
        kafka_ssl_key_location=pki["key"],
        kafka_ssl_key_password="k",
    )

    assert build_client_properties(cfg)["ssl.key.password"] == "k"
    assert Path(pki["key"]).is_file()
