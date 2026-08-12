"""
Analytics queries.

The metric definitions here follow the operational reality, not a textbook:

  Fulfillment gap  = ordered - picked            (warehouse could not supply;
                                                  NOT claimable against Flipkart)
  Claimable gap    = dispatched - received - damaged
                                                 (left the warehouse, never
                                                  reached the shelf, no reason
                                                  recorded)

Keeping these apart matters: on 11-Aug the fulfillment gap was 5.9% and the
claimable gap 1.51%. Blending them would have pointed the whole improvement
effort at the wrong stage.
"""
from datetime import date, timedelta

from sqlalchemy import func, and_, distinct

from .models import (SessionLocal, FactStoreReceiving, FactDispatch, FactReject,
                     FactRoute, DimStore, DimProduct)


def _apply(q, model, f):
    if f.get("date_from"):
        q = q.filter(model.invoice_date >= f["date_from"])
    if f.get("date_to"):
        q = q.filter(model.invoice_date <= f["date_to"])
    if f.get("categories"):
        q = q.filter(model.category.in_(f["categories"]))
    if f.get("brands"):
        q = q.filter(model.brand.in_(f["brands"]))
    if f.get("stores"):
        q = q.filter(model.warehouse_id.in_(f["stores"]))
    if f.get("vehicles"):
        sub = (SessionLocal().query(distinct(FactRoute.warehouse_id))
               .filter(FactRoute.vehicle_no.in_(f["vehicles"])))
        q = q.filter(model.warehouse_id.in_([r[0] for r in sub if r[0]]))
    return q


def filter_options(db):
    def col(model, c):
        return [r[0] for r in db.query(distinct(c)).filter(c.isnot(None)).all() if r[0]]

    stores = db.query(DimStore).order_by(DimStore.warehouse_name).all()
    dr = db.query(func.min(FactStoreReceiving.invoice_date),
                  func.max(FactStoreReceiving.invoice_date)).first()
    return {
        "categories": sorted(col(FactStoreReceiving, FactStoreReceiving.category)),
        "brands": sorted(col(FactStoreReceiving, FactStoreReceiving.brand)),
        "stores": [{"id": s.warehouse_id, "name": s.warehouse_name} for s in stores],
        "vehicles": sorted(col(FactRoute, FactRoute.vehicle_no)),
        "reasons": sorted(col(FactReject, FactReject.reason)),
        "date_min": str(dr[0]) if dr and dr[0] else None,
        "date_max": str(dr[1]) if dr and dr[1] else None,
    }


def headline(db, f):
    q = _apply(db.query(
        func.coalesce(func.sum(FactStoreReceiving.expected_qty), 0),
        func.coalesce(func.sum(FactStoreReceiving.received_qty), 0),
        func.coalesce(func.sum(FactStoreReceiving.damaged_qty), 0),
        func.coalesce(func.sum(FactStoreReceiving.excess_qty), 0),
        func.coalesce(func.sum(FactStoreReceiving.scanning_issue_qty), 0),
    ), FactStoreReceiving, f)
    dispatched, received, damaged, excess, scan = q.first()

    gap = dispatched - received
    claimable = max(gap - damaged, 0)

    # Warehouse-side fulfilment, from the batching file
    dq = db.query(func.coalesce(func.sum(FactDispatch.expected_qty), 0),
                  func.coalesce(func.sum(FactDispatch.picked_qty), 0))
    if f.get("date_from"):
        dq = dq.filter(FactDispatch.dispatch_date >= f["date_from"] - timedelta(days=1))
    if f.get("date_to"):
        dq = dq.filter(FactDispatch.dispatch_date <= f["date_to"])
    if f.get("categories"):
        dq = dq.filter(FactDispatch.category.in_(f["categories"]))
    if f.get("stores"):
        dq = dq.filter(FactDispatch.warehouse_id.in_(f["stores"]))
    ordered, picked = dq.first()

    return {
        "ordered": int(ordered), "picked": int(picked),
        "dispatched": int(dispatched), "received": int(received),
        "damaged": int(damaged), "excess": int(excess), "scanning": int(scan),
        "gap_units": int(gap),
        "claimable_units": int(claimable),
        "gap_pct": round(100 * gap / dispatched, 2) if dispatched else 0,
        "claimable_pct": round(100 * claimable / dispatched, 2) if dispatched else 0,
        "fulfillment_pct": round(100 * picked / ordered, 2) if ordered else 0,
        "fulfillment_gap_units": int(ordered - picked) if ordered else 0,
    }


