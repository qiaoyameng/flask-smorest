"""ETag feature"""

import hashlib
import http
import warnings
from copy import deepcopy
from functools import wraps

from flask import current_app, json, request

from .exceptions import (
    NotModified,
    PreconditionFailed,
    PreconditionRequired,
    RepresentationNotReplayableError,
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

    # Representation metadata included in the strong ETag. "Content-Type" is
    # added implicitly when computing the ETag from the final response.
    ETAG_REPRESENTATION_HEADERS = ["X-Pagination"]

    @staticmethod
    def _etag_header_items(response, header_names):
        """Return ``(name, value)`` pairs of selected representation headers

        Iterating ``response.headers`` preserves order and duplicate headers,
        so two representations differing only by a duplicate header value do
        not share the same ETag.
        """
        wanted = {name.lower() for name in header_names}
        return tuple(
            (name, value)
            for name, value in response.headers
            if name.lower() in wanted
        )

    @staticmethod
    def _if_match_matches(etags, new_etag):
        """Strong comparison against an ``If-Match`` header value

        Per RFC 9110, ``If-Match`` uses strong comparison: a weak validator
        in the list never matches. ``*`` matches when the selected
        representation exists, which is the case whenever an ETag can be
        computed.
        """
        # ``in`` performs a strong comparison and honours the ``*`` tag.
        return bool(etags) and new_etag in etags

    @staticmethod
    def _if_none_match_matches(etags, new_etag):
        """Weak comparison against an ``If-None-Match`` header value

        Per RFC 9110, ``If-None-Match`` uses weak comparison: weak and
        strong validators in the list match, and ``*`` matches when the
        selected representation exists.
        """
        return bool(etags) and (etags.star_tag or etags.contains_weak(new_etag))

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
                    _get_etag_ctx()[
                        "representation_based"
                    ] = self._is_representation_etag_enabled()

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
    def _generate_representation_etag(response):
        """Generate a strong ETag from the final response representation

        The ETag covers the exact bytes that are serialized on the wire
        (``response.get_data()``) together with the response metadata that
        participates in the selected representation: the ``Content-Type`` and
        the headers listed in ``ETAG_REPRESENTATION_HEADERS`` (e.g. the
        pagination metadata in ``X-Pagination``).

        Every piece of data fed to the hash is length-framed, so no
        ambiguity is possible between, say, a body ending with a digit and a
        header starting with one. The Python ``dict`` ``repr`` is never used:
        only the actual serialized bytes of the response are hashed.
        """
        body = response.get_data()
        content_type = response.headers.get("Content-Type", "")
        header_items = EtagMixin._etag_header_items(
            response, EtagMixin.ETAG_REPRESENTATION_HEADERS
        )
        return EtagMixin._hash_representation(body, content_type, header_items)

    @staticmethod
    def _hash_representation(body, content_type, header_items=()):
        """Hash length-framed representation bytes and metadata"""

        def feed(hash_obj, raw):
            hash_obj.update(str(len(raw)).encode("ascii"))
            hash_obj.update(b":")
            hash_obj.update(raw)

        etag_hash = hashlib.sha1()
        feed(etag_hash, body)
        feed(etag_hash, content_type.encode("utf-8"))
        for name, value in header_items:
            feed(etag_hash, name.encode("utf-8"))
            feed(etag_hash, value.encode("utf-8"))
        return etag_hash.hexdigest()

    def _generate_representation_etag_from_data(self, etag_data):
        """Generate a strong ETag from data as it will be serialized

        This reproduces the exact bytes ``jsonify`` would put on the wire for
        ``etag_data`` (including the trailing newline and using the Flask
        application JSON provider) together with the JSON content type. It is
        used by :meth:`check_etag` and :meth:`set_etag` so the tag they
        produce matches the one later computed from the final response.
        """
        response = current_app.json.response(etag_data)
        body = response.get_data()
        content_type = response.headers.get("Content-Type", "")
        return self._hash_representation(body, content_type)

    def _is_representation_etag_enabled(self):
        """Return True if strong ETags are computed from final responses"""
        ctx = _get_etag_ctx()
        if "representation_based" not in ctx:
            ctx["representation_based"] = current_api.config.get(
                "ETAG_REPRESENTATION_BASED", False
            )
        return ctx["representation_based"]

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
            if etag_schema is not None:
                etag_data = resolve_schema_instance(etag_schema).dump(etag_data)
            if self._is_representation_etag_enabled():
                new_etag = self._generate_representation_etag_from_data(
                    etag_data
                )
            else:
                new_etag = self._generate_etag(etag_data)
            _get_etag_ctx()["etag_checked"] = True
            if not self._if_match_matches(request.if_match, new_etag):
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

    def _check_not_modified(self, etag):
        """Raise NotModified if etag matches the If-None-Match header

        ``If-None-Match`` comparison is weak: weak validators and ``*``
        match. Only applies to methods returning a 304 (Not Modified) code.
        The ETag (and any ``Vary`` header) is stored in the app context
        first, so the error handler can attach it to the header-only 304
        response.
        """
        if request.method in self.METHODS_CHECKING_NOT_MODIFIED and (
            self._if_none_match_matches(request.if_none_match, etag)
        ):
            _get_etag_ctx()["etag"] = etag
            raise NotModified()

    def set_etag(self, etag_data, etag_schema=None):
        """Set ETag for this response

        Raise 304 if ETag identical to If-None-Match header

        Must be called from resource code, unless the view function is
        decorated with the ``response`` decorator, in which case the ETag is
        computed by default from response data if ``set_etag`` is not called.

        Issues a warning if called in a method other than GET, HEAD, POST, PUT
        or PATCH.
        """
        if request.method not in self.METHODS_ALLOWING_SET_ETAG:
            warnings.warn(
                f"ETag cannot be set on {request.method} request.",
                stacklevel=2,
            )
        if self._is_etag_enabled():
            if etag_schema is not None:
                etag_data = resolve_schema_instance(etag_schema).dump(etag_data)
            if self._is_representation_etag_enabled():
                new_etag = self._generate_representation_etag_from_data(
                    etag_data
                )
            else:
                new_etag = self._generate_etag(etag_data)
            if self._is_representation_etag_enabled():
                self._remember_representation_vary()
            self._check_not_modified(new_etag)
            # Store ETag in AppContext to add it to response headers later on
            _get_etag_ctx()["etag"] = new_etag

    def _set_etag_in_response(self, response):
        """Set ETag in response object

        Called automatically.

        If no ETag data was computed using set_etag, it is computed here from
        response data.

        When ``ETAG_REPRESENTATION_BASED`` is enabled, the ETag is a strong
        validator computed from the bytes of the final response (after schema
        serialization and wrapping) and from representation metadata
        (``Content-Type`` and pagination/other selected headers). A streaming
        or direct-passthrough response cannot be replayed to hash its body, so
        it is explicitly rejected unless ``set_etag`` was called manually.
        """
        if request.method in self.METHODS_ALLOWING_SET_ETAG:
            ctx = _get_etag_ctx()
            representation_based = ctx.get("representation_based", False)
            new_etag = ctx.get("etag")
            if new_etag is None:
                if representation_based:
                    self._set_representation_vary(response)
                    new_etag = self._generate_representation_etag_or_reject(
                        response
                    )
                else:
                    etag_data = get_appcontext()["result_dump"]
                    extra_data = self._etag_header_items(
                        response, self.ETAG_INCLUDE_HEADERS
                    )
                    new_etag = self._generate_etag(etag_data, extra_data)
                self._check_not_modified(new_etag)
                new_etag = ctx.get("etag", new_etag)
            if representation_based:
                self._set_representation_vary(response)
            response.set_etag(new_etag)

    def _generate_representation_etag_or_reject(self, response):
        """Compute representation ETag, rejecting non-replayable responses

        A streaming response (generator body) or a direct-passthrough
        response (e.g. ``send_file``) is consumed as it is sent. Buffering it
        to hash the bytes would break streaming, and hashing only the already
        buffered part would produce an ETag unrelated to the bytes the client
        receives. Such endpoints must call
        :meth:`set_etag <Blueprint.set_etag>` explicitly.
        """
        if response.direct_passthrough or response.is_streamed:
            raise RepresentationNotReplayableError(
                "Cannot compute a representation ETag for a streaming or "
                "direct-passthrough response: the body is not replayable. "
                "Call set_etag() explicitly in the view function."
            )
        return self._generate_representation_etag(response)

    def _set_representation_vary(self, response):
        """Advertise the request headers that select the representation

        The representation ETag covers ``Content-Type``, so caches must key
        on ``Accept``. The chosen values are remembered in the ETag context
        (so they survive on a body-less 304 response) and reflected on the
        response.
        """
        self._remember_representation_vary()
        response.vary.update(_get_etag_ctx()["vary"])

    def _remember_representation_vary(self):
        """Record representation-driven Vary entries in the ETag context"""
        ctx = _get_etag_ctx()
        vary = ctx.setdefault("vary", [])
        if "Accept" not in vary:
            vary.append("Accept")

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
