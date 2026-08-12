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
from sqlalchemy import delete, insert

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
    if i and i % every == 0:
        db.flush()


def bulk_insert(db, model, rows, chunk_size=1000):
    """
    Plain Core INSERT with no RETURNING clause, executed in chunks.

    db.add(Model(...)) always asks Postgres to RETURN the new primary key,
    which routes every bulk save through SQLAlchemy's "insertmanyvalues"
    machinery. That code path has a real bug: when a column is entirely NULL
    across the rows in one internal batch (brand and picked_by are frequently
    None here - unmatched FSNs, unassigned pickers), Postgres can misinfer
    that column's type and shift values into the wrong column entirely -
    this is what put a picker's name into an integer field, then a product
    title into another. None of these fact tables' auto-generated IDs are
    read back anywhere in this app, so there's no reason to pay for
    RETURNING at all: dropping it removes the buggy code path completely,
    for every table, rather than patching each column it happens to hit.
    """
    if not rows:
        return 0
    for i in range(0, len(rows), chunk_size):
        db.execute(insert(model), rows[i:i + chunk_size])
    return len(rows)


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
    rows = [dict(warehouse_id=r["warehouse_id"], warehouse_name=r.get("warehouse_name"),
                city_code=str(r["warehouse_id"]).split("_")[0],
                wh_serial_no=_int(r.get("wh_serial_no")))
            for _, r in df.iterrows()]
    bulk_insert(db, DimStore, rows)
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
    rows = [dict(fsn=r["fsn"], ean=str(r.get("ean"))[:20], brand=r.get("brand"),
                category=canon_category(r.get("category")), title=r.get("title"),
                mrp=r.get("mrp") if pd.notna(r.get("mrp")) else None,
                price=r.get("price") if pd.notna(r.get("price")) else None)
            for _, r in df.iterrows()]
    bulk_insert(db, DimProduct, rows)
    notes = [f"Category casing normalised; {dropped} duplicate FSN rows collapsed."]
    return len(df), dropped, [], notes


def load_batching(db, df):
    prod = _product_lookup(db)
    cutoff = _col(df, "cutoff")
    df["_date"] = _to_date(df[cutoff]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactDispatch, FactDispatch.dispatch_date, dates)

    wh = _col(df, "store_id") or _col(df, "store", "id")
    fsn_c, po_c, pt_c = _col(df, "fsn"), _col(df, "po number"), _col(df, "product title")
    exp_c, pk_c, sh_c, pd_c = (_col(df, "expected qty"), _col(df, "picked qty"),
                                _col(df, "shortage qty"), _col(df, "pending qty"))
    st_c, pb_c, pa_c = _col(df, "status"), _col(df, "picked by"), _col(df, "picked at")

    rows = []
    for _, r in df.iterrows():
        fsn = str(r[fsn_c]).strip()
        cat, brand = _resolve_cat_brand(fsn, prod)
        rows.append(dict(
            dispatch_date=r["_date"], cutoff_datetime=str(r[cutoff]),
            po_number=str(r[po_c]), fsn=fsn, product_title=r.get(pt_c),
            warehouse_id=str(r[wh]).lower().strip(), category=cat, brand=brand,
            expected_qty=_int(r[exp_c]), picked_qty=_int(r[pk_c]),
            shortage_qty=_int(r[sh_c]), pending_qty=_int(r[pd_c]),
            status=r.get(st_c), picked_by=r.get(pb_c), picked_at=str(r.get(pa_c))))
    bulk_insert(db, FactDispatch, rows)
    return len(rows), 0, dates, [f"Dispatch loaded for {len(dates)} date(s)."]


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
    fsn_c = _col(df, "fsn")
    inv_c, desc_c = _col(df, "invoice id"), _col(df, "description")
    exp_c, rec_c = _col(df, "expected quantity"), _col(df, "received quantity")
    dmg_c, sc_c = _col(df, "damaged quantity"), _col(df, "scanning issue quantity")
    ex_c, ret_c = _col(df, "excess quantity"), _col(df, "returned quantity")
    st_c, up_c = _col(df, "status"), _col(df, "uploaded at")

    rows = []
    for _, r in df.iterrows():
        fsn = str(r[fsn_c]).strip()
        cat, brand = _resolve_cat_brand(fsn, prod)
        rows.append(dict(
            invoice_date=r["_date"], warehouse_id=r["_wh"],
            invoice_id=str(r[inv_c]), fsn=fsn, description=r.get(desc_c),
            category=cat, brand=brand,
            expected_qty=_int(r[exp_c]), received_qty=_int(r[rec_c]),
            damaged_qty=_int(r[dmg_c]), scanning_issue_qty=_int(r[sc_c]),
            excess_qty=_int(r[ex_c]), returned_qty=_int(r[ret_c]),
            swapped_qty=_int(r[swap_c]) if swap_c else 0,
            status=r.get(st_c), uploaded_at=str(r.get(up_c))))
    bulk_insert(db, FactStoreReceiving, rows)

    notes = [f"Loaded {len(dates)} date(s)."]
    if dropped:
        notes.append(
            f"{dropped} rows ({dropped/n_in:.1%}) belonged to darkstores outside "
            f"your 48-store network and were excluded. Flipkart's export is national.")
    return len(rows), dropped, dates, notes


