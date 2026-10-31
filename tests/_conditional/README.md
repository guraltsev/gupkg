# Conditional tests

Each folder `tests/_conditional/<name>/` holds tests of a behaviour that exists only while a stated condition holds (a
compatibility path for an old layout, say). They are held to every rule of [`_docs/tests.md`](../../_docs/tests.md), like
permanent tests. The README of each folder gives `until:` (the condition that ends them), `expires:` (a `YYYY-MM-DD` date by
which the condition is checked again) and `purpose:`. After the date the suite fails until someone confirms the condition and
moves the date, or retires the tests. The rules are in [`_docs/tests_hygiene.md`](../../_docs/tests_hygiene.md) section 2.1.
The automatic check is rule H7 of `tests/support/hygiene.py`.
