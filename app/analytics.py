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

from sqlalchemy import func, and_, or_, case, distinct

from .models import (SessionLocal, FactStoreReceiving, FactDispatch, FactReject,
                     FactRoute, FactWarehouseReceiving, FactIndent,
                     DimStore, DimProduct,
                     DEPARTMENTS, DEPARTMENT_ORDER, UNASSIGNED, department_of,
                     FSN_PREFIX_CATEGORY)


def fsn_dept_expr(fsn_col):
    """
    Department derived straight from FSN prefix, in SQL. FactIndent has no
    category column - the PO is raised before anything is categorised - so
    this is the only way to filter or group an indent row by department.
    Built once from FSN_PREFIX_CATEGORY so it can never drift from the
    category-based mapping used everywhere else.
    """
    prefix_dept = {p: department_of(c) for p, c in FSN_PREFIX_CATEGORY.items()}
    return case(
        *[(func.upper(func.substr(fsn_col, 1, 3)) == p, d)
          for p, d in prefix_dept.items()],
        else_=UNASSIGNED,
    )


def dept_expr(model):
    """
    Department as a SQL expression derived from category.

    Built as a CASE rather than a stored column so the mapping in models.py
    is the single source of truth. Change a category's department there and
    every dashboard - including all historical data - reflects it on the
    next query. A stored column would need a backfill and would let old
    rows silently disagree with new ones.
    """
    return case(
        *[(model.category.in_(cats), dept) for dept, cats in DEPARTMENTS.items()],
        else_=UNASSIGNED,
    )


def _dept_filter(q, model, f):
    """Filter by department, expressed as the underlying category list so
    the database can still use the category index."""
    depts = f.get("departments")
    if not depts:
        return q
    cats, want_unassigned = [], False
    for d in depts:
        if d == UNASSIGNED:
            want_unassigned = True
        else:
            cats.extend(DEPARTMENTS.get(d, []))
    known = [c for cs in DEPARTMENTS.values() for c in cs]
    if want_unassigned and cats:
        return q.filter(or_(model.category.in_(cats),
                            model.category.is_(None),
                            ~model.category.in_(known)))
    if want_unassigned:
        return q.filter(or_(model.category.is_(None),
                            ~model.category.in_(known)))
    return q.filter(model.category.in_(cats))


