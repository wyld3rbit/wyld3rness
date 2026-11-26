# OpenBao for simprod: a real server, not dev mode, so no key is ever printed.
#
# Storage is in memory: a restart starts empty and sealed, and `make sim-up`
# initialises and unseals it again through the API (src/simprod/vault.py), which
# returns the root token and the unseal key to the host process only. That fits
# a simulation. Production keeps storage that persists and an unseal procedure.
storage "inmem" {}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true  # reachable only on 127.0.0.1 and simprod's internal network
}

disable_mlock = true  # the container has no capabilities (docker-compose.yml)
ui            = true  # the web UI at http://127.0.0.1:8200/ui, to inspect the key, policies and passes
api_addr      = "http://127.0.0.1:8200"