def load_wh_receiving(db, df):
    prod = _product_lookup(db)
    df["_date"] = _to_date(df[_col(df, "date")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactWarehouseReceiving, FactWarehouseReceiving.date, dates)

    exp_c = _col(df, "expiry")
    fsn_c = _col(df, "fsn")
    ean_c, prod_c, brand_c, cat_c = (_col(df, "ean"), _col(df, "product"),
                                      _col(df, "brand"), _col(df, "category"))
    poq_c, rec_c, sh_c = _col(df, "po qty"), _col(df, "received"), _col(df, "short")

    rows = []
    for _, r in df.iterrows():
        fsn = str(r[fsn_c]).strip()
        cat = canon_category(r.get(cat_c)) or category_from_fsn(fsn)
        exp = _to_date(r.get(exp_c)) if exp_c else None
        rows.append(dict(
            date=r["_date"], ean=str(r.get(ean_c))[:20], fsn=fsn,
            product=r.get(prod_c), brand=r.get(brand_c), category=cat,
            po_qty=_int(r.get(poq_c)), received_qty=_int(r.get(rec_c)),
            expiry_date=exp.date() if pd.notna(exp) else None,
            short_qty=_int(r.get(sh_c))))
    bulk_insert(db, FactWarehouseReceiving, rows)
    return len(rows), 0, dates, ["Category casing normalised."]


def load_rejects(db, df):
    df["_date"] = _to_date(df[_col(df, "date")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactReject, FactReject.date, dates)

    qty_c = _col(df, "qty")
    store_c = _col(df, "store")
    veh_c = _col(df, "vehicle")
    exp_c = _col(df, "expiry")
    fsn_c = _col(df, "fsn")
    ean_c, prod_c, brand_c, cat_c, rea_c = (_col(df, "ean"), _col(df, "product"),
                                              _col(df, "brand"), _col(df, "category"),
                                              _col(df, "reason"))

    df = df[df[fsn_c].notna()]
    rows, corrupted = [], 0
    for _, r in df.iterrows():
        fsn = str(r[fsn_c]).strip()
        qty, was_bad = fix_excel_date_qty(r.get(qty_c))
        corrupted += 1 if was_bad else 0
        exp = _to_date(r.get(exp_c)) if exp_c else None
        wh = str(r.get(store_c)).lower().strip() if store_c and pd.notna(r.get(store_c)) else None
        rows.append(dict(
            date=r["_date"], ean=str(r.get(ean_c))[:20], fsn=fsn,
            product=r.get(prod_c), brand=r.get(brand_c),
            category=canon_category(r.get(cat_c)) or category_from_fsn(fsn),
            qty=qty, qty_was_corrupted=was_bad, reason=r.get(rea_c),
            expiry=exp.date() if exp is not None and pd.notna(exp) else None,
            warehouse_id=wh,
            vehicle_number=str(r.get(veh_c)) if veh_c and pd.notna(r.get(veh_c)) else None))
    bulk_insert(db, FactReject, rows)

    notes = []
    if corrupted:
        notes.append(
            f"{corrupted}/{len(rows)} QTY values had been converted to dates by Excel "
            f"(cell formatted as date). Original integers recovered.")
    if not store_c or df[store_c].isna().all():
        notes.append("No store attribution in this file - rejects cannot be traced "
                     "to a specific darkstore.")
    return len(rows), 0, dates, notes


def load_route(db, df):
    valid = _valid_stores(db)
    df["_date"] = _to_date(df[_col(df, "date")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactRoute, FactRoute.date, dates)

    store_c = _col(df, "store no") or _col(df, "store")
    sno_c, drv_c, veh_c = _col(df, "sno"), _col(df, "driver"), _col(df, "vehicle")
    st_c, en_c = _col(df, "start"), _col(df, "end")
    co_c, ci_c, rm_c = _col(df, "crate out"), _col(df, "crate in"), _col(df, "remark")

    rows = []
    for _, r in df.iterrows():
        raw = str(r.get(store_c, "")).lower().strip()
        wh = raw if raw in valid else None
        rows.append(dict(
            date=r["_date"], stop_seq=_int(r.get(sno_c)),
            warehouse_id=wh, store_name=str(r.get(store_c)), driver=r.get(drv_c),
            vehicle_no=str(r.get(veh_c)).strip(),
            out_time=str(r.get(st_c)) if st_c else None,
            in_time=str(r.get(en_c)) if en_c else None,
            crate_out=_int(r.get(co_c)) if co_c else None,
            crate_in=_int(r.get(ci_c)) if ci_c else None,
            remark=r.get(rm_c)))
    bulk_insert(db, FactRoute, rows)
    return len(rows), 0, dates, ["Crate counts are per-vehicle, per-trip - they narrow a "
                                 "swap to a route, not to a specific stop."]


def load_indent(db, df):
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]
    df["_date"] = _to_date(df[_col(df, "indent_date") or _col(df, "indent")]).dt.date
    dates = sorted({d for d in df["_date"].dropna().unique()})
    _replace_dates(db, FactIndent, FactIndent.indent_date, dates)

    frq = _col(df, "final received")
    pod_c, brd_c, fsn_c = _col(df, "po date"), _col(df, "brand"), _col(df, "fsn")
    poq_c, vert_c, ttl_c = _col(df, "po qty"), _col(df, "vertical"), _col(df, "title")

    rows = []
    for _, r in df.iterrows():
        pod = _to_date(r.get(pod_c))
        rows.append(dict(
            indent_date=r["_date"],
            po_date=pod.date() if pd.notna(pod) else None,
            brand=r.get(brd_c), fsn=str(r.get(fsn_c)).strip(),
            po_qty=_int(r.get(poq_c)), vertical=r.get(vert_c), title=r.get(ttl_c),
            final_received_qty=_int(r.get(frq)) if frq and pd.notna(r.get(frq)) else None))
    bulk_insert(db, FactIndent, rows)

    notes = []
    if frq and df[frq].isna().all():
        notes.append("'Final Received Qty' is empty for every row - the indent loop "
                     "is not being closed operationally.")
    return len(rows), 0, dates, notes


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
