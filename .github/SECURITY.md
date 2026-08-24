# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.2.x   | Yes       |
| 0.1.x   | No        |

0.1.x has known unpatched issues. Upgrade to 0.2.x.

## Reporting a vulnerability

Please report security issues privately. **Do not open a public issue.**

- **Preferred:** use GitHub's private vulnerability reporting — open the
  **Security** tab of this repository and click **Report a vulnerability**.
- **Email:** dev@primer.systems

Please include, as far as you can:

- what the issue is and why it matters
- the version and platform you saw it on
- steps to reproduce, or a proof of concept
- any suggested fix

## What to expect

We will acknowledge your report, investigate, and keep you updated on progress.
If the issue is confirmed we will work on a fix and credit you in the release
notes unless you would rather we did not.

Please give us a reasonable opportunity to release a fix before disclosing the
issue publicly.

## Scope

Vault is a self-custodial wallet: it holds keys and signs transactions on the
user's machine. Reports affecting key material, the approval path, the agent and
admin APIs, or the spending and trading limits are of most interest.

Known limitations that are documented rather than fixed — and the reasoning
behind them — are listed in [docs/security.md](../docs/security.md). Please read
that first; issues already described there are known.
