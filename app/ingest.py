"""
File ingestion - built for scale.

Why this is written the way it is
---------------------------------
The first version looped with df.iterrows(), building one Python object per
row, then handed the whole lot to SQLAlchemy in a single transaction inside
the web request. That worked at 13k rows and degraded badly beyond it:
145k rows took 23s locally and far longer over a network, which is how an
upload ends up timing out with nothing saved and nothing logged.

Store POD files will be larger again, so this version:

* Converts columns with vectorised pandas operations instead of per-row
  Python loops. Whole-column work is far faster and uses much less memory
  than materialising a dict per row.
* Loads via Postgres COPY rather than INSERT. COPY is Postgres's
  purpose-built bulk path - typically 10-50x faster than row INSERTs, and
  it sidesteps insert-batching behaviour entirely. SQLite (local dev)
  falls back to chunked to_sql.
* Commits per chunk, so a failure at row 300,000 doesn't discard the
  299,999 rows that already succeeded.
* Reports progress through a callback, so the UI can show real movement
  rather than a spinner that may or may not still be alive.

Loading remains IDEMPOTENT per (table, date): re-uploading a file for a
date clears that date first, so overlapping historical exports are safe.
"""
import io
import csv

import pandas as pd
from sqlalchemy import delete

from .models import (SessionLocal, engine, DimStore, DimProduct, FactDispatch,
                     FactStoreReceiving, FactWarehouseReceiving, FactReject,
                     FactRoute, FactIndent, UploadLog,
                     canon_category, FSN_PREFIX_CATEGORY)

CHUNK_ROWS = 50_000

SIGNATURES = {
    "batching": {"po number", "cutoff datetime", "fsn", "store_id",
                 "total picked qty"},
    "store_receiving": {"warehouse id", "invoice id", "fsn",
                        "expected quantity", "received quantity"},
    "wh_receiving": {"fsn", "product", "brand", "category"},
    "rejects": {"fsn", "product", "qty", "reason"},
    "store_master": {"warehouse name", "facility site code / wh"},
    "product_master": {"brand", "category", "ean", "title", "mrp"},
    "indent": {"po qty", "vertical", "title"},
    "route": {"vehicle no", "store no"},
}


def _norm_cols(df):
    return {str(c).strip().lower().replace("\n", " ") for c in df.columns}


def detect_type(df):
    cols = _norm_cols(df)
    has = lambda *needles: any(all(x in c for x in needles) for c in cols)

    # RECEIVING and STORE_REJECTS both carry fsn/product/brand/category;
    # only rejects has 'reason', only indent has 'vertical'.
    if has("reason") and has("qty") and not has("expected", "quantity"):
        return "rejects", 1.0
    if has("vertical") and has("po", "qty"):
        return "indent", 1.0
    if has("po", "qty") and has("received") and not has("cutoff") and not has("warehouse", "id"):
        return "wh_receiving", 1.0

    scores = {}
    for name, sig in SIGNATURES.items():
        hits = sum(1 for s in sig if any(s in c for c in cols))
        scores[name] = hits / len(sig)
    best, score = max(scores.items(), key=lambda kv: kv[1])
    return (best, score) if score >= 0.6 else (None, score)


def read_any(filename, content):
    if filename.lower().endswith((".xlsx", ".xls", ".xlsm")):
        return pd.read_excel(io.BytesIO(content))
    for enc in ("utf-8", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(content), encoding=enc, low_memory=False)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(io.BytesIO(content), encoding="utf-8",
                       encoding_errors="ignore", low_memory=False)


def _col(df, *needles):
    for c in df.columns:
        n = str(c).strip().lower().replace("\n", " ")
        if all(x in n for x in needles):
            return c
    return None


# ---------------- vectorised converters ----------------
def v_int(s):
    """Whole-column numeric coercion; non-numeric and blank become 0."""
    return pd.to_numeric(s, errors="coerce").fillna(0).astype("int64")


def v_int_null(s):
    """As above but preserves NULL, for genuinely optional numbers."""
    return pd.to_numeric(s, errors="coerce").astype("Int64")


def v_date(s):
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


def v_str(s, lower=False, maxlen=None):
    out = s.astype("string").fillna("").str.strip()
    if lower:
        out = out.str.lower()
    if maxlen:
        out = out.str.slice(0, maxlen)
    return out


