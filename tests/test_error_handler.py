import pytest

from werkzeug.exceptions import InternalServerError, default_exceptions

from flask_smorest import Api, abort


class TestErrorHandler:
    @pytest.mark.parametrize("code", default_exceptions)
    def test_error_handler_on_abort(self, app, code):
        client = app.test_client()

        @app.route("/abort")
        def test_abort():
            abort(code)

        Api(app)

        response = client.get("/abort")
        assert response.status_code == code
        assert response.json["code"] == code
        assert response.json["status"] == default_exceptions[code]().name

    def test_error_handler_on_unhandled_error(self, app):
        # Unset TESTING to let Flask return 500 on unhandled exception
        app.config["TESTING"] = False
        client = app.test_client()

        @app.route("/uncaught")
        def test_uncaught():
            raise Exception("Oops, something really bad happened.")

        Api(app)

        response = client.get("/uncaught")
        assert response.status_code == 500
        assert response.json["code"] == 500
        assert response.json["status"] == InternalServerError().name

    def test_error_handler_payload(self, app):
        client = app.test_client()

        errors = {"dimensions": ["Too tall", "Too wide"], "color": ["Too bright"]}
        messages = {"name": ["Too long"], "age": ["Too young"]}

        @app.route("/message")
        def test_message():
            abort(404, message="Resource not found")

        @app.route("/messages")
        def test_messages():
            abort(422, messages=messages, message="Validation issue")

        @app.route("/errors")
        def test_errors():
            abort(422, errors=errors, messages=messages, message="Wrong!")

        @app.route("/headers")
        def test_headers():
            abort(
                401,
                message="Access denied",
                headers={"WWW-Authenticate": 'Basic realm="My Server"'},
            )

        Api(app)

        response = client.get("/message")
        assert response.status_code == 404
        assert response.json["message"] == "Resource not found"

        response = client.get("/messages")
        assert response.status_code == 422
        assert response.json["errors"] == messages

        response = client.get("/errors")
        assert response.status_code == 422
        assert response.json["errors"] == errors

        response = client.get("/headers")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="My Server"'
        assert response.json["message"] == "Access denied"


import marshmallow as ma
from marshmallow import ValidationError, validate, validates_schema

from flask_smorest import Blueprint


class _StructuredConfig:
    API_TITLE = "API Test"
    API_VERSION = "1"
    OPENAPI_VERSION = "3.0.2"
    TESTING = True
    ERROR_STRUCTURED_PATHS = True


