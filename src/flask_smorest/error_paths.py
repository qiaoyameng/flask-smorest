"""Structured validation error paths

Build client-facing error paths from a :class:`marshmallow.ValidationError`
raised while parsing request arguments.

Each entry points to the exact location of an error in the client request:

- ``location``: parameter location (``query``, ``body``, ``header``,
  ``cookie``, ``path``)
- ``path``: list of path segments (field names as seen by the client, i.e.
  ``data_key`` when defined, and integer indexes for list items)
- ``messages``: original text error messages
- ``type``: ``missing`` (required field missing), ``unknown`` (unknown
  field), ``schema`` (whole-object/schema-level error) or ``validation``
  (any other field validation error)
- ``value``: raw input value (only when explicitly enabled)
"""

import marshmallow as ma

from marshmallow.exceptions import SCHEMA

# Map webargs locations to client-facing parameter locations
LOCATION_MAPPING = {
    "json": "body",
    "form": "body",
    "files": "body",
    "query": "query",
    "querystring": "query",
    "headers": "header",
    "cookies": "cookie",
    "path": "path",
    "view_args": "path",
}

_MISSING = object()


def _normalize_location(location):
    return LOCATION_MAPPING.get(location, location)


def _find_field(schema, key):
    """Find the field of ``schema`` matching an error key

    Error keys may be attribute names (e.g. when set by schema-level
    validators) or client-facing names (``data_key``).
    """
    if schema is None:
        return None, None
    if key in schema.fields:
        return key, schema.fields[key]
    for attr_name, field in schema.fields.items():
        if field.data_key == key:
            return attr_name, field
    return None, None


def _lookup(data, key):
    """Get ``key`` from raw input ``data``, ``_MISSING`` if not found"""
    if data is _MISSING:
        return _MISSING
    try:
        if isinstance(data, dict):
            return data.get(key, _MISSING)
        if isinstance(data, (list, tuple)) and isinstance(key, int):
            return data[key]
        get = getattr(data, "get", None)
        if get is not None:
            return get(key, _MISSING)
    except (IndexError, TypeError):
        pass
    return _MISSING


def _as_messages(messages):
    if isinstance(messages, list):
        return [str(message) for message in messages]
    return [str(messages)]


def _append(entries, location, path, messages, error_type, value=_MISSING):
    entry = {
        "location": location,
        "path": path,
        "messages": _as_messages(messages),
        "type": error_type,
    }
    if value is not _MISSING:
        entry["value"] = value
    entries.append(entry)


def _collect(messages, schema, path, location, entries, data):
    """Walk an error message dict alongside the schema"""
    if isinstance(messages, list):
        # Object-level error (e.g. invalid input type)
        _append(entries, location, path, messages, "schema", data)
        return
    for key, sub_messages in messages.items():
        if key == SCHEMA:
            # Schema-level error: points to the object itself, don't
            # fabricate a field path segment
            _append(entries, location, path, sub_messages, "schema", data)
            continue
        attr_name, field = _find_field(schema, key)
        if field is None:
            # Key not in schema: unknown field sent by the client
            _append(
                entries,
                location,
                path + [key],
                sub_messages,
                "unknown",
                _lookup(data, key),
            )
            continue
        # Client-facing path segment
        segment = field.data_key or attr_name
        _collect_field(
            field,
            sub_messages,
            path + [segment],
            location,
            entries,
            _lookup(data, segment),
        )


def _collect_field(field, messages, path, location, entries, data):
    if isinstance(messages, list):
        required_message = field.error_messages.get("required")
        error_type = (
            "missing" if list(messages) == [required_message] else "validation"
        )
        _append(entries, location, path, messages, error_type, data)
        return
    if isinstance(field, ma.fields.Nested):
        _collect(messages, field.schema, path, location, entries, data)
    elif isinstance(field, ma.fields.List):
        for index, sub_messages in messages.items():
            if isinstance(index, str) and index.isdigit():
                index = int(index)
            _collect_field(
                field.inner,
                sub_messages,
                path + [index],
                location,
                entries,
                _lookup(data, index),
            )
    elif isinstance(field, ma.fields.Tuple):
        for index, sub_messages in messages.items():
            index = int(index)
            try:
                inner = field.tuple_fields[index]
            except IndexError:
                _append(entries, location, path + [index], sub_messages, "validation")
            else:
                _collect_field(
                    inner,
                    sub_messages,
                    path + [index],
                    location,
                    entries,
                    _lookup(data, index),
                )
    elif isinstance(field, ma.fields.Dict):
        for dict_key, sub_messages in messages.items():
            key_path = path + [dict_key]
            key_data = _lookup(data, dict_key)
            if isinstance(sub_messages, dict) and (
                "value" in sub_messages or "key" in sub_messages
            ):
                # marshmallow nests dict errors under "key"/"value"
                if "value" in sub_messages:
                    value_field = field.value_field
                    if value_field is not None:
                        _collect_field(
                            value_field,
                            sub_messages["value"],
                            key_path,
                            location,
                            entries,
                            key_data,
                        )
                    else:
                        _append(
                            entries,
                            location,
                            key_path,
                            sub_messages["value"],
                            "validation",
                            key_data,
                        )
                if "key" in sub_messages:
                    _append(
                        entries,
                        location,
                        key_path,
                        sub_messages["key"],
                        "validation",
                        key_data,
                    )
            else:
                _append(
                    entries, location, key_path, sub_messages, "validation", key_data
                )
    else:
        # Unexpected nested structure on a scalar field: report as-is
        _append(entries, location, path, messages, "validation", data)


def build_error_paths(error, schema=None, *, include_value=False):
    """Build structured error paths from a :class:`ValidationError`

    :param ValidationError error: Validation error raised while parsing
        request arguments. Its ``messages`` are expected to be namespaced
        by argument location, as done by webargs.
    :param Schema schema: Schema that raised the error. Used to resolve
        client-facing field names (``data_key``).
    :param bool include_value: Include the raw input value in each entry.
    """
    entries = []
    messages = error.messages
    data = error.data if include_value else _MISSING
    if isinstance(messages, dict):
        for location, location_messages in messages.items():
            _collect(
                location_messages,
                schema,
                [],
                _normalize_location(location),
                entries,
                data,
            )
    else:
        _collect(messages, schema, [], None, entries, data)
    return entries
