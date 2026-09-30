from mosaic_api.integrations.apim.inventory import _mcp_endpoints


def test_mcp_endpoints_accept_array_shape() -> None:
    endpoints = _mcp_endpoints([{"name": "message", "uriTemplate": "/mcp"}])

    assert [(endpoint.name, endpoint.uri_template) for endpoint in endpoints] == [
        ("message", "/mcp")
    ]


def test_mcp_endpoints_accept_live_dict_shape() -> None:
    endpoints = _mcp_endpoints({"message": {"uriTemplate": "/mcp"}})

    assert [(endpoint.name, endpoint.uri_template) for endpoint in endpoints] == [
        ("message", "/mcp")
    ]


def test_mcp_endpoints_accept_absent_value() -> None:
    assert _mcp_endpoints(None) == []
