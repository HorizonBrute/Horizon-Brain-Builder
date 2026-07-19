# github/ — GitHub access posture for delivery (non-secret ONLY)

The config seam for how the neuron **delivery adapters** (`system/common_neuron_platform/input/delivery/`)
reach source repos on GitHub. It exists to make one posture explicit and non-negotiable:

> **No credential material is loaded into this brain.** The brain holds nothing irreplaceable —
> least of all a key. GitHub *authentication* is the operator's concern, supplied by the
> operator's own tooling **outside** the brain; this seam holds only **non-secret configuration**.
> Never place a private key, a personal access token, or a password here or anywhere under the brain.

## Access tiers (cheapest first — driven by a git source's delivery knobs in the `===NEURONS===` zone of `../brain.env`)

| `auth` | How the source is delivered | Credential in the brain? |
|---|---|---|
| `public` | keyless HTTPS clone — the default, public repos only | **None.** |
| `operator-delivered` | operator tooling writes the tree into `brain_ro/<name>` out-of-band; the adapter clones nothing, ingest reads what is present | **None.** |
| `transient-cred` | a short-lived token (from the env var named by `GITHUB_TOKEN_ENV`) is injected on the **one** delivery run and discarded — as an `Authorization` header, never in the URL or `.git/config` | **None persisted.** |

## Files

- **`github.env`** — non-secret defaults the delivery pipeline consumes: `GITHUB_DEFAULT_AUTH`,
  `GITHUB_DEFAULT_PROTOCOL`, and the **name** of the transient-token env var (`GITHUB_TOKEN_ENV`,
  default `GITHUB_TOKEN`). **Never** put a token value here — the value is injected at delivery
  time, not stored.
- **`known_hosts`** — GitHub's **published** SSH host keys (non-secret). Pinning them lets an
  out-of-band SSH delivery verify the *server* without a first-use prompt. This verifies GitHub
  to the client; it is not a client auth key and grants nothing.
- **`gh_auth/`** — an operator-side auth vault stub (`gh_auth.env`, gitignored). Holds the
  operator's own tooling config, kept out of the brain's read surface; never a persisted brain credential.

## What does NOT live here

Private keys / deploy keys / `id_*` / `*.pem` — never. PATs / passwords — never. The repo list —
that is delivery config in the `===NEURONS===` zone of `../brain.env` (git sources).
