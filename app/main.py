"""Shri Balaji Ops - supply-chain analytics service."""
import os
import io
import json
import zipfile
import tempfile
import hashlib
import secrets
import smtplib
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .models import (SessionLocal, init_db, User, DashboardTemplate, UploadLog,
                     AlertRule, AlertLog, DimStore)
from .ingest import ingest_path, ingest_file
from .upload_templates import TEMPLATES, ORDER, template_csv, template_index
from . import analytics as A

BASE = Path(__file__).parent
app = FastAPI(title="Shri Balaji Ops")
app.add_middleware(SessionMiddleware,
                   secret_key=os.getenv("SECRET_KEY", secrets.token_hex(32)),
                   max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ----------------------------- auth ---------------------------------------
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return f"{salt}${h.hex()}"


def verify_pw(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db=Depends(get_db)):
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(401, "Not signed in")
    u = db.query(User).get(uid)
    if not u:
        raise HTTPException(401, "Not signed in")
    return u


def require_admin(user: User = Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Uploading is restricted to admins")
    return user


@app.on_event("startup")
def startup():
    init_db()
    db = SessionLocal()
    try:
        if not db.query(User).first():
            email = os.getenv("ADMIN_EMAIL", "admin@shribalaji.local")
            pw = os.getenv("ADMIN_PASSWORD", "changeme")
            db.add(User(email=email, name="Admin", password_hash=hash_pw(pw),
                        role="admin"))
        if not db.query(AlertRule).first():
            db.add(AlertRule(
                name="Store repeatedly above 3% claimable gap",
                scope="store", threshold=3.0, consecutive_days=3, min_volume=200))
            db.add(AlertRule(
                name="Category above 5% claimable gap",
                scope="category", threshold=5.0, consecutive_days=2, min_volume=500))
        db.commit()
    finally:
        db.close()


# ----------------------------- pages --------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          db=Depends(get_db)):
    u = db.query(User).filter(User.email == email.strip().lower()).first()
    if not u or not verify_pw(password, u.password_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Wrong email or password."},
            status_code=401)
    request.session["uid"] = u.id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db=Depends(get_db)):
    uid = request.session.get("uid")
    if not uid:
        return RedirectResponse("/login", status_code=303)
    u = db.query(User).get(uid)
    if not u:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("app.html", {
        "request": request,
        "user": {"name": u.name or u.email, "email": u.email, "role": u.role}})


# ----------------------------- filters ------------------------------------
def parse_filters(payload: dict):
    def lst(k):
        v = payload.get(k) or []
        return [x for x in v if x] if isinstance(v, list) else ([v] if v else [])

    def dt(k):
        v = payload.get(k)
        if not v:
            return None
        try:
            return datetime.strptime(v, "%Y-%m-%d").date()
        except ValueError:
            return None

    return {"date_from": dt("date_from"), "date_to": dt("date_to"),
            "departments": lst("departments"),
            "categories": lst("categories"), "brands": lst("brands"),
            "stores": lst("stores"), "vehicles": lst("vehicles"),
            "reasons": lst("reasons")}


@app.get("/api/options")
def api_options(user=Depends(current_user), db=Depends(get_db)):
    return A.filter_options(db)


@app.post("/api/funnel")
def api_funnel(payload: dict, user=Depends(current_user), db=Depends(get_db)):
    f = parse_filters(payload)
    group_by = "fsn" if payload.get("group_by") == "fsn" else "brand"
    return {"rows": A.stage_funnel(db, f, group_by=group_by), "group_by": group_by}


@app.post("/api/dashboard")
def api_dashboard(payload: dict, user=Depends(current_user), db=Depends(get_db)):
    f = parse_filters(payload)
    want = set(payload.get("widgets") or
               ["headline", "trend", "stores", "departments", "categories",
                "products", "swaps", "rejects", "quality"])
    out = {"filters_applied": {k: (str(v) if isinstance(v, date) else v)
                               for k, v in f.items() if v}}
    if "headline" in want:
        out["headline"] = A.headline(db, f)
    if "trend" in want:
        out["trend"] = A.daily_trend(db, f)
    if "stores" in want:
        out["stores"] = A.store_ranking(db, f)
    if "departments" in want:
        out["departments"] = A.department_breakdown(db, f)
    if "categories" in want:
        out["categories"] = A.category_breakdown(db, f)
    if "products" in want:
        out["products"] = A.product_detail(db, f)
    if "swaps" in want:
        out["swaps"] = A.swap_candidates(db, f)
    if "rejects" in want:
        out["rejects"] = A.reject_breakdown(db, f)
    if "quality" in want:
        out["quality"] = A.data_quality(db, f)
        out["pending_grns"] = A.pending_grns(db, f)
    return out


# ----------------------------- templates ----------------------------------
@app.get("/api/templates")
async def api_templates(user=Depends(current_user)):
    """Metadata for the template list in the Upload tab."""
    return {"templates": template_index()}


@app.get("/api/template/{key}")
async def api_template(key: str, user=Depends(current_user)):
    """Download one blank template, header row plus a filled example row."""
    if key not in TEMPLATES:
        raise HTTPException(404, "No such template")
    body = template_csv(key)
    return Response(
        content=body, media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{TEMPLATES[key]["filename"]}"'})


@app.get("/api/templates.zip")
async def api_templates_zip(user=Depends(current_user)):
    """
    All templates in one zip, numbered in the order stock physically moves,
    so the filenames themselves document the cycle.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, key in enumerate(ORDER, start=1):
            z.writestr(f"{i}_{TEMPLATES[key]['filename']}", template_csv(key))
        z.writestr("READ_ME_FIRST.txt", _template_readme())
    buf.seek(0)
    return Response(
        content=buf.read(), media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="shri-balaji-upload-templates.zip"'})


def _template_readme():
    lines = [
        "SHRI BALAJI OPS - UPLOAD TEMPLATES",
        "=" * 60, "",
        "Fill these, don't rename the column headers. The app detects the",
        "file type from the headers, so changing them breaks the import.",
        "",
        "THE TWO RULES THAT MAKE LINKING WORK",
        "-" * 60,
        "1. FSN is the join key. It connects a PO to an inbound delivery to",
        "   a pick to a store GRN. A row without FSN loads, but cannot be",
        "   traced through the cycle.",
        "2. Store ID must be the facility code (gur_106_wh_hl_01), never the",
        "   store's display name. Names vary between exports; codes don't.",
        "",
        "ORDER OF UPLOAD",
        "-" * 60,
        "Upload the two masters first (1 and 2). They are what the fact",
        "files resolve store names and categories against. After that the",
        "order doesn't matter.",
        "",
        "THE CYCLE",
        "-" * 60,
    ]
    for i, key in enumerate(ORDER, start=1):
        t = TEMPLATES[key]
        lines.append(f"{i}. {t['label']}  [{t['stage']}]")
        lines.append(f"   {t['why']}")
        for nt in t["notes"]:
            lines.append(f"   - {nt}")
        lines.append("")
    lines += [
        "EXCEL WARNING",
        "-" * 60,
        "Format every quantity column as Number before typing into it.",
        "Excel silently converts 5 into 05-Jan-1900 in a date-formatted",
        "cell. The app recovers these, but it's better not to create them.",
        "",
        "The example row in each file is there to show the format. Delete",
        "it before uploading real data.",
    ]
    return "\r\n".join(lines) + "\r\n"


# ----------------------------- upload -------------------------------------
SPOOL_CHUNK = 1024 * 1024          # 1MB at a time - never hold the file in RAM
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1024"))


async def spool_to_disk(upload: UploadFile) -> tuple[str, int]:
    """
    Write an incoming upload straight to a temp file in 1MB pieces.

    The previous version did `content = await f.read()`, which materialised
    the whole file in memory before pandas had even seen it - so a 200MB
    file cost 200MB of RAM before any parsing work began. Streaming to disk
    makes upload memory constant regardless of file size, and gives ingest
    a path it can then read in chunks.
    """
    suffix = Path(upload.filename or "upload").suffix or ".dat"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="upload_")
    size = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(SPOOL_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError(
                        f"File is larger than the {MAX_UPLOAD_MB}MB limit. "
                        f"Raise MAX_UPLOAD_MB or split the export by day.")
                out.write(chunk)
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path, size


@app.post("/api/upload")
async def api_upload(files: list[UploadFile] = File(...),
                     file_type: str = Form(None),
                     user=Depends(require_admin)):
    """
    Import one or more files. Each file is isolated: a failure on one is
    reported against that file and the rest still load. The response ALWAYS
    carries a `results` list, even on failure, so the frontend never has to
    guess whether it can be read.
    """
    results = []
    # Masters first so category/brand resolution is correct for the fact files
    ordered = sorted(files, key=lambda f: 0 if "master" in (f.filename or "").lower() else 1)
    for f in ordered:
        path = None
        try:
            path, size = await spool_to_disk(f)
            r = ingest_path(f.filename, path, user.email,
                            forced_type=file_type or None)
            r["size_mb"] = round(size / (1024 * 1024), 1)
            results.append(r)
        except Exception as e:
            results.append({"ok": False, "filename": f.filename,
                            "error": str(e), "type": file_type or "unknown",
                            "rows_in": 0, "rows_loaded": 0, "rows_dropped": 0,
                            "dates": [], "notes": []})
        finally:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return {"results": results}


@app.get("/api/uploads")
def api_uploads(user=Depends(current_user), db=Depends(get_db)):
    rows = db.query(UploadLog).order_by(UploadLog.uploaded_at.desc()).limit(60).all()
    return [{"at": r.uploaded_at.strftime("%d %b %H:%M") if r.uploaded_at else "",
             "by": r.uploaded_by, "filename": r.filename, "type": r.file_type,
             "dates": r.dates_covered, "rows_in": r.rows_in_source,
             "loaded": r.rows_loaded, "dropped": r.rows_dropped,
             "notes": r.notes, "status": r.status} for r in rows]


# ----------------------------- templates ----------------------------------
@app.get("/api/templates")
def list_templates(user=Depends(current_user), db=Depends(get_db)):
    rows = db.query(DashboardTemplate).filter(
        DashboardTemplate.user_id == user.id).order_by(DashboardTemplate.name).all()
    return [{"id": r.id, "name": r.name, "config": json.loads(r.config)} for r in rows]


@app.post("/api/templates")
def save_template(payload: dict, user=Depends(current_user), db=Depends(get_db)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the view a name")
    cfg = json.dumps({"filters": payload.get("filters", {}),
                      "widgets": payload.get("widgets", [])})
    row = db.query(DashboardTemplate).filter(
        DashboardTemplate.user_id == user.id,
        DashboardTemplate.name == name).first()
    if row:
        row.config = cfg
    else:
        db.add(DashboardTemplate(user_id=user.id, name=name, config=cfg))
    db.commit()
    return {"ok": True}


@app.delete("/api/templates/{tid}")
def delete_template(tid: int, user=Depends(current_user), db=Depends(get_db)):
    db.query(DashboardTemplate).filter(
        DashboardTemplate.id == tid,
        DashboardTemplate.user_id == user.id).delete()
    db.commit()
    return {"ok": True}


# ----------------------------- users --------------------------------------
@app.get("/api/users")
def list_users(user=Depends(require_admin), db=Depends(get_db)):
    return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role,
             "alerts": u.alerts_enabled} for u in db.query(User).all()]


@app.post("/api/users")
def add_user(payload: dict, user=Depends(require_admin), db=Depends(get_db)):
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email required")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "That email already has an account")
    db.add(User(email=email, name=payload.get("name"),
                password_hash=hash_pw(payload.get("password") or "changeme"),
                role=payload.get("role", "viewer")))
    db.commit()
    return {"ok": True}


@app.delete("/api/users/{uid}")
def del_user(uid: int, user=Depends(require_admin), db=Depends(get_db)):
    if uid == user.id:
        raise HTTPException(400, "You can't remove your own account")
    db.query(User).filter(User.id == uid).delete()
    db.commit()
    return {"ok": True}


# ----------------------------- alerts -------------------------------------
def build_alert_digest(db):
    """Only fires on repeated breaches - a single bad day is noise."""
    end = db.query(A.func.max(A.FactStoreReceiving.invoice_date)).scalar()
    if not end:
        return None
    start = end - timedelta(days=6)
    f = {"date_from": start, "date_to": end}

    lines, subject_bits = [], []
    for rule in db.query(AlertRule).filter(AlertRule.active.is_(True)).all():
        if rule.scope == "store":
            rows = A.store_ranking(db, f, flag_threshold=rule.threshold,
                                   min_volume=rule.min_volume)
            hits = [r for r in rows if r["days_flagged"] >= rule.consecutive_days]
            if hits:
                subject_bits.append(f"{len(hits)} store(s)")
                lines.append(f"\n{rule.name}")
                for r in hits[:10]:
                    lines.append(
                        f"  - {r['name']}: {r['gap_pct']}% claimable gap, "
                        f"flagged {r['days_flagged']} of {r['days_total']} days, "
                        f"{r['claimable_units']:,} units unaccounted")
        elif rule.scope == "category":
            for c in A.category_breakdown(db, f):
                if c["dispatched"] >= rule.min_volume and c["gap_pct"] >= rule.threshold:
                    lines.append(f"\n{rule.name}")
                    lines.append(f"  - {c['category']}: {c['gap_pct']}% "
                                 f"({c['claimable_units']:,} units)")
                    subject_bits.append(c["category"])
    if not lines:
        return None

    head = A.headline(db, f)
    body = (f"Shri Balaji - 7-day ops digest ({start} to {end})\n"
            f"{'=' * 58}\n"
            f"Dispatched {head['dispatched']:,}  |  Received {head['received']:,}\n"
            f"Claimable gap {head['claimable_pct']}% "
            f"({head['claimable_units']:,} units)\n"
            + "\n".join(lines) +
            "\n\nOpen the dashboard to filter by store, category or route.\n")
    return {"subject": f"Ops digest: {', '.join(subject_bits[:3])}", "body": body}


def send_email(subject, body, recipients):
    host = os.getenv("SMTP_HOST")
    if not host or not recipients:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "ops@shribalaji"))
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 587))) as s:
        s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg)
    return True


@app.post("/api/alerts/run")
def run_alerts(user=Depends(require_admin), db=Depends(get_db)):
    digest = build_alert_digest(db)
    if not digest:
        return {"sent": False, "reason": "Nothing crossed a threshold repeatedly."}
    rec = [u.email for u in db.query(User).filter(User.alerts_enabled.is_(True)).all()]
    delivered = False
    try:
        delivered = send_email(digest["subject"], digest["body"], rec)
    except Exception as e:
        digest["body"] += f"\n[delivery error: {e}]"
    db.add(AlertLog(rule_name="digest", subject=digest["subject"],
                    body=digest["body"], recipients=", ".join(rec),
                    delivered=delivered))
    db.commit()
    return {"sent": delivered, "preview": digest, "recipients": rec,
            "note": None if delivered else
            "SMTP is not configured yet - this is a preview of what would be sent."}


@app.get("/api/alerts/preview")
def preview_alerts(user=Depends(current_user), db=Depends(get_db)):
    d = build_alert_digest(db)
    return d or {"subject": None, "body": "Nothing currently meets an alert rule."}


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}
