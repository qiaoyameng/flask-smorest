import pytest

from werkzeug.exceptions import InternalServerError, default_exceptions

import marshmallow as ma

from flask_smorest import Api, Blueprint, abort


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


class ItemSchema(ma.Schema):
    display_name = ma.fields.String(data_key="displayName", required=True)

    @ma.validates_schema
    def validate_object(self, data, **kwargs):
        if data.get("display_name") == "forbidden":
            raise ma.ValidationError("Name is forbidden.")


class PayloadSchema(ma.Schema):
    user_name = ma.fields.String(data_key="userName", required=True)
    user_age = ma.fields.Integer(data_key="userAge")
    items = ma.fields.List(ma.fields.Nested(ItemSchema))
    mapping = ma.fields.Dict(
        keys=ma.fields.String(), values=ma.fields.Integer()
    )

    @ma.validates_schema
    def validate_object(self, data, **kwargs):
        if data.get("user_name") == "invalid":
            # Field error keyed by internal Python field name
            raise ma.ValidationError({"user_name": ["Invalid user name."]})


class TestValidationErrorPaths:
    """Test structured error paths in validation error responses"""

    @pytest.fixture
    def client(self, app):
        # Other tests may override the parser default validation status
        parser = Blueprint.ARGUMENTS_PARSER
        default_status = parser.DEFAULT_VALIDATION_STATUS
        parser.DEFAULT_VALIDATION_STATUS = 422

        api = Api(app)
        blp = Blueprint("test", __name__, url_prefix="/test")

        @blp.route("/body", methods=["POST"])
        @blp.arguments(PayloadSchema)
        def body(args):
            return args

        @blp.route("/query")
        @blp.arguments(PayloadSchema, location="query")
        def query(args):
            return args

        @blp.route("/headers")
        @blp.arguments(PayloadSchema, location="headers")
        def headers(args):
            return args

        api.register_blueprint(blp)
        yield app.test_client()

        parser.DEFAULT_VALIDATION_STATUS = default_status

    def test_error_paths_data_key(self, client):
        response = client.post("/test/body", json={"userName": 42})
        assert response.status_code == 422
        # Original text messages are preserved
        assert "userName" in response.json["errors"]["json"]
        assert response.json["error_paths"] == [
            {
                "location": "body",
                "path": ["userName"],
                "messages": ["Not a valid string."],
                "type": "validation",
            }
        ]

    def test_error_paths_missing_field(self, client):
        response = client.post("/test/body", json={})
        assert response.status_code == 422
        assert response.json["error_paths"] == [
            {
                "location": "body",
                "path": ["userName"],
                "messages": ["Missing data for required field."],
                "type": "missing",
            }
        ]

    def test_error_paths_unknown_field(self, client):
        response = client.post(
            "/test/body", json={"userName": "abc", "extra": 1}
        )
        assert response.status_code == 422
        assert response.json["error_paths"] == [
            {
                "location": "body",
                "path": ["extra"],
                "messages": ["Unknown field."],
                "type": "unknown",
            }
        ]

    def test_error_paths_list_item(self, client):
        response = client.post(
            "/test/body",
            json={
                "userName": "abc",
                "items": [{"displayName": "ok"}, {"displayName": 42}, {}],
            },
        )
        assert response.status_code == 422
        assert response.json["error_paths"] == [
            {
                "location": "body",
                "path": ["items", 1, "displayName"],
                "messages": ["Not a valid string."],
                "type": "validation",
            },
            {
                "location": "body",
                "path": ["items", 2, "displayName"],
                "messages": ["Missing data for required field."],
                "type": "missing",
            },
        ]

    def test_error_paths_dict_value(self, client):
        response = client.post(
            "/test/body",
            json={"userName": "abc", "mapping": {"size": "big"}},
        )
        assert response.status_code == 422
        assert response.json["error_paths"] == [
            {
                "location": "body",
                "path": ["mapping", "size"],
                "messages": ["Not a valid integer."],
                "type": "validation",
            }
        ]

    def test_error_paths_nested_schema_error(self, client):
        response = client.post(
            "/test/body",
            json={"userName": "abc", "items": [{"displayName": "forbidden"}]},
        )
        assert response.status_code == 422
        # Whole-object error: path points to the object, no invented field
        assert response.json["error_paths"] == [
            {
                "location": "body",
                "path": ["items", 0],
                "messages": ["Name is forbidden."],
                "type": "schema",
            }
        ]

    def test_error_paths_root_schema_error_internal_name(self, client):
        response = client.post("/test/body", json={"userName": "invalid"})
        assert response.status_code == 422
        # Schema-level validator used the internal Python field name,
        # the client-facing data_key is reported
        assert response.json["error_paths"] == [
            {
                "location": "body",
                "path": ["userName"],
                "messages": ["Invalid user name."],
                "type": "validation",
            }
        ]

    @pytest.mark.parametrize(
        "route,kwargs,location",
        [
            (
                "/test/query",
                {"query_string": {"userName": "abc", "userAge": "abc"}},
                "query",
            ),
            (
                "/test/headers",
                {"headers": {"userName": "abc", "userAge": "abc"}},
                "header",
            ),
        ],
    )
    def test_error_paths_locations(self, client, route, kwargs, location):
        response = client.get(route, **kwargs)
        assert response.status_code == 422
        assert response.json["error_paths"] == [
            {
                "location": location,
                "path": ["userAge"],
                "messages": ["Not a valid integer."],
                "type": "validation",
            }
        ]

    def test_error_paths_no_value_by_default(self, client):
        response = client.post("/test/body", json={"userName": 42})
        assert response.status_code == 422
        assert "value" not in response.json["error_paths"][0]

    def test_error_paths_include_value(self, app, client):
        app.config["VALIDATION_ERROR_PATHS_INCLUDE_VALUE"] = True
        response = client.post("/test/body", json={"userName": 42})
        assert response.status_code == 422
        assert response.json["error_paths"][0]["value"] == 42

    def test_error_paths_disabled(self, app, client):
        app.config["VALIDATION_ERROR_PATHS"] = False
        response = client.post("/test/body", json={"userName": 42})
        assert response.status_code == 422
        assert "error_paths" not in response.json
