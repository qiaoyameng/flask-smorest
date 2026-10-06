"""Test EtagMixin"""

import hashlib
import json

import pytest
import marshmallow as ma

from flask import Response, jsonify
from flask import request as f_request
from flask.views import MethodView

from flask_smorest import Api, Blueprint, abort
from flask_smorest.etag import _get_etag_ctx
from flask_smorest.exceptions import (
    CurrentApiNotAvailableError,
    NotModified,
    PreconditionFailed,
    PreconditionRequired,
    RepresentationNotReplayableError,
)
from flask_smorest.utils import get_appcontext

from .mocks import ItemNotFound
from .utils import build_ref, request_ctx_with_current_api

HTTP_METHODS = ["OPTIONS", "HEAD", "GET", "POST", "PUT", "PATCH", "DELETE"]
HTTP_METHODS_ALLOWING_SET_ETAG = ["GET", "HEAD", "POST", "PUT", "PATCH"]


@pytest.fixture(params=[True, False])
def app_with_etag(request, collection, schemas, app):
    """Return a basic API sample with ETag"""

    as_method_view = request.param
    DocSchema = schemas.DocSchema
    blp = Blueprint("test", __name__, url_prefix="/test")

    if as_method_view:
        # Decorate each function
        @blp.route("/")
        class Resource(MethodView):
            @blp.etag
            @blp.response(200, DocSchema(many=True))
            def get(self):
                return collection.items

            @blp.etag
            @blp.arguments(DocSchema)
            @blp.response(201, DocSchema)
            def post(self, new_item):
                return collection.post(new_item)

        # Better: decorate the whole MethodView
        @blp.route("/<int:item_id>")
        @blp.etag
        class ResourceById(MethodView):
            def _get_item(self, item_id):
                try:
                    return collection.get_by_id(item_id)
                except ItemNotFound:
                    abort(404)

            @blp.response(200, DocSchema)
            def get(self, item_id):
                return self._get_item(item_id)

            @blp.arguments(DocSchema)
            @blp.response(200, DocSchema)
            def put(self, new_item, item_id):
                item = self._get_item(item_id)
                blp.check_etag(item, DocSchema)
                return collection.put(item_id, new_item)

            @blp.response(204)
            def delete(self, item_id):
                item = self._get_item(item_id)
                blp.check_etag(item, DocSchema)
                del collection.items[collection.items.index(item)]

    else:

        @blp.route("/")
        @blp.etag
        @blp.response(200, DocSchema(many=True))
        def get_resources():
            return collection.items

        @blp.route("/", methods=("POST",))
        @blp.etag
        @blp.arguments(DocSchema)
        @blp.response(201, DocSchema)
        def post_resource(new_item):
            return collection.post(new_item)

        def _get_item(item_id):
            try:
                return collection.get_by_id(item_id)
            except ItemNotFound:
                abort(404)

        @blp.route("/<int:item_id>")
        @blp.etag
        @blp.response(200, DocSchema)
        def get_resource(item_id):
            return _get_item(item_id)

        @blp.route("/<int:item_id>", methods=("PUT",))
        @blp.etag
        @blp.arguments(DocSchema)
        @blp.response(200, DocSchema)
        def put_resource(new_item, item_id):
            item = _get_item(item_id)
            blp.check_etag(item, DocSchema)
            return collection.put(item_id, new_item)

        @blp.route("/<int:item_id>", methods=("DELETE",))
        @blp.etag
        @blp.response(204)
        def delete_resource(item_id):
            item = _get_item(item_id)
            blp.check_etag(item, DocSchema)
            del collection.items[collection.items.index(item)]

    api = Api(app)
    api.register_blueprint(blp)

    return app


