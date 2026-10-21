
# provcat (preview)

## Install
pip install jinja2 omegaconf lakefs-client boto3 etcd3-py mlflow

export LAKEFS_API_ENDPOINT=http://localhost:8000/api/v1
export AWS_ACCESS_KEY_ID=lakefs
export AWS_SECRET_ACCESS_KEY=lakefssecret
export S3_ENDPOINT_URL=http://localhost:8000  # lakeFS S3 gateway

## Bootstrap
mkdir -p config
cp /mnt/data/provcat/config/resolver.sample.yaml config/resolver.yaml

## Hello World (full WAM + Publish + Stage)
bash /mnt/data/provcat/scripts/hello_world_full.sh
