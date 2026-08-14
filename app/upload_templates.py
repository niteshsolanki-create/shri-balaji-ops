"""
Canonical upload templates.

These are the single source of truth for what each upload file should look
like. The headers here are exactly the ones ingest.py's column detector
looks for, so a file built from a template is guaranteed to be recognised
and to link correctly to the rest of the data.

Two rules that matter for linking, and are worth understanding rather than
just following:

1. FSN is the join key across every file. It is what connects a PO to an
   inbound delivery to a pick to a store GRN. A file without FSN can be
   loaded but cannot be traced through the cycle.
2. Store ID must be the facility code (gur_106_wh_hl_01), never the store's
   display name. Names are inconsistent across exports; the code is not.

The 'required' list drives the file-type detector. The 'optional' columns
improve analysis but a file loads without them.
"""

TEMPLATES = {
    "indent": {
        "label": "Indent / PO raised",
        "stage": "1. PO raised",
        "filename": "INDENT_template.csv",
        "why": "The start of the cycle — what you asked the vendor for.",
        "headers": ["Indent_Date", "PO Date", "PO Reference", "Brand", "FSN",
                    "PO Qty", "Vertical", "Title", "Final Received Qty"],
        "example": ["2026-08-01", "2026-08-01", "AMUL-PO-0142", "Amul",
                    "MLK000000001XYZ", "1000", "Dairy", "Amul Gold Milk 500ml", "800"],
        "notes": [
            "PO Reference is a number YOU make up when raising the PO — write anything unique, "
            "e.g. AMUL-PO-0142. Write the exact same value on the Warehouse Inbound file when "
            "that delivery arrives, and the app matches this exact PO to its exact delivery "
            "instead of just totalling the brand for the date range.",
            "Final Received Qty closes the loop — fill it once the vendor has delivered.",
            "Leave it blank only if the delivery hasn't happened yet.",
        ],
    },
    "batching": {
        "label": "Batching / dispatch",
        "stage": "3. Picked and batched per store",
        "filename": "BATCHING_template.csv",
        "why": "Flipkart's own export — what to pick, and what you picked.",
        "headers": ["PO Number", "Cutoff Datetime", "FSN", "Product Title",
                    "Store_ID", "Expected Qty", "Picked Qty", "Shortage Qty",
                    "Pending Qty", "Status", "Picked By", "Picked At"],
        "example": ["PO123456", "2026-08-01 18:00:00", "MLK000000001XYZ",
                    "Amul Gold Milk 500ml", "gur_106_wh_hl_01", "20", "18",
                    "2", "0", "PICKED", "Priyanshu", "2026-08-01 17:45:00"],
        "notes": [
            "Download this from Flipkart rather than retyping it.",
            "Picked Qty here becomes the 'expected' the store is measured against.",
        ],
    },
    "store_receiving": {
        "label": "Store receiving (GRN)",
        "stage": "4. Store acknowledges",
        "filename": "STORE_RECEIVING_template.csv",
        "why": "What actually landed on the darkstore shelf.",
        "headers": ["Warehouse ID", "Invoice Date", "Invoice ID", "FSN",
                    "Description", "Expected Quantity", "Received Quantity",
                    "Damaged Quantity", "Scanning Issue Quantity",
                    "Excess Quantity", "Returned Quantity", "Swapped Quantity",
                    "Status", "Uploaded At"],
        "example": ["gur_106_wh_hl_01", "2026-08-01", "SBF12143",
                    "MLK000000001XYZ", "Amul Gold Milk 500ml", "18", "17",
                    "1", "0", "0", "0", "0", "DELIVERED", "2026-08-01 20:00:00"],
        "notes": [
            "Damaged is tracked separately from missing — a damaged unit arrived, a missing one didn't.",
            "Keep them in separate columns or the claimable gap will be overstated.",
        ],
    },
    "rejects": {
        "label": "Rejects",
        "stage": "Any stage",
        "filename": "REJECTS_template.csv",
        "why": "Stock written off — damaged, expired, or returned.",
        "headers": ["Date", "EAN", "FSN", "Product", "Brand", "Category",
                    "Qty", "Reason", "Expiry", "Store", "Vehicle"],
        "example": ["2026-08-01", "8901030123456", "MLK000000001XYZ",
                    "Amul Gold Milk 500ml", "Amul", "Dairy", "5",
                    "Damaged in transit", "2026-08-10", "gur_106_wh_hl_01",
                    "HR26AB1234"],
        "notes": [
            "Format the Qty column as Number in Excel before typing — Excel turns 5 into 05-Jan-1900 otherwise.",
            "Filling Store and Vehicle is what lets a reject be traced to a route.",
        ],
    },
    "route": {
        "label": "Route / vehicle log",
        "stage": "3b. Vehicle leaves",
        "filename": "ROUTE_template.csv",
        "why": "Which vehicle went where — the evidence that separates a crate swap from a genuine shortage.",
        "headers": ["Date", "Sno", "Store No", "Driver", "Vehicle No",
                    "Start", "End", "Crate Out", "Crate In", "Remark"],
        "example": ["2026-08-01", "1", "gur_106_wh_hl_01", "Ramesh Kumar",
                    "HR26AB1234", "2026-08-01 19:00:00", "2026-08-01 20:15:00",
                    "12", "11", ""],
        "notes": [
            "Store No must be the facility code, not the store's display name.",
            "Crate counts are per trip — they narrow a swap to a route, not to a single stop.",
        ],
    },
    "store_master": {
        "label": "Store master",
        "stage": "Reference",
        "filename": "STORE_MASTER_template.csv",
        "why": "Your 48 darkstores. Anything not on this list is filtered out of every report.",
        "headers": ["Serial No", "Warehouse Name", "Facility Site Code / WH"],
        "example": ["1", "Darkstore Sector 106 Gurgaon", "gur_106_wh_hl_01"],
        "notes": [
            "Upload this first. Flipkart's exports are national — this list is what limits them to your network.",
            "Re-uploading replaces the whole list, so include every store each time.",
        ],
    },
    "product_master": {
        "label": "Product master",
        "stage": "Reference",
        "filename": "PRODUCT_MASTER_template.csv",
        "why": "FSN to product name, brand and category. Drives every category breakdown.",
        "headers": ["FSN", "EAN", "Brand", "Category", "Title", "MRP", "Price"],
        "example": ["MLK000000001XYZ", "8901030123456", "Amul", "Milk",
                    "Amul Gold Milk 500ml", "33", "31"],
        "notes": [
            "Category must be one of the specific values Flipkart uses — Milk, Curd & Yogurt, "
            "Paneer & Tofu, Eggs, Breads, Fruits & Vegetables, etc — not the department name. "
            "The app groups these into Dairy / Egg & Bread / F&V for you automatically.",
            "Products missing here still load, but their category is guessed from the FSN prefix.",
            "Re-uploading replaces the whole list, so include every product each time.",
        ],
    },
}

# Order shown in the UI — follows the physical flow of stock, so the list
# doubles as a description of the cycle.
ORDER = ["store_master", "product_master", "indent",
         "batching", "route", "store_receiving", "rejects"]


def template_csv(key):
    """Return the template as CSV text: header row + one example row."""
    t = TEMPLATES[key]
    lines = [",".join(_q(h) for h in t["headers"]),
             ",".join(_q(v) for v in t["example"])]
    return "\r\n".join(lines) + "\r\n"


def _q(v):
    v = str(v)
    return f'"{v}"' if ("," in v or '"' in v) else v


def template_index():
    """Metadata for the UI, in flow order."""
    return [{"key": k, **{kk: vv for kk, vv in TEMPLATES[k].items()
                          if kk != "example"}} for k in ORDER]
