import frappe


def setup_razorpay():
    """
    Setup Razorpay Mode of Payment for the default/first company only
    """
    
    try:
        # Step 1: Get default company or first company
        default_company = frappe.db.get_single_value("Global Defaults", "default_company")
        
        if not default_company:
            first_company = frappe.db.get_all("Company", limit=1)
            if not first_company:
                frappe.log_error("No company found in system", "Setup Failed")
                return
            default_company = first_company[0].name
        
        frappe.log_error(
            title="Working with Company",
            message=f"Company: {default_company}"
        )
        
        # Step 2: Check if Razorpay account already exists
        razorpay_account = frappe.db.get_value(
            "Account",
            {
                "account_name": "Razorpay",
                "company": default_company,
                "account_type": "Bank"
            }
        )
        
        # Step 3: If account doesn't exist, create it
        if not razorpay_account:
            frappe.log_error(
                title="Creating Account",
                message=f"Razorpay account not found. Creating..."
            )
            razorpay_account = create_razorpay_account(default_company)
        else:
            frappe.log_error(
                title="Account Exists",
                message=f"Account {razorpay_account} already exists"
            )
        
        if not razorpay_account:
            frappe.log_error(
                title="Account Creation Failed",
                message=f"Could not create Razorpay account"
            )
            return
        
        # Step 4: Check if Mode of Payment exists, if not create it
        if not frappe.db.exists("Mode of Payment", "Razorpay"):
            mode_of_payment = frappe.get_doc({
                "doctype": "Mode of Payment",
                "mode_of_payment": "Razorpay",
                "enabled": 1,
                "type": "Bank"
            })
            mode_of_payment.insert(ignore_permissions=True)
            frappe.log_error("Razorpay Mode of Payment created", "Setup")
        
        # Step 5: Check if already linked to this company
        existing = frappe.db.exists(
            "Mode of Payment Account",
            {"parent": "Razorpay", "company": default_company}
        )
        
        if existing:
            frappe.log_error(
                title="Already Linked",
                message=f"Razorpay already configured for {default_company}"
            )
            return
        
        # Step 6: Get the Mode of Payment document and add account
        mode_of_payment = frappe.get_doc("Mode of Payment", "Razorpay")
        
        # Add new row to accounts table
        mode_of_payment.append("accounts", {
            "company": default_company,
            "default_account": razorpay_account
        })
        
        # ✅ SAVE the parent document
        mode_of_payment.save(ignore_permissions=True)
        frappe.db.commit()
        
        frappe.log_error(
            title="Setup Complete ✅",
            message=f"Company: {default_company}\nAccount: {razorpay_account}\nMode of Payment: Razorpay linked"
        )
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Setup Failed ❌")
        raise


def create_razorpay_account(company_name):
    """
    Create Razorpay bank account for a company
    Frappe automatically adds company abbr: Razorpay - {abbr}
    """
    try:
        # Get the parent account (Bank account group)
        bank_parent_account = frappe.db.get_value(
            "Account",
            {
                "account_type": "Bank",
                "is_group": 1,
                "company": company_name
            }
        )
        
        if not bank_parent_account:
            frappe.log_error(
                title="Parent Account Not Found",
                message=f"No Bank group account found for {company_name}"
            )
            return None
        
        # Create Razorpay account (Frappe auto-adds abbr)
        razorpay_account = frappe.get_doc({
            "doctype": "Account",
            "account_name": "Razorpay",
            "parent_account": bank_parent_account,
            "company": company_name,
            "account_type": "Bank",
            "is_group": 0
        })
        
        razorpay_account.insert(ignore_permissions=True)
        frappe.db.commit()
        
        frappe.log_error(
            title="Account Created ✅",
            message=f"Account: {razorpay_account.name}\nCompany: {company_name}"
        )
        
        return razorpay_account.name
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Account Creation Failed")
        return None
