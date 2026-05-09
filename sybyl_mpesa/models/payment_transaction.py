# -*- coding: utf-8 -*-

import base64
import json
import logging
import time

import requests
from odoo.exceptions import ValidationError
from odoo.tools.float_utils import float_compare
from requests.auth import HTTPBasicAuth

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class MpesaOnlineTransaction(models.Model):
    _inherit = "payment.transaction"

    mpesa_online_merchant_request_id = fields.Char(
        "MPESA Merchant Request ID",
        readonly=True,
        help="MPESA transaction reference/receipt number",
    )
    mpesa_online_checkout_request_id = fields.Char(
        "MPESA Checkout Request ID", readonly=True
    )
    mpesa_online_time_stamp = fields.Char("MPESA Time Stamp", readonly=True)
    mpesa_online_password = fields.Char("MPESA Password", readonly=True)
    mpesa_online_currency_id = fields.Many2one(
        related="provider_id.mpesa_online_currency_id", string="Currency(Mpesa)"
    )
    provider = fields.Selection(related="provider_id.code", readonly=True)

    @api.model
    def _mpesa_online_form_get_tx_from_data(self, data):
        reference, currency, acquirer = (
            data.get("reference"),
            data.get("currency"),
            data.get("acquirer"),
        )
        txn = self.search(
            [
                ("reference", "=", reference),
                ("acquirer_id", "=", int(acquirer)),
                ("currency_id", "=", int(currency)),
            ]
        )
        if not txn or len(txn) > 1:
            error_msg = "MPESA_ONLINE: Received data for Order reference %s" % reference
            if not txn:
                error_msg += "; but no transaction found"
            else:
                error_msg += "; but multiple transactions found"
            _logger.error(error_msg)
        return txn

    def _mpesa_online_form_get_invalid_parameters(self, data):
        invalid_parameters = []
        # compare amount paid vs amount of the order
        if float_compare(float(data.get("amount")), (self.amount + self.fees), 2) != 0:
            invalid_parameters.append(
                ("amount", data.get("amount"), "%.2f" % self.amount)
            )

            # compare currency
        if int(data.get("currency")) != self.currency_id.id:
            invalid_parameters.append(
                ("currency", data.get("currency"), self.currency_id.id)
            )

            # compare acquirer
        if int(data.get("acquirer")) != self.provider_id.id:
            invalid_parameters.append(
                ("acquirer", data.get("acquirer"), self.provider_id.id)
            )

            # compare order reference
        if str(data.get("reference")) != self.reference:
            invalid_parameters.append(
                ("reference", data.get("reference"), self.reference)
            )

        return invalid_parameters

    def mpesa_online_message_validate(self, pay=None, vals=None):
        """Called when the mpesa online callback url receives data from safaricom mpesa API.
        Validates payment and return dict of values to be used to update the payment transaction.
        """
        if pay:
            pay.write(
                {
                    "reconciled": True,
                    "acquirer_id": self.provider_id.id,
                    "currency_id": self.provider_id.mpesa_online_currency_id.id,
                }
            )
            vals["date"] = fields.Datetime.now()
            vals["acquirer_reference"] = pay.display_name
            msg = _("MPESA_ONLINE: Customer paid")
            msg += " %s %s" % (pay.amount, self.mpesa_online_currency_id.name)
            msg += _(" against an order amounting to")
            msg += " %s %s" % (self.amount, self.currency_id.name)
            _logger.info(msg)
            amount_to_pay = self.amount
            # multi-currency support
            if self.provider_id.mpesa_online_currency_id.id != self.currency_id.id:
                amount_to_pay = self.currency_id._convert(
                    from_amount=self.amount,
                    company=self.partner_id.company_id,
                    to_currency=self.provider_id.mpesa_online_currency_id,
                    date=fields.Date.today(),
                )
            res = float_compare(
                pay.amount, amount_to_pay, self.provider_id.mpesa_online_dp
            )
            if res == 0:
                msg = _(
                    "MPESA_ONLINE: Payment successfully confirmed.Customer paid precise amount"
                )
                vals["state"] = "done"
                vals["state_message"] = msg
                _logger.info(msg)

            elif res == 1:
                delta = pay.amount - amount_to_pay
                msg = _(
                    "MPESA_ONLINE: Payment successfully confirmed."
                    "Customer paid more than the order amount by"
                )
                msg += " %s %s" % (
                    pay.currency_id.symbol or "",
                    "{:,.2f}".format(delta),
                )
                vals["state_message"] = msg
                vals["state"] = "done"
                _logger.info(msg)
            else:
                delta = amount_to_pay - pay.amount
                msg = _(
                    "MPESA_ONLINE: Payment validated but order not confirmed."
                    "Customer paid less than the order amount by"
                )
                msg += " %s %s" % (
                    pay.currency_id.symbol or "",
                    "{:,.2f}".format(delta),
                )
                vals["state"] = "pending"
                vals["state_message"] = msg
                _logger.info(msg)
        return vals

    def _mpesa_online_form_validate(self, data):
        # there will be not tx_id in data for portal case. Hence we need to
        # check and update before proceeding
        if not (data.get("tx_id", False)):
            data.update(tx_id=self.id)
        acq = self.env["payment.provider"].browse([int(data.get("acquirer"))])
        if not acq:
            return False
        return acq.mpesa_stk_push(data)

    def _send_payment_request(self):
        super()._send_payment_request()
        if self.provider_code != "mpesa_online":
            return
        return

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != "mpesa_online" or len(tx) == 1:
            return tx

        tx = self.search(
            [
                ("reference", "=", notification_data.get("reference")),
                ("provider_code", "=", "mpesa_online"),
            ]
        )
        if not tx:
            raise ValidationError(
                "MPesa: "
                + _(
                    "No transaction found matching reference %s.",
                    notification_data.get("reference"),
                )
            )
        return tx

    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code != "mpesa_online":
            return

        _logger.info(notification_data.get("phoneNumber"))
        _logger.info(self.amount)

        phone_number = self.format_phone(notification_data.get("phoneNumber"))
        stk_response = self.trigger_stk_push(
            self.provider_id, phone_number, self.amount
        )
        notification_data = {**notification_data, **stk_response}
        state = notification_data["payment_state"]
        if state == "pending":
            self._set_pending()
        elif state == "done":
            if self.capture_manually and not notification_data.get("manual_capture"):
                self._set_authorized()
            else:
                self._set_done()
                if self.operation == "refund":
                    self.env.ref("payment.cron_post_process_payment_tx")._trigger()
        elif state == "cancel":
            self._set_canceled()
        else:  # Simulate an error state.
            self._set_error(
                _(
                    "%s",
                    notification_data.get("error_message"),
                )
            )

    def format_phone(self, phone):
        formatted_phone = (
            f"254{phone}"
        )
        return formatted_phone

    def get_timestamp_passkey(self, short_code, pass_key):
        time_stamp = str(time.strftime("%Y%m%d%H%M%S"))
        if not short_code or not pass_key:
            raise ValidationError(_("Please check the configuration!"))
        passkey = short_code + pass_key + time_stamp
        password = str(base64.b64encode(passkey.encode("utf-8")), "utf-8")
        return time_stamp, password

    def _mpesa_get_access_token(self, customer_key, secrete_key, test_mode):
        url = (
            "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
            if not test_mode
            else "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
        )
        response = requests.get(
            url,
            auth=HTTPBasicAuth(customer_key, secrete_key),
            timeout=30,
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


    # def _mpesa_get_access_token(self, customer_key, secrete_key, test_mode):
    #     url = (
    #         "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    #         if not test_mode
    #         else "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    #     )
    #     response = requests.get(
    #         url, auth=HTTPBasicAuth(customer_key, secrete_key), timeout=30
    #     )
    #     _logger.info(customer_key)
    #     _logger.info(secrete_key)
    #     _logger.info(response)
    #     if response.text:
    #         json_data = json.loads(response.text)
    #     else:
    #         return False
    #     _logger.info(json_data)
    #     return json_data["access_token"]

    def trigger_stk_push(self, provider, mobile_number, amount):
        test_mode = (
            True
            if provider.state in ["test", "enabled"]
            else None if provider.state == "disabled" else False
        )
        mpesa_endpoint = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
        token = self._mpesa_get_access_token(
                provider.mpesa_online_consumer_key,
                provider.mpesa_online_consumer_secret,
                test_mode,
            )
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer %s"
            % token,
        }

        short_code = provider.mpesa_online_service_number
        pass_key = provider.mpesa_online_passkey

        time_stamp, password = self.get_timestamp_passkey(short_code, pass_key)

        payload = {
            "BusinessShortCode": short_code,
            "Password": password,
            "Timestamp": time_stamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": mobile_number,
            "PartyB": provider.mpesa_online_service_number,
            "PhoneNumber": mobile_number,
            "CallBackURL": provider.mpesa_online_callback_url,
            "AccountReference": self.reference,
            "TransactionDesc": "Product Purchase",
        }
        _logger.info(payload)
        response = requests.post(
            mpesa_endpoint,
            headers=headers,
            data=json.dumps(payload),
            timeout=30,
        )
        _logger.info("respose %s",response)
        _logger.info("response.text %s",response.text)
        # _logger.info("self.reference %s",self.reference)
        _logger.info(response)
        _logger.info(response.text)
        _logger.info(self.reference)
        checkout_request_id = response.json().get("CheckoutRequestID")
        self.mpesa_online_checkout_request_id = checkout_request_id
        self.mpesa_online_time_stamp = time_stamp
        self.mpesa_online_password = password
        if response.status_code == 200:
            _logger.info("STK push sent successfully")

            mpesa_url = (
                "https://api.safaricom.co.ke/mpesa/stkpushquery/v1/query"
            )
            # Prepare the payload
            values_status_check = {
                "BusinessShortCode": short_code,
                "Password": password,
                "Timestamp": time_stamp,
                "CheckoutRequestID": checkout_request_id,
            }

            timeout_duration = 30  # Polling duration in seconds
            interval = 3  # Poll every 3 seconds
            start_time = time.time()
            token = self._mpesa_get_access_token(
                provider.mpesa_online_consumer_key,
                provider.mpesa_online_consumer_secret,
                test_mode,
            )
            headers = {
                "Authorization": "Bearer %s"
                % token
            }
            while time.time() - start_time < timeout_duration:
                try:
                    # Make the request
                    status_response = requests.post(mpesa_url, json=values_status_check, headers=headers, timeout=30)
                    status_response = status_response.json()
                    _logger.info("status_response %s",status_response)
                    result_code = status_response.get("ResultCode")
                    state = "done" if result_code == "0" else "cancel"
                    if state == 'done':
                        # print("Status check success:", status_response.json())
                        # You can add a condition to exit the loop if a success status_response is returned
                        break
                    else:
                        _logger.info("status response error %s",status_response.get("errorMessage"))
                        _logger.info("status status_response errorCode %s",status_response.get("errorCode"))
                        # print("Error with status code:", status_response.status_code)
                except requests.exceptions.RequestException as e:
                    print("Request failed:", e)

                # Wait for the next poll interval
                time.sleep(interval)

            # Final check after the polling duration ends
            final_state = ''
            if state != 'done':
                final_response = requests.post(mpesa_url, json=values_status_check, headers=headers, timeout=30)
                final_response = final_response.json()
                _logger.info("final_response %s",final_response)
                result_code = final_response.get("ResultCode")
                final_state = "done" if result_code == "0" else "cancel"

            if final_state == 'done' or state == 'done':
                return {
                    "payment_state": "done",
                    "status": "success",
                    "message": "STK push sent successfully",
                    "error_message": "",
                }
                self.create_mpesa_logs(payload,response.status_code,response.json(),checkout_request_id,self.id,"0",'')
            else:
                result_code = final_response.get("ResultCode")
                if result_code == 1032 or result_code == "1032":
                    error_msg = "The transaction failed because the payment request was canceled by the MPesa subscriber."
                elif result_code == 2001 or result_code == "2001":
                    error_msg = "The transaction failed because an incorrect PIN was entered by the MPesa subscriber."
                elif result_code == 1037 or result_code == "1037":
                    error_msg = "The transaction failed due to a time-out."
                elif result_code == 1 or result_code == "1":
                    error_msg = "The transaction failed due to insufficient funds in the MPesa account of the subscriber."
                else:
                    error_msg = final_response.get("ResultDesc")

                _logger.info("Failed to get Payment Response by Customer")
                _logger.info("Final Response Result errorMessage Description %s",final_response.get("ResultDesc"))
                _logger.info("Final Response Result errorCode %s",final_response.get("ResultCode"))
                self.create_mpesa_logs(payload,response.status_code,response.json(),checkout_request_id,self.id,final_response.get("ResultCode"),error_msg)
                return {
                    "payment_state": "error",
                    "status": "error",
                    "message": "Failed to send STK push",
                    "error_message": error_msg,
                }
        else:
            _logger.info("Failed to send STK push")
            _logger.info("errorMessage %s",response.json().get("errorMessage"))
            _logger.info("ResultDesc %s",response.json().get("ResultDesc"))
            return {
                "payment_state": "error",
                "status": "error",
                "message": "Failed to send STK push",
                "error_message": response.json().get("errorMessage"),
            }
            self.create_mpesa_logs(payload,response.status_code,response.json(),checkout_request_id,self.id,'','')
            
    def create_mpesa_logs(self,payload,response_status_code,response_json,checkout_request_id,self_id,final_response,error_msg):
        log_value = {
            "name": payload,
            "status_code": response_status_code,
            "response": response_json,
            "checkout_request_id": checkout_request_id,
            "payment_transaction_id": self_id,
            "result_code": final_response,
            "response_error": error_msg
        }
        log_id = self.env["mpesa.log"].sudo().create(log_value)

    def query_transaction_status(self):
        for record in self.search([("mpesa_online_checkout_request_id", "!=", False)]):
            print(record)
            print(record.payment_method_id)
            provider_id = record.payment_method_id.provider_ids
            time_stamp, password = self.get_timestamp_passkey(
                provider_id.mpesa_online_service_number,
                provider_id.mpesa_online_consumer_secret,
            )
            query_data = {
                #     "BusinessShortCode": record.shortcode,
                "BusinessShortCode": provider_id.mpesa_online_service_number,
                "Password": record.mpesa_online_password,
                "Timestamp": record.mpesa_online_time_stamp,
                "CheckoutRequestID": record.mpesa_online_checkout_request_id,
            }
            test_mode = (
                True
                if provider_id.state in ["test", "enabled"]
                else None if provider_id.state == "disabled" else False
            )
            mpesa_endpoint = (
                "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
            )
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer %s"
                % self._mpesa_get_access_token(
                    provider_id.mpesa_online_consumer_key,
                    provider_id.mpesa_online_consumer_secret,
                    test_mode,
                ),
            }
            response = requests.post(mpesa_endpoint, json=query_data, headers=headers)
            response_data = response.json()
            print(response_data)
