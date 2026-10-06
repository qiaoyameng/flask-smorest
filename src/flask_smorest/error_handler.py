"""Exception handler"""

from werkzeug.exceptions import HTTPException

import marshmallow as ma

from .error_paths import build_error_paths


class ErrorSchema(ma.Schema):
    """Schema describing the error payload

    Not actually used to dump payload, but only for documentation purposes
    """

    code = ma.fields.Integer(metadata={"description": "Error code"})
    status = ma.fields.String(metadata={"description": "Error name"})
    message = ma.fields.String(metadata={"description": "Error message"})
    errors = ma.fields.Dict(metadata={"description": "Errors"})
    error_paths = ma.fields.List(
        ma.fields.Dict,
        metadata={"description": "Structured validation error paths"},
    )


class ErrorHandlerMixin:
    """Extend Api to manage error handling."""

    # Should match payload structure in handle_http_exception
    ERROR_SCHEMA = ErrorSchema

    def _register_error_handlers(self):
        """Register error handlers in Flask app

        This method registers a default error handler for ``HTTPException``.
        """
        self._app.register_error_handler(HTTPException, self.handle_http_exception)

    def handle_http_exception(self, error):
        """Return a JSON response containing a description of the error

        This method is registered at app init to handle ``HTTPException``.

        - When ``abort`` is called in the code, an ``HTTPException`` is
          triggered and Flask calls this handler.

        - When an exception is not caught in a view, Flask makes it an
          ``InternalServerError`` and calls this handler.

        flask-smorest republishes webargs's
        :func:`abort <webargs.flaskparser.abort>`. This ``abort`` allows the
        caller to pass kwargs and stores them in ``exception.data`` so that the
        error handler can use them to populate the response payload.

        Extra information expected by this handler:

        - `message` (``str``): a comment
        - `errors` (``dict``): errors, typically validation errors in
            parameters and request body
        - `headers` (``dict``): additional headers

        When the error was raised while validating request arguments, a
        structured ``error_paths`` list is added to the payload, unless
        ``VALIDATION_ERROR_PATHS`` application parameter is set to
        ``False``. Each entry provides the parameter ``location``
        (``query``, ``body``, ``header``, ``cookie`` or ``path``), the
        client-facing field ``path`` (using ``data_key`` names and list
        indexes), the original text ``messages`` and an error ``type``
        (``missing``, ``unknown``, ``schema`` or ``validation``). Raw input
        values are not included unless
        ``VALIDATION_ERROR_PATHS_INCLUDE_VALUE`` application parameter is
        set to ``True``.
        """
        headers = {}
        payload = {"code": error.code, "status": error.name}

        # Get additional info passed as kwargs when calling abort
        # data may not exist if HTTPException was raised without webargs abort
        data = getattr(error, "data", None)
        if data:
            # If we passed a custom message
            if "message" in data:
                payload["message"] = data["message"]
            # If we passed "errors"
            if "errors" in data:
                payload["errors"] = data["errors"]
            # If webargs added validation errors as "messages"
            # (you should use 'errors' as it is more explicit)
            elif "messages" in data:
                payload["errors"] = data["messages"]
            # If we passed additional headers
            if "headers" in data:
                headers = data["headers"]
            # If a validation error was raised while parsing arguments,
            # add structured error paths
            exc = getattr(error, "exc", None)
            if isinstance(exc, ma.ValidationError) and self.config.get(
                "VALIDATION_ERROR_PATHS", True
            ):
                payload["error_paths"] = build_error_paths(
                    exc,
                    schema=data.get("schema"),
                    include_value=self.config.get(
                        "VALIDATION_ERROR_PATHS_INCLUDE_VALUE", False
                    ),
                )

        return payload, error.code, headers
