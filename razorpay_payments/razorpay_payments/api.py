import frappe
import hmac
import hashlib
import json
import base64
import requests

# Utility for Razorpay headers and authentication
def get_razorpay_headers():
    settings = frappe.get_single("Razorpay Settings")
    api_key = settings.api_key
    api_secret = settings.get_password("api_secret")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode("utf-8")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Basic {token}"
    }

# Secure fetch for webhook secret
def get_webhook_secret():
    settings = frappe.get_single("Razorpay Settings")
    return settings.webhook_secret or frappe.conf.get("RAZORPAY_WEBHOOK_SECRET")

# Unified error logger
def log_error(title, message):
    frappe.log_error(title=title, message=message)

@frappe.whitelist()
def send_payment_link_on_invoice_submit(doc, method):
    customer = frappe.get_doc("Customer", doc.customer)
    email = customer.email_id
    phone = customer.mobile_no
    invoice_phone = doc.custom_payment_mobile_number or phone

    # Validation: Ensure customer info is present
    if not invoice_phone:
        frappe.throw("Missing customer mobile/email. Cannot send payment link.")

    url = "https://api.razorpay.com/v1/payment_links"
    payload = {
        "amount": int(doc.outstanding_amount * 100),
        "currency": doc.currency or "INR",
        "accept_partial": True,
        "reference_id": doc.name,
        "description": f"Invoice {doc.name}",
        "customer": {
            "name": customer.customer_name,
            "contact": invoice_phone,
            "email": email
        },
        "notify": {"sms": True, "email": True},
        "reminder_enable": True,
        "notes": {"invoice_name": doc.name}
    }
    headers = get_razorpay_headers()

    # Improved reliability: Retry on connection errors
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=8)
            if response.status_code == 200:
                link = response.json()
                frappe.db.set_value("Sales Invoice", doc.name, "custom_razorpay_payment_link_id", link.get("id"))
                frappe.db.set_value("Sales Invoice", doc.name, "custom_razorpay_payment_link_url", link.get("short_url"))
                frappe.msgprint(f"✅ Payment link sent to: {invoice_phone}", alert=True)
                return
            else:
                log_error("Payment Link Creation", f"Razorpay error: {response.text}")
                # For transient errors, retry; for validation/auth errors, break early
                if response.status_code not in [429, 500, 502, 503, 504]:
                    break
        except Exception as e:
            log_error("Payment Link Creation", f"Razorpay error: {e}")
    frappe.throw("Failed to send payment link after retries.")

@frappe.whitelist()
def resend_payment_link(invoice_name, via="sms"):
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    link_id = invoice.custom_razorpay_payment_link_id
    if not link_id:
        frappe.throw("No Payment Link ID found. Create link first.")

    url = f"https://api.razorpay.com/v1/payment_links/{link_id}/notify_by/{via}"
    headers = get_razorpay_headers()

    try:
        response = requests.post(url, headers=headers, timeout=8)
        if response.status_code == 200:
            frappe.msgprint("✅ Payment link resent via SMS")
        else:
            log_error("Razorpay Resend Error", response.text)
            frappe.throw("Failed to resend payment link. Check logs.")
    except Exception as e:
        log_error("Razorpay Resend Error", str(e))
        frappe.throw("Failed to resend payment link due to connection error.")

