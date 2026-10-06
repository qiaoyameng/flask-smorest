"""Tests for structured validation error paths"""

import pytest

import marshmallow as ma
from marshmallow import ValidationError, validate, validates_schema

from flask_smorest.error_paths import (
    build_structured_errors,
    location_label,
)


def _load_errors(schema, data, many=False):
    with pytest.raises(ValidationError) as exc:
        schema(many=many).load(data)
    return schema(many=many), exc.value


class TestLocationLabel:
    @pytest.mark.parametrize(
        "location,label",
        [
            ("json", "body"),
            ("json_or_form", "body"),
            ("query", "query"),
            ("querystring", "query"),
            ("form", "form"),
            ("headers", "header"),
            ("cookies", "cookie"),
            ("files", "files"),
            ("path", "path"),
        ],
    )
    def test_location_label(self, location, label):
        assert location_label(location) == label


class TestStructuredErrors:
    def test_data_key_replaces_internal_name(self):
        class S(ma.Schema):
            user_id = ma.fields.Integer(data_key="userId")

        schema, error = _load_errors(S, {"userId": "notint"})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details == [
            {
                "location": "body",
                "field_path": ["userId"],
                "kind": "field",
                "messages": ["Not a valid integer."],
            }
        ]

    def test_schema_validator_internal_name_is_translated(self):
        class S(ma.Schema):
            user_id = ma.fields.Integer(data_key="userId")

            @validates_schema
            def cross_check(self, data, **kwargs):
                raise ValidationError({"user_id": ["Must be positive"]})

        schema, error = _load_errors(S, {"userId": -1})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["userId"]
        assert details[0]["messages"] == ["Must be positive"]

    def test_list_index_uses_zero_based_position(self):
        class Item(ma.Schema):
            name = ma.fields.String(data_key="displayName")

        class S(ma.Schema):
            items = ma.fields.List(ma.fields.Nested(Item))

        schema, error = _load_errors(
            S,
            {"items": [{"displayName": 1}, {"displayName": 2},
                       {"displayName": 3}]},
        )
        details = build_structured_errors({"json": error.messages}, schema)
        paths = [d["field_path"] for d in details]
        assert paths == [
            ["items", 0, "displayName"],
            ["items", 1, "displayName"],
            ["items", 2, "displayName"],
        ]

    def test_list_of_scalars(self):
        class S(ma.Schema):
            nums = ma.fields.List(ma.fields.Integer())

        schema, error = _load_errors(S, {"nums": [1, "x", 3]})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["nums", 1]
        assert details[0]["kind"] == "field"

    def test_nested_schema_level_error_ends_at_object(self):
        class Inner(ma.Schema):
            code = ma.fields.String(data_key="codeExt")

            @validates_schema
            def whole(self, data, **kwargs):
                raise ValidationError("inner object invalid")

        class S(ma.Schema):
            inner = ma.fields.Nested(Inner)

        schema, error = _load_errors(S, {"inner": {"codeExt": "ok"}})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details == [
            {
                "location": "body",
                "field_path": ["inner"],
                "kind": "object",
                "messages": ["inner object invalid"],
            }
        ]

    def test_root_schema_level_error_has_empty_path(self):
        class S(ma.Schema):
            a = ma.fields.Integer()

            @validates_schema
            def whole(self, data, **kwargs):
                raise ValidationError("whole request invalid")

        schema, error = _load_errors(S, {"a": 1})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == []
        assert details[0]["kind"] == "object"

    def test_nested_many_schema_level_error(self):
        class Item(ma.Schema):
            n = ma.fields.Integer()

            @validates_schema
            def whole(self, data, **kwargs):
                raise ValidationError("item invalid")

        class S(ma.Schema):
            things = ma.fields.Nested(Item, many=True)

        schema, error = _load_errors(S, {"things": [{"n": 1}, {"n": 2}]})
        details = build_structured_errors({"json": error.messages}, schema)
        assert [d["field_path"] for d in details] == [
            ["things", 0],
            ["things", 1],
        ]
        assert all(d["kind"] == "object" for d in details)

    def test_missing_field(self):
        class S(ma.Schema):
            db_name = ma.fields.String(data_key="displayName", required=True)

        schema, error = _load_errors(S, {})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["displayName"]
        assert details[0]["kind"] == "missing"

    def test_unknown_field(self):
        class S(ma.Schema):
            a = ma.fields.Integer()

            class Meta:
                unknown = ma.RAISE

        schema, error = _load_errors(S, {"a": 1, "ghost": 2})
        details = build_structured_errors({"json": error.messages}, schema)
        unknown = [d for d in details if d["kind"] == "unknown"]
        assert unknown[0]["field_path"] == ["ghost"]

    def test_missing_and_unknown_distinguished_from_object(self):
        class S(ma.Schema):
            a = ma.fields.Integer(required=True)

            class Meta:
                unknown = ma.RAISE

        schema, error = _load_errors(S, {"ghost": 2})
        details = build_structured_errors({"json": error.messages}, schema)
        kinds = {
            tuple(d["field_path"]): d["kind"] for d in details
        }
        assert kinds[("a",)] == "missing"
        assert kinds[("ghost",)] == "unknown"
        assert all(d["kind"] != "object" for d in details)

    def test_dict_key_and_value_errors(self):
        class S(ma.Schema):
            meta = ma.fields.Dict(
                keys=ma.fields.String(validate=validate.Length(min=3)),
                values=ma.fields.Integer(),
            )

        schema, error = _load_errors(
            S, {"meta": {"ab": 5, "ok": "bad"}}
        )
        details = build_structured_errors({"json": error.messages}, schema)
        by_path = {tuple(d["field_path"]): d for d in details}
        # Structural "key"/"value" markers do not leak into client paths.
        assert ("meta", "ab") in by_path
        assert ("meta", "ok") in by_path
        assert all(
            "key" not in d["field_path"] and "value" not in d["field_path"]
            for d in details
        )

    def test_dict_nested_value_field_path(self):
        class V(ma.Schema):
            v = ma.fields.Integer(data_key="vX")

        class S(ma.Schema):
            meta = ma.fields.Dict(values=ma.fields.Nested(V))

        schema, error = _load_errors(
            S, {"meta": {"a": {"vX": "bad"}}}
        )
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["meta", "a", "vX"]

    def test_location_is_namespaced_per_request_part(self):
        class Reused(ma.Schema):
            internal = ma.fields.Integer(data_key="X-Count")

        schema = Reused()
        with pytest.raises(ValidationError) as exc:
            schema.load({"X-Count": "bad"})
        for location, label in (
            ("query", "query"),
            ("headers", "header"),
        ):
            details = build_structured_errors(
                {location: exc.value.messages}, schema
            )
            assert details[0]["location"] == label
            assert details[0]["field_path"] == ["X-Count"]

    def test_input_not_included_by_default(self):
        class S(ma.Schema):
            a = ma.fields.Integer(data_key="aX")

        schema, error = _load_errors(S, {"aX": "bad"})
        details = build_structured_errors({"json": error.messages}, schema)
        assert "input" not in details[0]

    def test_input_included_on_demand(self):
        class Item(ma.Schema):
            name = ma.fields.String(data_key="nameX")

        class S(ma.Schema):
            items = ma.fields.List(ma.fields.Nested(Item))

        data = {"items": [{"nameX": 1}, {"nameX": 2}]}
        schema, error = _load_errors(S, data)
        details = build_structured_errors(
            {"json": error.messages}, schema,
            include_input=True, raw_data=data,
        )
        assert [d["input"] for d in details] == [1, 2]

    def test_missing_input_is_none_when_included(self):
        class S(ma.Schema):
            a = ma.fields.String(required=True)

        schema, error = _load_errors(S, {})
        details = build_structured_errors(
            {"json": error.messages}, schema,
            include_input=True, raw_data={},
        )
        assert details[0]["input"] is None
        assert details[0]["kind"] == "missing"

    def test_messages_are_unchanged(self):
        class S(ma.Schema):
            a = ma.fields.Integer(data_key="aX")

        schema, error = _load_errors(S, {"aX": "bad"})
        messages = {"json": {"a": ["Not a valid integer."]}}
        # webargs messages still carry the internal name; the structured list
        # only adds translated paths without altering the original messages.
        details = build_structured_errors(messages, schema)
        assert messages == {"json": {"a": ["Not a valid integer."]}}
        assert details[0]["messages"] == ["Not a valid integer."]

    def test_messages_without_location_namespace(self):
        class S(ma.Schema):
            a = ma.fields.Integer()

        schema, error = _load_errors(S, {"a": "bad"})
        details = build_structured_errors(error.messages, schema)
        assert details[0]["location"] is None
        assert details[0]["field_path"] == ["a"]


    def test_nested_field_itself_missing_or_null(self):
        class Inner(ma.Schema):
            code = ma.fields.String(data_key="codeExt")

        class Required(ma.Schema):
            inner = ma.fields.Nested(Inner, required=True)

        schema, error = _load_errors(Required, {})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["inner"]
        assert details[0]["kind"] == "missing"

        class Nullable(ma.Schema):
            inner = ma.fields.Nested(Inner)

        schema, error = _load_errors(Nullable, {"inner": None})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["inner"]
        assert details[0]["kind"] == "field"
        assert details[0]["messages"] == ["Field may not be null."]

    def test_tuple_index_path(self):
        class S(ma.Schema):
            pair = ma.fields.Tuple(
                (ma.fields.Integer(), ma.fields.String(data_key="sX"))
            )

        schema, error = _load_errors(S, {"pair": ["x", 5]})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["pair", 0]
        assert details[1]["field_path"] == ["pair", 1]

    def test_list_itself_wrong_type(self):
        class Item(ma.Schema):
            a = ma.fields.Integer()

        class S(ma.Schema):
            items = ma.fields.List(ma.fields.Nested(Item))

        schema, error = _load_errors(S, {"items": "nope"})
        details = build_structured_errors({"json": error.messages}, schema)
        assert details[0]["field_path"] == ["items"]
        assert details[0]["kind"] == "field"

    def test_schema_none_is_handled(self):
        details = build_structured_errors(
            {"json": {"foo": ["bad"]}}, None
        )
        assert details[0]["field_path"] == ["foo"]
