
import os
from lakefs_client import Configuration, ApiClient
from lakefs_client.apis import BranchesApi, CommitsApi
from lakefs_client.models import BranchCreation, CommitCreation, Merge

def client(endpoint=None, access_key=None, secret_key=None):
    endpoint = endpoint or os.getenv("LAKEFS_API_ENDPOINT","http://localhost:8000/api/v1")
    access_key = access_key or os.getenv("AWS_ACCESS_KEY_ID","lakefs")
    secret_key = secret_key or os.getenv("AWS_SECRET_ACCESS_KEY","lakefssecret")
    cfg = Configuration(); cfg.username=access_key; cfg.password=secret_key; cfg.host=endpoint.rstrip("/")
    return ApiClient(cfg)

def branch_list(api_client, repo:str):
    return BranchesApi(api_client).list_branches(repository=repo).results

def branch_create(api_client, repo:str, name:str, source:str="main"):
    try:
        BranchesApi(api_client).create_branch(repo, BranchCreation(name=name, source=source))
    except Exception as e:
        if "already exists" not in str(e):
            raise

def branch_merge(api_client, repo:str, source:str, into:str, message:str):
    CommitsApi(api_client).merge_into_branch(repository=repo, source_ref=source, destination_branch=into, merge=Merge(message=message))

def commit_repo(api_client, repo:str, branch:str, message:str, metadata:dict|None=None):
    CommitsApi(api_client).commit(repository=repo, branch=branch, commit_creation=CommitCreation(message=message, metadata=metadata or {}))
