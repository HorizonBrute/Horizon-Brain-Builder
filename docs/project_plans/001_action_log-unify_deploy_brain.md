# Project 001 — Action Log

Serial, append-only postcard timeline. One line per action, appended to the END.
Format: `ISO8601-UTC,<postcard>`. Append with:
`echo "$(date -u +%Y-%m-%dT%H:%M:%SZ),<postcard>" >> <this file>`
Do not rewrite history; only append. This is a `log` doc — no YAML frontmatter by convention.

2026-07-21T00:00:00Z,project scaffold created (six files + index + guide); planning-only, no product code
2026-07-21T00:00:01Z,seeded detail from two investigations (cert-bug root cause + Windows build-engine lifecycle map); 8-section plan
2026-07-21T00:00:02Z,recorded NOTE 001-1 (Linux engine artifact = docker save + config bundle, recommended, pending user confirm) — Section 3 BLOCKED on it
2026-07-21T00:00:03Z,logged BUG-001-1 (Linux gateway cert false-green) + DEBT-001-1/2/3; dev_brain left down by design (NOTE 001-3)
2026-07-21T00:00:04Z,NOTE 001-1 RESOLVED — user confirmed Linux engine = docker save + ollama-volume tar + config/cert bundle; Section 3 unblocked
2026-07-21T00:00:05Z,built deploy_brain.py foundation — Section 1 (PlatformBackend + Linux/Windows backends) + Section 6 (shared cert stage, no-arg gen-cert + rc-check); selftest green, BUG-001-1 closed at contract level
