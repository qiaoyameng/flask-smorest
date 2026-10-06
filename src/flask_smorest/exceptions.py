"""Custom exceptions"""

import werkzeug.exceptions as wexc


class FlaskSmorestError(Exception):
    """Generic flask-smorest exception"""


class MissingAPIParameterError(FlaskSmorestError):
    """Missing API parameter"""


class NotModified(wexc.HTTPException, FlaskSmorestError):
    """Resource was not modified (Etag is unchanged)

    Exception created to compensate for a lack in Werkzeug (and Flask)
    """

    code = 304
    description = "Resource not modified since last request."


class PreconditionRequired(wexc.PreconditionRequired, FlaskSmorestError):
    """Etag required but missing"""

    # Overriding description as we don't provide If-Unmodified-Since
    description = 'This request is required to be conditional; try using "If-Match".'


class PreconditionFailed(wexc.PreconditionFailed, FlaskSmorestError):
    """Etag required and wrong ETag provided"""


class RepresentationNotReplayableError(FlaskSmorestError):
    """Representation ETag cannot be computed from a non-replayable response

    Raised when a representation-based strong ETag is requested on a response
    whose body cannot be buffered and hashed (a streaming or direct
    passthrough response). Such a response is not replayable: reading the
    body to compute the ETag would consume it, so the ETag must be provided
    manually with :meth:`Blueprint.set_etag <Blueprint.set_etag>`.
    """


class CurrentApiNotAvailableError(FlaskSmorestError):
    """`current_api` not available"""
