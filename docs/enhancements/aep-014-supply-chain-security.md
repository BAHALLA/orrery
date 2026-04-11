# AEP-014: Supply Chain Security

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P0 |
| **Effort** | Medium (3-5 days) |
| **Impact** | High |
| **Dependencies** | AEP-011 (completed) |

## Gap Analysis

### Current Implementation

With AEP-011 landed, the project publishes multi-arch images to GHCR via
`.github/workflows/docker-publish.yml` with buildx `sbom: true` and
`provenance: true` flags. That's a strong starting point but stops short
of what an enterprise procurement review will ask for:

- **SBOM is generated but not attached/verified**: buildx embeds an SBOM
  attestation on the image, but it isn't exported as a release artifact
  nor independently verifiable by downstream consumers.
- **Images are not signed**: anyone with GHCR write access (or a
  compromised token) can push a malicious `latest` tag, and there is no
  way for the cluster to reject it.
- **No vulnerability scan gate**: the CI pipeline has `bandit` for Python
  source but no scan of the final container image. A vulnerable base
  image or transitive package would ship unnoticed.
- **Base image is pinned by tag, not digest**: `FROM python:3.11-slim-bookworm`
  in `Dockerfile.prod` resolves to whatever tag `latest` points to at
  build time. Two builds on different days can produce different base
  images without any visible diff.
- **No dependency review in CI**: new dependencies added via `uv add` are
  not audited for known CVEs before merge.
- **No admission policy**: the Kubernetes manifests don't enforce that
  only signed images from GHCR can run.

### What's available

- **Sigstore / cosign**: keyless (OIDC) signing in GitHub Actions — no
  key material to rotate. Images are signed against the workflow
  identity (`https://github.com/BAHALLA/devops-agents/.github/workflows/docker-publish.yml@refs/tags/v0.2.0`).
- **Trivy**: industry-standard container + filesystem scanner, works as
  a GitHub Action and as a Kubernetes admission controller.
- **Grype + Syft**: alternative SBOM / scan stack from Anchore.
- **CycloneDX Python plugin (`cyclonedx-py`)**: emits a Python-specific
  SBOM that catches transitive LLM-provider dependencies that container
  scanners miss.
- **GitHub Dependency Review Action**: gates PRs on new CVEs introduced
  by dependency changes.
- **Kyverno / Sigstore Policy Controller**: admission-time signature
  verification for Kubernetes clusters.

### Gap

The platform has no chain of custody from source → build → deploy.
A supply-chain attack on any upstream Python package (via `uv.lock`
tampering or a malicious PyPI release) would not be caught.

## Proposed Solution

### Step 1: Pin the base image by digest

```dockerfile
# Dockerfile.prod — pin by digest, not tag
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim@sha256:<digest> AS builder
FROM python:3.11-slim-bookworm@sha256:<digest>
```

Automate digest refresh with Renovate or Dependabot's `docker` ecosystem.

### Step 2: Generate and publish SBOMs

Extend `docker-publish.yml` to emit a CycloneDX SBOM of the Python
dependency graph (not just the OS layer) and upload it as a release
artifact:

```yaml
- name: Generate Python SBOM
  run: |
    uvx cyclonedx-py requirements uv.lock \
      -o sbom-python.cdx.json

- name: Upload SBOM
  uses: actions/upload-artifact@v4
  with:
    name: sbom
    path: sbom-python.cdx.json

- name: Attach SBOM to release
  if: startsWith(github.ref, 'refs/tags/v')
  uses: softprops/action-gh-release@v2
  with:
    files: sbom-python.cdx.json
```

### Step 3: Sign images with cosign (keyless)

```yaml
- name: Install cosign
  uses: sigstore/cosign-installer@v3

- name: Sign the image
  env:
    COSIGN_EXPERIMENTAL: "true"
  run: |
    cosign sign --yes \
      ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
```

Verify from the command line:

```bash
cosign verify \
  --certificate-identity-regexp "https://github.com/BAHALLA/devops-agents/.*" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/bahalla/devops-agents:v0.2.0
```

### Step 4: Scan images with Trivy as a CI gate

```yaml
- name: Scan image with Trivy
  uses: aquasecurity/trivy-action@0.28.0
  with:
    image-ref: ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}
    severity: CRITICAL,HIGH
    exit-code: 1
    ignore-unfixed: true
    format: sarif
    output: trivy-results.sarif

- name: Upload SARIF to code scanning
  if: always()
  uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: trivy-results.sarif
```

### Step 5: Gate PRs on dependency CVEs

```yaml
# .github/workflows/ci.yml (new job)
dependency-review:
  name: Dependency Review
  runs-on: ubuntu-latest
  if: github.event_name == 'pull_request'
  steps:
    - uses: actions/checkout@v6
    - uses: actions/dependency-review-action@v4
      with:
        fail-on-severity: high
```

### Step 6: Admission-time signature verification (optional)

For clusters that can run it, install the Sigstore Policy Controller and
create a `ClusterImagePolicy` that only admits images signed by the
expected workflow identity:

```yaml
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: ai-agents-images
spec:
  images:
    - glob: "ghcr.io/bahalla/devops-agents*"
  authorities:
    - keyless:
        url: https://fulcio.sigstore.dev
        identities:
          - issuer: https://token.actions.githubusercontent.com
            subjectRegExp: "https://github.com/BAHALLA/devops-agents/.*"
```

## Affected Files

| File | Change |
|------|--------|
| `Dockerfile.prod` | Pin base images by digest |
| `.github/workflows/docker-publish.yml` | Add SBOM generation, cosign signing, Trivy scan |
| `.github/workflows/ci.yml` | Add `dependency-review` job |
| `.github/dependabot.yml` | Enable `docker` ecosystem for base image updates |
| `deploy/k8s/imagepolicy.yaml` | New — Sigstore Policy Controller rule (optional) |
| `docs/security.md` | New — document the verification flow for downstream users |

## Acceptance Criteria

- [ ] Base images pinned by digest in `Dockerfile.prod`
- [ ] Renovate/Dependabot open PRs for base image digest updates
- [ ] CycloneDX Python SBOM generated on every CI build
- [ ] SBOM attached to GitHub releases as a downloadable artifact
- [ ] Images signed with cosign keyless identity in CI
- [ ] Signature verification documented in `docs/security.md`
- [ ] Trivy scan gates image publish on HIGH/CRITICAL unfixed CVEs
- [ ] Trivy SARIF uploaded to GitHub Code Scanning
- [ ] PR dependency review gate merged to `ci.yml`
- [ ] (Stretch) Admission-time signature verification via Sigstore Policy Controller

## Notes

- Trivy can produce false positives on development dependencies;
  consider `--skip-files` for files not shipped in the final image.
- Keyless signing requires the GitHub Actions OIDC token — make sure
  `id-token: write` is set on the job (already set in AEP-011's workflow).
- The Python SBOM is more valuable than the OS SBOM for this project
  because the primary attack surface is the LLM client libraries and
  provider SDKs, not the Debian base.
- Pair with **AEP-013** (authentication) — supply-chain hardening and
  auth/auth are the two P0 gaps after AEP-011. Either can ship first.
