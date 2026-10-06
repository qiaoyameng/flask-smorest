.. _etag:
.. currentmodule:: flask_smorest

ETag
====

ETag is a web cache validation mechanism. It allows an API client to make
conditional requests, such as

- GET a resource unless it is the same as the version in cache.
- PUT/PATCH/DELETE a resource unless the version in cache is outdated.

The first case is mostly useful to limit the bandwidth usage, the latter
addresses the case where two clients update a resource at the same time (known
as the "*lost update problem*").

The ETag featured is available through the
:meth:`Blueprint.etag <Blueprint.etag>` decorator. It can be disabled globally
with the `ETAG_DISABLED` application parameter.

`flask-smorest` provides helpers to compute ETag, but ultimately, only the
developer knows what data is relevant to use as ETag source, so there can be
manual work involved.

ETag Computed with API Response Data
------------------------------------

The simplest case is when the ETag is computed using returned data, using the
:class:`Schema <marshmallow.Schema>` that serializes the data.

In this case, almost eveything is automatic. Only the call to
:meth:`Blueprint.check_etag <Blueprint.check_etag>` is manual.

The :class:`Schema <marshmallow.Schema>` must be provided explicitly, even
though it is the same as the response schema.

.. code-block:: python
    :emphasize-lines: 2,15,26,34

    @blp.route("/")
    @blp.etag
    class Pet(MethodView):
        @blp.response(200, PetSchema(many=True))
        def get(self):
            return Pet.get()

        @blp.arguments(PetSchema)
        @blp.response(201, PetSchema)
        def post(self, new_data):
            return Pet.create(**new_data)


    @blp.route("/<pet_id>")
    @blp.etag
    class PetById(MethodView):
        @blp.response(200, PetSchema)
        def get(self, pet_id):
            return Pet.get_by_id(pet_id)

        @blp.arguments(PetSchema)
        @blp.response(200, PetSchema)
        def put(self, update_data, pet_id):
            pet = Pet.get_by_id(pet_id)
            # Check ETag is a manual action and schema must be provided
            blp.check_etag(pet, PetSchema)
            pet.update(update_data)
            return pet

        @blp.response(204)
        def delete(self, pet_id):
            pet = Pet.get_by_id(pet_id)
            # Check ETag is a manual action and schema must be provided
            blp.check_etag(pet, PetSchema)
            Pet.delete(pet_id)

ETag Computed on Arbitrary Data
-------------------------------

The ETag can also be computed from arbitrary data by calling
:meth:`Blueprint.set_etag <Blueprint.set_etag>` manually.

The example below illustrates this with no ETag schema, but it is also possible
to pass an ETag schema to :meth:`set_etag <Blueprint.set_etag>` and
:meth:`check_etag <Blueprint.check_etag>`.

.. code-block:: python
    :emphasize-lines: 2,8,15,20,25,33,36,43

    @blp.route("/")
    @blp.etag
    class Pet(MethodView):
        @blp.response(200, PetSchema(many=True))
        def get(self):
            pets = Pet.get()
            # Compute ETag using arbitrary data
            blp.set_etag([pet.update_time for pet in pets])
            return pets

        @blp.arguments(PetSchema)
        @blp.response(201, PetSchema)
        def post(self, new_data):
            # Compute ETag using arbitrary data
            blp.set_etag(new_data["update_time"])
            return Pet.create(**new_data)


    @blp.route("/<pet_id>")
    @blp.etag
    class PetById(MethodView):
        @blp.response(200, PetSchema)
        def get(self, pet_id):
            # Compute ETag using arbitrary data
            blp.set_etag(new_data["update_time"])
            return Pet.get_by_id(pet_id)

        @blp.arguments(PetSchema)
        @blp.response(200, PetSchema)
        def put(self, update_data, pet_id):
            pet = Pet.get_by_id(pet_id)
            # Check ETag is a manual action
            blp.check_etag(pet, ["update_time"])
            pet.update(update_data)
            # Compute ETag using arbitrary data
            blp.set_etag(new_data["update_time"])
            return pet

        @blp.response(204)
        def delete(self, pet_id):
            pet = Pet.get_by_id(pet_id)
            # Check ETag is a manual action
            blp.check_etag(pet, ["update_time"])
            Pet.delete(pet_id)

ETag Not Checked Warning
------------------------

It is up to the developer to call
:meth:`Blueprint.check_etag <Blueprint.check_etag>` in the view function. It
can't be automatic.

A warning is issued if ETag is enabled and
:meth:`check_etag <Blueprint.check_etag>` is not called.

Include Headers Content in ETag
-------------------------------

When ETag is computed with response data, that data may contain headers. It is
up to the developer to decide whether this data should be part of the ETag.

By default, only pagination header is included in the ETag computation. This
can be changed by customizing `Blueprint.ETAG_INCLUDE_HEADERS`.

Strong ETag Computed from the Final Response Representation
-----------------------------------------------------------

The computation above hashes the dumped Python structure. As a consequence,
it does not change when only the output schema, the response envelope, the
serialization format or the pagination metadata changes, even though the
representation actually sent to the client is different. It is therefore a
weak coupling with the response bytes.

Set `ETAG_REPRESENTATION_BASED` to ``True`` to compute a *strong* ETag from
the final response representation instead:

- the exact bytes serialized on the wire (after schema dumping and JSON
  serialization, including separators and the trailing newline) are hashed;
- the representation metadata participates in the hash: the
  ``Content-Type`` header and the headers listed in
  `Blueprint.ETAG_REPRESENTATION_HEADERS` (``X-Pagination`` by default);
- every piece hashed is length-framed, so header values cannot be confused
  with body bytes. The Python ``dict`` ``repr`` is never used.

With this setting, the same business object serialized through two different
schemas produces two different ETags, as expected.

`Content Negotiation`_: because the ``Content-Type`` is part of the
representation, a ``Vary: Accept`` header is automatically added to the
responses.

Conditional requests follow RFC 9110 comparison rules:

- ``If-None-Match`` uses *weak* comparison. A weak validator
  (``W/"..."``), a strong validator, an entry in a list or ``*`` all match
  the current representation and produce a ``304``.
- ``If-Match`` uses *strong* comparison. A weak validator never matches;
  only a strong validator (or ``*``, when the representation exists) passes.

In this mode, `Blueprint.check_etag` and `Blueprint.set_etag` hash the data
the same way ``jsonify`` serializes it, so the tag passed in ``If-Match``
matches the ETag later returned on the response.

On a ``304`` response or a ``HEAD`` request, no body is sent, but the
``ETag`` and ``Vary`` headers corresponding to the representation that would
have been returned are preserved.

Streaming Responses
~~~~~~~~~~~~~~~~~~~

A representation-based ETag cannot be computed on a non-replayable
streaming response (a response built from a generator, a file object or with
``direct_passthrough`` enabled, such as the response returned by
:func:`flask.send_file`): reading the body to hash it would consume the
stream. Such an endpoint raises a
``RepresentationNotReplayableError``. It must call
`Blueprint.set_etag` explicitly to provide a strong ETag computed from
another stable source.

.. _Content Negotiation: https://developer.mozilla.org/en-US/docs/Web/HTTP/Content_negotiation
