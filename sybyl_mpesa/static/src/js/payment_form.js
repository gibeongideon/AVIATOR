/** @odoo-module **/
/* global Accept */

import paymentForm from '@payment/js/payment_form';
import paymentDemoMixin from '@payment_demo/js/payment_demo_mixin';
import { RPCError } from '@web/core/network/rpc_service';
import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";


paymentForm.include({

    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        console.log("_prepareInlineForm");
        console.log(providerCode);
        if (providerCode !== 'mpesa_online') {
            this._super(...arguments);
            return;
        } else if (flow === 'token') {
            return;
        }
        this._setPaymentFlow('direct');
    },

    async _processDirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        console.log("_processDirectFlow");
        console.log(providerCode);
        if (providerCode !== 'mpesa_online') {
            this._super(...arguments);
            return;
        }
        const mpesaNumber = document.getElementById('mpesa_phone_number');
        const phoneNumber = mpesaNumber.value;
        const phoneRegex = /^\d{9}$/;
        if (!phoneRegex.test(phoneNumber)) {

            this._enableButton();
            this._displayErrorDialog(_t("Invalid Details"),"Please enter a valid 9-digit phone number.");
            return
        }
        this.processMPesaPayment(processingValues);
    },

    processMPesaPayment(processingValues) {
        const mpesaNumber = document.getElementById('mpesa_phone_number');
        const phoneNumber = mpesaNumber.value;
        console.log(phoneNumber);
        this.rpc("/payment/mpesa_online/return", {
            'phoneNumber': phoneNumber,
            'reference': processingValues.reference,
        }).then(function(result) {
            window.location = '/payment/status';
        });
    },

    async processMPesaPayment2(processingValues) {
        const customerInput = document.getElementById('customer_input').value;
        const simulatedPaymentState = document.getElementById('simulated_payment_state').value;

        jsonrpc('/payment/demo/simulate_payment', {
            'reference': processingValues.reference,
            'payment_details': customerInput,
            'payment_state': simulatedPaymentState,
        }).then(() => {
            window.location = '/payment/status';
        }).catch(error => {
            if (error instanceof RPCError) {
                this._displayErrorDialog(_t("Payment processing failed"), error.data.message);
                this._enableButton?.(); // This method doesn't exists in Express Checkout form.
            } else {
                return Promise.reject(error);
            }
        });
    },
});

class PaymentForm extends Component {
    setup() {
        this.ajax = useService("ajax");
        this.core = useService("core");
        this.state = useState({
            phoneNumber: '',
        });
    }

