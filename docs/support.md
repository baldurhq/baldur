# Support

How to get help with Baldur, and what to expect when you reach out.

## Channels

### OSS (free)

- **Questions & discussion** — open a thread in [GitHub Discussions](https://github.com/gotoUSA/baldur-python/discussions).
- **Bug reports** — file a [GitHub Issue](https://github.com/gotoUSA/baldur-python/issues) with a minimal reproduction.
- **Security vulnerabilities** — do **not** open a public issue. Follow the [Security Policy](https://github.com/gotoUSA/baldur-python/blob/main/SECURITY.md).

### PRO & Design Partners

- **Email** — `support@baldur.sh` is the single entry point for all PRO support.
- Include your **license key or order reference** so we can verify your entitlement and route the request.
- For code, logs, and stack traces, a private support repository is provided to PRO customers for context-rich discussion.
- **Security reports** — see the [Security Policy](https://github.com/gotoUSA/baldur-python/blob/main/SECURITY.md); PRO-impacting reports are triaged with the same priority as OSS advisories.

## Support Response Commitment

Baldur is currently maintained by a solo developer. Rather than promise response times we cannot reliably honor, we commit to a model — not a clock:

- **Acknowledge** — every report is seen and logged.
- **Track** — it is recorded and does not get dropped.
- **Prioritize** — severity drives the order, not the arrival timestamp.

In practice, reports are reviewed within days, not weeks. We deliberately do not publish a fixed response-time SLA: an explicit time guarantee from a solo maintainer would set expectations we could not keep. This mirrors the response process in the [Security Policy](https://github.com/gotoUSA/baldur-python/blob/main/SECURITY.md).

## Known issues

Confirmed, user-affecting bugs are tracked publicly with the `known-bug` label on the issue tracker. Before filing, you can search the [`known-bug` issues](https://github.com/gotoUSA/baldur-python/issues?q=label%3Aknown-bug) to see whether what you hit is already on our radar — and whether a workaround or fix is already noted.

## What support covers

- **OSS** — installation, configuration, and usage of the documented public API across the supported framework adapters.
- **PRO** — everything above, plus the PRO feature set, for customers with an active subscription.

Out of scope: debugging application code unrelated to Baldur, custom integrations beyond the documented adapters, and behavior reproducible only with non-production settings. For those, GitHub Discussions is the right venue.
