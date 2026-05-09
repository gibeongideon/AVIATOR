# -*- coding: utf-8 -*-

from odoo import models


class PosSession(models.Model):
    _inherit = "pos.session"

    def _loader_params_pos_payment_method(self):
        result = super()._loader_params_pos_payment_method()
        result["search_params"]["fields"].extend(
            [
                "mpesa_secrete_key",
                "mpesa_customer_key",
                "mpesa_short_code",
                "mpesa_pass_key",
                "mpesa_test_mode",
            ]
        )
        return result
