"""
File ingestion.

Design notes for whoever maintains this next:

* Files are auto-detected by their column signature, so Nitesh can drop the
  day's exports in any order without picking a type from a dropdown.
* Loading is IDEMPOTENT per (table, date). Re-uploading a file for a date
  wipes that date's rows first, then re-inserts. This is what makes the
  overlapping historical exports safe - the 1-7 and 7-10 store-receiving
  files both contain 7-Aug, and without this the day would double-count.
* Every known data defect found during the 11-Aug audit is corrected here,
  and recorded in upload_log rather than silently fixed.
"""
import io
import hashlib
from datetime import datetime

import pandas as pd
from sqlalchemy import delete

from .models import (SessionLocal, DimStore, DimProduct, FactDispatch,
                     FactStoreReceiving, FactWarehouseReceiving, FactReject,
                     FactRoute, FactIndent, UploadLog,
                     canon_category, category_from_fsn)

# --------------------------------------------------------------------------
# Column signatures used for auto-detection
# --------------------------------------------------------------------------
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

    # Disambiguate the two look-alikes first. RECEIVING and STORE_REJECTS both
    # carry fsn/product/brand/category; only rejects has a 'reason' column, and
    # only warehouse receiving has a 'po qty'/'received' pair.
    has = lambda *needles: any(all(x in c for x in needles) for c in cols)
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
            return pd.read_csv(io.BytesIO(content), encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(io.BytesIO(content), encoding="utf-8", errors="ignore")


def _col(df, *needles):
    """Find a column whose normalised name contains all needles."""
    for c in df.columns:
        n = str(c).strip().lower().replace("\n", " ")
        if all(x in n for x in needles):
            return c
    return None


def _to_date(s):
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


def _maybe_flush(db, i, every=150):
    """Bounds how many pending objects SQLAlchemy ever batches into one
    multi-row INSERT. See the insertmanyvalues_page_size note in models.py -
    this is the second half of that fix, applied at the loader level so it
    holds regardless of engine configuration."""
    if i and i % every == 0:
        db.flush()


def _int(v):
    if pd.isna(v):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fix_excel_date_qty(val):
    """
    STORE_REJECTS quantities get silently converted to dates by Excel when
    the cell is date-formatted: typing 5 stores as 1900-01-05. On the 11-Aug
    file this hit 12 of 32 rows. Recover the original integer from the
    1900 date serial.
    """
    if pd.isna(val):
        return 0, False
    if isinstance(val, (int, float)):
        return int(val), False
    try:
        ts = pd.Timestamp(val)
        if ts.year == 1900:
            return int((ts - pd.Timestamp("1899-12-31")).days), True
        return 0, True
    except Exception:
        try:
            return int(val), False
        except Exception:
            return 0, True


# --------------------------------------------------------------------------
def _valid_stores(db):
    return {s.warehouse_id for s in db.query(DimStore).all()}


def _product_lookup(db):
    return {p.fsn: p for p in db.query(DimProduct).all()}


def _resolve_cat_brand(fsn, prod_map):
    p = prod_map.get(fsn)
    if p and p.category:
        return p.category, p.brand
    return category_from_fsn(fsn), (p.brand if p else None)


def _replace_dates(db, model, date_col, dates):
    """Idempotency: clear existing rows for the dates this file covers."""
    if not dates:
        return 0
    n = db.query(model).filter(date_col.in_(dates)).count()
    db.execute(delete(model).where(date_col.in_(dates)))
    return n


# --------------------------------------------------------------------------
# Loaders. Each returns (rows_loaded, rows_dropped, dates, notes[])
# --------------------------------------------------------------------------
def load_store_master(db, df):
    df = df.rename(columns={_col(df, "serial"): "wh_serial_no",
                            _col(df, "warehouse name"): "warehouse_name",
                            _col(df, "facility"): "warehouse_id"})
    df["warehouse_id"] = df["warehouse_id"].astype(str).str.lower().str.strip()
    db.query(DimStore).delete()
    for _, r in df.iterrows():
        db.add(DimStore(warehouse_id=r["warehouse_id"],
                        warehouse_name=r.get("warehouse_name"),
                        city_code=str(r["warehouse_id"]).split("_")[0],
                        wh_serial_no=_int(r.get("wh_serial_no"))))
    return len(df), 0, [], ["Store master replaced in full."]


def load_product_master(db, df):
    fsn_c = _col(df, "fsn")
    df = df.rename(columns={_col(df, "brand"): "brand",
                            _col(df, "category"): "category",
                            _col(df, "ean"): "ean", fsn_c: "fsn",
                            _col(df, "title"): "title",
                            _col(df, "mrp"): "mrp", _col(df, "price"): "price"})
    n_in = len(df)
    df["fsn"] = df["fsn"].astype(str).str.strip()
    df = df.drop_duplicates(subset="fsn", keep="first")
    dropped = n_in - len(df)
    db.query(DimProduct).delete()
    for _, r in df.iterrows():
        db.add(DimProduct(fsn=r["fsn"], ean=str(r.get("ean"))[:20],
                          brand=r.get("brand"),
                          category=canon_category(r.get("category")),
                          title=r.get("title"),
                          mrp=r.get("mrp") if pd.notna(r.get("mrp")) else None,
                          price=r.get("price") if pd.notna(r.get("price")) else None))
    notes = [f"Category casing normalised; {dropped} duplicate FSN rows collapsed."]
    return len(df), dropped, [], notes


def load_batching(db, df):
    prod = _product_lookup(db)
    cutoff = _col(df, "cutoff")
    df["_date"] = _to_date(df[cutoff]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactDispatch, FactDispatch.dispatch_date, dates)

    wh = _col(df, "store_id") or _col(df, "store", "id")
    rows = 0
    for _, r in df.iterrows():
        fsn = str(r[_col(df, "fsn")]).strip()
        cat, brand = _resolve_cat_brand(fsn, prod)
        db.add(FactDispatch(
            dispatch_date=r["_date"], cutoff_datetime=str(r[cutoff]),
            po_number=str(r[_col(df, "po number")]), fsn=fsn,
            product_title=r.get(_col(df, "product title")),
            warehouse_id=str(r[wh]).lower().strip(),
            category=cat, brand=brand,
            expected_qty=_int(r[_col(df, "expected qty")]),
            picked_qty=_int(r[_col(df, "picked qty")]),
            shortage_qty=_int(r[_col(df, "shortage qty")]),
            pending_qty=_int(r[_col(df, "pending qty")]),
            status=r.get(_col(df, "status")),
            picked_by=r.get(_col(df, "picked by")),
            picked_at=str(r.get(_col(df, "picked at")))))
        rows += 1
        _maybe_flush(db, rows)
    return rows, 0, dates, [f"Dispatch loaded for {len(dates)} date(s)."]


def load_store_receiving(db, df):
    prod = _product_lookup(db)
    valid = _valid_stores(db)
    whc = _col(df, "warehouse id")
    df["_wh"] = df[whc].astype(str).str.lower().str.strip()
    df["_date"] = _to_date(df[_col(df, "invoice date")]).dt.date

    n_in = len(df)
    df = df[df["_wh"].isin(valid)] if valid else df
    dropped = n_in - len(df)

    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactStoreReceiving, FactStoreReceiving.invoice_date, dates)

    swap_c = _col(df, "swapped")
    rows = 0
    for _, r in df.iterrows():
        fsn = str(r[_col(df, "fsn")]).strip()
        cat, brand = _resolve_cat_brand(fsn, prod)
        db.add(FactStoreReceiving(
            invoice_date=r["_date"], warehouse_id=r["_wh"],
            invoice_id=str(r[_col(df, "invoice id")]), fsn=fsn,
            description=r.get(_col(df, "description")),
            category=cat, brand=brand,
            expected_qty=_int(r[_col(df, "expected quantity")]),
            received_qty=_int(r[_col(df, "received quantity")]),
            damaged_qty=_int(r[_col(df, "damaged quantity")]),
            scanning_issue_qty=_int(r[_col(df, "scanning issue quantity")]),
            excess_qty=_int(r[_col(df, "excess quantity")]),
            returned_qty=_int(r[_col(df, "returned quantity")]),
            swapped_qty=_int(r[swap_c]) if swap_c else 0,
            status=r.get(_col(df, "status")),
            uploaded_at=str(r.get(_col(df, "uploaded at")))))
        rows += 1
        _maybe_flush(db, rows)

    notes = [f"Loaded {len(dates)} date(s)."]
    if dropped:
        notes.append(
            f"{dropped} rows ({dropped/n_in:.1%}) belonged to darkstores outside "
            f"your 48-store network and were excluded. Flipkart's export is national.")
    return rows, dropped, dates, notes


