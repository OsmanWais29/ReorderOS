"""Background worker entry points.

Deployed workers (one DigitalOcean component each; all run as service_worker and
assert the service-pool role at startup before doing any work):
  python -m app.workers.inbox_worker              # POS event inbox drain
  python -m app.workers.receipt_extraction_worker # receipt photo -> line extraction
  python -m app.workers.inbound_email_worker      # inbound invoice email intake
  python -m app.workers.reconciliation_worker     # POS reconciliation + POS token refresh

There is NO dedicated token-refresh worker: POS token refresh runs inside
reconciliation_worker (app.modules.pos.token_refresh.refresh_expiring_tokens).
"""
