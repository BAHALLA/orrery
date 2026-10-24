"""Generate the Studio demo's internal CA and the writer's server certificate.

Run from the image (the ``studio-demo-certs`` compose service does, on every
``up``)::

    python -m orrery_core.studio.demo.certs --out /certs --ca-out /ca \\
        --host demo-writer.studio.internal --owner 1000

Writes ``writer.pem`` and ``writer-key.pem`` to ``--out`` (only the writer
mounts it) and ``ca.pem`` to ``--ca-out`` (what Orrery trusts, via
``ORRERY_A2A_CA_FILE``; Orrery mounts only this one, so it never sees the
writer's key). The CA's private key is discarded once it has signed: nothing
else can be issued from it.

Existing files are kept while they are usable, so restarting the profile
does not change the writer's certificate. They are regenerated when one is
missing, when either certificate expires within ``RENEW_BEFORE``, when the
writer's certificate does not name ``--host``, or when it was not issued by
the CA beside it. After a renewal both ``demo-writer`` and ``orrery-studio``
must restart: each reads its files once, at startup.

A throwaway CA for a local demo. Do not reuse it for anything else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

#: Certificates closer than this to expiry are replaced.
RENEW_BEFORE = dt.timedelta(days=7)

CA_FILE = "ca.pem"
CERT_FILE = "writer.pem"
KEY_FILE = "writer-key.pem"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make(out: Path, host: str, days: int = 30, *, ca_out: Path | None = None) -> None:
    """Write the writer's pair to ``out`` and the CA to ``ca_out`` (default: ``out``)."""
    ca_out = ca_out or out
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("Orrery Studio demo CA")
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(host))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    out.mkdir(parents=True, exist_ok=True)
    ca_out.mkdir(parents=True, exist_ok=True)
    (ca_out / CA_FILE).write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (out / CERT_FILE).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = out / KEY_FILE
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)


def renewal_reason(
    out: Path, ca_out: Path, now: dt.datetime | None = None, *, host: str | None = None
) -> str | None:
    """Why the files in ``out``/``ca_out`` must be regenerated, or ``None`` to keep them."""
    now = now or dt.datetime.now(dt.UTC)
    paths = (ca_out / CA_FILE, out / CERT_FILE, out / KEY_FILE)
    missing = [path.name for path in paths if not path.exists()]
    if missing:
        return f"missing {', '.join(missing)}"
    try:
        ca = x509.load_pem_x509_certificate((ca_out / CA_FILE).read_bytes())
        cert = x509.load_pem_x509_certificate((out / CERT_FILE).read_bytes())
    except ValueError as exc:
        return f"unreadable certificate ({exc})"
    for label, certificate in (("the CA", ca), ("the writer's certificate", cert)):
        if certificate.not_valid_after_utc - now < RENEW_BEFORE:
            return f"{label} expires {certificate.not_valid_after_utc:%Y-%m-%d}"
    if host is not None:
        try:
            names = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        if host not in names:
            return f"the writer's certificate names {names}, not {host}"
    try:
        cert.verify_directly_issued_by(ca)
    except ValueError, TypeError, InvalidSignature:
        return "the writer's certificate was not issued by this CA"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, required=True, help="Directory for the writer's pair.")
    parser.add_argument("--ca-out", type=Path, help="Directory for ca.pem (default: --out).")
    parser.add_argument("--host", required=True, help="The writer's DNS name.")
    parser.add_argument("--owner", type=int, help="uid to own the writer's files.")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    ca_out = args.ca_out or args.out
    reason = renewal_reason(args.out, ca_out, host=args.host)
    if reason is None:
        print(f"certificates in {args.out} and {ca_out} are valid; keeping them")
        return
    print(f"regenerating: {reason}")
    make(args.out, args.host, args.days, ca_out=ca_out)
    if args.owner is not None:
        for name in (CERT_FILE, KEY_FILE):
            os.chown(args.out / name, args.owner, args.owner)
    print(f"wrote {CERT_FILE}, {KEY_FILE} to {args.out} and {CA_FILE} to {ca_out} for {args.host}")
    print("restart demo-writer and orrery-studio to load them")


if __name__ == "__main__":
    main()