def load_wh_receiving(db, df):
    prod = _product_lookup(db)
    df["_date"] = _to_date(df[_col(df, "date")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactWarehouseReceiving, FactWarehouseReceiving.date, dates)

    exp_c = _col(df, "expiry")
    rows = 0
    for _, r in df.iterrows():
        fsn = str(r[_col(df, "fsn")]).strip()
        cat = canon_category(r.get(_col(df, "category"))) or category_from_fsn(fsn)
        exp = _to_date(r.get(exp_c)) if exp_c else None
        db.add(FactWarehouseReceiving(
            date=r["_date"], ean=str(r.get(_col(df, "ean")))[:20], fsn=fsn,
            product=r.get(_col(df, "product")), brand=r.get(_col(df, "brand")),
            category=cat,
            po_qty=_int(r.get(_col(df, "po qty"))),
            received_qty=_int(r.get(_col(df, "received"))),
            expiry_date=exp.date() if pd.notna(exp) else None,
            short_qty=_int(r.get(_col(df, "short")))))
        rows += 1
        _maybe_flush(db, rows)
    return rows, 0, dates, ["Category casing normalised."]


def load_rejects(db, df):
    df["_date"] = _to_date(df[_col(df, "date")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactReject, FactReject.date, dates)

    qty_c = _col(df, "qty")
    store_c = _col(df, "store")
    veh_c = _col(df, "vehicle")
    exp_c = _col(df, "expiry")
    fsn_c = _col(df, "fsn")

    rows = corrupted = 0
    df = df[df[fsn_c].notna()]
    for _, r in df.iterrows():
        fsn = str(r[fsn_c]).strip()
        qty, was_bad = fix_excel_date_qty(r.get(qty_c))
        corrupted += 1 if was_bad else 0
        exp = _to_date(r.get(exp_c)) if exp_c else None
        wh = str(r.get(store_c)).lower().strip() if store_c and pd.notna(r.get(store_c)) else None
        db.add(FactReject(
            date=r["_date"], ean=str(r.get(_col(df, "ean")))[:20], fsn=fsn,
            product=r.get(_col(df, "product")), brand=r.get(_col(df, "brand")),
            category=canon_category(r.get(_col(df, "category"))) or category_from_fsn(fsn),
            qty=qty, qty_was_corrupted=was_bad,
            reason=r.get(_col(df, "reason")),
            expiry=exp.date() if exp is not None and pd.notna(exp) else None,
            warehouse_id=wh,
            vehicle_number=str(r.get(veh_c)) if veh_c and pd.notna(r.get(veh_c)) else None))
        rows += 1
        _maybe_flush(db, rows)

    notes = []
    if corrupted:
        notes.append(
            f"{corrupted}/{rows} QTY values had been converted to dates by Excel "
            f"(cell formatted as date). Original integers recovered.")
    if not store_c or df[store_c].isna().all():
        notes.append("No store attribution in this file - rejects cannot be traced "
                     "to a specific darkstore.")
    return rows, 0, dates, notes


def load_route(db, df):
    valid = _valid_stores(db)
    df["_date"] = _to_date(df[_col(df, "date")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactRoute, FactRoute.date, dates)

    store_c = _col(df, "store no") or _col(df, "store")
    rows = 0
    for _, r in df.iterrows():
        raw = str(r.get(store_c, "")).lower().strip()
        wh = raw if raw in valid else None
        db.add(FactRoute(
            date=r["_date"], stop_seq=_int(r.get(_col(df, "sno"))),
            warehouse_id=wh, store_name=str(r.get(store_c)),
            driver=r.get(_col(df, "driver")),
            vehicle_no=str(r.get(_col(df, "vehicle"))).strip(),
            out_time=str(r.get(_col(df, "start"))) if _col(df, "start") else None,
            in_time=str(r.get(_col(df, "end"))) if _col(df, "end") else None,
            crate_out=_int(r.get(_col(df, "crate out"))) if _col(df, "crate out") else None,
            crate_in=_int(r.get(_col(df, "crate in"))) if _col(df, "crate in") else None,
            remark=r.get(_col(df, "remark"))))
        rows += 1
        _maybe_flush(db, rows)
    return rows, 0, dates, ["Crate counts are per-vehicle, per-trip - they narrow a "
                            "swap to a route, not to a specific stop."]


def load_indent(db, df):
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]
    df["_date"] = _to_date(df[_col(df, "indent_date") or _col(df, "indent")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactIndent, FactIndent.indent_date, dates)

    frq = _col(df, "final received")
    rows = 0
    for _, r in df.iterrows():
        db.add(FactIndent(
            indent_date=r["_date"],
            po_date=_to_date(r.get(_col(df, "po date"))).date() if pd.notna(r.get(_col(df, "po date"))) else None,
            brand=r.get(_col(df, "brand")),
            fsn=str(r.get(_col(df, "fsn"))).strip(),
            po_qty=_int(r.get(_col(df, "po qty"))),
            vertical=r.get(_col(df, "vertical")),
            title=r.get(_col(df, "title")),
            final_received_qty=_int(r.get(frq)) if frq and pd.notna(r.get(frq)) else None))
        rows += 1
        # Flush every single row (not batched) - this table has columns that
        # are entirely NULL across the whole file (final_received_qty,
        # ds_delivery_date), which trips a Postgres/SQLAlchemy multi-row
        # VALUES type-inference bug when more than one such row is batched
        # together. Indent files are small (low thousands of rows), so the
        # per-row round-trip cost is negligible here - unlike batching or
        # store-receiving files, which can run into six figures of rows and
        # must stay on fast bulk inserts.
        db.flush()

    notes = []
    if frq and df[frq].isna().all():
        notes.append("'Final Received Qty' is empty for every row - the indent loop "
                     "is not being closed operationally.")
    return rows, 0, dates, notes


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


def ingest_file(filename, content, user_email, forced_type=None):
    db = SessionLocal()
    try:
        df = read_any(filename, content)
        n_in = len(df)
        ftype = forced_type
        if not ftype:
            ftype, score = detect_type(df)
        if not ftype:
            raise ValueError(
                "Could not recognise this file's columns. Pick the type manually.")

        loaded, dropped, dates, notes = LOADERS[ftype](db, df)
        log = UploadLog(
            uploaded_by=user_email, filename=filename, file_type=ftype,
            dates_covered=", ".join(str(d) for d in dates) if dates else "-",
            rows_in_source=n_in, rows_loaded=loaded, rows_dropped=dropped,
            notes=" ".join(notes), status="ok")
        db.add(log)
        db.commit()
        return {"ok": True, "type": ftype, "rows_in": n_in, "rows_loaded": loaded,
                "rows_dropped": dropped, "dates": [str(d) for d in dates],
                "notes": notes}
    except Exception as e:
        db.rollback()
        db.add(UploadLog(uploaded_by=user_email, filename=filename,
                         file_type=forced_type or "unknown", rows_in_source=0,
                         rows_loaded=0, rows_dropped=0, notes=str(e), status="error"))
        db.commit()
        return {"ok": False, "error": str(e), "filename": filename}
    finally:
        db.close()
