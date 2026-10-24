"""Generate the Studio demo's internal CA and the writer's server certificate.

Run once by the ``studio-demo-certs`` compose service into a shared volume::

    python make_certs.py --out /certs --host demo-writer.studio.internal --owner 1000

Writes ``ca.pem`` (what Orrery trusts, via ``ORRERY_A2A_CA_FILE``),
``writer.pem`` and ``writer-key.pem``. The CA's private key is discarded once
it has signed: nothing else can be issued from it. Existing files are kept, so
restarting the profile does not change the writer's certificate.

A throwaway CA for a local demo. Do not reuse it for anything else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make(out: Path, host: str, days: int = 30) -> None:
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
    (out / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (out / "writer.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = out / "writer-key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--host", required=True, help="The writer's DNS name.")
    parser.add_argument("--owner", type=int, help="uid to own the files (the writer's user).")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if all((args.out / f).exists() for f in ("ca.pem", "writer.pem", "writer-key.pem")):
        print(f"certificates already in {args.out}; keeping them")
        return
    make(args.out, args.host, args.days)
    if args.owner is not None:
        for path in args.out.iterdir():
            os.chown(path, args.owner, args.owner)
    print(f"wrote ca.pem, writer.pem, writer-key.pem to {args.out} for {args.host}")


if __name__ == "__main__":
    main()
