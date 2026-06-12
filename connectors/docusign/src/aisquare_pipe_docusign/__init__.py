"""DocuSign eSignature connector for aisquare.pipe.

Pure ``(config) -> DocuSign REST API`` adapter:
- :class:`~aisquare_pipe_docusign.connector.DocuSignSink` pushes a PDF
  envelope out for signature (recipients via PushParams).
- :class:`~aisquare_pipe_docusign.connector.DocuSignSource` pulls signed
  combined documents and lists in-flight envelopes as resources.
- :mod:`~aisquare_pipe_docusign.webhook` verifies DocuSign Connect HMAC
  (the host owns the HTTP route).
- OAuth helpers in :mod:`~aisquare_pipe_docusign.client` RETURN token
  payloads — the host persists them (CredentialProvider seam).
"""

from aisquare_pipe_docusign.connector import DocuSignSink, DocuSignSource

__all__ = ["DocuSignSource", "DocuSignSink"]
