/** @odoo-module */
import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { _t } from "@web/core/l10n/translation";
import { sprintf } from "@web/core/utils/strings";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";

export class PaymentMpesa extends PaymentInterface {

    setup(pos, payment_method) {
        this.env = pos.env;
        this.pos = pos;
        this.payment_method = payment_method;
        this.supports_reversals = false;
        this.mpesa_terminal = true;
    }

    send_payment_request(cid) {
        super.send_payment_request(cid);
        this._reset_state();
        this.phone = $("#phone").val();
        this.ref_no = $("#reference_no").val();

        const selectedRadioButton = document.querySelector("input[name='payment_reference']:checked");

        if (selectedRadioButton) {
            const isPaymentCheck = selectedRadioButton.id === "paymentCheck";
            const isReferenceCheck = selectedRadioButton.id === "referenceCheck";

            if ((selectedRadioButton.id === "paymentCheck" && !this._isPhoneValid()) ||
                (selectedRadioButton.id === "referenceCheck" && !this._isRefNoValid())) {
                return;
            }

            if (isPaymentCheck) {
                console.log(this.phone);
                return this._validate_and_process_phone();
            } else if (isReferenceCheck) {
                console.log(this.ref_no);
                return this._mpesa_pay_ref_no();
            }
        } else {
            this._show_error(sprintf(_t("Select any option!")));
            return false;
        }
    }

    _isPhoneValid() {
        if (!this.phone) {
            this._show_error(sprintf(_t("MPESA phone number is required")));
            return false;
        }
        return true;
    }

    _isRefNoValid() {
        if (!this.ref_no) {
            this._show_error(sprintf(_t("Enter transaction ID")));
            return false;
        }
        return true;
    }

    _validate_and_process_phone() {
        const numbers = /^\d+$/;
        const validPhonePatterns = /^(0\d{9}|\d{9})$/;

        if (!this.phone.match(numbers)) {
            return this._show_error(sprintf(_t("Invalid phone number. Spaces and non-numeric characters are not allowed")), "User Error");
        } else if (!this.phone.match(validPhonePatterns)) {
            return this._show_error(sprintf(_t("Invalid phone number. Please use the format 0xxxxxxxxx or xxxxxxxxx. A total of 9 or 10 digits")), "User Error");
        } else {
            return this._mpesa_pay();
        }
    }

    send_payment_cancel(order, cid) {
        super.send_payment_cancel(order, cid);
        // set only if we are polling
        this.was_cancelled = !!this.polling;
        return Promise.resolve(true);
    }

    close() {
        // QUESTION: What does this do?
        super.close();
    }

    _reset_state() {
        // To track if query has been cancelled 
        // QUESTION: How can we set this using the response?
        this.was_cancelled = false;
        // QUESTION: What does this do?
        this.remaining_polls = 2;
        clearTimeout(this.polling);
    }

    _handle_odoo_connection_failure(data) {
        // handle timeout
        let line = this.pos.get_order().selected_paymentline;
        if (line) {
            line.set_payment_status("retry");
        }
        this._show_error(_t("Could not connect to the Odoo server, please check your internet connection and try again."));
        return Promise.reject(data); // prevent subsequent onFullFilled's from being called
    }

    _call_mpesa(data) {
        let order = this.pos.get_order();
        return this.env.services.orm.silent.call(
            "pos.payment.method",
            "mpesa_stk_push", [
                data,
                this.payment_method.mpesa_test_mode,
                this.payment_method.id,
                order.get_partner(),
            ]).catch(this._handle_odoo_connection_failure.bind(this));
    }

    _mpesa_pay_ref_no() {
        let self = this;
        let order = self.pos.get_order();
        let line = order.selected_paymentline;
        this.env.services.orm.call(
            "pos.mpesa.payment",
            "search_reference_no", [
                self.ref_no,
                order.uid,
                self.pos.config_id,
                line.amount,
                this.payment_method.id
            ], {}).then(function(result) {
            let set_payment_status = result[0];
            console.log(set_payment_status);
            if (set_payment_status == "error") {
                self._show_error(sprintf(_t(result[1])));
            }
            //	Fetching Payment line and setting payment status
            let order = self.pos.get_order();
            let line = order.selected_paymentline;
            line.set_payment_status(result[0]);
            if (result[1] > 0 && result[1] != line.amount) {
                line.set_amount(result[1]);
            }
        })
    }

