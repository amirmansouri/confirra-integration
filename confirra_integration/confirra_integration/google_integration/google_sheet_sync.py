import frappe
import requests
from confirra_integration.confirra_integration.google_integration.google_oauth import get_valid_token

@frappe.whitelist()
def create_google_sheet_for_orders(*args, **kwargs):

    # ---- Read user parameter safely ----
    confirra_user = kwargs.get("confirra_user")
    if not confirra_user:
        frappe.throw("Missing confirra_user")

    token = get_valid_token(confirra_user)

    # ---- Create Google Sheet ----
    url = "https://sheets.googleapis.com/v4/spreadsheets"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    body = {
        "properties": {"title": "Sales Order"},
        "sheets": [{"properties": {"title": "Orders"}}],
    }

    response = requests.post(url, headers=headers, json=body).json()
    sheet_id = response.get("spreadsheetId")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    if not sheet_id:
        frappe.throw(f"Google Sheet creation failed: {response}")

    # ---- Insert HEADER ROWS (your required columns) ----
    header_row = [[
        "ID", "Series", "Customer", "Order Type", "Date", "Company",
        "Currency", "Exchange Rate", "Price List", "Price List Currency",
        "Price List Exchange Rate", "Status",
        "ID (Items)", "Item Code (Items)", "Item Name (Items)",
        "Quantity (Items)", "UOM (Items)", "UOM Conversion Factor (Items)"
    ]]

    append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Orders!A1:append?valueInputOption=RAW"

    requests.post(
        append_url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"values": header_row}
    )

    # ---- Save Sheet in ERPNext Doctype ----
    frappe.get_doc({
        "doctype": "Google Sheet",
        "sheet_name": "Order",
        "sheet_id": sheet_id,
        "sheet_url": sheet_url,
        "linked_user": confirra_user
    }).insert(ignore_permissions=True)

    return {
        "status": "created",
        "sheet_id": sheet_id,
        "sheet_url": sheet_url
    }


@frappe.whitelist()
def sync_sales_orders_to_google(confirra_user, sheet_id):
    """Export ERPNext Sales Orders + Items into Google Sheets"""

    token = get_valid_token(confirra_user)

    # ------------------------------
    # FETCH SALES ORDERS
    # ------------------------------
    orders = frappe.get_all(
        "Sales Order",
        fields=["name", "customer_name", "transaction_date", "status", "company", "currency", "grand_total"]
    )

    order_rows = [["Order ID", "Customer", "Date", "Status", "Company", "Currency", "Grand Total"]]

    for o in orders:
        order_rows.append([
            o.name,
            o.customer_name,
            str(o.transaction_date),
            o.status,
            o.company,
            o.currency,
            o.grand_total
        ])

    # ------------------------------
    # FETCH SALES ORDER ITEMS
    # ------------------------------
    item_rows = [["Order ID", "Item Code", "Item Name", "Qty", "UOM", "Rate"]]

    so_items = frappe.get_all(
        "Sales Order Item",
        fields=["parent", "item_code", "item_name", "qty", "uom", "rate"]
    )

    for it in so_items:
        item_rows.append([
            it.parent,
            it.item_code,
            it.item_name,
            it.qty,
            it.uom,
            it.rate
        ])

    # ------------------------------
    # SEND TO GOOGLE SHEET
    # ------------------------------

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def upload(sheet_name, rows):
        clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{sheet_name}!A1:Z999:clear"
        requests.post(clear_url, headers=headers)

        append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{sheet_name}!A1:append?valueInputOption=RAW"

        return requests.post(append_url, headers=headers, json={"values": rows}).json()

    result_orders = upload("Orders", order_rows)
    result_items = upload("Order Items", item_rows)

    return {
        "status": "success",
        "orders_synced": len(order_rows) - 1,
        "items_synced": len(item_rows) - 1,
        "google_responses": {
            "orders": result_orders,
            "items": result_items
        }
    }