def v_category_from_fsn(fsn_series):
    """Vectorised FSN-prefix category fallback. F&V has no product-master
    entry, so without this it silently vanishes from every breakdown."""
    return (fsn_series.str.slice(0, 3).str.upper()
            .map(FSN_PREFIX_CATEGORY).fillna("Unknown"))


def fix_excel_date_qty_vec(s):
    """
    Excel converts quantities to dates when the cell is date-formatted -
    typing 5 becomes 1900-01-05. Recover the integer from the 1900 serial.
    Returns (values, was_corrupted_flags).
    """
    numeric = pd.to_numeric(s, errors="coerce")
    as_dt = pd.to_datetime(s, errors="coerce")
    is_1900 = as_dt.dt.year.eq(1900).fillna(False)
    recovered = (as_dt - pd.Timestamp("1899-12-31")).dt.days
    vals = numeric.where(~is_1900, recovered).fillna(0).astype("int64")
    corrupted = (is_1900 | (numeric.isna() & s.notna())).fillna(False)
    return vals, corrupted


# ---------------- bulk load ----------------
def copy_into(df, table_name, columns, progress=None):
    """
    Stream a DataFrame into Postgres with COPY, in chunks, committing each.
    Falls back to chunked to_sql on SQLite, which has no COPY.
    """
    total = len(df)
    if total == 0:
        return 0

    is_pg = engine.dialect.name == "postgresql"
    done = 0

    for start in range(0, total, CHUNK_ROWS):
        chunk = df.iloc[start:start + CHUNK_ROWS]

        if is_pg:
            buf = io.StringIO()
            chunk.to_csv(buf, index=False, header=False, na_rep="\\N",
                         quoting=csv.QUOTE_MINIMAL)
            buf.seek(0)
            raw = engine.raw_connection()
            try:
                cur = raw.cursor()
                cur.copy_expert(
                    f"COPY {table_name} ({', '.join(columns)}) "
                    f"FROM STDIN WITH (FORMAT CSV, NULL '\\N')", buf)
                raw.commit()
            finally:
                raw.close()
        else:
            chunk.to_sql(table_name, engine, if_exists="append", index=False,
                         method="multi", chunksize=500)

        done += len(chunk)
        if progress:
            progress(done, total)

    return done


def _valid_stores(db):
    return {s.warehouse_id for s in db.query(DimStore).all()}


def _attach_cat_brand(out, db):
    """Left-join product master, then fall back to the FSN prefix for
    category - as a whole-frame merge, not per-row lookups."""
    rows = db.query(DimProduct.fsn, DimProduct.category, DimProduct.brand).all()
    pm = pd.DataFrame(rows, columns=["fsn", "pm_category", "pm_brand"]) if rows \
        else pd.DataFrame(columns=["fsn", "pm_category", "pm_brand"])
    merged = out.merge(pm, on="fsn", how="left")
    merged["category"] = merged["pm_category"].fillna(v_category_from_fsn(merged["fsn"]))
    merged["brand"] = merged["pm_brand"]
    return merged.drop(columns=["pm_category", "pm_brand"])


def _replace_dates(db, model, date_col, dates):
    if dates:
        db.execute(delete(model).where(date_col.in_(dates)))
        db.commit()


def _dates_of(dt_series):
    return sorted({d for d in dt_series.dropna().dt.date.unique()})


# --------------------------------------------------------------------------
# Loaders -> (rows_loaded, rows_dropped, dates, notes)
# --------------------------------------------------------------------------
def load_store_master(db, df, progress=None):
    ser = _col(df, "serial")
    out = pd.DataFrame({
        "warehouse_id": v_str(df[_col(df, "facility")], lower=True),
        "warehouse_name": v_str(df[_col(df, "warehouse name")]),
        "wh_serial_no": v_int(df[ser]) if ser else 0,
    })
    out["city_code"] = out["warehouse_id"].str.split("_").str[0]
    db.query(DimStore).delete()
    db.commit()
    cols = ["warehouse_id", "warehouse_name", "wh_serial_no", "city_code"]
    n = copy_into(out[cols], "dim_store", cols, progress)
    return n, 0, [], ["Store master replaced in full."]