def _apply(q, model, f):
    if f.get("date_from"):
        q = q.filter(model.invoice_date >= f["date_from"])
    if f.get("date_to"):
        q = q.filter(model.invoice_date <= f["date_to"])
    q = _dept_filter(q, model, f)
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

    # Pulled from every stage, not just store_receiving - a brand or
    # category that only appears in the indent file (raised, not yet
    # delivered) still needs to be filterable, or it's invisible until the
    # cycle completes.
    cats = sorted(set(col(FactStoreReceiving, FactStoreReceiving.category))
                  | set(col(FactDispatch, FactDispatch.category))
                  | set(col(FactWarehouseReceiving, FactWarehouseReceiving.category))
                  | set(col(FactReject, FactReject.category)))
    brands = sorted(set(col(FactStoreReceiving, FactStoreReceiving.brand))
                    | set(col(FactDispatch, FactDispatch.brand))
                    | set(col(FactWarehouseReceiving, FactWarehouseReceiving.brand))
                    | set(col(FactIndent, FactIndent.brand)))

    present = {department_of(c) for c in cats}
    depts = [d for d in DEPARTMENT_ORDER if d in present]

    return {
        "departments": depts,
        "categories": cats,
        "category_departments": {c: department_of(c) for c in cats},
        "brands": brands,
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
    dq = _dept_filter(dq, FactDispatch, f)
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


def _stage_rows(db, model, date_col, brand_col, fsn_col, dept_expr_, sums, f):
    """
    Sum a stage table grouped by (brand, fsn), filtered on that table's OWN
    date/brand/department columns - never store_receiving's. This is what
    lets indent and warehouse-inbound show up even on dates or brands that
    haven't reached the store yet.
    """
    q = db.query(brand_col.label("brand"), fsn_col.label("fsn"),
                 *[func.sum(col).label(k) for k, col in sums.items()])
    if f.get("date_from"):
        q = q.filter(date_col >= f["date_from"])
    if f.get("date_to"):
        q = q.filter(date_col <= f["date_to"])
    if f.get("brands"):
        q = q.filter(brand_col.in_(f["brands"]))
    if f.get("departments"):
        q = q.filter(dept_expr_.in_(f["departments"]))
    if hasattr(model, "warehouse_id") and f.get("stores"):
        q = q.filter(model.warehouse_id.in_(f["stores"]))
    rows = q.group_by(brand_col, fsn_col).all()
    return {(r.brand or "Unknown", r.fsn or "Unknown"):
            {k: int(getattr(r, k) or 0) for k in sums} for r in rows}


ZERO_FUNNEL = {"indent_qty": 0, "inbound_received": 0, "store_ordered": 0,
              "picked": 0, "store_received": 0, "damaged": 0}


def po_trace(db, f):
    """
    Exact PO-level match between what you ordered and what arrived, using
    the PO Reference written on both the Indent and Warehouse Inbound
    files - this is the precise version of the vendor gap that stage_funnel
    can only show as a brand+product total over a date range.

    Keyed by (reference, fsn) when a reference is present, so one PO
    covering several product lines still traces each line separately.
    A row with NO reference on either side is grouped under "No reference"
    by (brand, fsn) instead - the old aggregate behaviour - and marked
    matched=False. That row is never silently merged into a referenced
    PO's numbers: a blank key staying separate is what keeps the fallback
    honest rather than misleading.
    """
    def rows(model, date_col, sums):
        q = db.query(model.po_reference.label("ref"), model.brand.label("brand"),
                     model.fsn.label("fsn"),
                     *[func.sum(col).label(k) for k, col in sums.items()])
        if f.get("date_from"):
            q = q.filter(date_col >= f["date_from"])
        if f.get("date_to"):
            q = q.filter(date_col <= f["date_to"])
        if f.get("brands"):
            q = q.filter(model.brand.in_(f["brands"]))
        return q.group_by(model.po_reference, model.brand, model.fsn).all()

    ind = rows(FactIndent, FactIndent.indent_date, {"indent_qty": FactIndent.po_qty})
    wh = rows(FactWarehouseReceiving, FactWarehouseReceiving.date,
             {"inbound_received": FactWarehouseReceiving.received_qty})

    def key_of(r):
        ref = (r.ref or "").strip()
        return (ref, r.fsn or "Unknown") if ref else (None, r.brand or "Unknown", r.fsn or "Unknown")

    merged = {}
    for r in ind:
        k = key_of(r)
        m = merged.setdefault(k, {"indent_qty": 0, "inbound_received": 0, "brand": r.brand})
        m["indent_qty"] += int(r.indent_qty or 0)
        m["brand"] = m["brand"] or r.brand
    for r in wh:
        k = key_of(r)
        m = merged.setdefault(k, {"indent_qty": 0, "inbound_received": 0, "brand": r.brand})
        m["inbound_received"] += int(r.inbound_received or 0)
        m["brand"] = m["brand"] or r.brand

    out = []
    for k, vals in merged.items():
        matched = k[0] is not None
        fsn = k[1] if matched else k[2]
        out.append({
            "po_reference": k[0] if matched else "No reference",
            "matched": matched,
            "brand": vals["brand"] or "Unknown", "fsn": fsn,
            "indent_qty": vals["indent_qty"],
            "inbound_received": vals["inbound_received"],
            "vendor_gap": max(vals["indent_qty"] - vals["inbound_received"], 0),
        })
    # Referenced (precise) rows first, worst gap first within each group.
    return sorted(out, key=lambda r: (not r["matched"], -r["vendor_gap"]))


def stage_funnel(db, f, group_by="brand"):
    """
    The full cycle, independent stage by stage: PO raised -> vendor
    delivered -> picked/batched -> store received.

    Each stage is queried against its OWN table with its OWN date and
    department columns, then merged in Python on (brand, fsn) - never
    filtered through store_receiving. That is what makes this different
    from every other view in the app: a PO can show up here the day it's
    raised, days before any store has received anything, because it was
    never required to join against store_receiving to be visible.

    Three gaps, three different owners - do not blend them:
      vendor_gap       = indent_qty - inbound_received   (the vendor's problem)
      fulfillment_gap  = store_ordered - picked           (your picking, NOT claimable)
      claimable_gap    = picked - store_received - damaged (transit loss, claimable)

    group_by 'brand' rolls FSNs up to one row per brand - useful for "how is
    Amul doing". group_by 'fsn' keeps one row per (brand, fsn) - useful for
    "which specific SKU is the problem".
    """
    indent = _stage_rows(db, FactIndent, FactIndent.indent_date,
                         FactIndent.brand, FactIndent.fsn,
                         fsn_dept_expr(FactIndent.fsn),
                         {"indent_qty": FactIndent.po_qty}, f)
    inbound = _stage_rows(db, FactWarehouseReceiving, FactWarehouseReceiving.date,
                          FactWarehouseReceiving.brand, FactWarehouseReceiving.fsn,
                          dept_expr(FactWarehouseReceiving),
                          {"inbound_received": FactWarehouseReceiving.received_qty}, f)
    batched = _stage_rows(db, FactDispatch, FactDispatch.dispatch_date,
                          FactDispatch.brand, FactDispatch.fsn,
                          dept_expr(FactDispatch),
                          {"store_ordered": FactDispatch.expected_qty,
                           "picked": FactDispatch.picked_qty}, f)
    received = _stage_rows(db, FactStoreReceiving, FactStoreReceiving.invoice_date,
                           FactStoreReceiving.brand, FactStoreReceiving.fsn,
                           dept_expr(FactStoreReceiving),
                           {"store_received": FactStoreReceiving.received_qty,
                            "damaged": FactStoreReceiving.damaged_qty}, f)

    keys = set(indent) | set(inbound) | set(batched) | set(received)
    fine = []
    for k in keys:
        row = dict(ZERO_FUNNEL)
        row.update(indent.get(k, {}))
        row.update(inbound.get(k, {}))
        row.update(batched.get(k, {}))
        row.update(received.get(k, {}))
        row["brand"], row["fsn"] = k
        fine.append(row)

    def with_gaps(row):
        row["vendor_gap"] = max(row["indent_qty"] - row["inbound_received"], 0)
        row["fulfillment_gap"] = max(row["store_ordered"] - row["picked"], 0)
        row["claimable_gap"] = max(row["picked"] - row["store_received"] - row["damaged"], 0)
        return row

    if group_by == "fsn":
        out = [with_gaps(r) for r in fine]
    else:
        agg = {}
        for row in fine:
            a = agg.setdefault(row["brand"], {"brand": row["brand"], **dict(ZERO_FUNNEL)})
            for k in ZERO_FUNNEL:
                a[k] += row[k]
        out = [with_gaps(a) for a in agg.values()]

    return sorted(out, key=lambda r: -(r["indent_qty"] + r["store_ordered"]))


def department_breakdown(db, f):
    """
    Both gaps side by side per department.

    The two gaps are kept apart here for the same reason they're apart at
    headline level: they have different owners. A department can look fine
    on the claimable gap while its picking is the worst in the building, and
    a single blended number would hide that. Dairy and F&V in particular
    fail in different ways - dairy short-picks, F&V goes missing in transit.
    """
    recv = _apply(db.query(
        dept_expr(FactStoreReceiving).label("dept"),
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
        func.count(distinct(FactStoreReceiving.fsn)),
        func.count(distinct(FactStoreReceiving.warehouse_id)),
    ), FactStoreReceiving, f).group_by("dept").all()

    # Picking side comes from the batching file, which is a different table
    # and a different stage - joined here only for display.
    dq = db.query(dept_expr(FactDispatch).label("dept"),
                  func.sum(FactDispatch.expected_qty),
                  func.sum(FactDispatch.picked_qty))
    if f.get("date_from"):
        dq = dq.filter(FactDispatch.dispatch_date >= f["date_from"] - timedelta(days=1))
    if f.get("date_to"):
        dq = dq.filter(FactDispatch.dispatch_date <= f["date_to"])
    dq = _dept_filter(dq, FactDispatch, f)
    if f.get("stores"):
        dq = dq.filter(FactDispatch.warehouse_id.in_(f["stores"]))
    pick = {d: (int(o or 0), int(p or 0)) for d, o, p in dq.group_by("dept").all()}

    out = []
    for dept, exp, rec, dmg, skus, stores in recv:
        exp, rec, dmg = int(exp or 0), int(rec or 0), int(dmg or 0)
        claim = max(exp - rec - dmg, 0)
        ordered, picked = pick.get(dept, (0, 0))
        out.append({
            "department": dept,
            "dispatched": exp, "received": rec, "damaged": dmg,
            "claimable_units": claim,
            "claimable_pct": round(100 * claim / exp, 2) if exp else 0,
            "ordered": ordered, "picked": picked,
            "fulfillment_gap_units": max(ordered - picked, 0),
            "fulfillment_pct": round(100 * picked / ordered, 2) if ordered else 0,
            "skus": int(skus or 0), "stores": int(stores or 0),
        })
    order = {d: i for i, d in enumerate(DEPARTMENT_ORDER)}
    return sorted(out, key=lambda r: order.get(r["department"], 99))


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
        out.append({"category": cat or "Unknown",
                    "department": department_of(cat),
                    "dispatched": exp,
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
    q = _dept_filter(q, FactReject, f)
    if f.get("categories"):
        q = q.filter(FactReject.category.in_(f["categories"]))
    if f.get("reasons"):
        q = q.filter(FactReject.reason.in_(f["reasons"]))
    rows = q.group_by(FactReject.reason, FactReject.category).all()
    return [{"reason": r or "Not recorded", "category": c or "Unknown",
             "department": department_of(c),
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
