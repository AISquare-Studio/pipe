"""Salesforce connector for aisquare.pipe.

Pure ``(config) -> Salesforce REST API`` adapter:
- :class:`~aisquare_pipe_salesforce.connector.SalesforceSource` pulls record
  attachments (ContentVersion bytes) as DataEnvelopes.
- :class:`~aisquare_pipe_salesforce.connector.SalesforceSink` pushes JSON
  field updates onto sObjects (e.g. host-state sync to custom fields).
- :mod:`~aisquare_pipe_salesforce.webhook` verifies inbound shared-secret
  HMAC webhooks (the host owns the HTTP route).
- OAuth helpers in :mod:`~aisquare_pipe_salesforce.client` RETURN token
  payloads — the host persists them (CredentialProvider seam).
"""

from aisquare_pipe_salesforce.connector import SalesforceSink, SalesforceSource

__all__ = ["SalesforceSource", "SalesforceSink"]