def load_product_master(db, df, progress=None):
    n_in = len(df)
    out = pd.DataFrame({
        "fsn": v_str(df[_col(df, "fsn")]),
        "ean": v_str(df[_col(df, "ean")], maxlen=20),
        "brand": v_str(df[_col(df, "brand")]),
        "category": df[_col(df, "category")].map(canon_category),
        "title": v_str(df[_col(df, "title")]),
        "mrp": pd.to_numeric(df[_col(df, "mrp")], errors="coerce"),
        "price": pd.to_numeric(df[_col(df, "price")], errors="coerce"),
    })
    out = out[out["fsn"] != ""].drop_duplicates(subset="fsn", keep="first")
    dropped = n_in - len(out)
    db.query(DimProduct).delete()
    db.commit()
    cols = ["fsn", "ean", "brand", "category", "title", "mrp", "price"]
    n = copy_into(out[cols], "dim_product", cols, progress)
    return n, dropped, [], [
        f"Category casing normalised; {dropped} duplicate FSN rows collapsed."]


def load_batching(db, df, progress=None):
    cutoff = _col(df, "cutoff")
    dt = v_date(df[cutoff])
    dates = _dates_of(dt)
    _replace_dates(db, FactDispatch, FactDispatch.dispatch_date, dates)

    out = pd.DataFrame({
        "dispatch_date": dt.dt.date,
        "cutoff_datetime": v_str(df[cutoff]),
        "po_number": v_str(df[_col(df, "po number")]),
        "fsn": v_str(df[_col(df, "fsn")]),
        "product_title": v_str(df[_col(df, "product title")]),
        "warehouse_id": v_str(df[_col(df, "store_id") or _col(df, "store", "id")],
                              lower=True),
        "expected_qty": v_int(df[_col(df, "expected qty")]),
        "picked_qty": v_int(df[_col(df, "picked qty")]),
        "shortage_qty": v_int(df[_col(df, "shortage qty")]),
        "pending_qty": v_int(df[_col(df, "pending qty")]),
        "status": v_str(df[_col(df, "status")]),
        "picked_by": v_str(df[_col(df, "picked by")]),
        "picked_at": v_str(df[_col(df, "picked at")]),
    })
    out = _attach_cat_brand(out, db)
    cols = ["dispatch_date", "cutoff_datetime", "po_number", "fsn", "product_title",
            "warehouse_id", "expected_qty", "picked_qty", "shortage_qty",
            "pending_qty", "status", "picked_by", "picked_at", "category", "brand"]
    n = copy_into(out[cols], "fact_dispatch", cols, progress)
    return n, 0, dates, [f"Dispatch loaded for {len(dates)} date(s)."]


def load_store_receiving(db, df, progress=None):
    valid = _valid_stores(db)
    wh = v_str(df[_col(df, "warehouse id")], lower=True)
    dt = v_date(df[_col(df, "invoice date")])

    n_in = len(df)

    # Some exports pad the file with entirely blank rows - the 11-Aug export
    # carried 233k of them, 94% of the file. They are not lost data (no
    # status, no quantities, no timestamps) but they must be reported
    # separately from genuine out-of-network rows, or a broken export looks
    # identical to a national one.
    blank = dt.isna() & (wh == "")
    n_blank = int(blank.sum())
    df, wh, dt = df[~blank], wh[~blank], dt[~blank]

    keep = wh.isin(valid) if valid else pd.Series(True, index=df.index)
    n_foreign = int((~keep).sum())
    df, wh, dt = df[keep], wh[keep], dt[keep]
    dropped = n_blank + n_foreign

    dates = _dates_of(dt)
    _replace_dates(db, FactStoreReceiving, FactStoreReceiving.invoice_date, dates)

    swap_c = _col(df, "swapped")
    out = pd.DataFrame({
        "invoice_date": dt.dt.date,
        "warehouse_id": wh,
        "invoice_id": v_str(df[_col(df, "invoice id")]),
        "fsn": v_str(df[_col(df, "fsn")]),
        "description": v_str(df[_col(df, "description")]),
        "expected_qty": v_int(df[_col(df, "expected quantity")]),
        "received_qty": v_int(df[_col(df, "received quantity")]),
        "damaged_qty": v_int(df[_col(df, "damaged quantity")]),
        "scanning_issue_qty": v_int(df[_col(df, "scanning issue quantity")]),
        "excess_qty": v_int(df[_col(df, "excess quantity")]),
        "returned_qty": v_int(df[_col(df, "returned quantity")]),
        "swapped_qty": v_int(df[swap_c]) if swap_c else 0,
        "status": v_str(df[_col(df, "status")]),
        "uploaded_at": v_str(df[_col(df, "uploaded at")]),
    })
    out = _attach_cat_brand(out, db)
    cols = ["invoice_date", "warehouse_id", "invoice_id", "fsn", "description",
            "expected_qty", "received_qty", "damaged_qty", "scanning_issue_qty",
            "excess_qty", "returned_qty", "swapped_qty", "status", "uploaded_at",
            "category", "brand"]
    n = copy_into(out[cols], "fact_store_receiving", cols, progress)

    notes = [f"Loaded {len(dates)} date(s)."]
    if n_blank:
        notes.append(
            f"{n_blank:,} of {n_in:,} rows ({n_blank/n_in:.0%}) were completely "
            f"blank padding from the export - no store, date, status or "
            f"quantities. Ignored. If you expected more days of data, the export "
            f"likely didn't run over the full date range.")
    if n_foreign:
        notes.append(
            f"{n_foreign:,} rows belonged to darkstores outside your 48-store "
            f"network and were excluded - Flipkart's export is national.")
    return n, dropped, dates, notes