def daily_trend(db, f):
    q = _apply(db.query(
        FactStoreReceiving.invoice_date,
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
    ), FactStoreReceiving, f).group_by(FactStoreReceiving.invoice_date) \
        .order_by(FactStoreReceiving.invoice_date)

    out = []
    for d, exp, rec, dmg in q.all():
        exp, rec, dmg = int(exp or 0), int(rec or 0), int(dmg or 0)
        claim = max(exp - rec - dmg, 0)
        out.append({"date": str(d), "dispatched": exp, "received": rec,
                    "gap_pct": round(100 * (exp - rec) / exp, 2) if exp else 0,
                    "claimable_pct": round(100 * claim / exp, 2) if exp else 0})
    return out


def store_ranking(db, f, flag_threshold=3.0, min_volume=100):
    """
    Ranked by claimable gap %, with a repeat-offender count.

    A store is only flagged for a day where it BOTH exceeded the threshold and
    carried enough volume to be meaningful - otherwise a store receiving 12
    units and missing one shows up as an 8% crisis.
    """
    rows = _apply(db.query(
        FactStoreReceiving.warehouse_id,
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
        func.sum(FactStoreReceiving.excess_qty),
    ), FactStoreReceiving, f).group_by(FactStoreReceiving.warehouse_id).all()

    per_day = _apply(db.query(
        FactStoreReceiving.warehouse_id,
        FactStoreReceiving.invoice_date,
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
    ), FactStoreReceiving, f).group_by(FactStoreReceiving.warehouse_id,
                                       FactStoreReceiving.invoice_date).all()

    flags, days = {}, {}
    for wh, d, exp, rec, dmg in per_day:
        exp, rec, dmg = int(exp or 0), int(rec or 0), int(dmg or 0)
        days[wh] = days.get(wh, 0) + 1
        if exp >= min_volume:
            claim = max(exp - rec - dmg, 0)
            if exp and 100 * claim / exp >= flag_threshold:
                flags[wh] = flags.get(wh, 0) + 1

    names = {s.warehouse_id: s.warehouse_name for s in db.query(DimStore).all()}
    out = []
    for wh, exp, rec, dmg, exc in rows:
        exp, rec, dmg, exc = int(exp or 0), int(rec or 0), int(dmg or 0), int(exc or 0)
        claim = max(exp - rec - dmg, 0)
        pct = round(100 * claim / exp, 2) if exp else 0
        nflag, ndays = flags.get(wh, 0), days.get(wh, 0)
        status = "clean"
        if ndays >= 3 and nflag >= max(2, int(ndays * 0.5)):
            status = "repeat"
        elif nflag:
            status = "watch"
        out.append({"warehouse_id": wh, "name": names.get(wh, wh),
                    "dispatched": exp, "received": rec, "damaged": dmg,
                    "excess": exc, "claimable_units": claim, "gap_pct": pct,
                    "days_flagged": nflag, "days_total": ndays, "status": status})
    return sorted(out, key=lambda r: -r["gap_pct"])


def category_breakdown(db, f):
    rows = _apply(db.query(
        FactStoreReceiving.category,
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
    ), FactStoreReceiving, f).group_by(FactStoreReceiving.category).all()

    out = []
    for cat, exp, rec, dmg in rows:
        exp, rec, dmg = int(exp or 0), int(rec or 0), int(dmg or 0)
        claim = max(exp - rec - dmg, 0)
        out.append({"category": cat or "Unknown", "dispatched": exp,
                    "received": rec, "damaged": dmg, "claimable_units": claim,
                    "gap_pct": round(100 * claim / exp, 2) if exp else 0})
    return sorted(out, key=lambda r: -r["claimable_units"])


