# -*- coding: utf-8 -*-

from odoo import models, fields


class MpesaLog(models.Model):
    _name = "mpesa.log"
    _description = "Mpesa Log"
    _order = "id desc"

    name = fields.Text(string="Request")
    response = fields.Text()
    status_code = fields.Text()
    checkout_request_id = fields.Text()
    payment_transaction_id = fields.Many2one("payment.transaction", readonly=True)
    result_code = fields.Text()
    response_error = fields.Text()
    