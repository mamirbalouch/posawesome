# -*- coding: utf-8 -*-
# Copyright (c) 2020, Youssef Restom and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import json
import frappe
from frappe.utils import nowdate, flt
from frappe import _
from erpnext.accounts.party import get_party_bank_account
from erpnext.accounts.doctype.payment_entry.payment_entry import (
    reconcile_against_document,
    reconcile_dr_cr_note,
)
from erpnext.accounts.doctype.payment_request.payment_request import (
    get_dummy_message,
    get_existing_payment_request_amount,
)
from posawesome.posawesome.api.utilities import ensure_child_doctype


def get_posawesome_credit_redeem_remark(invoice_name):
    return _("POS Awesome credit redemption for Sales Invoice {0}").format(invoice_name)


@frappe.whitelist()
def create_payment_request(doc):
    doc = json.loads(doc)
    for pay in doc.get("payments"):
        if pay.get("type") == "Phone":
            if pay.get("amount") <= 0:
                frappe.throw(_("Payment amount cannot be less than or equal to 0"))

            if not doc.get("contact_mobile"):
                frappe.throw(_("Please enter the phone number first"))

            pay_req = get_existing_payment_request(doc, pay)
            if not pay_req:
                pay_req = get_new_payment_request(doc, pay)
                pay_req.submit()
            else:
                pay_req.request_phone_payment()

            return pay_req


def get_new_payment_request(doc, mop):
    payment_gateway_account = frappe.db.get_value(
        "Payment Gateway Account",
        {
            "payment_account": mop.get("account"),
        },
        ["name"],
    )

    args = {
        "dt": "Sales Invoice",
        "dn": doc.get("name"),
        "recipient_id": doc.get("contact_mobile"),
        "mode_of_payment": mop.get("mode_of_payment"),
        "payment_gateway_account": payment_gateway_account,
        "payment_request_type": "Inward",
        "party_type": "Customer",
        "party": doc.get("customer"),
        "return_doc": True,
    }
    return make_payment_request(**args)


def get_payment_gateway_account(args):
    return frappe.db.get_value(
        "Payment Gateway Account",
        args,
        ["name", "payment_gateway", "payment_account", "message"],
        as_dict=1,
    )


def get_existing_payment_request(doc, pay):
    payment_gateway_account = frappe.db.get_value(
        "Payment Gateway Account",
        {
            "payment_account": pay.get("account"),
        },
        ["name"],
    )

    args = {
        "doctype": "Payment Request",
        "reference_doctype": "Sales Invoice",
        "reference_name": doc.get("name"),
        "payment_gateway_account": payment_gateway_account,
        "email_to": doc.get("contact_mobile"),
    }
    pr = frappe.db.exists(args)
    if pr:
        return frappe.get_doc("Payment Request", pr)


def make_payment_request(**args):
    """Make payment request"""

    args = frappe._dict(args)

    ref_doc = frappe.get_doc(args.dt, args.dn)
    gateway_account = get_payment_gateway_account(args.get("payment_gateway_account"))
    if not gateway_account:
        frappe.throw(_("Payment Gateway Account not found"))

    grand_total = get_amount(ref_doc, gateway_account.get("payment_account"))
    if args.loyalty_points and args.dt == "Sales Order":
        from erpnext.accounts.doctype.loyalty_program.loyalty_program import (
            validate_loyalty_points,
        )

        loyalty_amount = validate_loyalty_points(ref_doc, int(args.loyalty_points))
        frappe.db.set_value(
            "Sales Order",
            args.dn,
            "loyalty_points",
            int(args.loyalty_points),
            update_modified=False,
        )
        frappe.db.set_value(
            "Sales Order",
            args.dn,
            "loyalty_amount",
            loyalty_amount,
            update_modified=False,
        )
        grand_total = grand_total - loyalty_amount

    bank_account = (
        get_party_bank_account(args.get("party_type"), args.get("party")) if args.get("party_type") else ""
    )

    existing_payment_request = None
    if args.order_type == "Shopping Cart":
        existing_payment_request = frappe.db.get_value(
            "Payment Request",
            {
                "reference_doctype": args.dt,
                "reference_name": args.dn,
                "docstatus": ("!=", 2),
            },
        )

    if existing_payment_request:
        frappe.db.set_value(
            "Payment Request",
            existing_payment_request,
            "grand_total",
            grand_total,
            update_modified=False,
        )
        pr = frappe.get_doc("Payment Request", existing_payment_request)
    else:
        if args.order_type != "Shopping Cart":
            existing_payment_request_amount = get_existing_payment_request_amount(args.dt, args.dn)

            if existing_payment_request_amount:
                grand_total -= existing_payment_request_amount

        pr = frappe.new_doc("Payment Request")
        pr.update(
            {
                "payment_gateway_account": gateway_account.get("name"),
                "payment_gateway": gateway_account.get("payment_gateway"),
                "payment_account": gateway_account.get("payment_account"),
                "payment_channel": gateway_account.get("payment_channel"),
                "payment_request_type": args.get("payment_request_type"),
                "currency": ref_doc.currency,
                "grand_total": grand_total,
                "mode_of_payment": args.mode_of_payment,
                "email_to": args.recipient_id or ref_doc.owner,
                "subject": _("Payment Request for {0}").format(args.dn),
                "message": gateway_account.get("message") or get_dummy_message(ref_doc),
                "reference_doctype": args.dt,
                "reference_name": args.dn,
                "party_type": args.get("party_type") or "Customer",
                "party": args.get("party") or ref_doc.get("customer"),
                "bank_account": bank_account,
            }
        )

        if args.order_type == "Shopping Cart" or args.mute_email:
            pr.flags.mute_email = True

        pr.insert(ignore_permissions=True)
        if args.submit_doc:
            pr.submit()

    if args.order_type == "Shopping Cart":
        frappe.db.commit()
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = pr.get_payment_url()

    if args.return_doc:
        return pr

    return pr.as_dict()


