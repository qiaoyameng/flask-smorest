.. _arguments:
.. currentmodule:: flask_smorest

Arguments
=========

To inject arguments into a view function, use the :meth:`Blueprint.arguments
<Blueprint.arguments>` decorator. It allows to specify a :class:`Schema
<marshmallow.Schema>` to deserialize and validate the parameters.

When processing a request, the input data is deserialized, validated, and
injected in the view function.

.. code-block:: python
    :emphasize-lines: 3,5,8,10

    @blp.route("/")
    class Pets(MethodView):
        @blp.arguments(PetQueryArgsSchema, location="query")
        @blp.response(200, PetSchema(many=True))
        def get(self, args):
            return Pet.get(filters=args)

        @blp.arguments(PetSchema)
        @blp.response(201, PetSchema)
        def post(self, pet_data):
            return Pet.create(**pet_data)

Arguments Location
------------------

The following locations are allowed:

    - ``"json"``
    - ``"query"`` (or ``"querystring"``)
    - ``"path"``
    - ``"form"``
    - ``"headers"``
    - ``"cookies"``
    - ``"files"``
    - ``"json_or_form"``

The location defaults to ``"json"``, which means `body` parameter.

Arguments Injection
-------------------

By default, arguments are passed as a single positional ``dict`` argument.
If ``as_kwargs=True`` is passed, the decorator passes deserialized input data
as keyword arguments instead.

.. code-block:: python
    :emphasize-lines: 3,5

    @blp.route("/")
    class Pets(MethodView):
        @blp.arguments(PetQueryArgsSchema, location="query", as_kwargs=True)
        @blp.response(200, PetSchema(many=True))
        def get(self, **kwargs):
            return Pet.get(filters=kwargs)

This decorator can be called several times on a resource function, for instance
to accept both `body` and `query` parameters. The order of the decorator calls
matters as it determines the order in which the parameters are passed to the
view function.

.. code-block:: python
    :emphasize-lines: 4,5,6

    @blp.route("/")
    class Pets(MethodView):
        @blp.arguments(PetSchema)
        @blp.arguments(QueryArgsSchema, location="query")
        def post(pet_data, query_args):
            return Pet.create(pet_data, **query_args)

Dealing With Unknown Arguments
------------------------------

When input data contains unknown fields, a marshmallow ``Schema`` may raise a
``ValidationError`` (default), exclude those fields, or include them without
validation. This can be controlled by the ``unknown`` Meta attribute of the
``Schema``, which can be set to ``RAISE``, ``EXCLUDE`` or ``INCLUDE``.

Marshmallow also allows to pass an ``unknown`` argument to change this on the
fly on each load. This parameter, if not ``None``, overrides the Meta
attribute. But it does not propagate to nested ``Schema`` s.

``json``, ``form`` or ``json_or_form`` locations may contain nested schemas, so
the only way to change behaviour about ``unknown`` (for instance to set it to
``EXCLUDE``), is to set it in a Meta attribute. This can be done conveniently
in a base ``Schema`` class.

For locations that typically don't use nested ``Schema`` s (``query_string``,
``headers``, ``cookies`` and ``files``), ``unknown=EXCLUDE`` is passed, as it
is considered a more sensible default.

The ``unknown`` argument passed to the ``Schema`` for each location can be
customized by subclassing the ``FlaskParser`` imported from webargs and set it
in a base :class:`Blueprint` class:

.. code-block:: python

    import marshmallow as ma
    from webargs.flaskparser import FlaskParser
    from flask_smorest import Blueprint


    class MyFlaskParser(FlaskParser):
        DEFAULT_UNKNOWN_BY_LOCATION = {
            "query": ma.RAISE,
            # ...
        }


    class MyBlueprint(Blueprint):
        ARGUMENTS_PARSER = MyFlaskParser()

For the reason stated above, setting a value there for locations containing
nested ``Schema`` s is not recommended because that value would only apply to
the first level and would not propagate to nested ``Schema`` s.

Setting ``None`` for a location disables the feature for that location: no
``unknown`` argument is passed and marshmallow uses the ``Schema``'s
``unknown`` Meta attribute or falls back to marshmallow default (``RAISE``).

Setting ``None`` as ``DEFAULT_UNKNOWN_BY_LOCATION`` instead of a location/value
mapping disables the feature for all locations.

.. note:: More info about customizing ``unknown`` can be found in `webargs
   documentation
   <https://webargs.readthedocs.io/en/latest/advanced.html#setting-unknown>`_.

Multiple Arguments Schemas
--------------------------

Calls to ``arguments`` decorator can be stacked, for instance to define
arguments from multiple locations:

