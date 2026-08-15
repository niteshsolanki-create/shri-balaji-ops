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
                     FactIndent,
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


def _apply(q, model, f, submitted_only=False):
    """
    submitted_only applies to store-receiving queries that compute a gap.
    It drops GRNs the store hasn't submitted yet, which would otherwise
    read as received=0 and look like a total loss.
    """
    if submitted_only and model is FactStoreReceiving:
        q = _submitted_only(q, model)
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
    return q


# A GRN the store hasn't submitted yet reads as received_qty = 0. That is
# not a loss - it is an absence of a count. Including these rows makes an
# un-counted store look like a total write-off and inflates the claimable
# gap, which is the one number that turns into a claim against Flipkart.
# On 13-Aug a single such store moved the gap from 1.89% to 2.25%.
PENDING_GRN_STATUSES = ("waiting", "pending", "in_progress", "in progress")


def _submitted_only(q, model=FactStoreReceiving):
    """Restrict to GRNs the store has actually completed."""
    return q.filter(or_(model.status.is_(None),
                        ~func.lower(func.trim(model.status)).in_(PENDING_GRN_STATUSES)))


def pending_grns(db, f):
    """
    Rows excluded from the gap maths because the store hasn't submitted the
    GRN. Surfaced rather than hidden: these are chase-the-store items, not
    shrinkage, and they need to be visible or they'll never get closed.
    """
    rows = _apply(db.query(
        FactStoreReceiving.warehouse_id,
        func.count(FactStoreReceiving.id),
        func.sum(FactStoreReceiving.expected_qty),
        func.min(FactStoreReceiving.invoice_date),
        func.max(FactStoreReceiving.invoice_date),
    ), FactStoreReceiving, f).filter(
        func.lower(func.trim(FactStoreReceiving.status)).in_(PENDING_GRN_STATUSES)
    ).group_by(FactStoreReceiving.warehouse_id).all()

    names = {s.warehouse_id: s.warehouse_name for s in db.query(DimStore).all()}
    out = [{"warehouse_id": w, "store": names.get(w, w), "rows": int(n or 0),
            "units_awaiting": int(u or 0),
            "from": str(d1) if d1 else None, "to": str(d2) if d2 else None}
           for w, n, u, d1, d2 in (rows or [])]
    return sorted(out, key=lambda r: -r["units_awaiting"])


