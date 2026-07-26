"""S3 with versioning — a delete marker is not a deletion."""

from pii_erasure.participants.upload_bucket.handler import SYSTEM_ID, UploadBucket, lambda_handler

__all__ = ["SYSTEM_ID", "UploadBucket", "lambda_handler"]
