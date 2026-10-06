"""Structured validation error paths

This module turns the nested ``messages`` dictionary produced by
marshmallow/webargs when request validation fails into a flat list of
structured errors. Each entry exposes:

- ``location``: where the parameter is read from (``query``, ``body``,
  ``header``...),
- ``field_path``: path of the offending field using the names the client sent
  in its request (marshmallow ``data_key`` values), including list indices and
  dictionary keys,
- ``kind``: ``"field"`` (a field value is invalid), ``"missing"`` (a required
  field is absent), ``"unknown"`` (the client sent a field the schema does not
  know) or ``"object"`` (the whole object at ``field_path`` failed
  schema-level validation; ``field_path`` is empty for the request root),
- ``messages``: the original, unmodified text messages.

Whole-object errors raised by ``validates_schema`` are never attached to a
made-up field such as ``_schema``: the path ends at the object that failed.
Likewise, marshmallow's internal Dict ``"key"``/``"value"`` markers never
appear in ``field_path``: a key error ends at the dictionary key, a value
error continues into the value.
"""

from collections.abc import Mapping, Sequence

import marshmallow as ma
from marshmallow.exceptions import SCHEMA


# Structural markers marshmallow uses inside Dict field error mappings.
DICT_KEY_MARKER = "key"
DICT_VALUE_MARKER = "value"

_UNKNOWN_MESSAGE = ma.Schema._default_error_messages["unknown"]

# webargs location names -> labels clients use to locate their parameters.
_LOCATION_LABELS = {
    "json": "body",
    "json_or_form": "body",
    "query": "query",
    "querystring": "query",
    "form": "form",
    "headers": "header",
    "cookies": "cookie",
    "files": "files",
    "path": "path",
    "view_args": "path",
}


def location_label(location):
    """Return the client-facing label of a webargs request location."""
    return _LOCATION_LABELS.get(location, location)


def _is_index(key):
    return isinstance(key, int) or (isinstance(key, str) and key.isdigit())


def _as_index(key):
    return int(key) if isinstance(key, str) and key.isdigit() else key


def _input_field(data, key):
    """Raw value at field/index ``key``."""
    if isinstance(data, Mapping):
        return data.get(key)
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        idx = _as_index(key)
        if isinstance(idx, int) and 0 <= idx < len(data):
            return data[idx]
    return None


class _Level:
    """Context describing the container at a messages-tree level.

    ``kind`` is one of:

    - ``"schema"``/``"nested_field"``: ``schema`` is set; keys are field
      attribute names or ``data_key`` values. ``nested_field`` additionally
      keeps ``field`` set so direct leaves (missing/null on the nested field
      itself) are classified as field errors,
    - ``"sequence"``: ``container`` is a ``List``/``Tuple``/``Nested(many)``
      field; keys are numeric indices,
    - ``"dict"``: ``container`` is a ``Dict`` field; the messages directly
      below are keyed by actual dictionary keys,
    - ``"dict_entry"``: inside one dictionary entry (``dict_key`` is the entry
      key); keys are the structural ``"key"``/``"value"`` markers or nested
      value-schema field names,
    - ``"field"``: raw field node (``field`` set); used to classify leaves,
    - ``"object"``: whole-object error node; leaves are ``object`` errors,
    - ``"opaque"``: unresolvable structure; keys are passed through verbatim.
    """

    __slots__ = ("kind", "schema", "field", "container", "dict_key")

    def __init__(
        self,
        kind,
        schema=None,
        field=None,
        container=None,
        dict_key=None,
    ):
        self.kind = kind
        self.schema = schema
        self.field = field
        self.container = container
        self.dict_key = dict_key

    @classmethod
    def root(cls, schema):
        if schema is None:
            return cls("opaque")
        return cls("schema", schema=schema)

    @classmethod
    def from_field(cls, field):
        """Level introduced when descending through a schema field.

        A ``Nested`` field may either carry sub-field errors (schema level) or
        a direct leaf error such as "missing"/"may not be null". The returned
        level remembers the field so direct leaves can still be classified
        while nested keys are resolved against the inner schema.
        """
        nested = getattr(field, "schema", None)
        if nested is not None and not getattr(field, "many", False):
            return cls("nested_field", schema=nested, field=field,
                       container=field)
        if nested is not None:  # Nested(many=True)
            return cls("sequence", schema=nested, container=field)
        if isinstance(field, (ma.fields.Dict, ma.fields.List, ma.fields.Tuple)):
            return cls(
                "sequence"
                if isinstance(field, (ma.fields.List, ma.fields.Tuple))
                else "dict",
                container=field,
            )
        return cls("field", field=field)

    def _schema_lookup(self, key):
        fields = self.schema.fields
        field_obj = fields.get(key)
        if field_obj is None:
            for candidate in fields.values():
                if candidate.data_key == key:
                    field_obj = candidate
                    break
        if field_obj is None:
            return None
        external = (
            field_obj.data_key if field_obj.data_key is not None else key
        )
        return field_obj, external

    def resolve(self, key):
        """Resolve ``key`` at this level.

        Returns ``(external, child_level)`` or ``None``:

        - ``external`` is the component to append to the client path, ``None``
          for structural nodes that must not appear,
        - ``child_level`` is the context for ``messages[key]``.
        """
        if self.kind in ("schema", "nested_field"):
            found = self._schema_lookup(key)
            if found is None:
                return None
            field_obj, external = found
            return external, self.from_field(field_obj)

        if self.kind == "sequence":
            field = self.container
            if not _is_index(key):
                return None
            idx = _as_index(key)
            if isinstance(field, ma.fields.Tuple):
                if not (0 <= idx < len(field.tuple_fields)):
                    return None
                return idx, self.from_field(field.tuple_fields[idx])
            if isinstance(field, ma.fields.List):
                return idx, self.from_field(field.inner)
            # Nested(many=True)
            return idx, _Level("schema", schema=field.schema,
                               container=field)

        if self.kind == "dict":
            # Any key is a potential dictionary key; errors below belong to
            # that entry.
            return key, _Level(
                "dict_entry",
                container=self.container,
                dict_key=key,
            )

        if self.kind == "dict_entry":
            field = self.container
            if key == DICT_KEY_MARKER:
                return None, _Level("field", field=field.key_field)
            if key == DICT_VALUE_MARKER:
                return None, self.from_field(field.value_field)
            # Nested value schema reported without an explicit marker.
            nested = getattr(field.value_field, "schema", None)
            if nested is not None:
                entry_level = _Level("schema", schema=nested)
                found = entry_level._schema_lookup(key)
                if found is not None:
                    field_obj, external = found
                    return external, self.from_field(field_obj)
            return None

        return None


