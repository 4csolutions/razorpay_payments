import frappe
import hmac
import hashlib
import json
import base64
import requests
from erpnext import get_default_company

# Utility for Razorpay headers and authentication
def get_razorpay_headers():
    settings = frappe.get_single("Razorpay Settings")
    api_key = settings.api_key
    api_secret = settings.get_password("api_secret")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode("utf-8")
    return {"Content-Type": "application/json", "Authorization": f"Basic {token}"}


# Secure fetch for webhook secret
def get_webhook_secret():
    settings = frappe.get_single("Razorpay Settings")
    return settings.get_password("webhook_secret")


@frappe.whitelist()
def send_payment_link_on_invoice_submit(doc, method):
    if not doc.custom_send_razorpay_payment_link or not frappe.get_value("Razorpay Settings", "enable_payment_links"):
        return

    customer = frappe.get_doc("Customer", doc.customer)
    email = customer.email_id
    phone = customer.mobile_no
    invoice_phone = doc.custom_payment_mobile_no or phone

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
            "contact": f"+91{invoice_phone}",
            "email": email,
        },
        "notify": {"sms": True, "email": True},
        "reminder_enable": True,
        "notes": {"invoice_name": doc.name},
    }
    headers = get_razorpay_headers()

    # Improved reliability: Retry on connection errors
    # for attempt in range(3):
    try:
        response = requests.post(
            url, headers=headers, data=json.dumps(payload), timeout=8
        )
        if response.status_code == 200:
            link = response.json()
            frappe.db.set_value(
                "Sales Invoice",
                doc.name,
                {
                    "custom_razorpay_payment_link_id": link.get("id"),
                    "custom_razorpay_payment_link_url": link.get("short_url"),
                },
                update_modified=False,
            )

            frappe.msgprint(f"✅ Payment link sent to: {invoice_phone}", alert=True)
            return
        else:
            frappe.log_error(
                "Payment Link Creation", f"Razorpay error: {response.text}"
            )
    except Exception as e:
        frappe.log_error("Payment Link Creation", f"Razorpay error: {e}")


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
            frappe.log_error("Razorpay Resend Error", response.text)
            frappe.throw("Failed to resend payment link. Check logs.")
    except Exception as e:
        frappe.log_error("Razorpay Resend Error", str(e))
        frappe.throw("Failed to resend payment link due to connection error.")


@frappe.whitelist(allow_guest=True)
def razorpay_webhook():
    frappe.log_error("Webhook Called", "Webhook Start")
    try:
        data = frappe.request.data
        payload = json.loads(data.decode("utf-8"))
        frappe.log_error("Webhook Payload", json.dumps(payload))

        signature = frappe.get_request_header("X-Razorpay-Signature")
        webhook_secret = get_webhook_secret()
        if not webhook_secret:
            frappe.log_error("Webhook Setup Error", "Webhook secret missing in config.")
            return "Webhook secret not found"

        # Secure signature comparison
        generated_sig = hmac.new(
            webhook_secret.encode("utf-8"), data, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, generated_sig):
            frappe.log_error(
                "Webhook Auth Failed",
                f"Signature mismatch: Received {signature}, Generated {generated_sig}",
            )
            return "Invalid signature"

        event_type = payload.get("event")
        if event_type not in ["payment_link.paid", "payment_link.partially_paid"]:
            frappe.log_error("Webhook Event Skip", f"Event '{event_type}' not handled")
            return "Event not handled"

        payment_data = payload.get("payload", {})
        payment = payment_data.get("payment", {}).get("entity", {})
        payment_link = payment_data.get("payment_link", {})
        invoice_name = payment_link.get("entity", {}).get("reference_id")
        if not invoice_name:
            frappe.log_error("Webhook Missing Data", "Invoice name missing in notes.")
            return "Invoice name not found"
        amount_paid = int(payment.get("amount", 0)) / 100
        txn_id = payment.get("id")

        if frappe.db.exists("Payment Entry", {"reference_no": txn_id}):
            frappe.log_error("Webhook Duplicate", "Duplicate payment ignored.")
            return "Payment Already Recorded"

        frappe.enqueue(
            create_payment_entry,
            queue="short",
            timeout=300,
            invoice_name=invoice_name,
            amount_paid=amount_paid,
            txn_id=txn_id,
            enqueue_after_commit=True,
        )

        return "OK"

    except Exception as e:
        frappe.log_error("Webhook Exception", frappe.get_traceback())
        return "Failed"


def create_payment_entry(invoice_name, amount_paid, txn_id):
    """Background job to create payment entry with proper permissions"""
    try:
        frappe.set_user("Administrator")
        if frappe.db.exists("Payment Entry", {"reference_no": txn_id}):
            frappe.log_error(
                "Duplicate Check", f"Payment Entry already exists for txn_id: {txn_id}"
            )
            return

        invoice = frappe.get_doc("Sales Invoice", invoice_name)

        razorpay_account =  frappe.db.get_value(
            "Payment Gateway Account",
            {"payment_gateway": "Razorpay", "company": invoice.company},
            "payment_account"
        )
        # fetch mode of payment where payment_account = razorpay_account
        mode_of_payment = frappe.db.get_value(
            "Mode of Payment Account",
            {"default_account": razorpay_account, "company": invoice.company},
            "parent"
        )
        if not mode_of_payment:
            frappe.log_error(
                "Mode of Payment Missing",
                f"No Mode of Payment found for account: {razorpay_account} in company: {invoice.company}",
            )
            return

        paid_to_account = razorpay_account

        paid_from_account = frappe.get_value(
            "Party Account",
            {
                "parenttype": "Customer",
                "parent": invoice.customer,
                "company": invoice.company,
            },
            "account",
        ) or frappe.get_value("Company", invoice.company, "default_receivable_account")
        if not paid_from_account:
            frappe.log_error(
                "Receivable Account Missing",
                f"No receivable account found for customer: {invoice.customer}",
            )
            return

        payment_entry = frappe.get_doc(
            {
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
                "paid_to_account_currency": frappe.get_value(
                    "Account", paid_to_account, "account_currency"
                ),
                "paid_amount": amount_paid,
                "received_amount": amount_paid,
                "reference_no": txn_id,
                "reference_date": frappe.utils.nowdate(),
                "references": [
                    {
                        "reference_doctype": "Sales Invoice",
                        "reference_name": invoice_name,
                        "total_amount": invoice.grand_total,
                        "outstanding_amount": invoice.outstanding_amount,
                        "allocated_amount": amount_paid,
                    }
                ],
            }
        )
        payment_entry.setup_party_account_field()
        payment_entry.set_missing_values()
        payment_entry.set_exchange_rate()
        payment_entry.set_amounts()
        payment_entry.flags.ignore_permissions = True
        payment_entry.save(ignore_permissions=True)
        payment_entry.submit()
        return payment_entry.name

    except Exception:
        frappe.db.rollback()
        frappe.log_error("Payment Entry Failed", frappe.get_traceback())
        raise
