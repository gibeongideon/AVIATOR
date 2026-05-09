/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { jsonrpc, RPCError } from "@web/core/network/rpc_service";

patch(PaymentScreen.prototype, {

    addNewPaymentLine(paymentMethod) {
        const order = this.pos.get_order();
        const res = super.addNewPaymentLine(...arguments);
        if (paymentMethod.use_payment_terminal === "mpesa") {
            jsonrpc("/web/dataset/call_kw/", {
                model: "res.users",
                method: "search",
                args: [
                    []
                ],
                kwargs: {},
            }).then(() => {
                console.log("MPesa Payment Selected : POS Available Online");
            }).catch(error => {
                if (!(error instanceof RPCError)) {
                    console.log("Offline");
                    this.deletePaymentLine(this.selectedPaymentLine.cid);
                    this.popup.add(ErrorPopup, {
                        title: _t("Error"),
                        body: _t("POS offline, transaction cannot be processed!"),
                    });
                    return false;
                }
            });
        }

        if (res && paymentMethod.mpesa_payment_provider_id) {
            order.selected_paymentline.mpesa_swipe_pending = true;
        }
    },
});