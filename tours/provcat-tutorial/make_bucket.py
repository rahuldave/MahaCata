"""Make an empty bucket on the tour's moto server, with versioning on."""
import os
import sys

import boto3

s3 = boto3.client("s3", endpoint_url=f"http://localhost:{os.environ['TOUR_S3_PORT']}")
for name in sys.argv[1:]:
    s3.create_bucket(Bucket=name)
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
