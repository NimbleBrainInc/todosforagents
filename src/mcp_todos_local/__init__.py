"""The local runtime for To-dos: one principal and one SQLite file on the user's machine.

Select it with ``TODOS_RUNTIME=local``. Isolation is by installation: the store runs with
exactly one principal, ``Scope("local", "local")``, so no query needs a tenant predicate.
"""
