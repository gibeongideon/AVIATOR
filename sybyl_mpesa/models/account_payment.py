# -*- coding: utf-8 -*-


import logging

from odoo import models, api

_logger = logging.getLogger(__name__)


class AccountPayment(models.Model):
    _inherit = "account.payment"

    @api.constrains("payment_method_line_id")
    def _check_payment_method_line_id(self):
        """todo the 'payment_method_line_id' field is not null. Need to fix"""
        for pay in self:
            _logger.info(pay)
            _logger.info(pay.payment_method_line_id)
