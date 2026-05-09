/** @odoo-module */

import { Payment } from "@point_of_sale/app/store/models";
import { patch } from "@web/core/utils/patch";

patch(Payment.prototype, {
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        if (this.payment_method?.use_payment_terminal === 'mpesa') {
            this.mpesa_receipt = json.mpesa_receipt;
        }
    },
    export_as_JSON() {
        const result = super.export_as_JSON(...arguments);
        if (result && this.payment_method?.use_payment_terminal === 'mpesa') {
            return Object.assign(result, {
                mpesa_receipt: this.mpesa_receipt,
            });
        }
        return result
    },
});