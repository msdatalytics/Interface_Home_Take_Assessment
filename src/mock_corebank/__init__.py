"""A deliberately hostile, legacy-style core banking application used as the proxy target.

It mimics the kind of back-office surface found at banks and credit unions:
server-rendered pages, a ``<frameset>`` shell, table-based layouts, labels that are
not programmatically associated with their inputs, ASP.NET-style generated control
names, and no test IDs. It also supports fault injection (maintenance interstitials,
session expiry, slow responses, transient server errors, unknown blocking dialogs) so
the replay engine's error handling can be exercised deterministically.

Two tenant variants of the same "vendor product" are served so that cross-tenant reuse
of a single capability artifact can be demonstrated.

All data is synthetic.
"""