def load_wh_receiving(db, df, progress=None):
    dt = v_date(df[_col(df, "date")])
    dates = _dates_of(dt)
    _replace_dates(db, FactWarehouseReceiving, FactWarehouseReceiving.date, dates)

    exp_c, cat_c, sh_c = _col(df, "expiry"), _col(df, "category"), _col(df, "short")
    fsn = v_str(df[_col(df, "fsn")])
    out = pd.DataFrame({
        "date": dt.dt.date,
        "ean": v_str(df[_col(df, "ean")], maxlen=20),
        "fsn": fsn,
        "product": v_str(df[_col(df, "product")]),
        "brand": v_str(df[_col(df, "brand")]),
        "category": df[cat_c].map(canon_category) if cat_c else v_category_from_fsn(fsn),
        "po_qty": v_int(df[_col(df, "po qty")]),
        "received_qty": v_int(df[_col(df, "received")]),
        "expiry_date": v_date(df[exp_c]).dt.date if exp_c else None,
        "short_qty": v_int(df[sh_c]) if sh_c else 0,
    })
    cols = list(out.columns)
    n = copy_into(out, "fact_wh_receiving", cols, progress)
    return n, 0, dates, ["Category casing normalised."]


def load_rejects(db, df, progress=None):
    dt = v_date(df[_col(df, "date")])
    dates = _dates_of(dt)
    _replace_dates(db, FactReject, FactReject.date, dates)

    fsn_c, qty_c = _col(df, "fsn"), _col(df, "qty")
    store_c, veh_c, exp_c = _col(df, "store"), _col(df, "vehicle"), _col(df, "expiry")
    cat_c = _col(df, "category")

    mask = df[fsn_c].notna()
    df, dt = df[mask], dt[mask]

    qty, corrupted = fix_excel_date_qty_vec(df[qty_c])
    fsn = v_str(df[fsn_c])
    out = pd.DataFrame({
        "date": dt.dt.date,
        "ean": v_str(df[_col(df, "ean")], maxlen=20),
        "fsn": fsn,
        "product": v_str(df[_col(df, "product")]),
        "brand": v_str(df[_col(df, "brand")]),
        "category": df[cat_c].map(canon_category) if cat_c else v_category_from_fsn(fsn),
        "qty": qty,
        "qty_was_corrupted": corrupted,
        "reason": v_str(df[_col(df, "reason")]),
        "expiry": v_date(df[exp_c]).dt.date if exp_c else None,
        "warehouse_id": v_str(df[store_c], lower=True) if store_c else None,
        "vehicle_number": v_str(df[veh_c]) if veh_c else None,
    })
    cols = list(out.columns)
    n = copy_into(out, "fact_rejects", cols, progress)

    notes = []
    n_corrupt = int(corrupted.sum())
    if n_corrupt:
        notes.append(
            f"{n_corrupt}/{n} QTY values had been converted to dates by Excel. "
            f"Original integers recovered - set that column to Number at source "
            f"to stop this recurring.")
    if not store_c or out["warehouse_id"].eq("").all():
        notes.append("No store attribution in this file - rejects can't be traced "
                     "to a specific darkstore.")
    return n, 0, dates, notes


