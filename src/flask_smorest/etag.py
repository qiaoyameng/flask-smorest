"""ETag feature"""

import hashlib
import http
import warnings
from copy import deepcopy
from functools import wraps

from flask import current_app, json, request

from .exceptions import (
    CurrentApiNotAvailableError,
    FlaskSmorestError,
    NotModified,
    PreconditionFailed,
    PreconditionRequired,
)
from .globals import current_api
from .utils import deepupdate, get_appcontext, resolve_schema_instance

IF_NONE_MATCH_HEADER = {
    "name": "If-None-Match",
    "in": "header",
    "description": "Tag to check against",
    "schema": {"type": "string"},
}

IF_MATCH_HEADER = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": "Tag to check against",
    "schema": {"type": "string"},
}

ETAG_HEADER = {
    "description": "Tag for the returned entry",
    "schema": {"type": "string"},
}


def _parse_etag_header(header):
    """Parse an If-Match / If-None-Match header

    Returns a (tags, star) tuple where tags is a list of (is_weak, tag)
    tuples and star is True if the header contains "*".

    Quoted tags are unquoted. Unquoted tags are accepted leniently.
    """
    tags = []
    star = False
    if not header:
        return tags, star
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            star = True
            continue
        is_weak = part.startswith("W/")
        if is_weak:
            part = part[2:].strip()
        if len(part) >= 2 and part.startswith('"') and part.endswith('"'):
            part = part[1:-1]
        tags.append((is_weak, part))
    return tags, star


def _etag_matches_if_none_match(etag, header):
    """Check etag against If-None-Match header using weak comparison

    Weak comparison: tags match if their opaque tags are equal, whether
    or not they are prefixed with the weak validator "W/". "*" matches
    any current representation.
    """
    tags, star = _parse_etag_header(header)
    return star or any(tag == etag for _, tag in tags)


def _etag_matches_if_match(etag, header):
    """Check etag against If-Match header using strong comparison

    Strong comparison: tags only match if they are equal, character for
    character, and neither is weak. "*" matches any current
    representation.
    """
    tags, star = _parse_etag_header(header)
    return star or any(not is_weak and tag == etag for is_weak, tag in tags)


def _get_etag_ctx():
    """Get ETag section of AppContext"""
    return get_appcontext().setdefault("etag", {})


