"""Yahoo sessions that do not invoke curl's vulnerable Brotli decoder."""


def yahoo_session():
    from curl_cffi import requests
    from curl_cffi.const import CurlOpt

    # Identity alone does not stop curl decoding an unsolicited br response.
    return requests.Session(
        impersonate="chrome",
        headers={"Accept-Encoding": "identity"},
        curl_options={CurlOpt.HTTP_CONTENT_DECODING: 0},
    )
