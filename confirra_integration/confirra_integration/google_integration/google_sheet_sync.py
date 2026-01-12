"""
Google Sheets Order Sync Integration

TASK-2026-00034: Order Tracking — Normalized Orders + Order_Items with Sheet ↔ DB Sync + Upsells

This module provides:
- Create Google Sheets with proper headers for Orders and Order_Items
- Sync from Google Sheets to ERPNext (with Customer/Item auto-creation)
- Sync from ERPNext to Google Sheets
- Idempotent sync with row ID tracking
- Conflict resolution based on timestamps
"""

import frappe
import requests
from frappe.utils import flt, now_datetime, nowdate
from confirra_integration.confirra_integration.google_integration.google_oauth import get_valid_token
from datetime import datetime

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


# =============================================================================
# TASK-2026-00034: New Order Sync Functions
# =============================================================================

# Sheet column headers per spec section 4
ORDERS_HEADERS = [
    "order_id", "tenant_id", "order_date", "order_time", "order_source",
    "order_status", "customer_name", "customer_phone", "customer_city",
    "customer_address", "payment_method", "delivery_company", "tracking_number",
    "delivery_fee", "currency", "subtotal_main", "subtotal_upsell", "grand_total",
    "conversation_id", "ai_confidence_score", "follow_up_status", "follow_up_date",
    "internal_note", "lock_ai_edit", "last_updated_by", "last_updated_at",
    "sheet_orders_row_id"
]

ORDER_ITEMS_HEADERS = [
    "order_id", "tenant_id", "item_type", "product_name", "product_sku",
    "variant", "quantity", "unit_price", "currency", "line_total", "notes",
    "last_updated_by", "last_updated_at", "sheet_items_row_id"
]


@frappe.whitelist()
def create_order_tracking_sheet(confirra_user, sheet_name="Order Tracking"):
    """
    Create a new Google Sheet with Orders and Order_Items tabs.

    TASK-2026-00034: Creates sheet with proper headers per spec section 4.

    Args:
        confirra_user: Confirra Users ID
        sheet_name: Name for the new sheet

    Returns:
        dict with sheet_id, sheet_url
    """
    token = get_valid_token(confirra_user)

    # Create Google Sheet with two tabs
    url = "https://sheets.googleapis.com/v4/spreadsheets"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    body = {
        "properties": {"title": sheet_name},
        "sheets": [
            {"properties": {"title": "Orders"}},
            {"properties": {"title": "Order_Items"}}
        ],
    }

    response = requests.post(url, headers=headers, json=body).json()
    sheet_id = response.get("spreadsheetId")

    if not sheet_id:
        frappe.throw(f"Google Sheet creation failed: {response}")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    # Add headers to Orders sheet
    append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Orders!A1:append?valueInputOption=RAW"
    requests.post(append_url, headers=headers, json={"values": [ORDERS_HEADERS]})

    # Add headers to Order_Items sheet
    append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Order_Items!A1:append?valueInputOption=RAW"
    requests.post(append_url, headers=headers, json={"values": [ORDER_ITEMS_HEADERS]})

    # Save sheet reference in ERPNext
    frappe.get_doc({
        "doctype": "Google Sheet",
        "sheet_name": sheet_name,
        "sheet_id": sheet_id,
        "sheet_url": sheet_url,
        "linked_user": confirra_user,
        "sync_direction": "Bidirectional"
    }).insert(ignore_permissions=True)

    frappe.db.commit()

    return {
        "status": "created",
        "sheet_id": sheet_id,
        "sheet_url": sheet_url,
        "message": "Sheet created with Orders and Order_Items tabs"
    }


