# tls/ — gateway TLS certificate

The gateway's TLS certificate and private key — the **only PKI in the stack**. Chroma and
ollama have no cert; they are sealed on `brain_net` and reached in-network by token. The
gateway is the single door and the only thing that terminates TLS.

## Files (template ships `.example`; the real files are gitignored per-brain)

- **`cert.pem.example`** / **`cert.key.example`** — templates. At deploy the stack generates a
  self-signed `cert.pem` + `cert.key` here (Personal: `localhost`+`127.0.0.1` SAN; Server: add
  a LAN `DNS:`/`IP:` SAN so off-box clients validate).

To **bring your own** cert (Enterprise), drop `cert.pem` + `cert.key` here with those exact names.
These are copied into the running stack at `~/gateway/gateway_out/` on apply; edit here (the
source of truth), the distro copy is overwritten on the next apply / boot. The key is kept mode
`600`, readable only by the gateway identity.
