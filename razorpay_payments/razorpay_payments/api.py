import frappe, hmac, hashlib, json
import frappe, base64
import requests


@frappe.whitelist()
def send_payment_link_on_invoice_submit(doc, method):
    """Create and send Razorpay payment link when Sales Invoice is submitted."""

    customer = frappe.get_doc("Customer", doc.customer)
    email = customer.email_id
    phone = customer.mobile_no

    # ✅ Custom mobile number logic
    invoice_phone = doc.custom_payment_mobile_number or phone

    settings = frappe.get_single("Razorpay Settings")
    api_key = settings.api_key
    api_secret = settings.get_password('api_secret')

    url = "https://api.razorpay.com/v1/payment_links"

    payload = {
        "amount": int(doc.outstanding_amount * 100),
        "currency": doc.currency or "INR",
        "accept_partial": True,
        "reference_id": doc.name,
        "description": f"Invoice {doc.name}",
        "customer": {
            "name": customer.customer_name,
            "contact": invoice_phone or "",
            "email": email or ""
        },
        "notify": {
            "sms": True,
            "email": True
        },
        "reminder_enable": True,
        "notes": {
            "invoice_name": doc.name
        }
    }

    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode("utf-8")
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {token}'
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        if response.status_code == 200:
            link = response.json()
            frappe.db.set_value("Sales Invoice", doc.name, "custom_razorpay_payment_link_id", link.get("id"))
            frappe.db.set_value("Sales Invoice", doc.name, "custom_razorpay_payment_link_url", link.get("short_url"))
            frappe.msgprint(f"✅ Payment link sent to: {invoice_phone}", alert=True)
        else:
            frappe.log_error(f"Razorpay error: {response.text}", "Payment Link Creation")
    except Exception as e:
        frappe.log_error(f"Razorpay error: {e}", "Payment Link Creation")


# # your_app/api.py
# @frappe.whitelist(allow_guest=True)
# def razorpay_webhook():
#     raw_body = frappe.request.get_data(as_text=True)
#     signature = frappe.get_request_header("X-Razorpay-Signature")
#     if not signature:
#         frappe.local.response["http_status_code"] = 400
#         return "Missing signature"

#     # settings = frappe.get_single("Razorpay Settings")
#     # webhook_secret = settings.webhook_secret  # Add this Password field in your Settings
#     webhook_secret = "pass@1234"

#     expected = hmac.new(
#         webhook_secret.encode("utf-8"),
#         raw_body.encode("utf-8"),
#         hashlib.sha256
#     ).hexdigest()

#     if not hmac.compare_digest(expected, signature):
#         frappe.local.response["http_status_code"] = 400
#         return "Invalid signature"

#     payload = json.loads(raw_body)
#     event = payload.get("event")

#     if event not in ("payment_link.paid", "payment_link.partially_paid"):
#         return "Ignored"

#     # Extract reference_id and paid amount (paise) from payment_link entity
#     pl_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {}) or {}
#     reference_id = pl_entity.get("reference_id")
#     payment_link_id = pl_entity.get("id")
#     amount_paid = (pl_entity.get("amount_paid") or 0) / 100.0

#     # Payment id if available
#     payment = payload.get("payload", {}).get("payment", {}) or {}
#     payment_id = (payment.get("entity") or {}).get("id")

#     if not reference_id:
#         frappe.log_error(f"No reference_id in webhook for link {payment_link_id}", "Razorpay Webhook")
#         return "No reference_id"

#     # Load Sales Invoice by name (you set reference_id=doc.name while creating link)
#     inv = frappe.get_doc("Sales Invoice", reference_id)
#     if inv.docstatus != 1:
#         return "Invoice not submitted"

#     # Create Payment Entry for the paid amount (cap to outstanding)
#     alloc = min(amount_paid, float(inv.outstanding_amount))
#     if alloc <= 0:
#         return "Nothing to allocate"

#     company = inv.company
#     receivable = frappe.get_cached_value("Company", company, "default_receivable_account")
#     bank_account = frappe.db.get_value("Bank Account", {"company": company, "is_default": 1}, "account")
#     if not receivable or not bank_account:
#         frappe.log_error("Missing default accounts for Payment Entry", "Razorpay Webhook")
#         return "Missing accounts"

#     pe = frappe.get_doc({
#         "doctype": "Payment Entry",
#         "payment_type": "Receive",
#         "party_type": "Customer",
#         "party": inv.customer,
#         "company": company,
#         "paid_from": receivable,
#         "paid_to": bank_account,
#         "paid_amount": alloc,
#         "received_amount": alloc,
#         "mode_of_payment": "Razorpay",
#         "reference_no": payment_id or payment_link_id,
#         "reference_date": frappe.utils.today(),
#         "references": [{
#             "reference_doctype": "Sales Invoice",
#             "reference_name": inv.name,
#             "allocated_amount": alloc
#         }]
#     })
#     pe.insert(ignore_permissions=True)
#     pe.submit()

#     inv.add_comment("Comment", f"Payment via Razorpay link {payment_link_id}, payment_id: {payment_id}, amount: {alloc}")
#     return "ok"