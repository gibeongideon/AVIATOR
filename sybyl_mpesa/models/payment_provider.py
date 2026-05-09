# -*- coding: utf-8 -*-
# pylint: disable=except-pass
import base64
import logging
from datetime import timedelta

import requests
from odoo.exceptions import ValidationError
from requests.auth import HTTPBasicAuth

from odoo import _, fields, models, api
from ..mpesa import c2b
from ..mpesa.mpesa_error import MpesaResponseCode

_logger = logging.getLogger(__name__)


class MpesaOnlineAcquirer(models.Model):
    """inherited to add mpesa features"""

    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[("mpesa_online", "Lipa Na Mpesa Online")],
        ondelete={"mpesa_online": "set default"},
    )
    mpesa_pos_payment_method_ids = fields.One2many(
        "pos.payment.method", "mpesa_payment_provider_id"
    )
    mpesa_online_currency_id = fields.Many2one(
        "res.currency",
        "M-PESA Currency",
        required_if_provider="mpesa_online",
        default=lambda self: self.env.ref("base.KES").id,
        help="The M-PESA currency. Default is KES. \n If the sales order is in a different "
        "currency other than the M-PESA currency, \nit has to be converted to the M-PESA currency",
    )
    mpesa_online_service_name = fields.Char(
        "Service Name",
        required_if_provider="mpesa_online",
        help="Enter the mobile money service name,e.g  MPESA if safaricom.\
                This will appear in E-commerce website ",
    )
    mpesa_online_service_number = fields.Char(
        "Service Number",
        required_if_provider="mpesa_online",
        help="Enter the mobile money service number or shortcode e.g the Till number\
                or Pay bill number if MPESA, this will appear in E-commerce website for your customers to use",
    )
    mpesa_online_dp = fields.Integer(
        "Decimal Precision",
        default=0,
        help="This is the decimal precision to be used when \
                checking if customer paid exact,higher or less than the order amount. \
                Default is zero meaning the paid amount and order amount are rounded up to\
                the nearest 'ones' by default..i.e no checking of decimals (cents) in comparing the paid \
                amount vs the sales order amount",
    )

    mpesa_online_passkey = fields.Char(
        "MPESA Passkey",
        required_if_provider="mpesa_online",
        default="bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919",
    )
    mpesa_online_resource_url = fields.Char(
        "Resource URL",
        required_if_provider="mpesa_online",
        default="https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
    )
    mpesa_online_access_token_url = fields.Char(
        "Access Token URL",
        required_if_provider="mpesa_online",
        default="https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials",
    )
    mpesa_online_callback_url = fields.Char(
        "Callback URL",
        required_if_provider="mpesa_online",
        default=lambda self: self.env["ir.config_parameter"].get_param(
            "web.base.url", ""
        )
        + "/mpesa_express",
    )
    mpesa_online_consumer_key = fields.Char(
        "Consumer Key", required_if_provider="mpesa_online"
    )
    mpesa_online_consumer_secret = fields.Char(
        "Consumer Secret", required_if_provider="mpesa_online"
    )
    mpesa_online_access_token = fields.Char("M-PESA Access Token", readonly=True)
    mpesa_online_token_expiry_date = fields.Datetime(
        "Token Expiry Date",
        default=lambda self: fields.Datetime.now(),
        readonly=True,
        help="This date and time will automatically be updated \n\
                every time the system gets a new token from mpesa API",
    )

    # C2B / Collection settings
    c2b_active_flag = fields.Selection(
        [("connected", "Connected"), ("disconnected", "Disconnected")],
        string="C2b Status",
        default="disconnected",
    )
    c2b_callback_active = fields.Selection(
        [("none", "Select"), ("yes", "Yes"), ("no", "No")],
        string="C2B Callback active",
        default="yes",
    )
    c2b_callback_http_method = fields.Selection(
        [("post", "HTTP POST"), ("get", "HTTP GET")],
        string="C2B Callback HTTP method",
        default="get",
    )
    c2b_callback_url = fields.Char(string="C2B Callback url")
    c2b_extra_parameter = fields.Char(string="C2B extra parameters (JSON)")
    c2b_validation_url = fields.Char(string="Validation Url")
    c2b_confirmation_url = fields.Char(string="Confirmation Url")

    @api.onchange("code")
    def _onchange_code(self):
        if self.code == "mpesa_online":
            self.module_id = self.env.ref("base.module_sybyl_mpesa").id

    def action_c2b_connection(self):
        if not self.c2b_confirmation_url:
            raise ValidationError(_("Please add Confirmation URL!"))
        if not self.c2b_validation_url:
            raise ValidationError(_("Please add Validation URL!"))
        flag = True
        if self.state == "test":
            env = "sandbox"
        elif self.state == "enabled":
            env = "production"
        else:
            raise ValidationError(_("Please enable the configuration!"))
        MPESA = c2b.C2B(
            env=env,
            app_key=self.mpesa_online_consumer_key,
            app_secret=self.mpesa_online_consumer_secret,
        )
        response = MPESA.register(
            self.mpesa_online_service_number,
            "Completed",
            self.c2b_confirmation_url,
            self.c2b_validation_url,
        )
        _logger.info(response)
        if response.get("errorCode"):
            error_obj = MpesaResponseCode("C2B_REGISTER")
            message = error_obj.get_c2b_register(response["errorCode"])
            _logger.info(
                "C2bRegisterUrl Error Code Is %s : %s",
                str(response["errorCode"]),
                str(message),
            )
            flag = False
            title = "MPesa C2b Url."
            msg = _(
                "C2b url Error Code Is %s : %s",
                str(response["errorCode"]),
                str(message),
            )
            self.write({"c2b_active_flag": "disconnected"})
            return self.notify("warning", title, msg)
        if response.get("Envelope"):
            flag = False
            title = "MPesa C2b Url."
            msg = "Registered UnSuccessfully."
            self.write({"c2b_active_flag": "disconnected"})
            return self.notify("warning", title, msg)
        _logger.info("C2bRegisterUrl Successfully Register. %s", (str(response)))
        if flag:
            self.write({"c2b_active_flag": "connected"})
            title = "MPesa C2b Url"
            msg = "Registered Successfully."
            return self.notify("success", title, msg)

    def notify(self, type, title, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": type,
                "title": _(title),
                "message": _(message),
                "next": {"type": "ir.actions.act_window_close"},
                "sticky": False,
            },
        }

    def _mpesa_online_get_access_token(self):
        self.ensure_one()
        payload = None
        if (
            not self.mpesa_online_access_token
            or fields.Datetime.now() >= self.mpesa_online_token_expiry_date
        ):
            try:
                res = requests.get(
                    self.mpesa_online_access_token_url,
                    auth=HTTPBasicAuth(
                        self.mpesa_online_consumer_key,
                        self.mpesa_online_consumer_secret,
                    ),
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                _logger.warning("MPESA_ONLINE: %s", exc)
            else:
                if res.status_code == 200:
                    payload = res.json()
                else:
                    msg = _("Cannot fetch access token. Received HTTP Error code ")
                    _logger.warning(
                        "MPESA_ONLINE: "
                        + msg
                        + str(res.status_code)
                        + ", "
                        + res.reason
                        + " url: "
                        + res.url
                    )
            if payload:
                self.write(
                    dict(
                        mpesa_online_access_token=payload.get("access_token"),
                        mpesa_online_token_expiry_date=fields.Datetime.to_string(
                            fields.Datetime.from_string(fields.Datetime.now())
                            + timedelta(seconds=int(payload.get("expires_in")))
                        ),
                    )
                )
        return self.mpesa_online_access_token

    def mpesa_stk_push(self, data):
        """method to be called from payment transaction model when form data is received."""
        self.ensure_one()
        return self._mpesa_online_stk_push(data)

    def _convert_currency(self, data):
        amount = data.get("amount")
        if (
            int(data.get("currency")) != self.mpesa_online_currency_id.id
        ):  # multi-currency support
            amount = (
                self.env["res.currency"]
                .browse([int(data.get("currency"))])
                ._convert(
                    from_amount=float(amount),
                    company=self.company_id,
                    to_currency=self.mpesa_online_currency_id,
                    date=fields.Date.today(),
                )
            )
        return amount

    def _generate_timestamp(self):
        return fields.Datetime.context_timestamp(
            self, timestamp=fields.Datetime.from_string(fields.Datetime.now())
        ).strftime("%Y%m%d%H%M%S")

    def _generate_password(self, timestamp):
        string = (
            self.mpesa_online_service_number + self.mpesa_online_passkey + timestamp
        )
        return base64.b64encode(bytes(string, "latin-1")).decode("utf-8")

    def _log_response(self, jsn, data, amount):
        _logger.info(
            "MPESA_ONLINE: response Code: %s, %s. <Mpesa phone: %s> <amount requested: %s %s> <Order ref: %s>",
            jsn.get("ResponseCode", ""),
            jsn.get("ResponseDescription", ""),
            data.get("mpesa_phone_number"),
            amount,
            self.mpesa_online_currency_id.name,
            data.get("reference"),
        )

    def _update_transaction(self, jsn, data):
        tx_id = data.get("tx_id", False)
        if not tx_id:
            return False
        txn = self.env["payment.transaction"].browse([int(tx_id)])
        if not txn:
            return False
        vals = {
            "mpesa_online_merchant_request_id": jsn.get("MerchantRequestID", False),
            "mpesa_online_checkout_request_id": jsn.get("CheckoutRequestID", False),
            "date_validate": fields.Datetime.now(),
            "state": "pending",
        }
        return txn.write(vals)

    def _handle_error(self, res):
        msg = _("Cannot push request for payment. Received HTTP Error code ")
        _logger.warning(
            "MPESA_ONLINE: "
            + msg
            + str(res.status_code)
            + ", "
            + res.reason
            + " url: "
            + res.url
        )
        try:
            message = res.json()
            code = message.get("errorCode") or message.get("responseCode", None)
            desc = message.get("errorMessage") or message.get("responseDesc", None)
            if code and desc:
                _logger.warning("MPESA_ONLINE: Error code " + code + ": " + desc)
        except BaseException as e:
            _logger.error(e)
            raise

    def _mpesa_online_stk_push(self, data):
        self.ensure_one()
        if self.mpesa_online_resource_url:
            amount = self._convert_currency(data)
            timestamp = self._generate_timestamp()
            body = {
                "BusinessShortCode": self.mpesa_online_service_number,
                "Password": self._generate_password(timestamp),
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(float(amount)),
                "PartyA": data.get("mpesa_phone_number"),
                "PartyB": self.mpesa_online_service_number,
                "PhoneNumber": data.get("mpesa_phone_number"),
                "CallBackURL": data.get("callback_url"),
                "AccountReference": data.get("reference"),
                "TransactionDesc": data.get("reference"),
            }
            try:
                res = requests.post(
                    self.mpesa_online_resource_url,
                    json=body,
                    headers={
                        "Authorization": "Bearer %s"
                        % self._mpesa_online_get_access_token()
                    },
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                _logger.warning("MPESA_ONLINE: %s", exc)
                return False
            else:
                if res.status_code == 200:
                    jsn = res.json()
                    self._log_response(jsn, data, amount)
                    return self._update_transaction(jsn, data)
                else:
                    self._handle_error(res)
                    return False

        return False

    def mpesa_online_form_generate_values(self, values):
        """additional values for the  mpesa express"""
        if not values:
            values = {}
        if self.mpesa_online_callback_url:
            values.update(callback_url=self.mpesa_online_callback_url)
        # MPESA does not support decimal places in amount.
        # if values.get('amount'):
        #    values.update(amount=round(values.get('amount')))
        return values

    def _get_feature_support(self):
        """Get advanced feature support by provider.

        Each provider should add its technical name in the corresponding key for the following features:
        * fees: support payment fees computations
        * authorize: support authorizing payment (separates authorization and capture)
        * tokenize: support saving payment data in a payment.tokenize object"""
        res = super(MpesaOnlineAcquirer, self)._get_feature_support()
        res["fees"].append("mpesa_online")
        return res

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
            "domain": [
                ("payment_method_id", "in", self.mpesa_pos_payment_method_ids.ids)
            ],
        }
