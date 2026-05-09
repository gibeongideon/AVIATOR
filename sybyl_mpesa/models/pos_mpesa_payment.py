# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class PosMpesaPayment(models.Model):
    _name = "pos.mpesa.payment"
    _description = "POS Mpesa Payments"
    _order = "id desc"

    ref = fields.Char(string="Paybill/ShortCode", copy=False, index=True)
    active = fields.Boolean(default=True)
    response_datas = fields.Text()
    datas = fields.Text(string="Json Datas")
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Reconciled"), ("cancel", "Cancelled")],
        default="draft",
    )

    currency_id = fields.Many2one(
        "res.currency", compute="_compute_company_currency", readonly=True
    )
    amount = fields.Monetary(currency_field="currency_id")
    balance = fields.Monetary(currency_field="currency_id")

    # Ref. no. based fields
    transaction_type = fields.Char(default="STK Push")
    trans_id = fields.Char(string="TransID")
    trans_time = fields.Char(string="TransTime")
    business_shortcode = fields.Char(string="Business ShortCode")
    bill_ref_number = fields.Char(string="BillRefNumber")
    invoice_number = fields.Char(string="InvoiceNumber")
    org_account_balance = fields.Char()
    third_party_transid = fields.Char(string="ThirdPartyTransID")
    msisdn = fields.Char(string="MSISDN")
    first_name = fields.Char(string="FirstName")
    middle_name = fields.Char(string="MiddleName")
    last_name = fields.Char(string="LastName")

    checkout_request_id = fields.Char(string="Checkout Request ID")
    is_pos_request = fields.Boolean("POS")
    customer_message = fields.Text()
    response_code = fields.Text()
    response_description = fields.Text()
    result_description = fields.Text()
    result_code = fields.Text()
    merchant_request_id = fields.Char()
    receipt_number = fields.Char(string="Receipt No.")
    receipt_date = fields.Datetime(readonly=True)
    phone_number = fields.Char(string="Phone")
    partner_id = fields.Many2one("res.partner", string="Customer")
    payment_method_id = fields.Many2one("pos.payment.method", string="Payment Method")
    payment_date = fields.Datetime(
        required=True, readonly=True, default=lambda self: fields.Datetime.now()
    )
    order_number = fields.Char("Order Reference No")
    pos_order_id = fields.Many2one("pos.order", compute="_compute_pos_order_id")
    reconciled_amt = fields.Float("Reconciled Amount")

    @api.depends("checkout_request_id", "trans_id")
    def _compute_display_name(self):
        for record in self:
            if record.checkout_request_id:

                record.display_name = record.checkout_request_id
            elif record.trans_id:
                record.display_name = record.trans_id
            else:
                record.display_name = record.merchant_request_id

    @api.depends("first_name", "middle_name", "last_name")
    def name_get(self):
        res = []
        for rec in self:
            name = (
                "["
                + (rec.checkout_request_id or "")
                + "] "
                + (rec.first_name or "")
                + " "
                + (rec.middle_name or "")
                + " "
                + (rec.last_name or "")
            )
            res.append((rec.id, name))
        return res

    def _compute_company_currency(self):
        for record in self:
            record.currency_id = record.env.company.currency_id

    def _compute_pos_order_id(self):
        for record in self:
            if record.order_number:
                pos_reference = "%" + record.order_number
                record.pos_order_id = self.env["pos.order"].search(
                    [("pos_reference", "ilike", pos_reference)]
                )
            else:
                record.pos_order_id = None

    @api.model
    def search_reference_no(
        self, transaction_id, order_number, config, amount, payment_method_id
    ):
        pos_mpesa_payment_id = self.env["pos.mpesa.payment"].search(
            [("trans_id", "=", transaction_id), ("state", "=", "draft")], limit=1
        )
        if not pos_mpesa_payment_id:
            pos_mpesa_payment_id = self.env["pos.mpesa.payment"].search(
                [("trans_id", "=", transaction_id), ("state", "=", "done")], limit=1
            )
            if pos_mpesa_payment_id:
                return ["error", "Transaction ID already utilized."]
        if pos_mpesa_payment_id and pos_mpesa_payment_id.amount >= amount:
            pos_mpesa_payment_id.write(
                {
                    "state": "done",
                    "order_number": order_number,
                    "reconciled_amt": amount,
                    "payment_method_id": payment_method_id,
                }
            )
            return ["done", amount]
        elif pos_mpesa_payment_id and pos_mpesa_payment_id.amount < amount:
            pos_mpesa_payment_id.write(
                {
                    "state": "done",
                    "order_number": order_number,
                    "reconciled_amt": amount,
                    "payment_method_id": payment_method_id,
                }
            )
            return ["done", pos_mpesa_payment_id.amount]
        else:
            return ["error", "Transaction ID not found."]