@frappe.whitelist(allow_guest=True)
def razorpay_webhook():
    log_error("Webhook Called", "Webhook Start")
    try:
        data = frappe.request.data
        payload = json.loads(data.decode("utf-8"))

        signature = frappe.get_request_header("X-Razorpay-Signature")
        # webhook_secret = get_webhook_secret()
        webhook_secret = "Pass@1234"
        if not webhook_secret:
            log_error("Webhook Setup Error", "Webhook secret missing in config.")
            return "Webhook secret not found"

        # Secure signature comparison
        generated_sig = hmac.new(
            webhook_secret.encode("utf-8"), data, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, generated_sig):
            log_error("Webhook Auth Failed", f"Signature mismatch: Received {signature}, Generated {generated_sig}")
            return "Invalid signature"

        event_type = payload.get("event")
        log_error("Webhook Payload", json.dumps(payload, indent=2))
        if event_type not in ["payment_link.paid", "payment.captured"]:
            log_error("Webhook Event Skip", f"Event '{event_type}' not handled")
            return "Event not handled"

        payment_data = payload.get("payload", {})
        payment = (payment_data.get("payment", {}) or {}).get("entity", {})
        payment_link = (payment_data.get("payment_link", {}) or {}).get("entity", {})
        notes = payment.get("notes") or payment_link.get("notes")
        invoice_name = notes.get("invoice_name") if notes else None
        if not invoice_name:
            log_error("Webhook Missing Data", "Invoice name missing in notes.")
            return "Invoice name not found"
        amount_paid = int(payment.get("amount", 0)) / 100
        txn_id = payment.get("id")

        log_error("Payment Processing", f"Invoice: {invoice_name}\nAmount: {amount_paid}\nTxn ID: {txn_id}")
        if frappe.db.exists("Payment Entry", {"reference_no": txn_id}):
            log_error("Webhook Duplicate", "Duplicate payment ignored.")
            return "Payment Already Recorded"

        frappe.enqueue(
            create_payment_entry,
            queue='short',
            timeout=300,
            invoice_name=invoice_name,
            amount_paid=amount_paid,
            txn_id=txn_id,
            enqueue_after_commit=True
        )

        log_error("Webhook Success", "Payment enqueued successfully")
        return "OK"

    except Exception as e:
        log_error("Webhook Exception", frappe.get_traceback())
        return "Failed"

def create_payment_entry(invoice_name, amount_paid, txn_id):
    """Background job to create payment entry with proper permissions"""
    try:
        frappe.set_user("Administrator")
        if frappe.db.exists("Payment Entry", {"reference_no": txn_id}):
            log_error("Duplicate Check", f"Payment Entry already exists for txn_id: {txn_id}")
            return

        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        mode_of_payment = "Razorpay"
        paid_to_account = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mode_of_payment, "company": invoice.company},
            "default_account"
        )
        if not paid_to_account:
            log_error("Account Missing", f"No default account set for Mode of Payment: {mode_of_payment} in company: {invoice.company}")
            return

        paid_from_account = frappe.get_value(
            "Party Account",
            {"parenttype": "Customer", "parent": invoice.customer, "company": invoice.company},
            "account"
        ) or frappe.get_value("Company", invoice.company, "default_receivable_account")
        if not paid_from_account:
            log_error("Receivable Account Missing", f"No receivable account found for customer: {invoice.customer}")
            return

        log_error("Account Setup", f"paid_from: {paid_from_account}\npaid_to: {paid_to_account}")

        payment_entry = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "company": invoice.company,
            "posting_date": frappe.utils.nowdate(),
            "party_type": "Customer",
            "party": invoice.customer,
            "mode_of_payment": mode_of_payment,
            "paid_from": paid_from_account,
            "paid_to": paid_to_account,
            "paid_from_account_currency": invoice.currency,
            "paid_to_account_currency": frappe.get_value("Account", paid_to_account, "account_currency"),
            "paid_amount": amount_paid,
            "received_amount": amount_paid,
            "reference_no": txn_id,
            "reference_date": frappe.utils.nowdate(),
            "references": [{
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice_name,
                "total_amount": invoice.grand_total,
                "outstanding_amount": invoice.outstanding_amount,
                "allocated_amount": amount_paid
            }]
        })
        payment_entry.setup_party_account_field()
        payment_entry.set_missing_values()
        payment_entry.set_exchange_rate()
        payment_entry.set_amounts()
        payment_entry.flags.ignore_permissions = True
        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()
        frappe.db.commit()
        log_error("Payment Entry Created", f"PE: {payment_entry.name}\nInvoice: {invoice_name}\nAmount: {amount_paid}")
        return payment_entry.name

    except Exception:
        frappe.db.rollback()
        log_error("Payment Entry Failed", frappe.get_traceback())
        raise