@frappe.whitelist()
def sync_sheet_to_db(confirra_user, sheet_id):
    """
    Sync orders from Google Sheet to ERPNext database.

    TASK-2026-00034: Pull sync with Customer/Item auto-creation.

    Flow:
    1. Read Orders sheet (tries multiple tab names)
    2. Read Order_Items sheet (optional)
    3. For each order:
       - Check/Create Customer
       - Check/Create Items
       - Create/Update Sales Order
    4. Track row IDs for sync

    Args:
        confirra_user: Confirra Users ID
        sheet_id: Google Sheet ID

    Returns:
        dict with sync results
    """
    debug = []
    def log(msg):
        debug.append(msg)
        frappe.logger().info(f"[TASK-2026-00034] {msg}")

    try:
        token = get_valid_token(confirra_user)
        log(f"Token OK for user {confirra_user}")
    except Exception as e:
        return {"status": "error", "error": str(e), "debug": debug}

    headers = {"Authorization": f"Bearer {token}"}

    # Try multiple possible tab names for Orders
    orders_tab_names = ["Orders", "Orders.csv", "Sheet1", "orders", "Feuille 1"]
    orders_rows = []
    orders_tab_found = None

    for tab_name in orders_tab_names:
        orders_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{tab_name}!A2:AA999"
        orders_response = requests.get(orders_url, headers=headers).json()

        if "error" not in orders_response:
            orders_rows = orders_response.get("values", [])
            orders_tab_found = tab_name
            log(f"Found Orders in tab '{tab_name}' with {len(orders_rows)} rows")
            break

    if not orders_tab_found:
        return {"status": "error", "error": "No Orders tab found. Expected tab names: Orders, Sheet1, orders", "debug": debug}

    # Try multiple possible tab names for Order_Items (optional)
    items_tab_names = ["Order_Items", "Order_Items.csv", "Sheet2", "order_items", "Items", "Feuille 2"]
    items_rows = []

    for tab_name in items_tab_names:
        items_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{tab_name}!A2:N999"
        items_response = requests.get(items_url, headers=headers).json()

        if "error" not in items_response:
            items_rows = items_response.get("values", [])
            log(f"Found Order_Items in tab '{tab_name}' with {len(items_rows)} rows")
            break

    if not items_rows:
        log("No Order_Items tab found - will create orders without separate items")
    log(f"Found {len(items_rows)} item rows")

    # Group items by order_id
    items_by_order = {}
    for row_idx, row in enumerate(items_rows, start=2):
        if len(row) < 5:
            continue
        order_id = row[0] if len(row) > 0 else ""
        if not order_id:
            continue

        if order_id not in items_by_order:
            items_by_order[order_id] = []

        items_by_order[order_id].append({
            "order_id": row[0] if len(row) > 0 else "",
            "tenant_id": row[1] if len(row) > 1 else "",
            "item_type": row[2] if len(row) > 2 else "main",
            "product_name": row[3] if len(row) > 3 else "",
            "product_sku": row[4] if len(row) > 4 else "",
            "variant": row[5] if len(row) > 5 else "",
            "quantity": row[6] if len(row) > 6 else "1",
            "unit_price": row[7] if len(row) > 7 else "0",
            "currency": row[8] if len(row) > 8 else "MAD",
            "line_total": row[9] if len(row) > 9 else "0",
            "notes": row[10] if len(row) > 10 else "",
            "last_updated_by": row[11] if len(row) > 11 else "",
            "last_updated_at": row[12] if len(row) > 12 else "",
            "sheet_items_row_id": str(row_idx),
        })

    results = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": []
    }

    # Process each order row
    for row_idx, row in enumerate(orders_rows, start=2):
        log(f"Row {row_idx}: {len(row)} columns, first 10: {row[:10] if len(row) >= 10 else row}")

        if len(row) < 7:
            log(f"Row {row_idx}: Skipping - insufficient columns ({len(row)} < 7)")
            results["skipped"] += 1
            continue

        try:
            # Parse order data (matching ORDERS_HEADERS)
            order_data = {
                "order_id": row[0] if len(row) > 0 else "",
                "tenant_id": row[1] if len(row) > 1 else "",
                "order_date": row[2] if len(row) > 2 else nowdate(),
                "order_time": row[3] if len(row) > 3 else "",
                "order_source": row[4] if len(row) > 4 else "Manual",
                "order_status": row[5] if len(row) > 5 else "Draft",
                "customer_name": row[6] if len(row) > 6 else "",
                "customer_phone": row[7] if len(row) > 7 else "",
                "customer_city": row[8] if len(row) > 8 else "",
                "customer_address": row[9] if len(row) > 9 else "",
                "payment_method": row[10] if len(row) > 10 else "",
                "delivery_company": row[11] if len(row) > 11 else "",
                "tracking_number": row[12] if len(row) > 12 else "",
                "delivery_fee": row[13] if len(row) > 13 else "0",
                "currency": row[14] if len(row) > 14 else "MAD",
                "subtotal_main": row[15] if len(row) > 15 else "0",
                "subtotal_upsell": row[16] if len(row) > 16 else "0",
                "grand_total": row[17] if len(row) > 17 else "0",
                "conversation_id": row[18] if len(row) > 18 else "",
                "ai_confidence_score": row[19] if len(row) > 19 else "0",
                "follow_up_status": row[20] if len(row) > 20 else "",
                "follow_up_date": row[21] if len(row) > 21 else "",
                "internal_note": row[22] if len(row) > 22 else "",
                "lock_ai_edit": row[23] if len(row) > 23 else "0",
                "last_updated_by": row[24] if len(row) > 24 else "Human",
                "last_updated_at": row[25] if len(row) > 25 else "",
                "sheet_orders_row_id": str(row_idx),
            }

            if not order_data["customer_name"]:
                log(f"Row {row_idx}: Skipping - no customer name")
                results["skipped"] += 1
                continue

            # Get items for this order
            order_items = items_by_order.get(order_data["order_id"], [])

            # Create order using the orders API
            from confirra.confirra.api.orders import create_order

            # Format items for API
            items_for_api = []
            for item in order_items:
                items_for_api.append({
                    "product_name": item["product_name"],
                    "product_sku": item["product_sku"],
                    "variant": item["variant"],
                    "quantity": flt(item["quantity"]),
                    "unit_price": flt(item["unit_price"]),
                    "item_type": item["item_type"],
                    "notes": item["notes"],
                    "sheet_items_row_id": item["sheet_items_row_id"],
                })

            # If no items from Order_Items sheet, create dummy item
            if not items_for_api:
                items_for_api.append({
                    "product_name": "Sheet Order Item",
                    "product_sku": "",
                    "quantity": 1,
                    "unit_price": flt(order_data["grand_total"]),
                    "item_type": "main",
                })

            result = create_order(
                customer_name=order_data["customer_name"],
                customer_phone=order_data["customer_phone"],
                customer_city=order_data["customer_city"],
                customer_address=order_data["customer_address"],
                order_source=order_data["order_source"],
                conversation_id=order_data["conversation_id"],
                sheet_orders_row_id=order_data["sheet_orders_row_id"],
                items=items_for_api,
                follow_up_status=order_data["follow_up_status"],
                follow_up_date=order_data["follow_up_date"] if order_data["follow_up_date"] else None,
                ai_confidence_score=flt(order_data["ai_confidence_score"]),
                internal_note=order_data["internal_note"],
                tenant_id=confirra_user,  # Pass tenant_id to set Store
            )

            if result.get("success"):
                results["created"] += 1
                log(f"Row {row_idx}: Created Sales Order {result.get('sales_order_name')}")
            else:
                results["errors"].append({
                    "row": row_idx,
                    "error": result.get("error", "Unknown error")
                })

        except Exception as e:
            log(f"Row {row_idx}: Error - {str(e)}")
            results["errors"].append({"row": row_idx, "error": str(e)})

    frappe.db.commit()

    return {
        "status": "success",
        "created": results["created"],
        "updated": results["updated"],
        "skipped": results["skipped"],
        "errors": results["errors"],
        "debug": debug
    }


