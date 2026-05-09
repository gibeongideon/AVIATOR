/** @odoo-module */

import { PaymentScreenPaymentLines } from "@point_of_sale/app/screens/payment_screen/payment_lines/payment_lines";
import { patch } from "@web/core/utils/patch";

patch(PaymentScreenPaymentLines.prototype, {
    selectedLineClass(line) {
        const result = super.selectedLineClass(line);
        return {...result,
            o_sybyl_mpesa_swipe_pending: line.mpesa_swipe_pending,
        }
    },

    unselectedLineClass(line) {
        const result = super.unselectedLineClass(line);
        return {...result,
            o_sybyl_mpesa_swipe_pending: line.mpesa_swipe_pending,
        }
    },
});