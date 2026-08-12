"""Database schema for Shri Balaji supply-chain analytics."""
import os
from datetime import datetime
from sqlalchemy import (create_engine, Column, Integer, String, Float, Date,
                        DateTime, Text, Boolean, Index)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./shri_balaji.db")
# Railway/Render hand out postgres:// but SQLAlchemy 2.x wants postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


# --------------------------------------------------------------------------
# Category resolution
#
# PRODUCT_MASTER only covers ~237 packaged SKUs. On 11-Aug, 243 FSNs
# (56% of the day's volume) had no master entry at all - all of them F&V.
# Flipkart FSNs are prefixed by vertical, so the prefix is a reliable
# fallback and means F&V is never silently dropped from analysis again.
# --------------------------------------------------------------------------
FSN_PREFIX_CATEGORY = {
    "MLK": "Milk", "DRC": "Milk",
    "CUY": "Curd & Yogurt",
    "BTM": "Buttermilk & Lassi", "LSI": "Buttermilk & Lassi",
    "PTF": "Paneer & Tofu",
    "BAB": "Breads", "CEP": "Bakery",
    "EEG": "Eggs",
    "VEG": "Fruits & Vegetables", "FRT": "Fruits & Vegetables",
    "FVB": "Fruits & Vegetables",
    "FFW": "Flowers",
    "SAM": "Sweets & Mithai",
    "RYM": "Ready Mixes", "RMD": "Ready Meals", "SAK": "Ready Meals",
    "CHT": "Pickles & Chutneys",
    "PSG": "Plants & Garden", "PAE": "Plants & Garden",
    "MEA": "Meat", "SFD": "Seafood",
    "PLS": "Staples", "SUG": "Staples", "RIC": "Staples", "NDF": "Staples",
    "SCM": "Staples", "GNM": "Staples", "EDS": "Staples", "FLR": "Staples",
}

# PRODUCT_MASTER ships categories in mixed case ("Milk"/"milk",
# "CurdYogurt"/"curdYogurt") which silently splits groupings.
CATEGORY_CANON = {
    "milk": "Milk", "curdyogurt": "Curd & Yogurt", "eggs": "Eggs",
    "breads": "Breads", "paneertofu": "Paneer & Tofu",
    "readymixes": "Ready Mixes", "buttermilkandlassi": "Buttermilk & Lassi",
    "readymeals": "Ready Meals", "cakepastry": "Cake & Pastry",
    "pickleschutneys": "Pickles & Chutneys",
    "buttermargarine": "Butter & Margarine", "sweetsmithai": "Sweets & Mithai",
}


def canon_category(raw):
    if raw is None:
        return None
    k = str(raw).strip().lower().replace(" ", "").replace("&", "and")
    return CATEGORY_CANON.get(k, str(raw).strip())


def category_from_fsn(fsn):
    if not fsn:
        return "Unknown"
    return FSN_PREFIX_CATEGORY.get(str(fsn)[:3].upper(), "Unknown")


# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(120))
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="viewer")  # admin | viewer
    alerts_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DimStore(Base):
    __tablename__ = "dim_store"
    warehouse_id = Column(String(40), primary_key=True)
    warehouse_name = Column(String(160))
    city_code = Column(String(10))
    wh_serial_no = Column(Integer)


class DimProduct(Base):
    __tablename__ = "dim_product"
    fsn = Column(String(40), primary_key=True)
    ean = Column(String(20))
    brand = Column(String(80))
    category = Column(String(60))
    title = Column(String(255))
    mrp = Column(Float)
    price = Column(Float)


class FactDispatch(Base):
    """Batching file - what the warehouse picked and sent out."""
    __tablename__ = "fact_dispatch"
    id = Column(Integer, primary_key=True)
    dispatch_date = Column(Date, index=True)
    cutoff_datetime = Column(String(30))
    po_number = Column(String(60))
    fsn = Column(String(40), index=True)
    product_title = Column(String(255))
    warehouse_id = Column(String(40), index=True)
    category = Column(String(60), index=True)
    brand = Column(String(80))
    expected_qty = Column(Integer, default=0)
    picked_qty = Column(Integer, default=0)
    shortage_qty = Column(Integer, default=0)
    pending_qty = Column(Integer, default=0)
    status = Column(String(20))
    picked_by = Column(String(120))
    picked_at = Column(String(30))


