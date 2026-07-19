# input_neurons — the WRITE-side neuron image (ADR-0015 / ADR-0017).
# ==================================================================
# Built at deploy from the /opt/input_neurons code-in seam (a RO drvfs mount of the host
# input_neurons/). The neuron CODE is BAKED here (COPY), never runtime-bind-mounted: a nested
# 9p mountpoint does not propagate into a rootless-docker container. Code edits => rebuild
# (deps cached; only the COPY layer rebuilds — fast).
#
# RUNTIME POSTURE (compose): read_only rootfs + tmpfs /tmp + a /state named volume. So:
#   * PYTHONDONTWRITEBYTECODE — never write .pyc into the read-only /app (import would EACCES).
#   * PYTHONPYCACHEPREFIX=/tmp — belt-and-suspenders: any bytecode goes to the tmpfs.
#   * The neuron reaches ONLY the gateway (neuron_net) — NO internet at run. Every dependency
#     and any model/tokenizer data must be present at BUILD time. This image needs none beyond
#     pip wheels (chunking is pure-python; embeddings/captions come from Ollama via the gateway).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache

WORKDIR /app

# Deps first (cached layer). Build HAS internet; runtime does NOT.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Neuron code (the fast-rebuild layer).
COPY . /app

# The pipeline is a batch job driven by args (default --ingest-only, set in compose command).
ENTRYPOINT ["python", "/app/neuron.py"]
CMD ["--ingest-only"]
