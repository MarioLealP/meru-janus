"""Log hygiene: a query string can carry a token or a secret, so no access-log line keeps one."""

import logging


class NoQueryString(logging.Filter):
    """Cut the query string from the path in uvicorn's access log lines.

    uvicorn logs `'%s - "%s %s HTTP/%s" %d'` with args (client, method, full path, http version, status); the full path
    may include anything a client put there. Dropped for every path, not just that one: nothing in
    Janus needs a query string in a log, and a path added later cannot forget to be listed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Always let the record through, with its query string removed."""
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].partition("?")[0], *args[3:])
        return True
