# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest minor (1.N.x) | ✅ Weekly patches as needed |
| Previous minor (1.(N-1).x) | ❌ EOL on the day 1.N.0 ships |
| < 1.0 | ❌ |

Solo-developer maintenance bandwidth; the cohort pricing model ties the release rhythm to the current minor only. Backporting fixes to retired minors is not committed.

## Reporting a Vulnerability

**DO NOT** open a public GitHub issue for security vulnerabilities.

### OSS reporting

Email `support@baldur.sh` with `[SECURITY]` in the subject line. Include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact assessment
- Suggested fix (if any)

### PRO reporting

For PRO or Design Partner security reports, email `support@baldur.sh` with `[SECURITY]` in the subject line. If the report concerns PRO functionality or `baldur_pro` internals, add `[PRO]` as well so we can route it appropriately. PRO-impacting reports are triaged with the same severity-driven priority as OSS advisories.

## Response Process

We acknowledge reports, track them, and prioritize by severity. We do not commit to specific response times — the project is currently solo-maintained and explicit SLAs would set expectations we cannot reliably honor. In practice, reports are reviewed within days, not weeks; severity drives the order, not the arrival timestamp.

## Disclosure Policy

We follow coordinated disclosure. Once a fix is available, the published advisory and patch release land together. We ask that public disclosure timing be coordinated with us — please refrain from publishing details until the fix has shipped, and we will work with you on a reasonable embargo window.

## Scope

### In scope

- Remote code execution
- Authentication / authorization bypass (RBAC, JWT, break-glass)
- Privilege escalation (Viewer → Operator → Admin)
- Information disclosure (audit log leak, PII unmasking)
- Audit integrity tamper (hash chain, Merkle proof, WAL)
- Rate-limit bypass (L1 / L2)
- Cryptographic weaknesses (Fernet misuse, key derivation, TLS downgrade)
- Resource exhaustion attainable with framework default settings on a typical production deployment
- Supply-chain (compromised dep, malicious wheel)
- PRO: Ed25519 license-key bypass, entitlement forgery, `baldur_pro` internal surface

**Default-configuration resource exhaustion is in scope; user-tuned high-limit exhaustion is out of scope.** Example: a report demonstrating worker exhaustion with `BALDUR_PROTECT_MAX_RETRIES=20` (the default) is in scope. The same exhaustion demonstrated with `MAX_RETRIES=1000` is out of scope, unless you also show the default value is itself unsafe in a typical deployment.

### Out of scope

- User misconfiguration (e.g., disabling RBAC enforcement, leaking env vars)
- Social engineering / phishing of project maintainers or users
- Physical access to production hosts
- Self-DoS via permissively-tuned settings (see in-scope clarification above)
- Vulnerabilities requiring pre-existing admin compromise
- Vulnerabilities in upstream dependencies — report upstream; we will pin / update once a fix exists
- Best-practice violations without a demonstrated exploit (use GitHub Discussions)
- Exposures only reproducible with `BALDUR_ENVIRONMENT` unset or set to a non-production value (Baldur expects `BALDUR_ENVIRONMENT=production` in deployed systems; non-production env disables the CRITICAL-secret startup abort by design)

## Safe Harbor

If you make a good-faith effort to comply with this policy during your security research, we will consider your research authorized, we will work with you to understand and resolve the issue, and we will not initiate or pursue legal action related to your research. Public disclosure timing should be coordinated with us.

## Recognition

Reporters of confirmed vulnerabilities are credited in the published advisory unless anonymity is requested.

## Security Features

Baldur implements the following security measures:

### Access Control
- **RBAC**: Role-based access control with three tiers (Viewer / Operator / Admin). On the framework-agnostic admin server these map to API-key credentials — the operator key (`BALDUR_ADMIN_KEY`) grants Operator, the optional read-only key (`BALDUR_ADMIN_READONLY_KEY`) grants Viewer, and Admin operations additionally require `BALDUR_ADMIN_UNLOCK=1`. The Django adapter enforces the same tiers via group membership (`baldur_viewer` / `baldur_operator` / `baldur_admin`).
- **Emergency Mode**: Admin-gated state transitions, recorded in the audit trail.
- **Approval Workflow** (PRO): Opt-in approve/reject for high-risk operations, with self-approval prevention (the approver must differ from the requester). Recovery and runbook automation is risk-routed — high-risk actions wait for approval, critical actions block pending manual approval. Self-approval prevention needs a resolved operator identity: it is effective on the Django adapter (authenticated user) and pending per-operator identity resolution on the framework-agnostic admin server, where actions are currently attributed to an anonymous operator.
- **Fail-Secure**: Production bypass attempts are blocked and logged

### Data Protection
- **PII Encryption**: Fernet (AES-128-CBC) for recoverable encryption
- **Audit Masking**: SHA-256 hashing for identity verification
- **SecretStr**: Automatic masking in logs and `repr()`

### Integrity
- **Hash Chain**: SHA-256 based audit log integrity
- **Merkle Tree**: Legal-grade proof of integrity
- **RFC 3161**: External timestamp authority support

### Rate Limiting
- **L1 + L2 Hybrid**: Memory fallback when Redis fails
- **Shadow Audit**: Forensic logging of rate limit events

## Dependency Security

This project uses automated dependency scanning:

- **pip-audit**: Weekly security audits in CI
- **bandit**: Static analysis of source code in CI
- **license-check**: Viral copyleft (GPL / AGPL / LGPL) gate in CI
- **Dependabot**: Automated dependency update PRs

## Secure Deployment

For production configuration — secret generation, classification (CRITICAL / IMPORTANT / OPTIONAL), TLS hardening, and the `validate_required_secrets()` pre-flight gate — see [`docs/runbooks/secure-deployment.md`](docs/runbooks/secure-deployment.md). The repo root also ships `.env.template` as a copy-pasteable starting point.