@frappe.whitelist()
def sync_google_to_order(confirra_user, sheet_id):
    """Reads Google Sheet rows and creates/updates ERPNext Sales Orders with debug logging."""

    debug_messages = []   # collect all debug info to return
    def log(msg):
        safe_title = msg[:120]      # prevent 140-char overflow
        print(msg)
        frappe.log_error(title=safe_title, message=msg)
        debug_messages.append(msg)

    # -----------------------
    # 1️⃣ Get Google Token
    # -----------------------
    try:
        token = get_valid_token(confirra_user)
        log(f"Token OK for user {confirra_user}")
    except Exception as e:
        log(f"Token Error: {str(e)}")
        return {"status": "error", "message": str(e), "debug": debug_messages}

    # -----------------------
    # 2️⃣ Read Sheet
    # -----------------------
    read_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Orders!A2:Z999"
    log(f"Reading sheet: {read_url}")

    data = requests.get(read_url, headers={"Authorization": f"Bearer {token}"}).json()

    if "error" in data:
        log(f"Google API Error: {data}")
        return {"status": "error", "message": data, "debug": debug_messages}

    rows = data.get("values", [])
    log(f"Total rows found: {len(rows)}")

    if not rows:
        return {"status": "empty", "message": "No rows found", "debug": debug_messages}

    # -----------------------
    # 3️⃣ Parse rows
    # -----------------------
    orders_map = {}

    for idx, row in enumerate(rows, start=2):  # row numbers starting A2
        log(f"Row {idx}: {row}")

        try:
            (
                order_id,
                series,
                customer,
                order_type,
                date,
                delivery_date,
                company,
                currency,
                exchange_rate,
                price_list,
                price_list_currency,
                price_list_exchange_rate,
                status,
                item_id,
                item_code,
                item_name,
                qty,
                uom,
                uom_cf,
            ) = row
        except Exception as e:
            log(f"Skipping row {idx}, parse error: {str(e)}")
            continue

        if order_id not in orders_map:
            orders_map[order_id] = {
                "customer": customer,
                "transaction_date": date,
                "company": company,
                "currency": currency,
                "selling_price_list": price_list,
                "delivery_date": delivery_date,
                "items": []
            }

        try:
            qty_val = float(qty)
            cf_val = float(uom_cf or 1)
        except:
            qty_val = 1
            cf_val = 1
            log(f"Row {idx}: qty or conversion invalid, default set to 1")

        orders_map[order_id]["items"].append({
            "item_code": item_code,
            "item_name": item_name,
            "qty": qty_val,
            "uom": uom,
            "conversion_factor": cf_val,
            "delivery_date": delivery_date
        })

    log(f"Orders parsed: {orders_map.keys()}")

    created = 0
    updated = 0

    # -----------------------
    # 4️⃣ Create Sales Orders
    # -----------------------
    for order_id, so_data in orders_map.items():
        log(f"Creating SO for Google ID: {order_id}")

        try:
            doc = frappe.get_doc({
                "doctype": "Sales Order",
                "customer": so_data["customer"],
                "transaction_date": so_data["transaction_date"],
                "company": so_data["company"],
                "currency": so_data["currency"],
                "selling_price_list": so_data["selling_price_list"],
                "items": so_data["items"],
            })
            frappe.get_doc({
    "doctype": "Google Sheet",
    "sheet_name": f"SO Sync {order_id}",
    "sheet_id": sheet_id,
    "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}",
    "linked_user": confirra_user,
    "sync_status": "Success",
    "synced_order_id": order_id
}).insert(ignore_permissions=True)

            doc.insert(ignore_permissions=True)
            created += 1
            log(f"SO Created: {doc.name}")
            
        except Exception as e:
            log(f"Failed to create SO for {order_id}: {str(e)}")
            continue

    frappe.db.commit()

    return {
        "status": "success",
        "created": created,
        "updated": updated,
        "total_orders": len(orders_map),
        "debug": debug_messages
    }

@frappe.whitelist()
def list_google_sheets(confirra_user):
    """
    Returns all Google Sheets from user's Google Drive.
    """

    token = get_valid_token(confirra_user)

    url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": "mimeType='application/vnd.google-apps.spreadsheet'",
        "fields": "files(id, name, webViewLink)"
    }

    response = requests.get(url, params=params, headers={
        "Authorization": f"Bearer {token}"
    }).json()

    if "files" not in response:
        return {
            "status": "error",
            "message": response
        }

    return {
        "status": "success",
        "total": len(response["files"]),
        "sheets": response["files"]
    }
@frappe.whitelist()
def add_example_orders(confirra_user, sheet_id):
    """Push 1 example Sales Order row to Google Sheet"""

    token = get_valid_token(confirra_user)

    HEADER = [
        "ID","Series","Customer","Order Type","Date","Company",
        "Currency","Exchange Rate","Price List","Price List Currency",
        "Price List Exchange Rate","Status","ID (Items)",
        "Item Code (Items)","Item Name (Items)",
        "Quantity (Items)","UOM (Items)","UOM Conversion Factor (Items)"
    ]

    EXAMPLE_ORDER = [
        "SO-TEST-001",
        "SAL-ORD-",
        "cus",
        "Sales",
        "2025-12-09",
        "Confirra",
        "MAD",
        "1",
        "Standard Selling",
        "MAD",
        "1",
        "Draft",
        "1",
        "p1",
        "p1",
        "1",
        "Nos",
        "1"
    ]

    # Append header + example row
    append_url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/"
        f"{sheet_id}/values/Orders!A1:append?valueInputOption=RAW"
    )

    payload = {
        "values": [
            HEADER,
            EXAMPLE_ORDER
        ]
    }

    res = requests.post(
        append_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        json=payload
    ).json()

    return {
        "status": "example_added",
        "sheet_id": sheet_id,
        "google_response": res
    }
