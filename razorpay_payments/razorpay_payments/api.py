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



@frappe.whitelist()
def resend_payment_link(invoice_name, via="sms"):
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    link_id = invoice.custom_razorpay_payment_link_id

    if not link_id:
        frappe.throw("No Payment Link ID found. Create link first.")

    settings = frappe.get_single("Razorpay Settings")
    api_key = settings.api_key
    api_secret = settings.get_password("api_secret")

    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode("utf-8")
    url = f"https://api.razorpay.com/v1/payment_links/{link_id}/notify_by/{via}"
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    response = requests.post(url, headers=headers)

    if response.status_code == 200:
        frappe.msgprint("✅ Payment link resent via SMS")
    else:
        frappe.log_error(f"{response.text}", "Razorpay Resend Error")
        frappe.throw("Failed to resend payment link. Check logs.")


import frappe, json, hmac, hashlib

@frappe.whitelist(allow_guest=True)
def razorpay_webhook():
    frappe.log_error("Webhook Called")
    try:
        data = frappe.request.data
        payload = json.loads(data.decode("utf-8"))

        signature = frappe.get_request_header("X-Razorpay-Signature")
        # webhook_secret = frappe.db.get_single_value("Razorpay Settings", "webhook_secret")
        webhook_secret = "Pass@1234"

        # ✅ Verify Razorpay signature
        generated_sig = hmac.new(
            webhook_secret.encode(), data, hashlib.sha256
        ).hexdigest()

        frappe.log_error(data, payload, signature, generated_sig)

        if signature != generated_sig:
            frappe.log_error("Invalid webhook signature", "Razorpay Webhook")
            return "Invalid signature"

        event_type = payload.get("event")
        frappe.log_error(payload, "Webhook Received ✅")

        if event_type not in ["payment_link.paid", "payment.captured"]:
            return "Event not handled"

        payment_data = payload.get("payload", {})
        payment = (payment_data.get("payment", {}) or {}).get("entity", {})
        payment_link = (payment_data.get("payment_link", {}) or {}).get("entity", {})

        notes = payment.get("notes") or payment_link.get("notes")
        invoice_name = notes.get("invoice_name")

        if not invoice_name:
            frappe.log_error("Invoice name missing", "Webhook Error")
            return "Invoice name not found"

        amount_paid = int(payment.get("amount", 0)) / 100
        txn_id = payment.get("id")

        # ✅ Prevent duplicate Payment Entry
        if frappe.db.exists("Payment Entry", {"reference_no": txn_id}):
            frappe.log_error("Duplicate payment ignored", "Webhook Info")
            return "Payment Already Recorded"

        invoice = frappe.get_doc("Sales Invoice", invoice_name)

        payment_entry = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "company": invoice.company,
            "party_type": "Customer",
            "party": invoice.customer,
            "posting_date": frappe.utils.nowdate(),
            "mode_of_payment": "Razorpay",
            "reference_no": txn_id,
            "reference_date": frappe.utils.nowdate(),
            "paid_amount": amount_paid,
            "received_amount": amount_paid,
            "references": [{
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice_name,
                "allocated_amount": amount_paid
            }]
        })

        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()

        frappe.db.commit()  # ✅ VERY IMPORTANT for webhook jobs

        frappe.log_error(f"Payment Entry Created: {payment_entry.name}", "Webhook Success ✅")

        return "OK"
    
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(str(e), "Webhook Failed ❌")
        return "Failed"