class TestEtag:
    """Test EtagMixin"""

    @pytest.mark.parametrize("extra_data", [None, {}, {"answer": 42}])
    def test_etag_generate_etag(self, extra_data):
        blp = Blueprint("test", __name__)
        item = {"item_id": 1, "db_field": 0}
        data = (item, extra_data) if extra_data else item

        assert (
            blp._generate_etag(item, extra_data=extra_data)
            == hashlib.sha1(
                bytes(json.dumps(data, sort_keys=True), "utf-8")
            ).hexdigest()
        )

    def test_etag_generate_etag_order_insensitive(self):
        blp = Blueprint("test", __name__)
        data_1 = {"a": 1, "b": 2}
        data_2 = {"b": 2, "a": 1}
        assert blp._generate_etag(data_1) == blp._generate_etag(data_2)

    @pytest.mark.parametrize("method", HTTP_METHODS)
    def test_etag_check_precondition(self, app, method):
        api = Api(app)
        blp = Blueprint("test", __name__)
        api.register_blueprint(blp)

        with request_ctx_with_current_api(app, blp, "/", method=method):
            if method in ["PUT", "PATCH", "DELETE"]:
                with pytest.raises(PreconditionRequired):
                    blp._check_precondition()
            else:
                blp._check_precondition()

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    @pytest.mark.parametrize("etag_disabled", (True, False))
    def test_etag_check_etag(self, app, schemas, method, etag_disabled):
        app.config["ETAG_DISABLED"] = etag_disabled
        api = Api(app)
        blp = Blueprint("test", __name__)
        api.register_blueprint(blp)
        schema = schemas.DocSchema
        old_item = {"item_id": 1, "db_field": 0}
        new_item = {"item_id": 1, "db_field": 1}
        old_etag = blp._generate_etag(old_item)
        old_etag_with_schema = blp._generate_etag(schema().dump(old_item))

        with request_ctx_with_current_api(
            app,
            blp,
            "/",
            method=method,
            headers={"If-Match": old_etag},
        ):
            blp.check_etag(old_item)
            if not etag_disabled:
                with pytest.raises(PreconditionFailed):
                    blp.check_etag(new_item)
            else:
                blp.check_etag(new_item)
        with request_ctx_with_current_api(
            app,
            blp,
            "/",
            method=method,
            headers={"If-Match": old_etag_with_schema},
        ):
            blp.check_etag(old_item, schema)
            if not etag_disabled:
                with pytest.raises(PreconditionFailed):
                    blp.check_etag(new_item, schema)
            else:
                blp.check_etag(new_item)

    @pytest.mark.parametrize("method", HTTP_METHODS)
    @pytest.mark.parametrize("etag_disabled", (True, False))
    def test_etag_check_etag_wrong_method_warning(
        self, app, method, etag_disabled, recwarn
    ):
        app.config["ETAG_DISABLED"] = etag_disabled
        api = Api(app)
        blp = Blueprint("test", __name__)
        api.register_blueprint(blp)

        with request_ctx_with_current_api(
            app,
            blp,
            "/",
            method=method,
            headers={"If-Match": ""},
        ):
            # Ignore ETag check fail. Just testing the warning.
            try:
                blp.check_etag(None)
            except PreconditionFailed:
                pass
        if method in ["PUT", "PATCH", "DELETE"]:
            assert not recwarn
        else:
            assert len(recwarn) == 1
            assert recwarn[0].category is UserWarning
            assert str(recwarn[0].message) == (
                f"ETag cannot be checked on {method} request."
            )

    @pytest.mark.parametrize("method", HTTP_METHODS)
    def test_etag_verify_check_etag_warning(self, app, method, recwarn):
        api = Api(app)
        blp = Blueprint("test", __name__)
        api.register_blueprint(blp)
        old_item = {"item_id": 1, "db_field": 0}
        old_etag = blp._generate_etag(old_item)

        with request_ctx_with_current_api(
            app,
            blp,
            "/",
            method=method,
            headers={"If-Match": old_etag},
        ):
            blp._verify_check_etag()
            if method in ["PUT", "PATCH", "DELETE"]:
                assert len(recwarn) == 1
                assert recwarn[0].category is UserWarning
                assert str(recwarn[0].message) == (
                    f"ETag not checked in endpoint {f_request.endpoint} "
                    f"on {method} request."
                )
            else:
                assert not recwarn
            blp.check_etag(old_item)
            recwarn.clear()
            blp._verify_check_etag()
            assert not recwarn

    @pytest.mark.parametrize("method", HTTP_METHODS_ALLOWING_SET_ETAG)
    @pytest.mark.parametrize("etag_disabled", (True, False))
    def test_etag_set_etag(self, app, schemas, method, etag_disabled):
        app.config["ETAG_DISABLED"] = etag_disabled
        api = Api(app)
        blp = Blueprint("test", __name__)
        api.register_blueprint(blp)
        schema = schemas.DocSchema
        item = {"item_id": 1, "db_field": 0}
        etag = blp._generate_etag(item)
        etag_with_schema = blp._generate_etag(schema().dump(item))

        with request_ctx_with_current_api(app, blp, "/", method=method):
            blp.set_etag(item)
            if not etag_disabled:
                assert _get_etag_ctx()["etag"] == etag
                del _get_etag_ctx()["etag"]
            else:
                assert "etag" not in _get_etag_ctx()
        with request_ctx_with_current_api(
            app,
            blp,
            "/",
            method=method,
            headers={"If-None-Match": etag},
        ):
            if not etag_disabled:
                if method in ["GET", "HEAD"]:
                    with pytest.raises(NotModified):
                        blp.set_etag(item)
            else:
                blp.set_etag(item)
                assert "etag" not in _get_etag_ctx()
        with request_ctx_with_current_api(
            app,
            blp,
            "/",
            method=method,
            headers={"If-None-Match": etag_with_schema},
        ):
            if not etag_disabled:
                if method in ["GET", "HEAD"]:
                    with pytest.raises(NotModified):
                        blp.set_etag(item, schema)
            else:
                blp.set_etag(item, schema)
                assert "etag" not in _get_etag_ctx()
        with request_ctx_with_current_api(
            app,
            blp,
            "/",
            method=method,
            headers={"If-None-Match": "dummy"},
        ):
            if not etag_disabled:
                blp.set_etag(item)
                assert _get_etag_ctx()["etag"] == etag
                del _get_etag_ctx()["etag"]
                blp.set_etag(item, schema)
                assert _get_etag_ctx()["etag"] == etag_with_schema
                del _get_etag_ctx()["etag"]
            else:
                blp.set_etag(item)
                assert "etag" not in _get_etag_ctx()
                blp.set_etag(item, schema)
                assert "etag" not in _get_etag_ctx()

    @pytest.mark.parametrize("etag_disabled", (True, False))
    @pytest.mark.parametrize("method", HTTP_METHODS)
    def test_etag_set_etag_method_not_allowed_warning(
        self, app, method, etag_disabled, recwarn
    ):
        app.config["ETAG_DISABLED"] = etag_disabled
        api = Api(app)
        blp = Blueprint("test", __name__)
        api.register_blueprint(blp)

        with request_ctx_with_current_api(app, blp, "/", method=method):
            blp.set_etag(None)
        if method in HTTP_METHODS_ALLOWING_SET_ETAG:
            assert not recwarn
        else:
            assert len(recwarn) == 1
            assert recwarn[0].category is UserWarning
            assert str(recwarn[0].message) == (
                f"ETag cannot be set on {method} request."
            )

    @pytest.mark.parametrize("paginate", (True, False))
    def test_etag_set_etag_in_response(self, app, schemas, paginate):
        blp = Blueprint("test", __name__)
        item = {"item_id": 1, "db_field": 0}
        if paginate:
            extra_data = (("X-Pagination", "Dummy pagination header"),)
        else:
            extra_data = tuple()
        etag = blp._generate_etag(item, extra_data=extra_data)

        with app.test_request_context("/"):
            resp = Response()
            if extra_data:
                resp.headers["X-Pagination"] = "Dummy pagination header"
            get_appcontext()["result_dump"] = item
            blp._set_etag_in_response(resp)
            assert resp.get_etag() == (etag, False)

    def test_if_none_match_comparison_rules(self, app):
        """If-None-Match uses weak comparison (also in legacy mode)"""
        api = Api(app)
        blp = Blueprint("test", __name__, url_prefix="/test")
        client = app.test_client()

        @blp.route("/")
        @blp.etag
        @blp.response(200)
        def get_resource():
            return {}

        api.register_blueprint(blp)
        etag = client.get("/test/").headers["ETag"]
        raw = etag.strip('"')

        assert (
            client.get("/test/", headers={"If-None-Match": etag}).status_code
            == 304
        )
        # Weak validator: weak comparison matches
        assert (
            client.get(
                "/test/", headers={"If-None-Match": f'W/"{raw}"'}
            ).status_code
            == 304
        )
        # Star
        assert (
            client.get("/test/", headers={"If-None-Match": "*"}).status_code
            == 304
        )
        # List containing the matching tag
        assert (
            client.get(
                "/test/",
                headers={"If-None-Match": f'"nope", "{raw}"'},
            ).status_code
            == 304
        )
        # No match: 200 with a body
        mismatch = client.get("/test/", headers={"If-None-Match": '"nope"'})
        assert mismatch.status_code == 200
        assert mismatch.data

    def test_304_response_keeps_etag_header(self, app):
        """304 has no body but still carries the representation ETag"""
        api = Api(app)
        blp = Blueprint("test", __name__, url_prefix="/test")
        client = app.test_client()

        @blp.route("/")
        @blp.etag
        @blp.response(200)
        def get_resource():
            return {}

        api.register_blueprint(blp)
        etag = client.get("/test/").headers["ETag"]
        response = client.get("/test/", headers={"If-None-Match": etag})
        assert response.status_code == 304
        assert response.data == b""
        assert response.headers["ETag"] == etag

    def test_if_match_ignores_weak_validators(self, app):
        """If-Match uses strong comparison: a weak validator never matches"""
        blp = Blueprint("test", __name__, url_prefix="/test")

        with app.test_request_context(
            "/test/", method="PUT", headers={"If-Match": 'W/"tag"'}
        ):
            assert not blp._if_match_matches(f_request.if_match, "tag")
        with app.test_request_context(
            "/test/", method="PUT", headers={"If-Match": '"tag"'}
        ):
            assert blp._if_match_matches(f_request.if_match, "tag")
        with app.test_request_context(
            "/test/", method="PUT", headers={"If-Match": "*"}
        ):
            assert blp._if_match_matches(f_request.if_match, "tag")
        with app.test_request_context(
            "/test/", method="PUT", headers={"If-Match": '"other"'}
        ):
            assert not blp._if_match_matches(f_request.if_match, "tag")

    def test_etag_duplicate_header(self, app):
        """Check duplicate header results in a different ETag"""

        class CustomBlueprint(Blueprint):
            ETAG_INCLUDE_HEADERS = Blueprint.ETAG_INCLUDE_HEADERS + ["X-test"]

        blp = CustomBlueprint("test", __name__, url_prefix="/test")

        with app.test_request_context("/"):
            resp = Response()
            resp.headers.add("X-test", "Test")
            get_appcontext()["result_dump"] = {}
            blp._set_etag_in_response(resp)
            etag_1 = resp.get_etag()

        with app.test_request_context("/"):
            resp = Response()
            resp.headers.add("X-test", "Test")
            resp.headers.add("X-test", "Test")
            get_appcontext()["result_dump"] = {}
            blp._set_etag_in_response(resp)
            etag_2 = resp.get_etag()

        assert etag_1 != etag_2

    def test_etag_response_object(self, app):
        api = Api(app)
        blp = Blueprint("test", __name__, url_prefix="/test")
        client = app.test_client()

        @blp.route("/<code>")
        @blp.etag
        @blp.response(200)
        @blp.alt_response(201, success=True)
        def func_response_etag(code):
            # When the view function returns a Response object,
            # the ETag must be specified manually
            # This is always the case when using alt_response
            blp.set_etag("test")
            return jsonify({}), code

        api.register_blueprint(blp)

        response = client.get("/test/200")
        assert response.json == {}
        assert response.get_etag() == (blp._generate_etag("test"), False)
        response = client.get("/test/201")
        assert response.json == {}
        assert response.get_etag() == (blp._generate_etag("test"), False)

    def test_etag_operations_etag_enabled(self, app_with_etag):
        client = app_with_etag.test_client()

        # GET without ETag: OK
        response = client.get("/test/")
        assert response.status_code == 200
        list_etag = response.headers["ETag"]

        # GET with correct ETag: Not modified
        response = client.get("/test/", headers={"If-None-Match": list_etag})
        assert response.status_code == 304

        # POST item_1
        item_1_data = {"field": 0}
        response = client.post(
            "/test/", data=json.dumps(item_1_data), content_type="application/json"
        )
        assert response.status_code == 201
        item_1_id = response.json["item_id"]

        # GET with wrong/outdated ETag: OK
        response = client.get("/test/", headers={"If-None-Match": list_etag})
        assert response.status_code == 200

        # GET by ID without ETag: OK
        response = client.get(f"/test/{item_1_id}")
        assert response.status_code == 200
        item_etag = response.headers["ETag"]

        # GET by ID with correct ETag: Not modified
        response = client.get(
            f"/test/{item_1_id}", headers={"If-None-Match": item_etag}
        )
        assert response.status_code == 304

        # PUT without ETag: Precondition required error
        item_1_data["field"] = 1
        response = client.put(
            f"/test/{item_1_id}",
            data=json.dumps(item_1_data),
            content_type="application/json",
        )
        assert response.status_code == 428

        # PUT with correct ETag: OK
        response = client.put(
            f"/test/{item_1_id}",
            data=json.dumps(item_1_data),
            content_type="application/json",
            headers={"If-Match": item_etag},
        )
        assert response.status_code == 200
        new_item_etag = response.headers["ETag"]

        # PUT with wrong/outdated ETag: Precondition failed error
        item_1_data["field"] = 2
        response = client.put(
            f"/test/{item_1_id}",
            data=json.dumps(item_1_data),
            content_type="application/json",
            headers={"If-Match": item_etag},
        )
        assert response.status_code == 412

        # GET by ID with wrong/outdated ETag: OK
        response = client.get(
            f"/test/{item_1_id}", headers={"If-None-Match": item_etag}
        )
        assert response.status_code == 200

        # DELETE without ETag: Precondition required error
        response = client.delete(f"/test/{item_1_id}")
        assert response.status_code == 428

        # DELETE with wrong/outdated ETag: Precondition failed error
        response = client.delete(f"/test/{item_1_id}", headers={"If-Match": item_etag})
        assert response.status_code == 412

        # DELETE with correct ETag: No Content
        response = client.delete(
            f"/test/{item_1_id}", headers={"If-Match": new_item_etag}
        )
        assert response.status_code == 204

    def test_etag_operations_etag_disabled(self, app_with_etag):
        app_with_etag.config["ETAG_DISABLED"] = True
        client = app_with_etag.test_client()

        # GET without ETag: OK
        response = client.get("/test/")
        assert response.status_code == 200

        # GET with whatever ETag: OK (dummy ETag ignored)
        response = client.get("/test/", headers={"If-None-Match": "dummy_etag"})
        assert response.status_code == 200

        # POST item_1
        item_1_data = {"field": 0}
        response = client.post(
            "/test/", data=json.dumps(item_1_data), content_type="application/json"
        )
        assert response.status_code == 201
        item_1_id = response.json["item_id"]

        # GET by ID: OK
        response = client.get(f"/test/{item_1_id}")
        assert response.status_code == 200

        # GET by ID with whatever ETag: OK (dummy ETag ignored)
        response = client.get(
            f"/test/{item_1_id}", headers={"If-None-Match": "dummy_etag"}
        )
        assert response.status_code == 200

        # PUT without ETag: OK
        item_1_data["field"] = 1
        response = client.put(
            f"/test/{item_1_id}",
            data=json.dumps(item_1_data),
            content_type="application/json",
        )
        assert response.status_code == 200

        # PUT with whatever ETag: OK (dummy ETag ignored)
        item_1_data["field"] = 2
        response = client.put(
            f"/test/{item_1_id}",
            data=json.dumps(item_1_data),
            content_type="application/json",
        )
        assert response.status_code == 200

        # POST item_2
        item_2_data = {"field": 9}
        response = client.post(
            "/test/", data=json.dumps(item_2_data), content_type="application/json"
        )
        assert response.status_code == 201
        item_2_id = response.json["item_id"]

        # DELETE without ETag: No Content (dummy ETag ignored)
        response = client.delete(f"/test/{item_1_id}")
        assert response.status_code == 204

        # DELETE with whatever ETag: No Content (dummy ETag ignored)
        response = client.delete(
            f"/test/{item_2_id}", headers={"If-Match": "dummy_etag"}
        )
        assert response.status_code == 204

    @pytest.mark.parametrize("decorate", (True, False))
    @pytest.mark.parametrize("has_response", (True, False))
    @pytest.mark.parametrize("etag_disabled", (True, False))
    @pytest.mark.parametrize(
        "method", ("OPTIONS", "HEAD", "GET", "POST", "PUT", "PATCH", "DELETE")
    )
    def test_etag_documents_responses(
        self,
        app,
        method,
        decorate,
        etag_disabled,
        has_response,
    ):
        app.config["ETAG_DISABLED"] = etag_disabled
        api = Api(app)
        blp = Blueprint("test", "test", url_prefix="/test")

        if decorate:
            if has_response:

                @blp.route("/", methods=[method])
                @blp.response(204)
                @blp.etag
                def func():
                    pass

            else:

                @blp.route("/", methods=[method])
                @blp.etag
                def func():
                    pass

        else:
            if has_response:

                @blp.route("/", methods=[method])
                @blp.response(204)
                def func():
                    pass

            else:

                @blp.route("/", methods=[method])
                def func():
                    pass

        api.register_blueprint(blp)

        operation = api.spec.to_dict()["paths"]["/test/"][method.lower()]
        responses = operation.get("responses", {})
        response_headers = responses.get("204", {}).get("headers", {})
        parameters = operation.get("parameters", [])

        if not decorate or etag_disabled:
            assert "304" not in responses
            assert "412" not in responses
            assert "428" not in responses
            assert "IF_NONE_MATCH" not in parameters
            assert "IF_MATCH" not in parameters
            assert "ETag" not in response_headers
        else:
            assert ("304" in responses) == (method in ["GET", "HEAD"])
            assert ("412" in responses) == (method in ["PUT", "PATCH", "DELETE"])
            assert ("428" in responses) == (method in ["PUT", "PATCH", "DELETE"])
            assert (
                build_ref(api.spec, "parameter", "IF_NONE_MATCH") in parameters
            ) == (method in ["GET", "HEAD"])
            assert (build_ref(api.spec, "parameter", "IF_MATCH") in parameters) == (
                method in ["PUT", "PATCH", "DELETE"]
            )
            assert not has_response or (
                response_headers.get("ETag") == build_ref(api.spec, "header", "ETAG")
            ) == (method in ["GET", "HEAD", "POST", "PUT", "PATCH"])

    @pytest.mark.parametrize("etag_disabled_for_v1", [False, True])
    @pytest.mark.parametrize("etag_disabled_for_v2", [False, True])
    def test_multiple_apis_per_app(
        self, app, etag_disabled_for_v1, etag_disabled_for_v2
    ):
        # All created APIs are using prefix. So default ETAG_DISABLED should be ignored
        app.config["ETAG_DISABLED"] = True
        app.config["V1_ETAG_DISABLED"] = etag_disabled_for_v1
        app.config["V2_ETAG_DISABLED"] = etag_disabled_for_v2

        for i in [1, 2]:
            api = Api(
                app,
                config_prefix=f"V{i}_",
                spec_kwargs={
                    "title": f"V{i}",
                    "version": f"{i}",
                    "openapi_version": "3.0.2",
                },
            )
            blp = Blueprint(f"test{i}", f"test{i}", url_prefix=f"/test-{i}")

            @blp.route("/")
            @blp.etag
            @blp.response(200)
            def test():
                return {}

            api.register_blueprint(blp)

        client = app.test_client()
        headers1 = client.get("/test-1/").headers
        headers2 = client.get("/test-2/").headers

        if etag_disabled_for_v1:
            assert "ETag" not in headers1
        else:
            assert "ETag" in headers1

        if etag_disabled_for_v2:
            assert "ETag" not in headers2
        else:
            assert "ETag" in headers2

    def test_trying_to_use_etag_without_current_api(self, app, collection):
        Api(app)
        blp = Blueprint("test", "test")
        collection.post({"item_id": 1, "field": "test"})
        item = collection.items[0]
        with app.test_request_context(f"/test/{item['item_id']}"):
            with pytest.raises(CurrentApiNotAvailableError):
                blp.set_etag(item)

    def test_etag_async_view(self, app):
        api = Api(app)
        blp = Blueprint("test", __name__, url_prefix="/test")
        client = app.test_client()

        @blp.route("/")
        @blp.etag
        @blp.response(200)
        async def func_response_etag():
            blp.set_etag("waow")
            return jsonify({}), 200

        api.register_blueprint(blp)

        response = client.get("/test/")
        assert response.json == {}
        assert response.get_etag() == (blp._generate_etag("waow"), False)


