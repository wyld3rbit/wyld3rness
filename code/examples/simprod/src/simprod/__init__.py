"""simprod: a small simulated production that an agent can be tested against.

Three services (gateway, ledger, notifications) behind an allow-list proxy
(edge), driven by an open-loop load generator. Standard library only. Each
service serves a health endpoint, Prometheus-text metrics, an OpenAPI spec and
JSON logs, so anyone can inspect it with curl, Postman or `docker compose logs`
without trusting the agent or the harness.
"""
