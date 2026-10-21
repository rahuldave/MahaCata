
"""provcat.s3io

Write/read small text objects to S3-compatible storage.
- Primary path: boto3 to lakeFS S3 gateway (or AWS S3)
- Fallback: local filesystem under /mnt/data/_s3 for sandbox/dev

Configuration (env vars):
- S3_ENDPOINT_URL   : http(s) endpoint of lakeFS S3 gateway (e.g., http://localhost:8000)
- AWS_ACCESS_KEY_ID
- AWS_SECRET_ACCESS_KEY
- AWS_REGION        : optional; defaults to 'us-east-1'
"""
import os
from typing import Optional

_BOTO_AVAILABLE = False
try:
    import boto3  # type: ignore
    _BOTO_AVAILABLE = True
except Exception:
    _BOTO_AVAILABLE = False

_LOCAL_BASE = "/mnt/data/_s3"

def _parse_s3(s3_url: str):
    assert s3_url.startswith("s3://"), f"expected s3:// url, got {s3_url}"
    without = s3_url[5:]
    bucket, _, key = without.partition("/")
    return bucket, key

def _boto_client():
    endpoint = os.getenv("S3_ENDPOINT_URL")  # lakeFS S3 gateway or AWS default if None
    region = os.getenv("AWS_REGION", "us-east-1")
    cfg = dict(region_name=region)
    if endpoint:
        cfg["endpoint_url"] = endpoint
    return boto3.client("s3", **cfg)

def put_text(s3_url: str, text: str):
    bucket, key = _parse_s3(s3_url)
    if _BOTO_AVAILABLE:
        try:
            cli = _boto_client()
            cli.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="text/plain")
            return {"ok": True, "bucket": bucket, "key": key, "via": "boto3"}
        except Exception as e:
            # soft-fallback to local for dev
            pass
    # fallback local
    import os
    path = os.path.join(_LOCAL_BASE, bucket, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return {"ok": True, "path": path, "via": "local"}

def get_text(s3_url: str) -> Optional[str]:
    bucket, key = _parse_s3(s3_url)
    if _BOTO_AVAILABLE:
        try:
            cli = _boto_client()
            obj = cli.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read().decode("utf-8")
        except Exception:
            pass
    # fallback local
    import os
    path = os.path.join(_LOCAL_BASE, bucket, key)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
