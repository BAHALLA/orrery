"""Kafka connection settings: TLS, SASL, and their validation.

The admin client used to be built from ``bootstrap.servers`` alone, so the
agent could only reach a PLAINTEXT listener. That rules out essentially every
production cluster: Confluent Cloud and Aiven (SASL_SSL), MSK with SCRAM or
TLS, and Strimzi's TLS and SCRAM listeners.

Settings are typed fields rather than a free-form property bag, so they are
validated when the agent starts. A cluster that needs SCRAM but got PLAIN, or
a client certificate without its key, is reported at boot with the variable
to fix. Otherwise it shows up as a broker timeout on the first tool call,
indistinguishable from the cluster being down. ``KAFKA_CLIENT_PROPERTIES`` is
the escape hatch for any other librdkafka setting, but it cannot override a
managed one: each setting has exactly one source.

Secrets are :class:`~pydantic.SecretStr`, so they never appear in a repr, a
validation error or a log line. They can come from the mounted secrets volume
like any other credential (``KAFKA_SASL_PASSWORD`` as a file under
``ORRERY_SECRETS_DIR``).

Not covered: AWS MSK IAM authentication, which needs a token-signing callback
(``aws-msk-iam-sasl-signer``) rather than configuration. Use SCRAM or mTLS on
MSK.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from orrery_core import AgentConfig

logger = logging.getLogger(__name__)

SecurityProtocol = Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"]
SaslMechanism = Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512", "OAUTHBEARER"]
#: What ``confluent_kafka`` accepts as a client configuration.
ClientProperties = dict[str, str | int | float | bool]

#: librdkafka properties set from typed fields; KAFKA_CLIENT_PROPERTIES may not
#: set these (each setting has exactly one source of truth).
MANAGED_PROPERTIES = frozenset(
    {
        "bootstrap.servers",
        "security.protocol",
        "sasl.mechanism",
        "sasl.mechanisms",
        "sasl.username",
        "sasl.password",
        "sasl.oauthbearer.method",
        "sasl.oauthbearer.client.id",
        "sasl.oauthbearer.client.secret",
        "sasl.oauthbearer.token.endpoint.url",
        "sasl.oauthbearer.scope",
        "ssl.ca.location",
        "ssl.certificate.location",
        "ssl.key.location",
        "ssl.key.password",
        "ssl.endpoint.identification.algorithm",
    }
)


class KafkaConfig(AgentConfig):
    """Kafka connection settings (``KAFKA_*`` environment variables)."""

    kafka_bootstrap_servers: str = "localhost:9092"

    kafka_security_protocol: SecurityProtocol = "PLAINTEXT"

    kafka_sasl_mechanism: SaslMechanism | None = None
    kafka_sasl_username: str | None = None
    kafka_sasl_password: SecretStr | None = None

    # OAUTHBEARER via OIDC client credentials (Confluent Cloud, Strimzi OAuth).
    kafka_sasl_oauthbearer_client_id: str | None = None
    kafka_sasl_oauthbearer_client_secret: SecretStr | None = None
    kafka_sasl_oauthbearer_token_endpoint_url: str | None = None
    kafka_sasl_oauthbearer_scope: str | None = None

    kafka_ssl_ca_location: str | None = None
    kafka_ssl_certificate_location: str | None = None
    kafka_ssl_key_location: str | None = None
    kafka_ssl_key_password: SecretStr | None = None
    #: "https" verifies the broker's hostname against its certificate.
    #: "none" disables that check (an impersonation risk); logged as a warning.
    kafka_ssl_endpoint_identification_algorithm: Literal["https", "none"] = "https"

    #: Any other librdkafka property, as a JSON object of strings, e.g.
    #: ``{"client.id": "orrery", "socket.keepalive.enable": "true"}``.
    kafka_client_properties: dict[str, str] = Field(default_factory=dict)

    @field_validator("kafka_security_protocol", mode="before")
    @classmethod
    def _normalize_protocol(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper() or "PLAINTEXT"
        return value

    @field_validator("kafka_sasl_mechanism", mode="before")
    @classmethod
    def _normalize_mechanism(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper() or None
        return value

    @field_validator("kafka_client_properties", mode="before")
    @classmethod
    def _parse_properties(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"KAFKA_CLIENT_PROPERTIES is not valid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError("KAFKA_CLIENT_PROPERTIES must be a JSON object")
        return {str(k): str(v) for k, v in value.items()}

    @model_validator(mode="after")
    def _check_consistency(self) -> KafkaConfig:
        problems: list[str] = []
        sasl = self.kafka_security_protocol.startswith("SASL_")
        tls = self.kafka_security_protocol.endswith("SSL")

        if sasl and self.kafka_sasl_mechanism is None:
            problems.append(
                f"KAFKA_SECURITY_PROTOCOL={self.kafka_security_protocol} needs "
                "KAFKA_SASL_MECHANISM (PLAIN, SCRAM-SHA-256, SCRAM-SHA-512 or OAUTHBEARER)"
            )
        if not sasl and self.kafka_sasl_mechanism is not None:
            problems.append(
                f"KAFKA_SASL_MECHANISM is set but KAFKA_SECURITY_PROTOCOL="
                f"{self.kafka_security_protocol} does not use SASL; use SASL_SSL"
            )

        password_mechanism = self.kafka_sasl_mechanism in (
            "PLAIN",
            "SCRAM-SHA-256",
            "SCRAM-SHA-512",
        )
        if (
            sasl
            and password_mechanism
            and (not self.kafka_sasl_username or not _secret(self.kafka_sasl_password))
        ):
            problems.append(
                f"KAFKA_SASL_MECHANISM={self.kafka_sasl_mechanism} needs "
                "KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD"
            )
        if sasl and self.kafka_sasl_mechanism == "OAUTHBEARER":
            missing = [
                name
                for name, value in (
                    ("KAFKA_SASL_OAUTHBEARER_CLIENT_ID", self.kafka_sasl_oauthbearer_client_id),
                    (
                        "KAFKA_SASL_OAUTHBEARER_CLIENT_SECRET",
                        _secret(self.kafka_sasl_oauthbearer_client_secret),
                    ),
                    (
                        "KAFKA_SASL_OAUTHBEARER_TOKEN_ENDPOINT_URL",
                        self.kafka_sasl_oauthbearer_token_endpoint_url,
                    ),
                )
                if not value
            ]
            if missing:
                problems.append(f"KAFKA_SASL_MECHANISM=OAUTHBEARER needs {', '.join(missing)}")
            url = self.kafka_sasl_oauthbearer_token_endpoint_url or ""
            if url and not url.startswith("https://"):
                problems.append(
                    "KAFKA_SASL_OAUTHBEARER_TOKEN_ENDPOINT_URL must be https:// — "
                    "the client secret is sent to it"
                )

        tls_files = {
            "KAFKA_SSL_CA_LOCATION": self.kafka_ssl_ca_location,
            "KAFKA_SSL_CERTIFICATE_LOCATION": self.kafka_ssl_certificate_location,
            "KAFKA_SSL_KEY_LOCATION": self.kafka_ssl_key_location,
        }
        if not tls and any(tls_files.values()):
            problems.append(
                f"KAFKA_SSL_* files are set but KAFKA_SECURITY_PROTOCOL="
                f"{self.kafka_security_protocol} does not use TLS; use SSL or SASL_SSL"
            )
        if bool(self.kafka_ssl_certificate_location) != bool(self.kafka_ssl_key_location):
            problems.append(
                "KAFKA_SSL_CERTIFICATE_LOCATION and KAFKA_SSL_KEY_LOCATION must be set together "
                "(a client certificate is useless without its key)"
            )
        for name, path in tls_files.items():
            if path and not Path(path).is_file():
                problems.append(f"{name}={path} does not exist or is not a file")

        if clash := sorted(MANAGED_PROPERTIES & self.kafka_client_properties.keys()):
            problems.append(
                f"KAFKA_CLIENT_PROPERTIES may not set {', '.join(clash)}: "
                "use the dedicated KAFKA_* variable instead"
            )

        if problems:
            raise ValueError("Invalid Kafka connection settings:\n  - " + "\n  - ".join(problems))

        if self.kafka_security_protocol == "SASL_PLAINTEXT" and self.kafka_sasl_mechanism in (
            "PLAIN",
            "OAUTHBEARER",
        ):
            logger.warning(
                "KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT with %s sends credentials without "
                "encryption; use SASL_SSL",
                self.kafka_sasl_mechanism,
            )
        if tls and self.kafka_ssl_endpoint_identification_algorithm == "none":
            logger.warning(
                "KAFKA_SSL_ENDPOINT_IDENTIFICATION_ALGORITHM=none: broker hostnames are not "
                "verified, so any host with a certificate from your CA can impersonate a broker"
            )
        return self


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def build_client_properties(cfg: KafkaConfig) -> ClientProperties:
    """The librdkafka configuration for an admin client (validated by *cfg*)."""
    props: ClientProperties = dict(cfg.kafka_client_properties)
    props["bootstrap.servers"] = cfg.kafka_bootstrap_servers
    props["security.protocol"] = cfg.kafka_security_protocol

    if cfg.kafka_sasl_mechanism is not None:
        props["sasl.mechanism"] = cfg.kafka_sasl_mechanism
        if cfg.kafka_sasl_mechanism == "OAUTHBEARER":
            props["sasl.oauthbearer.method"] = "oidc"
            props["sasl.oauthbearer.client.id"] = cfg.kafka_sasl_oauthbearer_client_id or ""
            props["sasl.oauthbearer.client.secret"] = _secret(
                cfg.kafka_sasl_oauthbearer_client_secret
            )
            props["sasl.oauthbearer.token.endpoint.url"] = (
                cfg.kafka_sasl_oauthbearer_token_endpoint_url or ""
            )
            if cfg.kafka_sasl_oauthbearer_scope:
                props["sasl.oauthbearer.scope"] = cfg.kafka_sasl_oauthbearer_scope
        else:
            props["sasl.username"] = cfg.kafka_sasl_username or ""
            props["sasl.password"] = _secret(cfg.kafka_sasl_password)

    if cfg.kafka_security_protocol.endswith("SSL"):
        props["ssl.endpoint.identification.algorithm"] = (
            cfg.kafka_ssl_endpoint_identification_algorithm
        )
        for key, value in (
            ("ssl.ca.location", cfg.kafka_ssl_ca_location),
            ("ssl.certificate.location", cfg.kafka_ssl_certificate_location),
            ("ssl.key.location", cfg.kafka_ssl_key_location),
        ):
            if value:
                props[key] = value
        if key_password := _secret(cfg.kafka_ssl_key_password):
            props["ssl.key.password"] = key_password

    return props


def describe_connection(cfg: KafkaConfig) -> str:
    """A log-safe one-line description of how the agent connects (no secrets)."""
    parts = [cfg.kafka_security_protocol]
    if cfg.kafka_sasl_mechanism:
        parts.append(cfg.kafka_sasl_mechanism)
    if cfg.kafka_ssl_certificate_location:
        parts.append("mTLS")
    return f"{cfg.kafka_bootstrap_servers} ({', '.join(parts)})"