def load_route(db, df, progress=None):
    valid = _valid_stores(db)
    dt = v_date(df[_col(df, "date")])
    dates = _dates_of(dt)
    _replace_dates(db, FactRoute, FactRoute.date, dates)

    store_c = _col(df, "store no") or _col(df, "store")
    raw_store = v_str(df[store_c], lower=True)
    co_c, ci_c = _col(df, "crate out"), _col(df, "crate in")
    st_c, en_c = _col(df, "start"), _col(df, "end")
    sno_c, drv_c, veh_c, rm_c = (_col(df, "sno"), _col(df, "driver"),
                                  _col(df, "vehicle"), _col(df, "remark"))

    out = pd.DataFrame({
        "date": dt.dt.date,
        "stop_seq": v_int(df[sno_c]) if sno_c else 0,
        "warehouse_id": raw_store.where(raw_store.isin(valid)),
        "store_name": v_str(df[store_c]),
        "driver": v_str(df[drv_c]) if drv_c else None,
        "vehicle_no": v_str(df[veh_c]) if veh_c else None,
        "out_time": v_str(df[st_c]) if st_c else None,
        "in_time": v_str(df[en_c]) if en_c else None,
        "crate_out": v_int_null(df[co_c]) if co_c else None,
        "crate_in": v_int_null(df[ci_c]) if ci_c else None,
        "remark": v_str(df[rm_c]) if rm_c else None,
    })
    cols = list(out.columns)
    n = copy_into(out, "fact_route", cols, progress)
    return n, 0, dates, ["Crate counts are per-vehicle, per-trip - they narrow a "
                         "swap to a route, not to a specific stop."]


def load_indent(db, df, progress=None):
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]
    dt = v_date(df[_col(df, "indent_date") or _col(df, "indent")])
    dates = _dates_of(dt)
    _replace_dates(db, FactIndent, FactIndent.indent_date, dates)

    frq, pod_c = _col(df, "final received"), _col(df, "po date")
    out = pd.DataFrame({
        "indent_date": dt.dt.date,
        "po_date": v_date(df[pod_c]).dt.date if pod_c else None,
        "brand": v_str(df[_col(df, "brand")]),
        "fsn": v_str(df[_col(df, "fsn")]),
        "po_qty": v_int(df[_col(df, "po qty")]),
        "vertical": v_str(df[_col(df, "vertical")]),
        "title": v_str(df[_col(df, "title")]),
        "final_received_qty": v_int_null(df[frq]) if frq else None,
    })
    cols = list(out.columns)
    n = copy_into(out, "fact_indent", cols, progress)

    notes = []
    if frq and df[frq].isna().all():
        notes.append("'Final Received Qty' is empty for every row - the indent "
                     "loop isn't being closed operationally.")
    return n, 0, dates, notes


LOADERS = {
    "store_master": load_store_master,
    "product_master": load_product_master,
    "batching": load_batching,
    "store_receiving": load_store_receiving,
    "wh_receiving": load_wh_receiving,
    "rejects": load_rejects,
    "route": load_route,
    "indent": load_indent,
}


def ingest_file(filename, content, user_email, forced_type=None, progress=None):
    db = SessionLocal()
    try:
        df = read_any(filename, content)
        n_in = len(df)
        ftype = forced_type or detect_type(df)[0]
        if not ftype:
            raise ValueError("Could not recognise this file's columns. "
                             "Pick the type manually.")

        loaded, dropped, dates, notes = LOADERS[ftype](db, df, progress)

        db.add(UploadLog(
            uploaded_by=user_email, filename=filename, file_type=ftype,
            dates_covered=", ".join(str(d) for d in dates) if dates else "-",
            rows_in_source=n_in, rows_loaded=loaded, rows_dropped=dropped,
            notes=" ".join(notes), status="ok"))
        db.commit()
        return {"ok": True, "type": ftype, "rows_in": n_in, "rows_loaded": loaded,
                "rows_dropped": dropped, "dates": [str(d) for d in dates],
                "notes": notes}
    except Exception as e:
        db.rollback()
        try:
            db.add(UploadLog(uploaded_by=user_email, filename=filename,
                             file_type=forced_type or "unknown", rows_in_source=0,
                             rows_loaded=0, rows_dropped=0,
                             notes=str(e)[:4000], status="error"))
            db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "error": str(e), "filename": filename}
    finally:
        db.close()
