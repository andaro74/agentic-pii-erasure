"""S3 Object Lock COMPLIANCE + KMS — erasure as irreversible loss of readability."""

from pii_erasure.participants.compliance_archive.handler import (
    SYSTEM_ID,
    ComplianceArchive,
    lambda_handler,
)

__all__ = ["SYSTEM_ID", "ComplianceArchive", "lambda_handler"]
