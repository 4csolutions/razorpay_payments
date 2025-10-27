frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (frm.doc.docstatus === 1 && frm.doc.custom_razorpay_payment_link_id) {
            frm.add_custom_button("Resend Payment Link", () => {
                frappe.call({
                    method: "razorpay_payments.razorpay_payments.api.resend_payment_link",
                    args: {
                        invoice_name: frm.doc.name,
                        via: "sms"
                    },
                    callback: () => {
                        frappe.show_alert("✅ Link Resent!");
                    }
                });
            });
        }
    }
});
