# shared/search_attributes.py
"""Custom Search Attribute keys, defined once so the Workflow (which upserts
them as an application moves through its lifecycle), the Worker/API (which
register them on the Cluster), and the tests all agree on names and types.

This module deliberately imports only `temporalio.common` — no Temporal *client*
or operator-service types — so it is safe to import from Workflow code inside
`workflow.unsafe.imports_passed_through()`. The registration helper that needs a
Client lives in `shared.temporal`, which is never imported by the sandbox.
"""
from temporalio.common import SearchAttributeKey

# Keyword attributes: exact-match, low-cardinality string values — the right type
# for a status enum and for a user identifier used in equality lookups. Names are
# prefixed with "Loan" so they don't collide with attributes other Workflow types
# might define in the same Namespace.
LOAN_STATUS = SearchAttributeKey.for_keyword("LoanStatus")
LOAN_USER_ID = SearchAttributeKey.for_keyword("LoanUserId")

# Every attribute this app defines, for the one-shot Cluster registration helper.
ALL = [LOAN_STATUS, LOAN_USER_ID]
