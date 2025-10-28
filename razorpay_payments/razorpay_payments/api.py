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
    frappe.log_error("Webhook Called", "Webhook Start")
    
    try:
        data = frappe.request.data
        payload = json.loads(data.decode("utf-8"))
        
        signature = frappe.get_request_header("X-Razorpay-Signature")
        webhook_secret = "Pass@1234"
        
        # Verify Razorpay signature
        generated_sig = hmac.new(
            webhook_secret.encode("utf-8"), data, hashlib.sha256
        ).hexdigest()
        
        # FIX: Move long content to message body, keep title short
        frappe.log_error(
            title="Signature Verification",
            message=f"Received: {signature}\nGenerated: {generated_sig}\nMatch: {signature == generated_sig}"
        )
        
        if signature != generated_sig:
            frappe.log_error("Signature mismatch", "Webhook Auth Failed")
            return "Invalid signature"
        
        event_type = payload.get("event")
        
        # FIX: Use json.dumps for payload in message, not title
        frappe.log_error(
            title="Webhook Payload",
            message=json.dumps(payload, indent=2)
        )
        
        if event_type not in ["payment_link.paid", "payment.captured"]:
            frappe.log_error(f"Event '{event_type}' not handled", "Webhook Event Skip")
            return "Event not handled"
        
        payment_data = payload.get("payload", {})
        payment = (payment_data.get("payment", {}) or {}).get("entity", {})
        payment_link = (payment_data.get("payment_link", {}) or {}).get("entity", {})
        
        notes = payment.get("notes") or payment_link.get("notes")
        invoice_name = notes.get("invoice_name") if notes else None
        
        if not invoice_name:
            frappe.log_error("Invoice name missing in notes", "Webhook Missing Data")
            return "Invoice name not found"
        
        amount_paid = int(payment.get("amount", 0)) / 100
        txn_id = payment.get("id")
        
        # FIX: Keep title short
        frappe.log_error(
            title="Payment Processing",
            message=f"Invoice: {invoice_name}\nAmount: {amount_paid}\nTxn ID: {txn_id}"
        )
        
        # Prevent duplicate Payment Entry
        if frappe.db.exists("Payment Entry", {"reference_no": txn_id}):
            frappe.log_error("Duplicate payment ignored", "Webhook Duplicate")
            return "Payment Already Recorded"
        
        # USE BACKGROUND JOB
        frappe.enqueue(
            create_payment_entry,
            queue='short',
            timeout=300,
            invoice_name=invoice_name,
            amount_paid=amount_paid,
            txn_id=txn_id,
            enqueue_after_commit=True
        )
        
        frappe.log_error("Payment enqueued successfully", "Webhook Success")
        return "OK"
    
    except Exception as e:
        frappe.log_error(
            title="Webhook Exception",
            message=frappe.get_traceback()
        )
        return "Failed"


def create_payment_entry(invoice_name, amount_paid, txn_id):
    """
    Background job to create payment entry with proper permissions
    """
    try:
        frappe.set_user("Administrator")
        
        # Check for duplicates again (in case of race condition)
        if frappe.db.exists("Payment Entry", {"reference_no": txn_id}):
            frappe.log_error(
                title="Duplicate Check",
                message=f"Payment Entry already exists for txn_id: {txn_id}"
            )
            return
        
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        
        # Get Mode of Payment account
        mode_of_payment = "Razorpay"
        paid_to_account = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mode_of_payment, "company": invoice.company},
            "default_account"
        )
        
        if not paid_to_account:
            frappe.log_error(
                title="Account Missing",
                message=f"No default account set for Mode of Payment: {mode_of_payment} in company: {invoice.company}"
            )
            return
        
        # Get customer's receivable account
        paid_from_account = frappe.get_value(
            "Party Account",
            {"parenttype": "Customer", "parent": invoice.customer, "company": invoice.company},
            "account"
        ) or frappe.get_value("Company", invoice.company, "default_receivable_account")
        
        if not paid_from_account:
            frappe.log_error(
                title="Receivable Account Missing",
                message=f"No receivable account found for customer: {invoice.customer}"
            )
            return
        
        frappe.log_error(
            title="Account Setup",
            message=f"paid_from: {paid_from_account}\npaid_to: {paid_to_account}"
        )
        
        # Create Payment Entry
        payment_entry = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "company": invoice.company,
            "posting_date": frappe.utils.nowdate(),
            "party_type": "Customer",
            "party": invoice.customer,
            "mode_of_payment": mode_of_payment,
            
            # Mandatory accounts
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
        
        # Set missing values
        payment_entry.setup_party_account_field()
        payment_entry.set_missing_values()
        payment_entry.set_exchange_rate()
        payment_entry.set_amounts()
        
        # Insert and submit
        payment_entry.flags.ignore_permissions = True
        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()
        
        frappe.db.commit()
        
        frappe.log_error(
            title="Payment Entry Created",
            message=f"PE: {payment_entry.name}\nInvoice: {invoice_name}\nAmount: {amount_paid}"
        )
        
        return payment_entry.name
        
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(
            title="Payment Entry Failed",
            message=frappe.get_traceback()
        )
        raise


