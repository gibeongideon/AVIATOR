# -*- coding: utf-8 -*-
import base64
import json
import logging
import time
import re

import requests
from odoo import models, fields, api, _
from ..controllers.main import PosMpesaController
from odoo.exceptions import ValidationError
from odoo.http import request
from requests.auth import HTTPBasicAuth
from requests.exceptions import HTTPError
from werkzeug import urls

_logger = logging.getLogger(__name__)


class PosPaymentMethod(models.Model):
    _inherit = "pos.payment.method"

    def _get_payment_terminal_selection(self):
        return super(PosPaymentMethod, self)._get_payment_terminal_selection() + [
            ("mpesa", "Mpesa")
        ]

    mpesa_payment_provider_id = fields.Many2one("payment.provider")
    mpesa_secrete_key = fields.Char(
        "Secret key", related="mpesa_payment_provider_id.mpesa_online_consumer_secret"
    )
    mpesa_customer_key = fields.Char(
        "Customer key", related="mpesa_payment_provider_id.mpesa_online_consumer_key"
    )
    mpesa_short_code = fields.Char(
        "Shortcode", related="mpesa_payment_provider_id.mpesa_online_service_number"
    )
    mpesa_pass_key = fields.Char(
        "Pass key", related="mpesa_payment_provider_id.mpesa_online_passkey"
    )
    mpesa_call_url = fields.Char(
        "Callback URL", default=lambda self: self.get_callback()
    )
    mpesa_test_mode = fields.Boolean(
        compute="_compute_mpesa_test_mode",
        help="Run transactions in the test environment.",
    )

    def _compute_mpesa_test_mode(self):
        for record in self:
            if record.mpesa_payment_provider_id.state == "test":
                record.mpesa_test_mode = True
            else:
                record.mpesa_test_mode = False

    @api.onchange("use_payment_terminal")
    def _onchange_use_payment_terminal(self):
        super(PosPaymentMethod, self)._onchange_use_payment_terminal()
        if self.use_payment_terminal != "mpesa":
            self.mpesa_payment_provider_id = False
        else:
            if not self.mpesa_payment_provider_id:
                mpesa_payment_provider_ids = self.env["payment.provider"].search(
                    [
                        ("code", "=", "mpesa_online"),
                        ("mpesa_pos_payment_method_ids", "=", False),
                    ],
                )
                self.mpesa_payment_provider_id = (
                    mpesa_payment_provider_ids[0]
                    if mpesa_payment_provider_ids
                    else None
                )

    @api.onchange("mpesa_payment_provider_id")
    def onchange_mpesa_terminal_identifier(self):
        for payment_method in self:
            if not payment_method.mpesa_payment_provider_id:
                continue
            existing_payment_method = self.search(
                [
                    ("id", "!=", payment_method._origin.id),
                    (
                        "mpesa_payment_provider_id",
                        "=",
                        payment_method.mpesa_payment_provider_id.id,
                    ),
                ],
                limit=1,
            )
            if existing_payment_method:
                raise ValidationError(
                    _("Terminal '%s' is already used on payment method '%s'.")
                    % (
                        payment_method.mpesa_payment_provider_id.display_name,
                        existing_payment_method.display_name,
                    )
                )

    @api.constrains("mpesa_payment_provider_id")
    def _check_mpesa_terminal_identifier(self):
        for payment_method in self:
            if not payment_method.mpesa_payment_provider_id:
                continue
            existing_payment_method = self.search(
                [
                    ("id", "!=", payment_method.id),
                    (
                        "mpesa_payment_provider_id",
                        "=",
                        payment_method.mpesa_payment_provider_id.id,
                    ),
                ],
                limit=1,
            )
            if existing_payment_method:
                payment_method.mpesa_payment_provider_id = False

    @api.model
    def get_latest_mpesa_status(
        self,
        short_code,
        pass_key,
        customer_key,
        secrete_key,
        checkout_request_id,
        test_mode,
    ):
        """FIXME: Do we know exactly which payment_method_id we want"""
        values = {}
        url = (
            "https://api.safaricom.co.ke/mpesa/stkpushquery/v1/query"
            if not test_mode
            else "https://sandbox.safaricom.co.ke/mpesa/stkpushquery/v1/query"
        )
        time_stamp, password = self.get_timestamp_passkey(short_code, pass_key)
        values.update(
            {
                "BusinessShortCode": short_code,
                "Password": password,
                "Timestamp": time_stamp,
                "CheckoutRequestID": checkout_request_id,
            }
        )
        _logger.info(values)
        headers = {
            "Authorization": "Bearer %s"
            % self._mpesa_get_access_token(customer_key, secrete_key, test_mode)
        }
        _logger.info(headers)
        resp = requests.post(url, json=values, headers=headers, timeout=30)
        _logger.info("resp :")
        _logger.info(resp)
        _logger.info("resp.text :")
        _logger.info(resp.text)
        resp = resp.json()
        pos_mpesa_payment = (
            request.env["pos.mpesa.payment"]
            .sudo()
            .search([("checkout_request_id", "=", checkout_request_id)], limit=1)
        )
        _logger.info("search : pos_mpesa_payment")
        _logger.info(pos_mpesa_payment)
        result_code = resp.get("ResultCode")
        state = "done" if result_code == "0" else "cancel"
        if pos_mpesa_payment:
            pos_mpesa_payment.write(
                {
                    "result_code": result_code,
                    "response_description": resp.get("ResponseDescription"),
                    "result_description": resp.get("ResultDesc"),
                    "response_datas": resp,
                    "state": state,
                }
            )
        return resp

    def format_phone_number(self, phone_number):
        # Remove any non-digit characters
        phone_number = re.sub(r"\D", "", phone_number)
        # Remove leading zero if present
        if phone_number.startswith("0"):
            phone_number = phone_number[1:]
        # Add the country code
        formatted_number = f"254{phone_number}"
        return formatted_number

    @api.model
    def mpesa_stk_push(self, data, test_mode, s_id, partner_id=False):
        url = (
            "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
            if not test_mode
            else "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
        )
        payment_method = self.browse(s_id)
        short_code = payment_method.mpesa_short_code or False
        time_stamp, password = self.get_timestamp_passkey(
            short_code, payment_method.mpesa_pass_key or False
        )
        full_phone = self.format_phone_number(data.get("phone"))
        values = {
            "BusinessShortCode": short_code,
            "Password": password,
            "Timestamp": time_stamp,
            "CallBackURL": payment_method.mpesa_call_url,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(round(data["amount"])),
            "PartyA": full_phone,
            "PartyB": short_code,
            "PhoneNumber": full_phone,
            "AccountReference": data.get("shop_name"),
            "TransactionDesc": data.get("order_id"),
        }

        headers = {
            "Authorization": "Bearer %s"
            % self._mpesa_get_access_token(
                payment_method.mpesa_customer_key or False,
                payment_method.mpesa_secrete_key or False,
                test_mode,
            )
        }
        _logger.info(url)
        _logger.info("values : %s", values)
        resp = requests.post(url, json=values, headers=headers, timeout=30)
        if not resp.ok:
            try:
                resp.raise_for_status()
            except HTTPError:
                _logger.info("resp.text")
                _logger.info(resp.text)
                mpesa_error = resp.json().get("errorMessage", {})
                error_msg = " " + (
                    _("MPesa gave us the following info about the problem: '%s'")
                    % mpesa_error
                )
                _logger.error(error_msg)
                return resp.json()
        _logger.info(resp.json())
        self = self.browse(data.get("payment_method_id"))
        self.mpesa_create_transaction(values, resp.json(), partner_id)
        _logger.info("resp")
        _logger.info(resp)
        _logger.info(resp.json())
        return resp.json()

    def get_timestamp_passkey(self, short_code, pass_key):
        time_stamp = str(time.strftime("%Y%m%d%H%M%S"))
        if not short_code or not pass_key:
            raise ValidationError(_("Please check the configuration!"))
        passkey = short_code + pass_key + time_stamp
        password = str(base64.b64encode(passkey.encode("utf-8")), "utf-8")
        return time_stamp, password

    def get_callback(self):
        base_url = self.get_base_url()
        if "127.0.0.1" in base_url:
            base_url = "https://pos.sybylcloud.com/"
        return urls.url_join(base_url, PosMpesaController._callback_url)

    def _mpesa_get_access_token(self, customer_key, secrete_key, test_mode):
        url = (
            "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
            if not test_mode
            else "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
        )
        response = requests.get(
            url, auth=HTTPBasicAuth(customer_key, secrete_key), timeout=30
        )
        _logger.info(customer_key)
        _logger.info(secrete_key)
        _logger.info(response)
        if response.text:
            json_data = json.loads(response.text)
        else:
            return False
        _logger.info(json_data)
        return json_data["access_token"]

    def get_base_url(self):
        url = ""
        if request:
            url = request.httprequest.url_root
        return url or self.env["ir.config_parameter"].sudo().get_param("web.base.url")

    def mpesa_create_transaction(self, values, resp, partner_id=False):
        _logger.info("mpesa_create_transaction...")
        _logger.info("values :%s", values)
        _logger.info("resp :%s", resp)
        if partner_id:
            partner_id = partner_id.get("id")
        self.env["pos.mpesa.payment"].sudo().create(
            {
                "amount": values["Amount"],
                "checkout_request_id": resp.get("CheckoutRequestID"),
                "customer_message": resp.get("CustomerMessage"),
                "response_description": resp.get("ResponseDescription"),
                "merchant_request_id": resp.get("MerchantRequestID"),
                "partner_id": partner_id,
                "phone_number": values["PartyA"],
                "payment_method_id": self.id,
                "is_pos_request": True,
            }
        )

    def button_open_pos_mpesa_payment(self):
        self.ensure_one()
        return {
            "name": _("MPesa Transaction"),
            "type": "ir.actions.act_window",
            "res_model": "pos.mpesa.payment",
            "view_mode": "tree,form",
            "context": {
                "create": False,
                "group_by": ["create_date:day", "transaction_type"],
            },
            "domain": [("payment_method_id", "=", self.id)],
        }

    def button_open_pos_payment(self):
        self.ensure_one()
        return {
            "name": _("POS Payment"),
            "type": "ir.actions.act_window",
            "res_model": "pos.payment",
            "context": {"create": False},
            "view_mode": "tree",
            "domain": [("payment_method_id", "=", self.id)],
        }


class PosPayment(models.Model):
    _inherit = "pos.payment"

    mpesa_receipt = fields.Char(string="Mpesa Receipt No.")