@frappe.whitelist()
def sync_db_to_sheet(confirra_user, sheet_id, since_datetime=None):
    """
    Sync orders from ERPNext database to Google Sheet.

    TASK-2026-00034: Push sync with proper column format.

    Args:
        confirra_user: Confirra Users ID
        sheet_id: Google Sheet ID
        since_datetime: Only sync orders modified after this time

    Returns:
        dict with sync results
    """
    debug = []
    def log(msg):
        debug.append(msg)
        frappe.logger().info(f"[TASK-2026-00034] {msg}")

    try:
        token = get_valid_token(confirra_user)
        log(f"Token OK for user {confirra_user}")
    except Exception as e:
        return {"status": "error", "error": str(e), "debug": debug}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # Get orders from API
    from confirra.confirra.api.orders import get_orders_for_sheet_sync
    result = get_orders_for_sheet_sync(since_datetime=since_datetime)

    if not result.get("success"):
        return {"status": "error", "error": result.get("error"), "debug": debug}

    orders = result.get("orders", [])
    items = result.get("items", [])

    log(f"Pushing {len(orders)} orders and {len(items)} items to sheet")

    # Clear and write Orders sheet
    clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Orders!A2:AA999:clear"
    requests.post(clear_url, headers=headers)

    # Build orders rows
    orders_rows = []
    for order in orders:
        orders_rows.append([
            order.get("order_id", ""),
            order.get("tenant_id", ""),
            order.get("order_date", ""),
            order.get("order_time", ""),
            order.get("order_source", ""),
            order.get("order_status", ""),
            order.get("customer_name", ""),
            order.get("customer_phone", ""),
            order.get("customer_city", ""),
            order.get("customer_address", ""),
            order.get("payment_method", ""),
            order.get("delivery_company", ""),
            order.get("tracking_number", ""),
            order.get("delivery_fee", 0),
            order.get("currency", "MAD"),
            order.get("subtotal_main", 0),
            order.get("subtotal_upsell", 0),
            order.get("grand_total", 0),
            order.get("conversation_id", ""),
            order.get("ai_confidence_score", 0),
            order.get("follow_up_status", ""),
            order.get("follow_up_date", ""),
            order.get("internal_note", ""),
            1 if order.get("lock_ai_edit") else 0,
            order.get("last_updated_by", ""),
            order.get("last_updated_at", ""),
            order.get("sheet_orders_row_id", ""),
        ])

    if orders_rows:
        append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Orders!A2:append?valueInputOption=RAW"
        requests.post(append_url, headers=headers, json={"values": orders_rows})

    # Clear and write Order_Items sheet
    clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Order_Items!A2:N999:clear"
    requests.post(clear_url, headers=headers)

    # Build items rows
    items_rows = []
    for item in items:
        items_rows.append([
            item.get("order_id", ""),
            item.get("tenant_id", ""),
            item.get("item_type", "main"),
            item.get("product_name", ""),
            item.get("product_sku", ""),
            item.get("variant", ""),
            item.get("quantity", 1),
            item.get("unit_price", 0),
            item.get("currency", "MAD"),
            item.get("line_total", 0),
            item.get("notes", ""),
            item.get("last_updated_by", ""),
            item.get("last_updated_at", ""),
            item.get("sheet_items_row_id", ""),
        ])

    if items_rows:
        append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Order_Items!A2:append?valueInputOption=RAW"
        requests.post(append_url, headers=headers, json={"values": items_rows})

    # Update last sync time in Google Sheet record
    google_sheet = frappe.db.get_value("Google Sheet", {"sheet_id": sheet_id}, "name")
    if google_sheet:
        frappe.db.set_value("Google Sheet", google_sheet, "last_sync_time", now_datetime())

    frappe.db.commit()

    return {
        "status": "success",
        "orders_synced": len(orders_rows),
        "items_synced": len(items_rows),
        "debug": debug
    }


@frappe.whitelist()
def get_sheet_templates():
    """
    Get downloadable sheet templates with headers.

    TASK-2026-00034: Spec section 8.1 - Downloadable Templates

    Returns:
        dict with orders and items template headers
    """
    return {
        "status": "success",
        "orders_template": {
            "headers": ORDERS_HEADERS,
            "sample_row": [
                "ORD-001", "TENANT-001", "2025-01-12", "10:00:00", "Manual",
                "Pending", "John Doe", "+212600000000", "Casablanca",
                "123 Main Street", "COD", "", "", "30", "MAD",
                "100", "50", "180", "", "0.95", "Needs Confirmation",
                "2025-01-15", "", "0", "Human", "", ""
            ]
        },
        "items_template": {
            "headers": ORDER_ITEMS_HEADERS,
            "sample_row": [
                "ORD-001", "TENANT-001", "main", "Product A", "SKU-001",
                "Large", "2", "50", "MAD", "100", "", "Human", "", ""
            ]
        }
    }
