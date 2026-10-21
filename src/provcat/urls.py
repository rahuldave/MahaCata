
from dataclasses import dataclass
from typing import Optional, Tuple, List

@dataclass
class Selector:
    kind: str
    value: str

def parse_selector(q: str) -> Optional[Selector]:
    if not q: return None
    if q.startswith('at='): q=q[3:]
    if ':' not in q: return None
    k, v = q.split(':', 1); return Selector(kind=k, value=v)

def split_logical_id(logical_id: str) -> Tuple[str, List[str], Optional[Selector]]:
    q=None
    if '?at=' in logical_id: logical_id, q = logical_id.split('?at=',1)
    scheme, rest = logical_id.split('://',1)
    parts = [p for p in rest.strip('/').split('/') if p]
    sel = parse_selector(q) if q else None
    return scheme, parts, sel
