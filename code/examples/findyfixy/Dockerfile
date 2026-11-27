# One image for FindyFixy's on-call agent and loop (src/findyfixy/oncall/service.py, ADR 0008 step 4).
#
# Only the core dependencies: the model client, validation and the harness. The
# `triage` extra (torch, the embedder and reranker, Postgres drivers, LangGraph,
# MCP) is not installed: the acting path does not load it. The requirements
# file is exported from uv.lock with hashes by `make oncall-up`
# (.oncall-requirements.txt, git-ignored), and pip refuses any file whose hash
# differs.
#
# Base: the same pinned and patched Python image as simprod's services
# (examples/simprod/Dockerfile, scanned with Grype before first use). The source
# keeps the repository's layout because findyfixy/paths.py finds its files from
# where its code sits.
FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade \
      zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
      libc6=2.41-12+deb13u5 \
      libc-bin=2.41-12+deb13u5 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --no-create-home findyfixy

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/repo/code/src:/repo/code/examples/findyfixy/src:/repo/code/examples/simprod/src

COPY examples/findyfixy/.oncall-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY src/wyld3rness /repo/code/src/wyld3rness
COPY examples/simprod/src/simprod /repo/code/examples/simprod/src/simprod
COPY examples/findyfixy/src/findyfixy /repo/code/examples/findyfixy/src/findyfixy
COPY examples/findyfixy/config/prices.toml /repo/code/examples/findyfixy/config/prices.toml

USER findyfixy
