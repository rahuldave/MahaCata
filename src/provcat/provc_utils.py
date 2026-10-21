from urllib.parse import urlparse

def parse_kv_list(kvs):
    out = {}
    for kv in kvs or []:
        k, sep, v = kv.partition("=")
        if not sep:
            raise ValueError(f"Invalid key=val: {kv}")
        out[k] = v
    return out

def parse_s3_url(url:str):
    u = urlparse(url)
    if u.scheme != "s3":
        raise ValueError("Not an s3 URL")
    return u.netloc, u.path.lstrip("/")
