# Shri Balaji Ops

Daily supply-chain analytics for the Flipkart Minutes 3PL operation —
upload the day's files, see where every packet is lost between order and shelf.

Built to run hosted from day one, on an account **you** own.

---

## What it does

- **One upload a day.** Drop batching, store-receiving, warehouse-receiving,
  rejects, route and indent files. Type is detected from the columns; you don't
  pick anything from a dropdown.
- **Every number recalculates under filters** — category, brand, store, vehicle,
  date range, reason. Filter to "Milk only" or "Gur_111, last 7 days" and the
  KPIs, funnel, rankings and tables all follow.
- **The packet-journey funnel** separates the two losses that were being blended
  before: units that never left the warehouse (fulfilment, *not* claimable) vs.
  units dispatched but never received (claimable GRN gap).
- **Store ranking flags repeat offenders**, not single bad days.
- **Route & swaps** cross-references excess at one store against shortage at
  another on the same vehicle — evidence for you to judge, not an automated verdict.
- **Data-quality panel** surfaces broken inputs (missing product-master entries,
  unattributed rejects, Excel-corrupted quantities) so a bad file is never
  mistaken for a bad store.
- **Saved views** remember filters *and* which panels are shown.
- **Email digests** fire only on repeated threshold breaches.
- **Admin vs viewer** roles — staff can see everything, only you can upload.

---

## Deploy to Railway (recommended, ~10 minutes)

You'll need a free [GitHub](https://github.com) account and a free
[Railway](https://railway.app) account.

### 1. Put the code on GitHub
- Create a new **private** repository on GitHub (e.g. `shri-balaji-ops`).
- Upload this whole folder to it (drag-and-drop works on the GitHub web UI:
  *Add file → Upload files*), or with git:
  ```bash
  git init && git add . && git commit -m "initial"
  git branch -M main
  git remote add origin https://github.com/<you>/shri-balaji-ops.git
  git push -u origin main
  ```

### 2. Create the project on Railway
- Railway → **New Project → Deploy from GitHub repo** → pick your repo.
- Railway reads the `Dockerfile` and starts building automatically.

### 3. Add a database
- In the project: **New → Database → PostgreSQL.**
- Railway sets a `DATABASE_URL` variable automatically. The app reads it and
  creates all tables on first boot.

### 4. Set the login and secret
In your service's **Variables** tab, add:

| Variable | Value |
|---|---|
| `ADMIN_EMAIL` | your email (this becomes your login) |
| `ADMIN_PASSWORD` | a strong password |
| `SECRET_KEY` | any long random string |

(Optional, import tuning — the defaults are fine for most boxes:)

| Variable | Default | What it does |
|---|---|---|
| `INGEST_CHUNK_ROWS` | `50000` | Rows held in memory at once during import. **Import memory scales with this number, not with file size** — a 200MB file and a 2GB file use the same RAM. Lower it to ~20000 on a 512MB container; raise it to 100000+ on a 4GB box for faster imports. |
| `MAX_UPLOAD_MB` | `1024` | Upload size ceiling. A file over this is rejected with a clear message instead of being accepted and then failing. |

(Optional, for email digests — Gmail needs an *app password*, not your normal one:)

| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your gmail |
| `SMTP_PASSWORD` | 16-char app password |
| `SMTP_FROM` | your gmail |

### 5. Open it
- Railway → **Settings → Networking → Generate Domain.**
- Visit the URL, sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Done.

---

## Deploy to Render (alternative)

`render.yaml` is already included. On [Render](https://render.com):
**New → Blueprint → connect your repo.** It provisions the web service **and**
a free Postgres database from that file. Set `ADMIN_EMAIL` and `ADMIN_PASSWORD`
when prompted.

> Render's free tier sleeps after inactivity and the free Postgres expires after
> 30 days — fine for trialling, but Railway is the steadier choice for daily use.

---

## First run

1. Sign in.
2. Go to **Upload**, drop **Store Master** and **Product Master** first (so
   brands/categories resolve), then the daily files. Re-uploading a date safely
   replaces it — overlapping historical exports won't double-count.
3. The dashboard refreshes automatically.
4. Add your team under **Team** (viewers can't upload).

---

## Running locally (optional)

```bash
pip install -r requirements.txt
export ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=test SECRET_KEY=dev
uvicorn app.main:app --reload
# open http://localhost:8000  (uses a local SQLite file, no Postgres needed)
```

---

## Notes carried over from the data audit

- **Store Receiving is a national export.** ~95% of its rows are other cities'
  darkstores; the app filters to your 48 stores using Store Master. Keep Store
  Master current or real stores get dropped.
- **F&V has no product master.** Category is inferred from the FSN prefix so F&V
  is never dropped, but brand/MRP is unavailable for those SKUs until you add
  them to the master.
- **Rejects need store attribution.** Until the reject file carries a store
  column, damage can't be pinned to a location. The app flags this itself.
- **Excel corrupts reject quantities** by auto-formatting them as dates. The app
  recovers them, but format that column as *Number* at source to stop it.