def get_amount(ref_doc, payment_account=None):
    """get amount based on doctype"""
    grand_total = 0
    for pay in ref_doc.payments:
        if pay.type == "Phone" and pay.account == payment_account:
            grand_total = pay.amount
            break

    if grand_total > 0:
        return grand_total

    else:
        frappe.throw(_("Payment Entry is already created or payment account is not matched"))


def redeeming_customer_credit(invoice_doc, data, is_payment_entry, total_cash, cash_account, payments):
    if not flt(data.get("redeemed_customer_credit")):
        return

    frappe.flags.ignore_account_permission = True

    for row in data.get("customer_credit_dict"):
        credit_to_redeem = flt(row.get("credit_to_redeem"))
        if not credit_to_redeem:
            continue

        if row.get("type") == "Advance":
            frappe.call(
                "erpnext.accounts.doctype.payment_entry.payment_entry.reconcile_against_document",
                {
                    "dt": "Payment Entry",
                    "dn": row.get("credit_origin"),
                    "against_vouchers": [
                        {
                            "voucher_type": invoice_doc.doctype,
                            "voucher_no": invoice_doc.name,
                            "due_date": invoice_doc.due_date,
                            "invoice_amount": invoice_doc.grand_total,
                            "outstanding_amount": invoice_doc.outstanding_amount,
                            "allocated_amount": credit_to_redeem,
                        }
                    ],
                },
            )
        elif row.get("type") == "Invoice":
            frappe.call(
                "erpnext.accounts.doctype.payment_entry.payment_entry.reconcile_dr_cr_note",
                {
                    "dr_note": row.get("credit_origin"),
                    "cr_note": invoice_doc.name,
                    "amount": credit_to_redeem,
                    "type": "Sales Invoice",
                },
            )


@frappe.whitelist()
def get_available_credit(customer, company):
    total_credit = []

    outstanding_invoices = frappe.get_all(
        "Sales Invoice",
        {
            "outstanding_amount": ["<", 0],
            "docstatus": 1,
            "customer": customer,
            "company": company,
        },
        ["name", "outstanding_amount", "is_return"],
    )

    allocations = {}
    invoice_names = [row.name for row in outstanding_invoices]
    if invoice_names:
        placeholders = ", ".join(["%s"] * len(invoice_names))
        payment_allocations = frappe.db.sql(
            f"""
                select
                    per.reference_name,
                    sum(per.allocated_amount) as allocated_amount
                from `tabPayment Entry Reference` per
                inner join `tabPayment Entry` pe on pe.name = per.parent
                where per.reference_doctype = 'Sales Invoice'
                    and per.reference_name in ({placeholders})
                    and pe.docstatus = 1
                    and pe.payment_type = 'Pay'
                group by per.reference_name
            """,
            invoice_names,
            as_dict=True,
        )

        allocations = {
            row.reference_name: flt(row.allocated_amount) for row in payment_allocations
        }

    for row in outstanding_invoices:
        outstanding_amount = -(row.outstanding_amount)
        cash_paid = allocations.get(row.name, 0)
        remaining_credit = flt(outstanding_amount - cash_paid)

        if remaining_credit <= 0:
            continue

        row = {
            "type": "Invoice",
            "credit_origin": row.name,
            "total_credit": remaining_credit,
            "credit_to_redeem": 0,
            "source_type": "Sales Return" if row.is_return else "Sales Invoice",
        }

        total_credit.append(row)

    advances = frappe.get_all(
        "Payment Entry",
        {
            "unallocated_amount": [">", 0],
            "party_type": "Customer",
            "party": customer,
            "company": company,
            "docstatus": 1,
        },
        ["name", "unallocated_amount"],
    )

    for row in advances:
        row = {
            "type": "Advance",
            "credit_origin": row.name,
            "total_credit": row.unallocated_amount,
            "credit_to_redeem": 0,
            "source_type": "Payment Entry",
        }

        total_credit.append(row)

    return total_credit
