
from typing import Optional
try:
    import lance  # noqa
except Exception:
    lance = None

def is_available() -> bool:
    return lance is not None

def dataset_uri_to_path(org:str, domain:str, env:str, family:str, name:str, repo:str, branch:str)->str:
    return f"s3://{repo}/{branch}/{org}/{domain}/{env}/{family}/{name}.lance"

def ensure_table(path: str, schema: dict | None = None):
    if lance is None:
        raise RuntimeError("lance is not installed. pip install lance pyarrow")
    import pyarrow as pa
    from lance import write_dataset
    if schema and "fields" in schema:
        fields = []
        for f in schema["fields"]:
            tname = f["type"]
            if tname == "timestamp":
                fields.append(pa.field(f["name"], pa.timestamp("ns")))
            else:
                fields.append(pa.field(f["name"], getattr(pa, tname)()))
        schema_pa = pa.schema(fields)
        arrays = [pa.array([], type=field.type) for field in schema_pa]
        names = [field.name for field in schema_pa]
        table = pa.Table.from_arrays(arrays, names=names)
    else:
        schema_pa = pa.schema([("id", pa.int64()), ("ts", pa.timestamp("ns"))])
        table = pa.Table.from_arrays([pa.array([], type=pa.int64()), pa.array([], type=pa.timestamp("ns"))], names=["id","ts"])
    write_dataset(table, path, mode="create", schema=schema_pa)
    return {"ok": True, "path": path}
