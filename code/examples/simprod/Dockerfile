# One image for every simprod service: Python's standard library and this folder's code, nothing else.
#
# Base: python:3.13.18-slim-trixie (Debian 13), published 2026-11-08, pinned by
# digest (multi-arch index). Scanned with Grype v0.120.1 before first use: no
# Critical; 5 High matches had a Debian fix, the 79 others none (CHANGELOG.md).
# The RUN below installs those fixes at pinned versions. Their changelogs are
# dated 2026-10-10 (glibc) and 2026-11-01 (zlib), past the 7-day cooldown. When
# Debian supersedes a pinned version and drops it from its mirror, this build
# fails loudly instead of drifting: rescan, then update the pins.
FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade \
      zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
      libc6=2.41-12+deb13u5 \
      libc-bin=2.41-12+deb13u5 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --no-create-home simprod

# No bytecode files (the root filesystem is read-only at run time) and no
# buffered output (a log line must reach `docker compose logs` when it is written).
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY src/simprod /app/simprod
USER simprod
