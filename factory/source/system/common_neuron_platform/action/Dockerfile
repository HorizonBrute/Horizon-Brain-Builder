# action_neurons — the READ-side neuron image (ADR-0015 / ADR-0017).
# ==================================================================
# Built at deploy from the /opt/action_neurons code-in seam. Code BAKED (COPY), not
# runtime-bind-mounted (a nested 9p mount does not propagate into rootless docker). Same
# read-only-rootfs posture as the input image: PYTHONDONTWRITEBYTECODE + pycache->/tmp; the
# retrieve+synthesize path (embeddings + LLM, both remote over the gateway) writes nothing to
# the rootfs. The neuron holds ONLY a chroma:reader + ollama:use bearer — it physically cannot
# write to Chroma (the write-funnel invariant is enforced by the gateway, not by app code).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# One-shot query (`--query "..."`) by default; `--serve` turns it into the long-running
# query API (the compose action_api service overrides CMD with --serve --host --port).
ENTRYPOINT ["python", "/app/action.py"]
CMD ["--help"]