def product_detail(db, f, limit=200):
    rows = _apply(db.query(
        FactStoreReceiving.fsn,
        func.max(FactStoreReceiving.description),
        func.max(FactStoreReceiving.category),
        func.count(distinct(FactStoreReceiving.warehouse_id)),
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
    ), FactStoreReceiving, f).group_by(FactStoreReceiving.fsn).all()

    out = []
    for fsn, desc, cat, nstores, exp, rec, dmg in rows:
        exp, rec, dmg = int(exp or 0), int(rec or 0), int(dmg or 0)
        claim = max(exp - rec - dmg, 0)
        if claim <= 0:
            continue
        out.append({"fsn": fsn, "description": desc, "category": cat,
                    "stores_affected": int(nstores), "dispatched": exp,
                    "received": rec, "claimable_units": claim,
                    "gap_pct": round(100 * claim / exp, 2) if exp else 0})
    return sorted(out, key=lambda r: -r["claimable_units"])[:limit]


def swap_candidates(db, f):
    """
    Excess at one store alongside shortage at another, same FSN, same day,
    same vehicle.

    Deliberately does NOT classify what happened. On 11-Aug the magnitude
    match alone was misleading: F&V items showed 143 units short spread over
    19 stores against 8 units excess elsewhere, which is systemic under-count,
    not a crate swap. The route link is what separates the two, so it is shown
    as evidence for the operator to judge.
    """
    rows = _apply(db.query(
        FactStoreReceiving.invoice_date, FactStoreReceiving.fsn,
        FactStoreReceiving.warehouse_id, FactStoreReceiving.description,
        FactStoreReceiving.expected_qty, FactStoreReceiving.received_qty,
        FactStoreReceiving.excess_qty,
    ), FactStoreReceiving, f).all()

    routes = {}
    for r in db.query(FactRoute).all():
        if r.warehouse_id:
            routes[(r.date, r.warehouse_id)] = r.vehicle_no

    by_key = {}
    for d, fsn, wh, desc, exp, rec, exc in rows:
        short = max((exp or 0) - (rec or 0), 0)
        if not short and not exc:
            continue
        k = (d, fsn)
        by_key.setdefault(k, {"desc": desc, "short": [], "excess": []})
        if short:
            by_key[k]["short"].append((wh, short))
        if exc:
            by_key[k]["excess"].append((wh, exc))

    out = []
    for (d, fsn), v in by_key.items():
        if not v["short"] or not v["excess"]:
            continue
        ts = sum(x[1] for x in v["short"])
        te = sum(x[1] for x in v["excess"])
        ex_vehicles = {routes.get((d, wh)) for wh, _ in v["excess"]}
        shared = [(wh, q) for wh, q in v["short"]
                  if routes.get((d, wh)) and routes.get((d, wh)) in ex_vehicles]
        out.append({
            "date": str(d), "fsn": fsn, "description": v["desc"],
            "total_short": ts, "total_excess": te,
            "stores_short": len(v["short"]), "stores_excess": len(v["excess"]),
            "excess_stores": [{"store": w, "qty": q, "vehicle": routes.get((d, w))}
                              for w, q in sorted(v["excess"], key=lambda x: -x[1])],
            "short_stores": [{"store": w, "qty": q, "vehicle": routes.get((d, w))}
                             for w, q in sorted(v["short"], key=lambda x: -x[1])][:12],
            "same_vehicle_pairs": len(shared),
            "spread": "concentrated" if len(v["short"]) <= 3 else "spread",
        })
    return sorted(out, key=lambda r: (-r["same_vehicle_pairs"], -r["total_excess"]))


