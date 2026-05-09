# -*- coding: utf-8 -*-
RESPONSE_ERROR_CODE = {
    "C2B_REGISTER": {
        "500.003.1001": "Internal Server Error",
        "400.003.01": "Invalid Access Token",
        "400.003.02": "Bad Request",
        "500.003.03": "Error Occured: Quota Violation",
        "500.003.02": "Error Occured: Spike Arrest Violation",
        "404.003.01": "Resource not found",
        "401.003.01": "Error Occurred - Invalid Access Token",
    }
}


class MpesaResponseCode:
    def __init__(
        self,
        api="",
    ):
        self.api = api

    def get_api_error_dict(self):
        if self.api not in RESPONSE_ERROR_CODE:
            return False
        return RESPONSE_ERROR_CODE[self.api]

    def get_c2b_register(self, code):
        error = self.get_api_error_dict()
        if not error:
            return error
        if code not in error:
            return False
        return error[code]
