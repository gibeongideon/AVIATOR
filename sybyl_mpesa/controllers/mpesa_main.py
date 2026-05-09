# -*- coding: utf-8 -*-

import json
import logging

from odoo.http import request

from odoo import http
from odoo.addons.portal.controllers.portal import CustomerPortal
from ..mpesa import c2b

_logger = logging.getLogger(__name__)


class MpesaPayment(CustomerPortal):

    def _prepare_mpesa_portal_layout_values(self):
        organization_list = request.env["mpesa.registration"].search([])
        return {"organizations": organization_list}

    @http.route(
        "/v1/c2b/confirm", type="json", auth="none", methods=["POST"], csrf=False
    )
    def mpesa_confirm(self, **post):
        _logger.info("mpesa_confirm data: transaction %s", request.httprequest)
        request_json = request.httprequest.json
        _logger.info("data: transaction %s", request_json)
        if request_json and request_json.get("BusinessShortCode"):
            values = {
                "ref": request_json.get("TransID", ""),
                "state": "draft",
                "transaction_type": request_json.get("TransactionType"),
                "trans_id": request_json.get("TransID"),
                "trans_time": request_json.get("TransTime"),
                "amount": float(request_json.get("TransAmount", 0.00)),
                "business_shortcode": request_json.get("BusinessShortCode"),
                "bill_ref_number": request_json.get("bill_ref_number"),
                "invoice_number": request_json.get("InvoiceNumber"),
                "org_account_balance": request_json.get("OrgAccountBalance"),
                "third_party_transid": request_json.get("ThirdPartyTransID"),
                "msisdn": request_json.get("MSISDN"),
                "first_name": request_json.get("FirstName"),
                "middle_name": request_json.get("MiddleName"),
                "last_name": request_json.get("LastName"),
                "datas": json.dumps(request_json),
            }
            request.env["pos.mpesa.payment"].create(values)
        return json.dumps({"ResultCode": 0, "ResultDesc": "Accepted"})

    @http.route(
        ["/v1/c2b/validate"], type="json", auth="none", methods=["POST"], csrf=False
    )
    def mpesa_validation(self, **post):
        _logger.info("mpesa_validation data: transaction %s", request.httprequest)
        request_json = request.httprequest.json
        _logger.info("data: transaction %s", request_json)
        return json.dumps({"ResultCode": 0, "ResultDesc": "Accepted"})

    @http.route(
        ["/v1/c2b/callback/post"],
        type="json",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def mPesaCallbackPost(self, **post):
        _logger.info("data: transaction Post %s", request.jsonrequest)
        return json.dumps({"ResultCode": 0, "ResultDesc": "Accepted"})

    @http.route(
        ["/v1/c2b/callback/get"], type="json", auth="none", methods=["GET"], csrf=False
    )
    def mPesaCallbackGET(self, **post):
        _logger.info("data: transaction GET %s", request.jsonrequest)
        return json.dumps({"ResultCode": 0, "ResultDesc": "Accepted"})

    @http.route(
        ["/mpesa/send/c2b"],
        type="http",
        method=["POST", "GET"],
        auth="none",
        website=False,
        csrf=False,
    )
    def send_c2b(self, **vals):
        short_code = "600730"
        mpesa_consumer_key = (
            request.env["ir.config_parameter"].sudo().get_param("mpesa_consumer_key")
        )
        mpesa_consumer_secret = (
            request.env["ir.config_parameter"].sudo().get_param("mpesa_consumer_secret")
        )
        request.env["mpesa.registration"].sudo().search(
            [
                "|",
                ("short_code", "=", short_code),
                ("paybill_number", "=", short_code),
            ]
        )
        mpesa_obj = c2b.C2B(
            env="sandbox", app_key=mpesa_consumer_key, app_secret=mpesa_consumer_secret
        )
        mpesa_obj.simulate(
            short_code, "CustomerPayBillOnline", "100", "919173770799", "account"
        )
