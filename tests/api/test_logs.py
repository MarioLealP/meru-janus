import logging

import pytest

from janus.api import app as app_module
from janus.api.logs import NoQueryString

CALLBACK = "/api/janus/approve/callback?code=one-time&state=abc"


def access_record(path: str) -> logging.LogRecord:
    # uvicorn's access log: '%s - "%s %s HTTP/%s" %d' with (client, method, full path, http version, status).
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1234", "GET", path, "1.1", 303),
        None,
    )


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        (CALLBACK, "/api/janus/approve/callback"),
        ("/api/janus/requests?x=1", "/api/janus/requests"),
        ("/heartbeat", "/heartbeat"),
    ],
)
def test_the_access_log_never_carries_a_query_string(path: str, logged: str):
    record = access_record(path)
    assert NoQueryString().filter(record) is True
    line = record.getMessage()
    assert line == f'10.0.0.1:1234 - "GET {logged} HTTP/1.1" 303'
    assert "one-time" not in line


def test_a_record_of_another_shape_passes_untouched():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "odd %s", ("a?b=c",), None)
    assert NoQueryString().filter(record) is True
    assert record.getMessage() == "odd a?b=c"


def test_the_app_installs_the_filter_on_uvicorns_access_log():
    assert any(isinstance(f, NoQueryString) for f in logging.getLogger("uvicorn.access").filters)
    assert app_module.app
