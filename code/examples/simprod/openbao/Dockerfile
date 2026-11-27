# OpenBao for simprod: the vault that holds the actions' signing key (src/simprod/vault.py).
#
# Base: OpenBao 2.7.2 (published 2026-10-26), pinned by digest; the same digest is
# published on quay.io and ghcr.io. Scanned with Grype v0.120.1 before first use:
# 1 Critical (zlib) and 21 High. 2.7.3 was inside the 7-day cooldown and fixes only
# one of them (klauspost/compress); its security fixes cover multi-node forwarding
# and auth-mount aliases, which a single node with token auth does not use.
# The RUN below installs the Alpine fixes at pinned versions: zlib 1.4-r0 (built
# 2026-11-06) and OpenSSL 3.5.9-r1 (built 2026-11-18). What stays is named in the
# changelog: OpenSSL advisories with no fix yet, and Go modules compiled into
# OpenBao itself, which only a new OpenBao release can change. When Alpine
# supersedes a pinned version, this build fails loudly instead of drifting.
FROM quay.io/openbao/openbao@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f

USER root
RUN apk add --no-cache --upgrade zlib=1.4-r0 libssl3=3.5.9-r1 libcrypto3=3.5.9-r1
COPY simprod.hcl /openbao/config/simprod.hcl
USER openbao