class TestStructuredErrorPaths:
    def _make_app(self, config=_StructuredConfig):
        # Local import to avoid interfering with the module-level fixtures.
        from flask import Flask

        app = Flask(__name__)
        app.config.from_object(config)
        Api(app)
        return app

    def test_structured_paths_disabled_by_default(self, app):
        import marshmallow as ma

        api = Api(app)
        blp = Blueprint("t", __name__, url_prefix="/t")

        class S(ma.Schema):
            user_id = ma.fields.Integer(data_key="userId")

        @blp.route("/", methods=["POST"])
        @blp.arguments(S, location="json")
        def func(args):
            return {}

        api.register_blueprint(blp)
        response = app.test_client().post("/t/", json={"userId": "bad"})
        assert response.status_code in (400, 422)
        assert "error_details" not in response.json
        # Original payload is preserved.
        assert response.json["errors"] == {
            "json": {"userId": ["Not a valid integer."]}
        }

    def test_data_key_list_item_and_nested_object(self):
        app = self._make_app()
        api = app.extensions["flask-smorest"]["apis"][""]["ext_obj"]
        blp = Blueprint("t2", __name__, url_prefix="/t2")

        class Item(ma.Schema):
            db_name = ma.fields.String(
                data_key="displayName", validate=validate.Length(min=3)
            )

            @validates_schema
            def whole(self, data, **kwargs):
                if data.get("db_name") and len(data["db_name"]) < 3:
                    raise ValidationError("item object invalid")

        class Body(ma.Schema):
            user_id = ma.fields.Integer(data_key="userId")
            items = ma.fields.List(ma.fields.Nested(Item))

            class Meta:
                unknown = ma.RAISE

        @blp.route("/", methods=["POST"])
        @blp.arguments(Body, location="json")
        def func(args):
            return {}

        api.register_blueprint(blp)
        response = app.test_client().post(
            "/t2/",
            json={
                "userId": "notint",
                "items": [
                    {"displayName": "good"},
                    {"displayName": "ab"},
                ],
                "ghost": 1,
            },
        )
        assert response.status_code in (400, 422)
        body = response.json
        # Original text messages are still there.
        assert body["errors"]["json"]["userId"] == [
            "Not a valid integer."
        ]
        details = body["error_details"]
        # Internal field names never leak.
        assert all("user_id" not in d["field_path"] for d in details)
        assert all("db_name" not in d["field_path"] for d in details)

        by_path = {tuple(d["field_path"]): d for d in details}
        # External name for a simple field.
        assert by_path[("userId",)]["kind"] == "field"
        assert by_path[("userId",)]["location"] == "body"
        # Second item in a list, 0-based index.
        second = by_path[("items", 1, "displayName")]
        assert second["kind"] == "field"
        assert second["messages"] == [
            "Shorter than minimum length 3."
        ]
        # Unknown field.
        assert by_path[("ghost",)]["kind"] == "unknown"

    def test_nested_object_error_does_not_invent_field(self):
        app = self._make_app()
        api = app.extensions["flask-smorest"]["apis"][""]["ext_obj"]
        blp = Blueprint("t3", __name__, url_prefix="/t3")

        class Inner(ma.Schema):
            code = ma.fields.String(data_key="codeExt")

            @validates_schema
            def whole(self, data, **kwargs):
                raise ValidationError("inner object invalid")

        class Body(ma.Schema):
            inner = ma.fields.Nested(Inner)

        @blp.route("/", methods=["POST"])
        @blp.arguments(Body, location="json")
        def func(args):
            return {}

        api.register_blueprint(blp)
        response = app.test_client().post(
            "/t3/", json={"inner": {"codeExt": "ok"}}
        )
        details = response.json["error_details"]
        assert details == [
            {
                "location": "body",
                "field_path": ["inner"],
                "kind": "object",
                "messages": ["inner object invalid"],
            }
        ]

    def test_same_schema_reused_in_query_and_header(self):
        app = self._make_app()
        api = app.extensions["flask-smorest"]["apis"][""]["ext_obj"]
        blp = Blueprint("t4", __name__, url_prefix="/t4")

        class Reused(ma.Schema):
            internal = ma.fields.Integer(data_key="X-Count")

        @blp.route("/", methods=["POST"])
        @blp.arguments(Reused, location="json")
        @blp.arguments(Reused, location="query")
        @blp.arguments(Reused, location="headers")
        def func(body, query, headers):
            return {}

        api.register_blueprint(blp)
        client = app.test_client()

        good = {"X-Count": 3}
        response = client.post("/t4/?X-Count=notint", json=good,
                               headers={"X-Count": "3"})
        detail = response.json["error_details"][0]
        assert detail["location"] == "query"
        assert detail["field_path"] == ["X-Count"]

        response = client.post("/t4/?X-Count=3", json=good,
                               headers={"X-Count": "alsobad"})
        detail = response.json["error_details"][0]
        assert detail["location"] == "header"
        assert detail["field_path"] == ["X-Count"]

    def test_input_not_returned_by_default(self):
        app = self._make_app()
        api = app.extensions["flask-smorest"]["apis"][""]["ext_obj"]
        blp = Blueprint("t5", __name__, url_prefix="/t5")

        class S(ma.Schema):
            a = ma.fields.Integer(data_key="aX")

        @blp.route("/", methods=["POST"])
        @blp.arguments(S, location="json")
        def func(args):
            return {}

        api.register_blueprint(blp)
        response = app.test_client().post("/t5/", json={"aX": "secret"})
        assert "input" not in response.json["error_details"][0]

    def test_input_returned_when_enabled(self):
        class Config(_StructuredConfig):
            ERROR_INCLUDE_INPUT = True

        app = self._make_app(Config)
        api = app.extensions["flask-smorest"]["apis"][""]["ext_obj"]
        blp = Blueprint("t6", __name__, url_prefix="/t6")

        class S(ma.Schema):
            a = ma.fields.Integer(data_key="aX")

        @blp.route("/", methods=["POST"])
        @blp.arguments(S, location="json")
        def func(args):
            return {}

        api.register_blueprint(blp)
        response = app.test_client().post("/t6/", json={"aX": "secret"})
        detail = response.json["error_details"][0]
        assert detail["input"] == "secret"
