# coding: utf-8
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    module_sybyl_mpesa = fields.Boolean(
        string="MPesa Payment Terminal",
        help="The transactions are processed by MPesa. Set your MPesa credentials on the related payment method.",
    )
