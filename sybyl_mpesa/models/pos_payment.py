# -*- coding: utf-8 -*-
from odoo import models, api


class PosOrder(models.Model):
    _inherit = "pos.order"

    @api.model
    def _payment_fields(self, order, ui_paymentline):
        fields = super()._payment_fields(order, ui_paymentline)
        fields.update({"mpesa_receipt": ui_paymentline.get("mpesa_receipt")})
        return fields