class TestRepresentationEtag:
    """Test strong ETag computed from the final response representation"""

    ITEM = {"item_id": 1, "db_field": 7}

    @pytest.fixture
    def rep_app(self, app):
        app.config["ETAG_REPRESENTATION_BASED"] = True
        api = Api(app)
        blp = Blueprint("test", __name__, url_prefix="/test")

        class FullSchema(ma.Schema):
            item_id = ma.fields.Int()
            field = ma.fields.Int(attribute="db_field")

        class BriefSchema(ma.Schema):
            item_id = ma.fields.Int()

        @blp.route("/full")
        @blp.etag
        @blp.response(200, FullSchema)
        def full():
            return self.ITEM

        @blp.route("/brief")
        @blp.etag
        @blp.response(200, BriefSchema)
        def brief():
            return self.ITEM

        @blp.route("/full", methods=["PUT"])
        @blp.etag
        @blp.arguments(FullSchema)
        @blp.response(200, FullSchema)
        def put_full(new_item):
            blp.check_etag(self.ITEM, FullSchema)
            return self.ITEM

        @blp.route("/stream")
        @blp.etag
        @blp.response(200)
        def stream():
            return Response(
                iter([b'{"a": 1}']), mimetype="application/json"
            )

        @blp.route("/stream-manual")
        @blp.etag
        @blp.response(200)
        def stream_manual():
            blp.set_etag(FullSchema().dump(self.ITEM))
            return Response(
                iter([b'{"a": 1}']), mimetype="application/json"
            )

        api.register_blueprint(blp)
        return app, blp, FullSchema, BriefSchema

    @staticmethod
    def _hash_representation(body, content_type, header_items=()):
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

    def test_etag_is_hash_of_real_serialized_bytes(self, rep_app):
        app, _, _, _ = rep_app
        client = app.test_client()
        response = client.get("/test/full")
        # The actual wire bytes (real JSON, trailing newline included)
        assert response.data == b'{"field":7,"item_id":1}\n'
        expected = self._hash_representation(
            response.data, response.headers["Content-Type"]
        )
        # Strong validator: quoted, not prefixed with W/
        assert response.headers["ETag"] == f'"{expected}"'
        assert response.headers["Vary"] == "Accept"

    def test_etag_changes_with_schema_representation(self, rep_app):
        """Same business object, different schema output => different ETag"""
        app, _, _, _ = rep_app
        client = app.test_client()
        full = client.get("/test/full")
        brief = client.get("/test/brief")
        # Same source object, different serialized representations
        assert full.data != brief.data
        assert full.headers["ETag"] != brief.headers["ETag"]

    def test_etag_changes_with_content_type(self, rep_app):
        app, _, _, _ = rep_app
        client = app.test_client()
        full = client.get("/test/full")

        # The representation metadata (Content-Type) participates in the
        # hash: same body with another Content-Type yields another ETag.
        other_content_type_etag = (
            '"'
            + self._hash_representation(
                full.data, "application/xml"
            )
            + '"'
        )
        assert other_content_type_etag != full.headers["ETag"]
        assert full.headers["ETag"] == (
            '"'
            + self._hash_representation(
                full.data, full.headers["Content-Type"]
            )
            + '"'
        )

    def test_if_none_match_strong_tag_returns_304(self, rep_app):
        app, _, _, _ = rep_app
        client = app.test_client()
        etag = client.get("/test/full").headers["ETag"]
        response = client.get("/test/full", headers={"If-None-Match": etag})
        assert response.status_code == 304
        assert response.data == b""
        assert response.headers["ETag"] == etag
        assert response.headers["Vary"] == "Accept"

    @pytest.mark.parametrize(
        "header_value",
        [
            'W/"{etag}"',  # weak validator: weak comparison matches
            "*",  # star matches any existing representation
            '"nope", "{etag}"',  # list containing the matching tag
            'W/"nope", W/"{etag}"',  # list with weak matching tag
        ],
    )
    def test_if_none_match_weak_comparison(self, rep_app, header_value):
        app, _, _, _ = rep_app
        client = app.test_client()
        etag = client.get("/test/full").headers["ETag"].strip('"')
        response = client.get(
            "/test/full",
            headers={"If-None-Match": header_value.format(etag=etag)},
        )
        assert response.status_code == 304
        # ETag/Vary preserved even without a body
        assert response.headers["ETag"] == f'"{etag}"'
        assert response.headers["Vary"] == "Accept"

    def test_if_none_match_mismatch_returns_full_body(self, rep_app):
        app, _, _, _ = rep_app
        client = app.test_client()
        original = client.get("/test/full")
        response = client.get(
            "/test/full", headers={"If-None-Match": '"other"'}
        )
        assert response.status_code == 200
        # Conditional miss returns exactly the same bytes as an unconditional
        # request.
        assert response.data == original.data
        assert response.headers["ETag"] == original.headers["ETag"]

    def test_head_has_no_body_but_keeps_etag_and_vary(self, rep_app):
        app, _, _, _ = rep_app
        client = app.test_client()
        get_response = client.get("/test/full")
        head_response = client.head("/test/full")
        assert head_response.status_code == 200
        assert head_response.data == b""
        assert head_response.headers["ETag"] == get_response.headers["ETag"]
        assert head_response.headers["Vary"] == "Accept"
        # HEAD-computed tag validates the same representation as GET
        cond_get = client.get(
            "/test/full",
            headers={"If-None-Match": head_response.headers["ETag"]},
        )
        assert cond_get.status_code == 304

    def test_if_match_strong_comparison_rules(self, rep_app):
        app, _, _, _ = rep_app
        client = app.test_client()
        etag = client.get("/test/full").headers["ETag"]
        payload = json.dumps({"item_id": 1, "field": 7})
        kwargs = {"data": payload, "content_type": "application/json"}

        # Strong tag matches
        assert (
            client.put("/test/full", headers={"If-Match": etag}, **kwargs).status_code
            == 200
        )
        # Weak validator never satisfies If-Match (strong comparison)
        weak = "W/" + etag
        assert (
            client.put("/test/full", headers={"If-Match": weak}, **kwargs).status_code
            == 412
        )
        # Star matches an existing representation
        assert (
            client.put("/test/full", headers={"If-Match": "*"}, **kwargs).status_code
            == 200
        )
        # List with the matching strong tag
        assert (
            client.put(
                "/test/full",
                headers={"If-Match": f'"nope", {etag}'},
                **kwargs,
            ).status_code
            == 200
        )
        # List with only a weak version does not match
        assert (
            client.put(
                "/test/full",
                headers={"If-Match": f'"nope", W/{etag}'},
                **kwargs,
            ).status_code
            == 412
        )
        # List without the tag does not match
        assert (
            client.put(
                "/test/full", headers={"If-Match": '"nope"'}, **kwargs
            ).status_code
            == 412
        )
        # Missing precondition
        assert client.put("/test/full", **kwargs).status_code == 428

    def test_check_etag_tag_matches_response_tag(self, rep_app):
        """check_etag must compare against the final representation tag"""
        app, _, FullSchema, _ = rep_app
        client = app.test_client()
        etag = client.get("/test/full").headers["ETag"]
        # A PUT with the representation tag passes check_etag
        response = client.put(
            "/test/full",
            data=json.dumps({"item_id": 1, "field": 7}),
            content_type="application/json",
            headers={"If-Match": etag},
        )
        assert response.status_code == 200

    def test_pagination_metadata_participates_in_etag(self, app, schemas):
        """X-Pagination metadata is part of the representation ETag"""
        app.config["ETAG_REPRESENTATION_BASED"] = True
        api = Api(app)
        blp = Blueprint("test", __name__, url_prefix="/test")
        items = [
            {"item_id": 1, "db_field": 0},
            {"item_id": 2, "db_field": 1},
        ]

        @blp.route("/")
        @blp.etag
        @blp.response(200, schemas.DocSchema(many=True))
        @blp.paginate(page=1, page_size=1)
        def get_resources(pagination_parameters):
            pagination_parameters.item_count = len(items)
            return items[
                pagination_parameters.first_item : pagination_parameters.last_item
                + 1
            ]

        api.register_blueprint(blp)
        client = app.test_client()

        page_1 = client.get("/test/")
        page_2 = client.get("/test/?page=2")
        # The bodies differ, of course
        assert page_1.data != page_2.data
        # Pagination metadata differs and is part of the ETag: recompute the
        # ETag of page 2's body *with page 1's pagination header* and check
        # it neither equals the real page 1 nor page 2 ETag.
        assert page_1.headers["ETag"] != page_2.headers["ETag"]
        assert page_1.headers["X-Pagination"]
        assert page_2.headers["X-Pagination"]
        assert (
            page_1.headers["X-Pagination"] != page_2.headers["X-Pagination"]
        )
        # 304 on page 1 preserves the ETag
        conditional = client.get(
            "/test/", headers={"If-None-Match": page_1.headers["ETag"]}
        )
        assert conditional.status_code == 304
        assert conditional.headers["ETag"] == page_1.headers["ETag"]
        # Page 2's tag must not validate page 1
        cross = client.get(
            "/test/", headers={"If-None-Match": page_2.headers["ETag"]}
        )
        assert cross.status_code == 200
        assert cross.data == page_1.data

    def test_streaming_response_is_rejected(self, rep_app):
        app, _, _, _ = rep_app
        client = app.test_client()
        with pytest.raises(RepresentationNotReplayableError):
            client.get("/test/stream")

    def test_streaming_response_allowed_with_manual_etag(self, rep_app):
        app, blp, FullSchema, _ = rep_app
        client = app.test_client()
        response = client.get("/test/stream-manual")
        assert response.status_code == 200
        # Body streamed through without buffering/hashing
        assert response.data == b'{"a": 1}'
        # Manual tag: hash of the jsonify bytes of the dumped payload
        with app.test_request_context():
            json_response = app.json.response(FullSchema().dump(self.ITEM))
            expected = blp._hash_representation(
                json_response.get_data(),
                json_response.headers["Content-Type"],
            )
        assert response.headers["ETag"] == f'"{expected}"'
        assert response.headers["Vary"] == "Accept"