class EtagMixin:
    """Extend Blueprint to add ETag handling"""

    METHODS_CHECKING_NOT_MODIFIED = ["GET", "HEAD"]
    METHODS_NEEDING_CHECK_ETAG = ["PUT", "PATCH", "DELETE"]
    METHODS_ALLOWING_SET_ETAG = ["GET", "HEAD", "POST", "PUT", "PATCH"]

    # Headers to include in ETag computation
    ETAG_INCLUDE_HEADERS = ["X-Pagination"]

    # ETag computation modes
    ETAG_MODES = ["data", "representation"]

    def etag(self, obj):
        """Decorator adding ETag management to the endpoint

        The ``etag`` decorator expects the decorated view function to return a
        ``Response`` object. It is the case if it is decorated with the
        ``response`` decorator.

        The ``etag`` decorator may be used to decorate a
        :class:`MethodView <flask.views.MethodView>`. In this case, it applies
        to all HTTP methods in the ``MethodView``.

        See :doc:`ETag <etag>`.
        """

        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                etag_enabled = self._is_etag_enabled()

                if etag_enabled:
                    # Check etag precondition
                    self._check_precondition()

                # Execute decorated function
                resp = current_app.ensure_sync(func)(*args, **kwargs)

                if etag_enabled:
                    # Verify check_etag was called in resource code if needed
                    self._verify_check_etag()
                    # Add etag value to response
                    self._set_etag_in_response(resp)

                return resp

            # Note function is decorated by etag in doc info
            # The deepcopy avoids modifying the wrapped function doc
            wrapper._apidoc = deepcopy(getattr(wrapper, "_apidoc", {}))
            wrapper._apidoc["etag"] = True

            return wrapper

        return self._decorate_view_func_or_method_view(decorator, obj)

    @staticmethod
    def _generate_etag(etag_data, extra_data=None):
        """Generate an ETag from data

        etag_data: Data to use to compute ETag
        extra_data: Extra data to add before hashing

        Typically, extra_data is used to add pagination metadata to the hash.
        It is not dumped through the Schema.

        Data is JSON serialized before hashing using the Flask app JSON serializer.
        """
        if extra_data:
            etag_data = (etag_data, extra_data)
        data = json.dumps(etag_data, sort_keys=True)
        return hashlib.sha1(bytes(data, "utf-8")).hexdigest()

    @staticmethod
    def _generate_etag_from_bytes(data, extra_data=None):
        """Generate an ETag from response representation bytes

        data: Response body as bytes, as serialized for the response
        extra_data: Extra (key, value) metadata pairs to add to the hash

        Typically, extra_data is used to add representation metadata such
        as pagination headers to the hash.
        """
        etag_hash = hashlib.sha1()
        etag_hash.update(data)
        for key, value in extra_data or ():
            etag_hash.update(key.encode("utf-8"))
            etag_hash.update(b"\0")
            etag_hash.update(value.encode("utf-8"))
            etag_hash.update(b"\0")
        return etag_hash.hexdigest()

    def _get_etag_mode(self):
        """Return ETag computation mode

        Modes:

        - "data" (default): ETag computed from data structures
        - "representation": strong ETag computed from the actual response
          representation bytes
        """
        try:
            mode = current_api.config.get("ETAG_MODE", "data")
        except (CurrentApiNotAvailableError, KeyError):
            # No current Api: fall back to default mode
            return "data"
        if mode not in self.ETAG_MODES:
            raise FlaskSmorestError(
                f"Invalid ETAG_MODE: {mode!r}. Must be one of {self.ETAG_MODES}."
            )
        return mode

    def _generate_etag_from_data(self, etag_data, etag_schema=None):
        """Generate an ETag from data, according to the ETag mode

        In "representation" mode, the data is serialized to JSON exactly
        like it would be in the response and the resulting bytes are
        hashed, so the ETag changes whenever the representation changes,
        even if the underlying data is identical.
        """
        if etag_schema is not None:
            etag_data = resolve_schema_instance(etag_schema).dump(etag_data)
        if self._get_etag_mode() == "representation":
            try:
                data = current_app.json.response(
                    self._prepare_response_content(etag_data)
                ).get_data()
            except TypeError as exc:
                raise FlaskSmorestError(
                    "ETag mode 'representation' requires JSON-serializable "
                    "data. Pass an etag_schema to serialize the data."
                ) from exc
            return self._generate_etag_from_bytes(data)
        return self._generate_etag(etag_data)

    def _generate_etag_from_response(self, response):
        """Generate a strong ETag from a response object

        Hashes the actual response body bytes and the headers in
        ETAG_INCLUDE_HEADERS.

        Streamed responses are not supported as their content can not be
        read without consuming the stream.
        """
        if response.is_streamed:
            raise FlaskSmorestError(
                "ETag mode 'representation' does not support streamed "
                "responses. Compute the ETag manually with set_etag or "
                "use ETag mode 'data'."
            )
        extra_data = tuple(
            (k, v) for k, v in response.headers if k in self.ETAG_INCLUDE_HEADERS
        )
        return self._generate_etag_from_bytes(response.get_data(), extra_data)

    def _check_precondition(self):
        """Check If-Match header is there

        Raise 428 if If-Match header missing

        Called automatically for PUT, PATCH and DELETE methods
        """
        # TODO: other methods?
        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/If-Match
        if request.method in self.METHODS_NEEDING_CHECK_ETAG and not request.if_match:
            raise PreconditionRequired

    def check_etag(self, etag_data, etag_schema=None):
        """Compare If-Match header with computed ETag

        Raise 412 if If-Match header does not match.

        Must be called from resource code to check ETag.

        The comparison uses the strong comparison rule: weak tags in the
        If-Match header never match.

        In "representation" ETag mode, the ETag is computed from the JSON
        serialization of the data, as it would be returned in the
        response, so the schema should be passed to serialize the data.

        Unfortunately, there is no way to call it automatically. It is the
        developer's responsability to do it. However, a warning is issued at
        runtime if this function was not called.

        Issues a warning if called in a method other than PUT, PATCH, or
        DELETE.
        """
        if request.method not in self.METHODS_NEEDING_CHECK_ETAG:
            warnings.warn(
                f"ETag cannot be checked on {request.method} request.",
                stacklevel=2,
            )
        if self._is_etag_enabled():
            new_etag = self._generate_etag_from_data(etag_data, etag_schema)
            _get_etag_ctx()["etag_checked"] = True
            if not _etag_matches_if_match(
                new_etag, request.headers.get("If-Match")
            ):
                raise PreconditionFailed

    def _is_etag_enabled(self):
        """Return True if ETag feature is enabled api-wise"""
        return not current_api.config.get("ETAG_DISABLED", False)

    def _verify_check_etag(self):
        """Verify check_etag was called in resource code

        Issues a warning if ETag is enabled but check_etag was not called in
        resource code in a PUT, PATCH or DELETE method.

        This is called automatically. It is meant to warn the developer about
        an issue in his ETag management.
        """
        if request.method in self.METHODS_NEEDING_CHECK_ETAG:
            if not _get_etag_ctx().get("etag_checked"):
                warnings.warn(
                    f"ETag not checked in endpoint {request.endpoint} "
                    f"on {request.method} request.",
                    stacklevel=2,
                )

    def _check_not_modified(self, etag, response=None):
        """Raise NotModified if etag is in If-None-Match header

        Only applies to methods returning a 304 (Not Modified) code

        If a response is passed, its ETag and Vary headers are copied to
        the 304 response.
        """
        if (
            request.method in self.METHODS_CHECKING_NOT_MODIFIED
            and _etag_matches_if_none_match(
                etag, request.headers.get("If-None-Match")
            )
        ):
            not_modified = NotModified()
            if response is not None:
                headers = {"ETag": f'"{etag}"'}
                if "Vary" in response.headers:
                    headers["Vary"] = response.headers["Vary"]
                not_modified.data = {"headers": headers}
            raise not_modified

    def set_etag(self, etag_data, etag_schema=None):
        """Set ETag for this response

        Raise 304 if ETag identical to If-None-Match header

        Must be called from resource code, unless the view function is
        decorated with the ``response`` decorator, in which case the ETag is
        computed by default from response data if ``set_etag`` is not called.

        The comparison with the If-None-Match header uses the weak
        comparison rule.

        In "representation" ETag mode, the ETag is computed from the JSON
        serialization of the data, as it would be returned in the
        response, so the schema should be passed to serialize the data.

        Issues a warning if called in a method other than GET, HEAD, POST, PUT
        or PATCH.
        """
        if request.method not in self.METHODS_ALLOWING_SET_ETAG:
            warnings.warn(
                f"ETag cannot be set on {request.method} request.",
                stacklevel=2,
            )
        if self._is_etag_enabled():
            new_etag = self._generate_etag_from_data(etag_data, etag_schema)
            self._check_not_modified(new_etag)
            # Store ETag in AppContext to add it to response headers later on
            _get_etag_ctx()["etag"] = new_etag

    def _set_etag_in_response(self, response):
        """Set ETag in response object

        Called automatically.

        If no ETag data was computed using set_etag, it is computed here from
        response data.
        """
        if request.method in self.METHODS_ALLOWING_SET_ETAG:
            new_etag = _get_etag_ctx().get("etag")
            # If no ETag data was manually provided, use response content
            if new_etag is None:
                if self._get_etag_mode() == "representation":
                    new_etag = self._generate_etag_from_response(response)
                else:
                    etag_data = get_appcontext()["result_dump"]
                    extra_data = tuple(
                        (k, v)
                        for k, v in response.headers
                        if k in self.ETAG_INCLUDE_HEADERS
                    )
                    new_etag = self._generate_etag(etag_data, extra_data)
                self._check_not_modified(new_etag, response=response)
            response.set_etag(new_etag)

    def _prepare_etag_doc(self, doc, doc_info, *, api, spec, method, **kwargs):
        if doc_info.get("etag", False) and not api.config.get("ETAG_DISABLED", False):
            responses = {}
            method_u = method.upper()
            if method_u in self.METHODS_CHECKING_NOT_MODIFIED:
                responses[304] = http.HTTPStatus(304).name
                doc.setdefault("parameters", []).append("IF_NONE_MATCH")
            if method_u in self.METHODS_NEEDING_CHECK_ETAG:
                responses[412] = http.HTTPStatus(412).name
                responses[428] = http.HTTPStatus(428).name
                doc.setdefault("parameters", []).append("IF_MATCH")
            if method_u in self.METHODS_ALLOWING_SET_ETAG:
                success_status_codes = doc_info.get("success_status_codes", [])
                for success_status_code in success_status_codes:
                    doc["responses"][success_status_code].setdefault("headers", {})[
                        "ETag"
                    ] = ETAG_HEADER if spec.openapi_version.major < 3 else "ETAG"

            if responses:
                doc = deepupdate(doc, {"responses": responses})
        return doc