def global_search(db, query, limit=12):
    """
    One search across everything a person would actually type into a search
    box: FSN, EAN, product name, brand, store name or ID. Sources DimProduct
    directly rather than any fact table's row set, because EAN only lives on
    the product master - no fact table carries it, so searching FSN/EAN/name
    together is only possible here, not from data already loaded client-side.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return {"products": [], "stores": [], "brands": []}
    like = f"%{q}%"

    products = (db.query(DimProduct)
                .filter(or_(DimProduct.fsn.ilike(like),
                           DimProduct.ean.ilike(like),
                           DimProduct.title.ilike(like),
                           DimProduct.brand.ilike(like)))
                .limit(limit).all())

    stores = (db.query(DimStore)
              .filter(or_(DimStore.warehouse_name.ilike(like),
                         DimStore.warehouse_id.ilike(like)))
              .limit(limit).all())

    brands = sorted({p.brand for p in products if p.brand} |
                    {b for b, in db.query(distinct(FactStoreReceiving.brand))
                     .filter(FactStoreReceiving.brand.ilike(like)).limit(limit).all()})

    return {
        "products": [{"fsn": p.fsn, "ean": p.ean, "title": p.title,
                      "brand": p.brand, "category": p.category,
                      "department": department_of(p.category)} for p in products],
        "stores": [{"warehouse_id": s.warehouse_id, "name": s.warehouse_name}
                  for s in stores],
        "brands": brands[:limit],
    }


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
                  | set(col(FactReject, FactReject.category)))
    brands = sorted(set(col(FactStoreReceiving, FactStoreReceiving.brand))
                    | set(col(FactDispatch, FactDispatch.brand))
                    | set(col(FactIndent, FactIndent.brand)))

    present = {department_of(c) for c in cats}
    depts = [d for d in DEPARTMENT_ORDER if d in present]

    return {
        "departments": depts,
        "categories": cats,
        "category_departments": {c: department_of(c) for c in cats},
        "brands": brands,
        "stores": [{"id": s.warehouse_id, "name": s.warehouse_name} for s in stores],
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
    ), FactStoreReceiving, f, submitted_only=True)
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
    ), FactStoreReceiving, f, submitted_only=True).group_by(FactStoreReceiving.invoice_date) \
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
    ), FactStoreReceiving, f, submitted_only=True).group_by(FactStoreReceiving.warehouse_id).all()

    per_day = _apply(db.query(
        FactStoreReceiving.warehouse_id,
        FactStoreReceiving.invoice_date,
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
    ), FactStoreReceiving, f, submitted_only=True).group_by(FactStoreReceiving.warehouse_id,
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


def _stage_rows(db, model, date_col, brand_col, fsn_col, dept_expr_, sums, f,
                submitted_only=False):
    """
    Sum a stage table grouped by (brand, fsn), filtered on that table's OWN
    date/brand/department columns - never store_receiving's. This is what
    lets indent and warehouse-inbound show up even on dates or brands that
    haven't reached the store yet.
    """
    q = db.query(brand_col.label("brand"), fsn_col.label("fsn"),
                 *[func.sum(col).label(k) for k, col in sums.items()])
    if submitted_only:
        q = _submitted_only(q, model)
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




def stage_funnel(db, f, group_by="brand"):
    """
    The cycle: PO raised -> picked/batched -> store received.

    Vendor delivery is tracked in Indent's "Final Received Qty", so you
    don't need a separate warehouse receiving stage.

    Two gaps, two different owners - do not blend them:
      fulfillment_gap  = store_ordered - picked           (your picking, NOT claimable)
      claimable_gap    = picked - store_received - damaged (transit loss, claimable)

    group_by 'brand' rolls FSNs up to one row per brand - useful for "how is
    Amul doing". group_by 'fsn' keeps one row per (brand, fsn) - useful for
    "which specific SKU is the problem".
    """
    # The PO and its delivery are separate events on separate dates, so they
    # are queried separately. Asking for "what did Amul deliver on the 14th"
    # must look at delivery_date; asking "what did we order on the 12th" must
    # look at indent_date. Merged afterwards on (brand, fsn).
    raised = _stage_rows(db, FactIndent, FactIndent.indent_date,
                         FactIndent.brand, FactIndent.fsn,
                         fsn_dept_expr(FactIndent.fsn),
                         {"indent_qty": FactIndent.po_qty}, f)
    delivered = _stage_rows(db, FactIndent, FactIndent.delivery_date,
                            FactIndent.brand, FactIndent.fsn,
                            fsn_dept_expr(FactIndent.fsn),
                            {"inbound_received": FactIndent.final_received_qty}, f)
    indent = {}
    for k, v in raised.items():
        indent.setdefault(k, {}).update(v)
    for k, v in delivered.items():
        indent.setdefault(k, {}).update(v)
    batched = _stage_rows(db, FactDispatch, FactDispatch.dispatch_date,
                          FactDispatch.brand, FactDispatch.fsn,
                          dept_expr(FactDispatch),
                          {"store_ordered": FactDispatch.expected_qty,
                           "picked": FactDispatch.picked_qty}, f)
    received = _stage_rows(db, FactStoreReceiving, FactStoreReceiving.invoice_date,
                           FactStoreReceiving.brand, FactStoreReceiving.fsn,
                           dept_expr(FactStoreReceiving),
                           {"store_received": FactStoreReceiving.received_qty,
                            "damaged": FactStoreReceiving.damaged_qty}, f,
                           submitted_only=True)

    keys = set(indent) | set(batched) | set(received)

    # FSN alone is unreadable - carry the product name through so the
    # by-product view names what it's talking about. Taken from whichever
    # stage has it; batching and GRN both carry a title.
    titles = {}
    for src, model, fsn_col, desc_col, cat_col in (
        ("recv", FactStoreReceiving, FactStoreReceiving.fsn,
         FactStoreReceiving.description, FactStoreReceiving.category),
        ("disp", FactDispatch, FactDispatch.fsn,
         FactDispatch.product_title, FactDispatch.category),
    ):
        try:
            for fsn, desc, cat in db.query(
                    fsn_col, func.max(desc_col), func.max(cat_col)
            ).group_by(fsn_col).all():
                if fsn and fsn not in titles:
                    titles[fsn] = (desc or "", cat or "")
        except Exception:
            # A missing column on one stage must not take the whole view down.
            pass

    fine = []
    for k in keys:
        row = dict(ZERO_FUNNEL)
        row.update(indent.get(k, {}))
        row.update(batched.get(k, {}))
        row.update(received.get(k, {}))
        row["brand"], row["fsn"] = k
        # A PO with no delivery row in this window hasn't been delivered yet
        # (or was delivered outside the dates). Either way it is pending, not
        # short - so the gap stays unknown rather than showing the full PO
        # quantity as a vendor failure.
        row["has_delivery"] = k in delivered
        desc, cat = titles.get(row["fsn"], ("", ""))
        row["description"], row["category"] = desc, cat
        fine.append(row)

    def with_gaps(row):
        # A gap is only meaningful when BOTH sides of it have data. With no
        # batching uploaded, picked is 0 and max(0 - received, 0) floors at
        # zero - which renders identically to a clean day. None means "no
        # data to compare", and the UI shows it as a dash, not a zero.
        row["vendor_gap"] = (max(row["indent_qty"] - row["inbound_received"], 0)
                             if row["indent_qty"] and row.get("has_delivery") else None)
        row["fulfillment_gap"] = (max(row["store_ordered"] - row["picked"], 0)
                                  if row["store_ordered"] else None)
        row["claimable_gap"] = (max(row["picked"] - row["store_received"] - row["damaged"], 0)
                                if row["picked"] else None)
        return row

    if group_by == "fsn":
        out = [with_gaps(r) for r in fine]
    else:
        agg = {}
        for row in fine:
            a = agg.setdefault(row["brand"], {"brand": row["brand"],
                                              "has_delivery": False,
                                              **dict(ZERO_FUNNEL)})
            for k in ZERO_FUNNEL:
                a[k] += row[k]
            a["has_delivery"] = a["has_delivery"] or row.get("has_delivery", False)
        out = [with_gaps(a) for a in agg.values()]

    return sorted(out, key=lambda r: -(r["indent_qty"] + r["store_ordered"]
                                       + r["store_received"]))


def vendor_reliability(db, f):
    """
    Vendor performance on BOTH axes: did they send the full quantity, and
    did they send it when they said they would.

    These fail independently and the distinction is operational. A vendor at
    100% fill but chronically a day late empties your shelves exactly like
    one who short-ships - but the fix is different: chase the schedule, not
    the quantity. Quantity alone can't tell them apart.

    Only rows with BOTH an expected and an actual delivery date count toward
    lateness; a PO that hasn't arrived yet isn't late, it's open.
    """
    q = db.query(FactIndent.brand,
                 FactIndent.expected_delivery_date,
                 FactIndent.delivery_date,
                 FactIndent.po_qty,
                 FactIndent.final_received_qty)
    if f.get("date_from"):
        q = q.filter(FactIndent.indent_date >= f["date_from"])
    if f.get("date_to"):
        q = q.filter(FactIndent.indent_date <= f["date_to"])
    if f.get("brands"):
        q = q.filter(FactIndent.brand.in_(f["brands"]))
    if f.get("departments"):
        q = q.filter(fsn_dept_expr(FactIndent.fsn).in_(f["departments"]))

    agg = {}
    for brand, exp_d, act_d, po, recv in q.all():
        b = brand or "Unknown"
        a = agg.setdefault(b, {"brand": b, "po_lines": 0, "ordered": 0,
                               "delivered": 0, "on_time": 0, "late": 0,
                               "late_days": 0, "open": 0, "worst_late": 0})
        a["po_lines"] += 1
        a["ordered"] += int(po or 0)
        a["delivered"] += int(recv or 0)
        if act_d is None:
            a["open"] += 1
        elif exp_d is not None:
            days = (act_d - exp_d).days
            if days > 0:
                a["late"] += 1
                a["late_days"] += days
                a["worst_late"] = max(a["worst_late"], days)
            else:
                a["on_time"] += 1

    out = []
    for a in agg.values():
        judged = a["on_time"] + a["late"]
        a["on_time_pct"] = round(100 * a["on_time"] / judged, 1) if judged else None
        a["avg_days_late"] = round(a["late_days"] / a["late"], 1) if a["late"] else 0
        a["fill_pct"] = (round(100 * a["delivered"] / a["ordered"], 1)
                         if a["ordered"] else None)
        a["short_units"] = max(a["ordered"] - a["delivered"], 0)
        out.append(a)
    # Worst fill first; brands with nothing judged yet sort last.
    return sorted(out, key=lambda r: (r["fill_pct"] is None,
                                      r["fill_pct"] if r["fill_pct"] is not None else 999))


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
    ), FactStoreReceiving, f, submitted_only=True).group_by("dept").all()

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
    ), FactStoreReceiving, f, submitted_only=True).group_by(FactStoreReceiving.category).all()

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


def product_detail(db, f, limit=2000):
    """
    Per-product view across batching and GRN.

    Three deliberate choices:
    - Products with no gap are KEPT. Dropping them meant you could only ever
      see problems, never confirm that the rest of the catalogue moved
      cleanly, and a product that stopped appearing was indistinguishable
      from one that stopped being ordered.
    - "Picked" comes from the batching file, not the GRN's expected qty.
      Those are different numbers: expected is what the store was told to
      expect, picked is what your team actually put on the vehicle.
    - The limit is a guard against a runaway response, not a display choice.
      It sits well above a normal day's catalogue (~400 SKUs), and when it
      does bite, the caller is told - a silently truncated list looks
      identical to a complete one.
    """
    rows = _apply(db.query(
        FactStoreReceiving.fsn,
        func.max(FactStoreReceiving.description),
        func.max(FactStoreReceiving.category),
        func.count(distinct(FactStoreReceiving.warehouse_id)),
        func.sum(FactStoreReceiving.expected_qty),
        func.sum(FactStoreReceiving.received_qty),
        func.sum(FactStoreReceiving.damaged_qty),
    ), FactStoreReceiving, f, submitted_only=True).group_by(FactStoreReceiving.fsn).all()

    # Picking side lives in a different table on a different date column.
    dq = db.query(FactDispatch.fsn,
                  func.sum(FactDispatch.expected_qty),
                  func.sum(FactDispatch.picked_qty))
    if f.get("date_from"):
        dq = dq.filter(FactDispatch.dispatch_date >= f["date_from"] - timedelta(days=1))
    if f.get("date_to"):
        dq = dq.filter(FactDispatch.dispatch_date <= f["date_to"])
    dq = _dept_filter(dq, FactDispatch, f)
    if f.get("categories"):
        dq = dq.filter(FactDispatch.category.in_(f["categories"]))
    if f.get("brands"):
        dq = dq.filter(FactDispatch.brand.in_(f["brands"]))
    if f.get("stores"):
        dq = dq.filter(FactDispatch.warehouse_id.in_(f["stores"]))
    pick = {fsn: (int(o or 0), int(p or 0))
            for fsn, o, p in dq.group_by(FactDispatch.fsn).all()}

    out = []
    for fsn, desc, cat, nstores, exp, rec, dmg in rows:
        exp, rec, dmg = int(exp or 0), int(rec or 0), int(dmg or 0)
        claim = max(exp - rec - dmg, 0)
        ordered, picked = pick.get(fsn, (0, 0))
        out.append({"fsn": fsn, "description": desc, "category": cat,
                    "department": department_of(cat),
                    "stores_affected": int(nstores),
                    "ordered": ordered, "picked": picked,
                    "fulfillment_gap": max(ordered - picked, 0),
                    "has_batching": fsn in pick,
                    "dispatched": exp,
                    "received": rec, "damaged": dmg, "claimable_units": claim,
                    "gap_pct": round(100 * claim / exp, 2) if exp else 0})
    # Sorted by claimable units so the problems surface first, then by volume
    # so a clean high-volume SKU ranks above a clean one-unit SKU rather than
    # landing in arbitrary order.
    out.sort(key=lambda r: (-r["claimable_units"], -r["dispatched"]))
    if len(out) > limit:
        out = out[:limit]
        out.append({"fsn": "", "description": f"… list truncated at {limit} products",
                    "category": None, "department": None, "stores_affected": 0,
                    "ordered": 0, "picked": 0, "fulfillment_gap": None,
                    "has_batching": False, "dispatched": 0, "received": 0,
                    "damaged": 0, "claimable_units": 0, "gap_pct": 0,
                    "truncated": True})
    return out


def swap_candidates(db, f):
    """
    Excess at one store alongside shortage at another, same FSN, same day.

    Deliberately does NOT classify what happened - a magnitude match alone
    can be misleading. On 11-Aug, F&V showed 143 units short spread over 19
    stores against 8 units excess elsewhere: that shape is a systemic
    under-count, not a crate swap, and no amount of matching totals changes
    that. 'spread' is left as the signal for the operator to read: a
    shortage concentrated at a couple of stores plausibly IS a misdelivery
    worth a phone call; one spread thin across many stores usually isn't.
    """
    rows = _apply(db.query(
        FactStoreReceiving.invoice_date, FactStoreReceiving.fsn,
        FactStoreReceiving.warehouse_id, FactStoreReceiving.description,
        FactStoreReceiving.expected_qty, FactStoreReceiving.received_qty,
        FactStoreReceiving.excess_qty,
    ), FactStoreReceiving, f, submitted_only=True).all()

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
        out.append({
            "date": str(d), "fsn": fsn, "description": v["desc"],
            "total_short": ts, "total_excess": te,
            "stores_short": len(v["short"]), "stores_excess": len(v["excess"]),
            "excess_stores": [{"store": w, "qty": q}
                              for w, q in sorted(v["excess"], key=lambda x: -x[1])],
            "short_stores": [{"store": w, "qty": q}
                             for w, q in sorted(v["short"], key=lambda x: -x[1])][:12],
            "spread": "concentrated" if len(v["short"]) <= 3 else "spread",
        })
    return sorted(out, key=lambda r: -r["total_excess"])


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
