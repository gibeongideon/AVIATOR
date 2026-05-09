# -*- coding: utf-8 -*-
{
    "name": "Dibon MPesa",
    "summary": """Integrate your POS with an MPesa payment terminal.
    Allows customer to pay for POS Order using MPesa stk push.""",
    "author": "Dibon",
    "website": "https://www.dibon.co.ke",
    "category": "Sales",
    "version": "0.0",
    "depends": [
        "sale",
        "account_payment",
        "payment_demo",
        "point_of_sale",
    ],
    "data": [
        "security/mpesa_security.xml",
        "security/ir.model.access.csv",
        # "views/payment_mpesa_templates.xml",
        "data/data.xml",
        "data/ir_cron_data.xml",
        "views/product_views.xml",
        "views/payment_transaction_views.xml",
        "views/payment_provider_views.xml",
        "views/pos_mpesa_payment_views.xml",
        "views/pos_config_views.xml",
        "views/pos_payment_method_views.xml",
        "views/payment_form_templates.xml",
        "views/mpesa_log_views.xml",
        "views/templates.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "sybyl_mpesa/static/src/js/mpesa_payment.js",
            "sybyl_mpesa/static/src/js/models.js",
            "sybyl_mpesa/static/src/js/pos_mpesa.js",
            "sybyl_mpesa/static/src/css/*",
            "sybyl_mpesa/static/src/overrides/components/payment_screen_payment_lines/*",
            "sybyl_mpesa/static/src/overrides/components/payment_screen/payment_screen.js",
        ],
        "web.assets_frontend": [
            "sybyl_mpesa/static/src/js/payment_form.js",
        ],
    },
    "application": True,
    "license": "LGPL-3",
}
