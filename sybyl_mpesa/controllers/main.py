# -*- coding: utf-8 -*-

import datetime
import json
import logging

from odoo.exceptions import ValidationError
from odoo.http import request
from odoo.tools.misc import DEFAULT_SERVER_DATETIME_FORMAT

import odoo
from odoo import _, fields, http

_logger = logging.getLogger(__name__)


class LipaNaMpesa(http.Controller):
    """Mpesa online routes for callback url and for submitting payment form data"""

    @http.route(
        "/mpesa_express", type="json", auth="public", methods=["POST"], website=True
    )
    def index(self, **kw):
        """Lina na MPESA Online Callback URL"""
        try:
            txn = None
            params = request.jsonrequest or {}
            if params:
                data = params["Body"]["stkCallback"]
                mrid = data.get("MerchantRequestID", None)
                crid = data.get("CheckoutRequestID", None)
                res_code = data.get("ResultCode", None)
                txn = (
                    request.env["payment.transaction"]
                    .sudo()
                    .search(
                        [
                            ("mpesa_online_merchant_request_id", "=", mrid),
                            ("mpesa_online_checkout_request_id", "=", crid),
                            ("date_validate", "<=", fields.Datetime.now()),
                        ],
                        limit=1,
                        order="id desc",
                    )
                )
            if res_code == 0:
                pay = request.env["mpesa.online"].sudo().save_data(data)
                if pay:
                    _logger.info("MPESA_ONLINE: %s", data.get("ResultDesc"))
                    _logger.info(
                        _("MPESA_ONLINE: Data successfully stored in the system")
                    )
                    if txn:
                        txn.write(txn.mpesa_online_message_validate(pay, {}))
                else:
                    _logger.info("MPESA_ONLINE: Failed")
            else:
                _logger.warning(
                    "MPESA_ONLINE: %s, %s", res_code, data.get("ResultDesc")
                )
                if txn:
                    txn.write(
                        dict(
                            date_validate=fields.Datetime.now(),
                            state="pending",
                            state_message=data.get("ResultDesc"),
                        )
                    )
        except Exception as e:
            _logger.error(e)

    @http.route(
        "/payment/mpesa_online",
        type="http",
        auth="public",
        methods=["POST"],
        website=True,
        csrf=False,
    )
    def lipa_na_mpesa(self, **post):
        """To handle HTTP Post from the lipa na mpesa form"""

        user = post.get("mpesa_phone_number")
        msg = _("MPESA_ONLINE: Receiving payment form data for transaction ref")
        msg += "<%s>" % post.get("reference", "")
        msg += " for <%s>" % user
        _logger.info(msg)
        if not http.request.session.get("sale_order_id") and http.request.session.get(
            "sale_last_order_id"
        ):
            http.request.session.update(
                sale_order_id=http.request.session.get("sale_last_order_id")
            )
        tx_id = request.session.get("__website_sale_last_tx_id", False)
        if tx_id:
            post.update(tx_id=tx_id)

        if (
            http.request.env["payment.transaction"]
            .sudo()
            .form_feedback(post, "mpesa_online")
        ):
            msg = _("MPESA_ONLINE: Completed sending payment request to customer")
            msg += " <%s>" % user
            _logger.info(msg)
            msg = _("MPESA_ONLINE: redirecting back to Odoo payment process.")
            _logger.info(msg)
            return http.request.redirect(post.pop("return_url"))
        return http.request.redirect("/shop/payment")