    _mpesa_get_account_reference() {
        let config = this.pos.config;
        // Trimmed first 10 letter because Shop name only allows 10
        return (config.display_name).slice(0, 10);
    }

    _mpesa_pay_data() {
        let config = this.pos.config;
        let order = this.pos.get_order();
        let line = order.selected_paymentline;
        let data = {
            "amount": line.amount,
            "currency_id": this.pos.currency.name,
            "payment_method_id": this.payment_method.id,
            "phone": this.phone,
            "order_id": order.uid,
            "customer_id": order.get_partner(),
            "shop_name": this._mpesa_get_account_reference(config)
        };
        return data;
    }

    _mpesa_pay() {
        let self = this;
        let data = this._mpesa_pay_data();

        return this._call_mpesa(data).then(function(data) {
            return self._mpesa_handle_response(data);
        });
    }

    _poll_for_response(resolve, reject) {
        let self = this;

        // QUESTION: Where is was_cancelled set?
        if (this.was_cancelled) {
            resolve(false);
            return Promise.resolve();
        }
        let line = this.pos.get_order().selected_paymentline;
        return this.env.services.orm.silent.call(
            "pos.payment.method",
            "get_latest_mpesa_status", [
                this.payment_method.mpesa_short_code,
                this.payment_method.mpesa_pass_key,
                this.payment_method.mpesa_customer_key,
                this.payment_method.mpesa_secrete_key,
                line.transaction_id,
                this.payment_method.mpesa_test_mode,
            ]).catch(function(data) {
            reject();
            return self._handle_odoo_connection_failure(data);
        }).then(function(status) {
            let result_code = status.ResultCode;
            console.log(result_code);
            // if () {
            // self.remaining_polls = 2;
            // } else {
            // self.remaining_polls--;
            // }
            if (result_code === 0 || result_code === "0") {
                resolve(true);
            } else if (result_code === 1032 || result_code === "1032") {
                line.set_payment_status("retry");
                self._show_error(_t("Request cancelled by user"));
                self._reset_state();
                resolve(false);
                return Promise.resolve(true);
            } else if (result_code === 2001 || result_code === "2001") {
                line.set_payment_status("retry");
                self._show_error(_t("The user has entered the wrong pin to validate the STK PUSH request."));
                self._reset_state();
                resolve(false);
                return Promise.resolve(true);
            } else if (result_code === 1037 || result_code === "1037") {
                line.set_payment_status("retry");
                self._show_error(_t("DS timeout user cannot be reached."));
                self._reset_state();
                resolve(false);
                return Promise.resolve(true);
            } else if (result_code === 1 || result_code === "1") {
                line.set_payment_status("retry");
                self._show_error(_t("The subscriber has insufficient funds on M-PESA."));
                self._reset_state();
                resolve(false);
                return Promise.resolve(true);
            } else if (self.remaining_polls <= 0) {
                console.log(status);
                self._show_error(_t("The connection to your payment terminal failed. Please check if it is still connected to the internet."));
                resolve(false);
            }
        });
    }

    _mpesa_handle_response(response) {
        let self = this;
        let line = this.pos.get_order().selected_paymentline;

        if (response.ResponseCode === "0") {
            line.set_payment_status("waitingCard");
            // This is not great, the payment screen should be
            // refactored so it calls render_paymentlines whenever a
            // paymentline changes. This way the call to
            // set_payment_status would re-render it automatically.
            console.log("CheckoutRequestId: %s", response.CheckoutRequestID);
            line.transaction_id = response.CheckoutRequestID;
            let res = new Promise(function(resolve, reject) {
                // clear previous intervals just in case, otherwise it'll run forever
                clearTimeout(self.polling);
                self.polling = setInterval(function() {
                    self._poll_for_response(resolve, reject);
                }, 3000);
            });
            // make sure to stop polling when we're done
            res.finally(function() {
                self._reset_state();
            });
            return res;
        } else {
            console.error("error from MPesa", response.errorMessage);
            let msg = "";
            if (response.errorMessage) {
                msg = response.errorMessage;
            }
            this._show_error(sprintf(_t("An unexpected error occurred. \nMessage from Mpesa: %s"), msg));
            if (line) {
                line.set_payment_status("force_done");
            }
            return Promise.resolve();
        }
    }

    _show_error(msg, title) {
        if (!title) {
            title = _t("MPesa Error");
        }
        console.log(title);
        console.log(msg);
        this.env.services.popup.add(ErrorPopup, {
            title: _t(title),
            body: _t(msg),
        });
    }
}