import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from erpnext import get_default_company

def setup_razorpay():
    """
    Setup Razorpay Mode of Payment for the default/first company only
    """

    try:
        default_company = get_default_company() or frappe.get_all("Company", limit=1)[0].name
        if not default_company:
            return

        custom_fields = {
            "Razorpay Settings": [
                dict(
                    fieldname="enable_payment_links",
                    label="Enable Payment Links",
                    default="1",
                    fieldtype="Check",
                    insert_after="redirect_to",
                ),
                dict(
                    fieldname="webhook_secret",
                    label="Webhook Secret",
                    fieldtype="Password",
                    insert_after="enable_payment_links",
                )
            ]
        }
        create_custom_fields(custom_fields, ignore_validate=True)

        payment_gateway = frappe.db.exists("Payment Gateway", "Razorpay")
        if not payment_gateway:
            frappe.log_error(
                title="Payment Gateway Missing",
                message="Razorpay Payment Gateway not found. Please setup 'Razorpay Settings' first."
            )
            return

        razorpay_account =  frappe.db.get_value(
            "Payment Gateway Account",
            {"payment_gateway": payment_gateway, "company": default_company},
            "payment_account"
        )

        if not frappe.db.exists("Mode of Payment", "Razorpay"):
            frappe.get_doc({
                "doctype": "Mode of Payment",
                "mode_of_payment": "Razorpay",
                "enabled": 1,
                "type": "Bank",
                "Mode of Payment Account": [{
                    "company": default_company,
                    "default_account": razorpay_account
                }]
            }).insert(ignore_permissions=True)

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Setup Failed ❌")
        raise