class PosMpesaController(http.Controller):
    _callback_url = "/payment/mpesa/callback/"

    @http.route(
        ["/payment/mpesa/callback/"],
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def mpesa_return(self, **post):
        """
        :param data:
        {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "20429-25587672-1",
                    "CheckoutRequestID": "ws_CO_020220221007215649",
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {
                                "Name": "Amount",
                                "Value": 1.0
                            },
                            {
                                "Name": "MpesaReceiptNumber",
                                "Value": "QB25IC9R1D"
                            },
                            {
                                "Name": "Balance"
                            },
                            {
                                "Name": "TransactionDate",
                                "Value": 20220202100931
                            },
                            {
                                "Name": "PhoneNumber",
                                "Value": 254727834462
                            }
                        ]
                    }
                }
            }
        }
        :return:
        """
        params = request.jsonrequest or {}
        _logger.info("Call Back request received from mpesa (json) :\n%s", params)
        if params:
            data = params["Body"]["stkCallback"]
            merchant_request_id = data.get("MerchantRequestID")
            checkout_request_id = data.get("CheckoutRequestID")
            res_code = data.get("ResultCode")
            amount = 0
            _logger.info("Data:\n%s", data)
            _logger.info("merchant_request_id:\n%s", merchant_request_id)
            _logger.info("checkout_request_id:\n%s", checkout_request_id)
            _logger.info("res_code:\n%s", res_code)
            if checkout_request_id:
                tx = self.find_or_create_payment(
                    merchant_request_id, checkout_request_id
                )
                _logger.info(tx)
                if not tx or len(tx) > 1:
                    error_msg = (
                        _("Mpesa: received data for CheckoutRequestID %s")
                        % checkout_request_id
                    )
                    if not tx:
                        error_msg += _("; no order found")
                    else:
                        error_msg += _("; multiple order found")
                    _logger.info(error_msg)
                    raise ValidationError(error_msg)
                (
                    receipt_number,
                    receipt_date,
                    phone_number,
                    amount,
                    state,
                    result_description,
                ) = self.extract_callback_metadata(
                    data.get("CallbackMetadata", {}),
                    data.get("ResultCode"),
                    data.get("ResultDesc"),
                )
                tx.write(
                    {
                        "receipt_number": receipt_number,
                        "receipt_date": receipt_date,
                        "amount": amount,
                        "phone_number": phone_number,
                        "result_description": result_description,
                        "state": state,
                        # 'is_pos_request': False,
                    }
                )
            txn = (
                request.env["pos.mpesa.payment"]
                .sudo()
                .search(
                    [
                        ("merchant_request_id", "=", merchant_request_id),
                        ("checkout_request_id", "=", checkout_request_id),
                        # ("date_validate", "<=", datetime.datetime.now()),
                    ],
                    limit=1,
                    order="id desc",
                )
            )
            _logger.info(txn)
            _logger.info(_("MPESA PAYMENT: Data successfully stored in the system"))
        return "Success"

    def extract_callback_metadata(
        self, callback_metadata, result_code, result_description
    ):
        receipt_number = None
        receipt_date = None
        phone_number = None
        amount = 0

        for item in callback_metadata.get("Item", []):
            name = item.get("Name")
            value = item.get("Value")
            if name == "TransactionDate":
                receipt_date = datetime.datetime.strptime(
                    str(value), "%Y%m%d%H%M%S"
                ).strftime(DEFAULT_SERVER_DATETIME_FORMAT)
            elif name == "Amount":
                amount = value
            elif name == "MpesaReceiptNumber":
                receipt_number = value
            elif name == "PhoneNumber":
                phone_number = value

        state = "done" if result_code == 0 else "cancel"

        return (
            receipt_number,
            receipt_date,
            phone_number,
            amount,
            state,
            result_description,
        )

    def find_or_create_payment(self, merchant_request_id, checkout_request_id):
        pos_mpesa_payment = (
            request.env["pos.mpesa.payment"]
            .sudo()
            .search(
                [
                    "|",
                    ("merchant_request_id", "=", merchant_request_id),
                    ("checkout_request_id", "=", checkout_request_id),
                ],
                limit=1,
            )
        )
        if not pos_mpesa_payment:
            _logger.info(
                _("Mpesa: received data with new reference (%s)") % checkout_request_id
            )
            values = {
                "checkout_request_id": checkout_request_id,
                "merchant_request_id": merchant_request_id,
            }
            pos_mpesa_payment = request.env["pos.mpesa.payment"].create(values)
            _logger.info(pos_mpesa_payment)
        return pos_mpesa_payment


class MpesaCashPayment(http.Controller):
    _return_url = "/payment/mpesa_online/return"

    @http.route(
        "/v1/payment_notification",
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def mpesa_notification_json_post(self, **kwargs):
        _logger.info("data: transaction %s", str(json.loads(request.httprequest.data)))
        values = {}
        not_accepted = "Not Accepted"
        post = json.loads(request.httprequest.data)
        if not post:
            _logger.info("Data is not getting!")
            result = {"ResultCode": 1, "ResultDesc": not_accepted}
            return json.dumps(result)
        values.update(
            {
                "ref": post.get("BusinessShortCode"),
                "datas": json.dumps(post),
                "transaction_type": post.get("TransactionType"),
                "trans_id": post.get("TransID"),
                "trans_time": post.get("TransTime"),
                "amount": float(post.get("TransAmount", 0.00)),
                "business_shortcode": post.get("BusinessShortCode"),
                "bill_ref_number": post.get("BillRefNumber"),
                "invoice_number": post.get("InvoiceNumber"),
                "org_account_balance": post.get("OrgAccountBalance"),
                "third_party_transid": post.get("ThirdPartyTransID"),
                "msisdn": post.get("MSISDN"),
                "first_name": post.get("FirstName"),
                "middle_name": post.get("MiddleName"),
                "last_name": post.get("LastName"),
            }
        )
        date = fields.Date.to_string(datetime.date.today())
        values.update({"payment_date": date})
        dbname = post.get("db", "")
        if not dbname:
            _logger.warning("Database name is not found!")
            result = {"ResultCode": 1, "ResultDesc": not_accepted}
            return json.dumps(result)
        odoo.registry(str(dbname))
        try:
            request.env["pos.mpesa.payment"].sudo().create(values)
        except Exception as e:
            if not str(e).isnumeric():
                return json.dumps(
                    {"ResultCode": 1, "ResultDesc": not_accepted, "Error": str(e)}
                )
        result = {"ResultCode": 0, "ResultDesc": "Accepted"}
        return json.dumps(result)

    @http.route(_return_url, type="json", auth="public")
    def mpesa_return_url(self, **data):
        tx_sudo = (
            request.env["payment.transaction"]
            .sudo()
            ._handle_notification_data("mpesa_online", data)
        )