def _classify_leaf(messages, level):
    """Classify leaf messages."""
    if level.kind == "object":
        return "object"
    if messages == [_UNKNOWN_MESSAGE]:
        return "unknown"
    field = level.field
    if field is not None:
        required_message = field.error_messages.get("required")
        if required_message and messages == [required_message]:
            return "missing"
    return "field"


def _emit_leaf(messages, level, location, path, input_data, include_input,
               result):
    if isinstance(messages, (list, tuple)):
        leaf_messages = list(messages)
    else:
        leaf_messages = [messages]
    kind = _classify_leaf(leaf_messages, level)
    entry = {
        "location": location,
        "field_path": list(path),
        "kind": kind,
        "messages": leaf_messages,
    }
    if include_input:
        # Missing fields have no offending input value by definition.
        entry["input"] = None if kind == "missing" else input_data
    result.append(entry)


def _walk(messages, level, location, path, input_data, include_input, result):
    """Traverse one node of the marshmallow messages tree."""
    if not isinstance(messages, Mapping):
        _emit_leaf(messages, level, location, path, input_data, include_input,
                   result)
        return

    for key, sub_messages in messages.items():
        if key == SCHEMA:
            # Whole-object error for the schema at this level. It is not tied
            # to any field: emit it at the current path (the object itself),
            # without inventing a field name.
            _walk(sub_messages, _Level("object"), location, path, input_data,
                  include_input, result)
            continue

        # Structural Dict markers add no path component. A key error reports
        # the dictionary key itself; a value error keeps the current entry
        # value.
        if level.kind == "dict_entry":
            dict_field = level.container
            if key == DICT_KEY_MARKER:
                marker_input = level.dict_key if include_input else None
                _walk(sub_messages,
                      _Level("field", field=dict_field.key_field),
                      location, path, marker_input, include_input, result)
                continue
            if key == DICT_VALUE_MARKER:
                _walk(sub_messages, _Level.from_field(dict_field.value_field),
                      location, path, input_data, include_input, result)
                continue

        next_input = _input_field(input_data, key) if include_input else None
        resolved = level.resolve(key)

        if resolved is None:
            # A schema validator may raise ValidationError with a dict keyed by
            # internal field names. The key does not go through the regular
            # field error path, but it can still be translated to the external
            # (data_key) name through schema introspection. Anything that
            # cannot be resolved is kept verbatim rather than guessed.
            if level.kind in ("schema", "nested_field"):
                field_obj = level.schema.fields.get(key)
                if field_obj is not None:
                    external = (
                        field_obj.data_key
                        if field_obj.data_key is not None
                        else key
                    )
                    _walk(sub_messages, _Level.from_field(field_obj), location,
                          path + [external], next_input, include_input, result)
                    continue
            component = _as_index(key) if _is_index(key) else key
            _walk(sub_messages, _Level("opaque"), location,
                  path + [component], next_input, include_input, result)
            continue

        external, child_level = resolved
        next_path = path if external is None else path + [external]
        _walk(sub_messages, child_level, location, next_path, next_input,
              include_input, result)


def build_structured_errors(
    messages, schema, *, include_input=False, raw_data=None
):
    """Build a flat list of structured validation errors.

    :param dict messages: webargs-style messages mapping, namespaced under
        request locations (e.g. ``{"json": {...}}``). Mappings without a
        location namespace are accepted as well.
    :param schema: marshmallow schema that produced the errors (may be
        ``None``).
    :param bool include_input: attach the raw offending input value to each
        entry when it can be located. Missing fields always report ``None``.
    :param raw_data: raw request payload, used when ``include_input`` is set.
    """
    result = []

    if isinstance(messages, Mapping) and messages and all(
        key in _LOCATION_LABELS for key in messages
    ):
        locations = messages.items()
    else:
        locations = ((None, messages),)

    for location, location_messages in locations:
        label = location_label(location) if location is not None else None
        _walk(
            location_messages,
            _Level.root(schema),
            label,
            [],
            raw_data,
            include_input,
            result,
        )

    return result
