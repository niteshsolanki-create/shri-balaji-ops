"""Load the real uploaded files through the actual ingest path."""
import os, pathlib
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
from app.models import init_db
from app.ingest import ingest_file

init_db()
U = pathlib.Path("/mnt/user-data/uploads")
order = [
    ("STORE_MASTER.xlsx", None), ("PRODUCT_MASTER.xlsx", None),
    ("1786466510384_BATCHING_10-august.csv", None),
    ("STORE_RECEIVING.csv", None), ("RECEIVING.xlsx", None),
    ("STORE_REJECTS.xlsx", None), ("INDENT.xlsx", None),
]
for fn, ft in order:
    p = U / fn
    if not p.exists():
        print(f"SKIP {fn}"); continue
    r = ingest_file(fn, p.read_bytes(), "test@local", ft)
    if r.get("ok"):
        print(f"OK   {r['type']:<16} in={r['rows_in']:>7} loaded={r['rows_loaded']:>7} dropped={r['rows_dropped']:>7}")
        for note in r["notes"]: print(f"       ! {note}")
    else:
        print(f"FAIL {fn}: {r['error']}")