.. code-block:: python
    :emphasize-lines: 3,4

    @blp.route("/")
    class Pets(MethodView):
        @blp.arguments(PetSchema)
        @blp.arguments(QueryArgsSchema, location="query")
        @blp.response(201, PetSchema)
        def post(self, pet_data, query_args):
            pet = Pet.create(**pet_data)
            # Use query args
            ...
            return pet

It can also be done to define multiple arguments for the same location. Of
course, this only makes sense for locations where ``unknown`` is set to
``EXCLUDE``.

Content Type
------------

When using body arguments, a default content type is assumed depending on the
location. The location / content type mapping can be customized by modifying
``Blueprint.DEFAULT_LOCATION_CONTENT_TYPE_MAPPING``.

.. code-block:: python

    DEFAULT_LOCATION_CONTENT_TYPE_MAPPING = {
        "json": "application/json",
        "form": "application/x-www-form-urlencoded",
        "files": "multipart/form-data",
    }

It is also possible to override those defaults in a single resource by passing
a string as ``content_type`` argument to :meth:`Blueprint.arguments
<Blueprint.arguments>`.

.. note:: The content type is only used for documentation purpose and has no
   impact on request parsing.

.. note:: Multipart requests with mixed types (file, form, etc.) are not
   supported. They can be achieved but the documentation is not correctly
   generated. ``arguments`` decorator can be called multiple times on the same
   view function but it should not be called with more that one request body
   location. This limitation is discussed in :issue:`46`.

Structured Validation Errors
----------------------------

By default, a validation error produces an ``errors`` field containing the
nested messages dictionary built by marshmallow/webargs. Its keys may be
internal Python field names (for errors raised from schema-level validators)
or marshmallow internal markers such as ``_schema``, which makes them hard to
map back to the client request.

Setting ``ERROR_STRUCTURED_PATHS = True`` in the application configuration adds
an ``error_details`` field to the error response. It is a flat list of entries,
each describing one error with the location and the field path using the names
the client actually sent (marshmallow ``data_key`` values):

.. code-block:: python

    {
        "code": 422,
        "status": "UNPROCESSABLE_ENTITY",
        "errors": {"json": {"user_id": ["Not a valid integer."]}},
        "error_details": [
            {
                "location": "body",
                "field_path": ["userId"],
                "kind": "field",
                "messages": ["Not a valid integer."],
            }
        ],
    }

Each entry contains:

- ``location``: the request part the parameter belongs to: ``"body"``,
  ``"query"``, ``"header"``, ``"form"``, ``"cookie"``, ``"files"`` or
  ``"path"``. The same schema used in several ``arguments`` decorators produces
  paths with the correct location.
- ``field_path``: list of path components from the request root. Field names
  are the client-facing ``data_key`` values. List items are represented by
  their zero-based index (``["items", 1, "displayName"]``) and dictionary
  values by their key (``["meta", "color"]``). The marshmallow internal
  ``"key"``/``"value"`` markers never appear in the path.
- ``kind``: ``"field"`` for an invalid field value, ``"missing"`` for a
  required field that was not provided, ``"unknown"`` for a field the schema
  does not accept and ``"object"`` for a whole-object (schema-level)
  validation failure. Whole-object errors are not attached to a fabricated
  field: ``field_path`` ends at the offending object and is empty for the
  request body itself.
- ``messages``: the original, unmodified validation messages.

The original text messages are always preserved in ``errors`` and
``error_details``. The raw offending input value is never returned by default;
set ``ERROR_INCLUDE_INPUT = True`` to additionally add an ``input`` field to
each entry (missing fields always report ``null``).

File Upload
-----------

File uploads as `multipart/form-data` are supported for both
`OpenAPI 3 <https://swagger.io/docs/specification/describing-request-body/file-upload/>`_
and
`OpenAPI 2 <https://swagger.io/docs/specification/2-0/file-upload/>`_.

The arguments ``Schema`` should contain :class:`Upload <fields.Upload>`
fields. The files are injected in the view function as a ``dict`` of werkzeug
:class:`FileStorage <werkzeug.datastructures.FileStorage>` instances.

.. code-block:: python

    import os.path
    from werkzeug.utils import secure_filename
    from flask_smorest.fields import Upload


    class MultipartFileSchema(ma.Schema):
        file_1 = Upload()


    @blp.route("/", methods=["POST"])
    @blp.arguments(MultipartFileSchema, location="files")
    @blp.response(201)
    def func(files):
        base_dir = "/path/to/storage/dir/"
        file_1 = files["file_1"]
        file_1.save(os.path.join(base_dir, secure_filename(file_1.filename)))