    async payEvent(ev) {
        ev.preventDefault();
        const form = this.el;
        const checkedRadio = this.el.querySelector('input[type="radio"]:checked');
        const button = ev.target;

        // MPESA online START
        const mpesaNumberInput = this.el.querySelector('input[name="mpesa_phone_number"]');
        if (mpesaNumberInput && checkedRadio && checkedRadio.dataset.provider === 'mpesa_online') {
            const phoneNumber = mpesaNumberInput.value;
            const numbers = /^[0-9]+$/;
            const KE = /^254[0-9]+$/;

            if (!phoneNumber) {
                this.displayError(
                    _t('User Error'),
                    _t('MPESA phone number is required.')
                );
                return;
            }

            if (!phoneNumber.match(numbers)) {
                this.displayError(
                    _t('User Error'),
                    _t('Invalid phone number. Spaces and non-numeric characters are not allowed')
                );
                return;
            }

            if (!phoneNumber.match(KE) || phoneNumber.length !== 12) {
                this.displayError(
                    _t('User Error'),
                    _t('Invalid phone number. Please use the format 254xxxxxxxxx. A total of 12 digits')
                );
                return;
            }
        }
        // MPESA online END

        // Check that a payment method has been selected
        if (checkedRadio) {
            const acquirerId = this.getAcquirerIdFromRadio(checkedRadio);
            const acquirerForm = this.isNewPaymentRadio(checkedRadio) ?
                this.el.querySelector(`#o_payment_add_token_acq_${acquirerId}`) :
                this.el.querySelector(`#o_payment_form_acq_${acquirerId}`);

            const inputsForm = acquirerForm.querySelectorAll('input');
            const dataSet = acquirerForm.querySelector('input[name="data_set"]').dataset;

            // If adding a new payment
            if (this.isNewPaymentRadio(checkedRadio)) {
                if (!this.options.partnerId) {
                    console.warn('payment_form: unset partner_id when adding new token; things could go wrong');
                }

                const formData = this.getFormData(inputsForm);
                let wrongInput = false;

                inputsForm.forEach(element => {
                    if (element.type === 'hidden') return;

                    element.closest('div.form-group').classList.remove('has-error');
                    const invalidField = element.closest('div.form-group').querySelector(".o_invalid_field");
                    if (invalidField) invalidField.remove();

                    element.dispatchEvent(new Event("focusout"));

                    if (element.dataset.isRequired && element.value.length === 0) {
                        element.closest('div.form-group').classList.add('has-error');
                        element.closest('div.form-group').insertAdjacentHTML('beforeend', '<div style="color: red" class="o_invalid_field">' + _t("The value is invalid.") + '</div>');
                        wrongInput = true;
                    } else if (element.closest('div.form-group').classList.contains('has-error')) {
                        wrongInput = true;
                        element.closest('div.form-group').insertAdjacentHTML('beforeend', '<div style="color: red" class="o_invalid_field">' + _t("The value is invalid.") + '</div>');
                    }
                });

                if (wrongInput) return;

                this.disableButton(button);
                const verifyValidity = this.el.querySelector('input[name="verify_validity"]');

                if (verifyValidity) {
                    formData.verify_validity = verifyValidity.value === "1";
                }

                try {
                    const data = await this.ajax.jsonRpc(dataSet.createRoute, 'call', formData);

                    if (data.result) {
                        if (data['3d_secure']) {
                            document.body.innerHTML = data['3d_secure'];
                        } else {
                            checkedRadio.value = data.id;
                            form.submit();
                        }
                    } else {
                        this.displayError(
                            '',
                            data.error || _t('Server Error') + ': ' + _t('e.g. Your credit card details are wrong. Please verify.')
                        );
                    }
                } catch (message) {
                    this.enableButton(button);
                    this.displayError(
                        _t('Server Error'),
                        _t("We are not able to add your payment method at the moment.") + message.data.message
                    );
                }
            }
            // If using a form payment
            else if (this.isFormPaymentRadio(checkedRadio)) {
                const txUrlInput = this.el.querySelector('input[name="prepare_tx_url"]');
                const mpesaNumberInput = this.el.querySelector('input[name="mpesa_phone_number"]');

                if (txUrlInput) {
                    const formSaveToken = acquirerForm.querySelector('input[name="o_payment_form_save_token"]').checked;
                    try {
                        const result = await this.ajax.jsonRpc(txUrlInput.value, 'call', {
                            acquirer_id: parseInt(acquirerId),
                            save_token: formSaveToken,
                            access_token: this.options.accessToken,
                            success_url: this.options.successUrl,
                            error_url: this.options.errorUrl,
                            callback_method: this.options.callbackMethod,
                        });

                        if (result) {
                            const newForm = document.createElement('form');
                            newForm.method = "post";
                            newForm.provider = checkedRadio.dataset.provider;
                            newForm.hidden = true;
                            newForm.innerHTML = result;
                            const actionUrl = newForm.querySelector('input[name="data_set"]').dataset.actionUrl;

                            if (mpesaNumberInput) {
                                const mpesaNumber = newForm.querySelector('input[name="mpesa_phone_number"]');
                                if (mpesaNumber) {
                                    mpesaNumber.value = mpesaNumberInput.value;
                                }
                            }

                            newForm.action = actionUrl;
                            document.body.append(newForm);
                            newForm.querySelectorAll('input[data-remove-me]').forEach(input => input.remove());
                            if (actionUrl) newForm.submit();
                        } else {
                            this.displayError(
                                _t('Server Error'),
                                _t("We are not able to redirect you to the payment form.")
                            );
                        }
                    } catch (message) {
                        this.displayError(
                            _t('Server Error'),
                            _t("We are not able to redirect you to the payment form. ") + message.data.message
                        );
                    }
                } else {
                    this.displayError(
                        _t("Cannot set-up the payment"),
                        _t("We're unable to process your payment.")
                    );
                }
            }
            // If using an old payment
            else {
                this.disableButton(button);
                form.submit();
            }
        } else {
            this.displayError(
                _t('No payment method selected'),
                _t('Please select a payment method.')
            );
        }
    }

    getAcquirerIdFromRadio(checkedRadio) {
        // Implement this method based on your logic
    }

    isNewPaymentRadio(checkedRadio) {
        // Implement this method based on your logic
    }

    isFormPaymentRadio(checkedRadio) {
        // Implement this method based on your logic
    }

    getFormData(inputs) {
        // Implement this method based on your logic
    }

    displayError(title, message) {
        // Implement this method based on your logic
    }

    disableButton(button) {
        button.disabled = true;
    }

    enableButton(button) {
        button.disabled = false;
    }
}

export default PaymentForm;