def reject_breakdown(db, f):
    q = db.query(FactReject.reason, FactReject.category,
                 func.sum(FactReject.qty), func.count(FactReject.id))
    if f.get("date_from"):
        q = q.filter(FactReject.date >= f["date_from"])
    if f.get("date_to"):
        q = q.filter(FactReject.date <= f["date_to"])
    if f.get("categories"):
        q = q.filter(FactReject.category.in_(f["categories"]))
    if f.get("reasons"):
        q = q.filter(FactReject.reason.in_(f["reasons"]))
    rows = q.group_by(FactReject.reason, FactReject.category).all()
    return [{"reason": r or "Not recorded", "category": c or "Unknown",
             "qty": int(q_ or 0), "incidents": int(n)}
            for r, c, q_, n in sorted(rows, key=lambda x: -(x[2] or 0))]


def data_quality(db, f):
    """Surfaces gaps in the inputs themselves, so bad data is never mistaken
    for a bad store."""
    issues = []

    unmapped = _apply(db.query(
        func.count(distinct(FactStoreReceiving.fsn)),
        func.sum(FactStoreReceiving.expected_qty)
    ), FactStoreReceiving, f).outerjoin(
        DimProduct, FactStoreReceiving.fsn == DimProduct.fsn
    ).filter(DimProduct.fsn.is_(None)).first()
    if unmapped and unmapped[0]:
        issues.append({
            "severity": "high",
            "title": f"{unmapped[0]} products missing from Product Master",
            "detail": f"{int(unmapped[1] or 0):,} units dispatched with no master "
                      f"record. Category is being inferred from the FSN prefix; "
                      f"brand, MRP and price are unavailable for these."})

    total_days = db.query(func.count(distinct(FactStoreReceiving.invoice_date))).scalar() or 0
    active = db.query(func.count(distinct(FactStoreReceiving.warehouse_id))).scalar() or 0
    known = db.query(func.count(DimStore.warehouse_id)).scalar() or 0
    if known and active < known:
        issues.append({
            "severity": "medium",
            "title": f"{known - active} of {known} stores have no receiving data",
            "detail": "Either they had no supply on the loaded dates, or their GRN "
                      "file never arrived. Worth confirming which."})

    nreject = db.query(func.count(FactReject.id)).scalar() or 0
    unattr = db.query(func.count(FactReject.id)).filter(
        FactReject.warehouse_id.is_(None)).scalar() or 0
    if nreject and unattr:
        issues.append({
            "severity": "medium" if unattr < nreject else "high",
            "title": f"{unattr}/{nreject} reject records have no store attached",
            "detail": "Rejects logged as a daily total cannot be traced to a store, "
                      "so damage patterns can't be pinned to a location or route."})

    corrupt = db.query(func.count(FactReject.id)).filter(
        FactReject.qty_was_corrupted.is_(True)).scalar() or 0
    if corrupt:
        issues.append({
            "severity": "medium",
            "title": f"{corrupt} reject quantities were auto-converted to dates by Excel",
            "detail": "Recovered on import, but the source sheet's QTY column should "
                      "be formatted as Number to stop this recurring."})

    if total_days < 7:
        issues.append({
            "severity": "low",
            "title": f"Only {total_days} day(s) of history loaded",
            "detail": "Repeat-offender flagging needs at least 7 days before a store "
                      "ranking means anything. Upload the historical exports."})

    # A file named for a date range but carrying one date usually means the
    # export ran with the wrong range, or padded the file with blank rows.
    # Worth surfacing here rather than only in the upload note, since the
    # symptom people notice is "the dashboard looks empty".
    from .models import UploadLog
    padded = (db.query(UploadLog)
              .filter(UploadLog.notes.like("%blank padding%"))
              .order_by(UploadLog.uploaded_at.desc()).first())
    if padded:
        issues.append({
            "severity": "medium",
            "title": "Latest store-receiving export contained blank padding rows",
            "detail": f"{padded.filename} covered {padded.dates_covered} but most of "
                      f"the file was empty rows. If you were expecting a date range, "
                      f"re-run the export with the full range selected."})
    return issues