class FactStoreReceiving(Base):
    """Store GRN - what the darkstore acknowledged receiving."""
    __tablename__ = "fact_store_receiving"
    id = Column(Integer, primary_key=True)
    invoice_date = Column(Date, index=True)
    warehouse_id = Column(String(40), index=True)
    invoice_id = Column(String(60))
    fsn = Column(String(40), index=True)
    description = Column(String(255))
    category = Column(String(60), index=True)
    brand = Column(String(80), index=True)
    expected_qty = Column(Integer, default=0)
    received_qty = Column(Integer, default=0)
    damaged_qty = Column(Integer, default=0)
    scanning_issue_qty = Column(Integer, default=0)
    excess_qty = Column(Integer, default=0)
    returned_qty = Column(Integer, default=0)
    swapped_qty = Column(Integer, default=0)  # optional; often blank
    status = Column(String(20))
    uploaded_at = Column(String(30))


Index("ix_sr_date_wh", FactStoreReceiving.invoice_date, FactStoreReceiving.warehouse_id)


class FactWarehouseReceiving(Base):
    """What the warehouse itself received from suppliers."""
    __tablename__ = "fact_wh_receiving"
    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True)
    ean = Column(String(20))
    fsn = Column(String(40), index=True)
    product = Column(String(255))
    brand = Column(String(80))
    category = Column(String(60), index=True)
    po_qty = Column(Integer, default=0)
    received_qty = Column(Integer, default=0)
    expiry_date = Column(Date)
    short_qty = Column(Integer, default=0)


class FactReject(Base):
    __tablename__ = "fact_rejects"
    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True)
    ean = Column(String(20))
    fsn = Column(String(40), index=True)
    product = Column(String(255))
    brand = Column(String(80))
    category = Column(String(60), index=True)
    qty = Column(Integer, default=0)
    qty_was_corrupted = Column(Boolean, default=False)
    reason = Column(String(60), index=True)
    expiry = Column(Date)
    warehouse_id = Column(String(40), index=True)  # blank in historic files
    vehicle_number = Column(String(30))


class FactRoute(Base):
    __tablename__ = "fact_route"
    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True)
    stop_seq = Column(Integer)
    warehouse_id = Column(String(40), index=True)
    store_name = Column(String(160))
    driver = Column(String(120))
    vehicle_no = Column(String(30), index=True)
    out_time = Column(String(20))
    in_time = Column(String(20))
    crate_out = Column(Integer)
    crate_in = Column(Integer)
    remark = Column(String(255))


class FactIndent(Base):
    __tablename__ = "fact_indent"
    id = Column(Integer, primary_key=True)
    indent_date = Column(Date, index=True)
    po_date = Column(Date)
    ds_delivery_date = Column(Date)
    brand = Column(String(80))
    fsn = Column(String(40), index=True)
    po_qty = Column(Integer, default=0)
    vertical = Column(String(60))
    title = Column(String(255))
    final_received_qty = Column(Integer)


class UploadLog(Base):
    __tablename__ = "upload_log"
    id = Column(Integer, primary_key=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(255))
    filename = Column(String(255))
    file_type = Column(String(40))
    dates_covered = Column(String(255))
    rows_in_source = Column(Integer)
    rows_loaded = Column(Integer)
    rows_dropped = Column(Integer)
    notes = Column(Text)
    status = Column(String(20), default="ok")


class DashboardTemplate(Base):
    """Saved view: filters + which widgets are shown, per user."""
    __tablename__ = "dashboard_templates"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    name = Column(String(80))
    config = Column(Text)  # JSON: {filters:{...}, widgets:[...]}
    created_at = Column(DateTime, default=datetime.utcnow)


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id = Column(Integer, primary_key=True)
    name = Column(String(120))
    scope = Column(String(20), default="store")   # store | category | overall
    metric = Column(String(30), default="gap_pct")
    threshold = Column(Float, default=5.0)
    consecutive_days = Column(Integer, default=3)
    min_volume = Column(Integer, default=200)  # ignore tiny-volume noise
    active = Column(Boolean, default=True)


class AlertLog(Base):
    __tablename__ = "alert_log"
    id = Column(Integer, primary_key=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    rule_name = Column(String(120))
    subject = Column(String(255))
    body = Column(Text)
    recipients = Column(Text)
    delivered = Column(Boolean, default=False)


def init_db():
    Base.metadata.create_all(engine)
