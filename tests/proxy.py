from . import httpurl

class TestHttpClientWithProxy(httpurl.TestHttpClient):
    with_proxy = True
    server_concurrency = 'process'