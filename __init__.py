# -*- coding: utf-8 -*-
"""Employee loans, including the payroll deduction they drive.

The install-time work lives on the model, in
``hr.salary.rule._nihao_setup_loan_rule()``. This hook only calls it.

That split matters: a ``post_init_hook`` runs at **install** and never again --
an upgrade does not re-run it. Anything that must reach an already-installed
database (a changed formula, a new sequence) therefore goes in a migration
script that calls the same method, which is why the method exists rather than
the logic living here. It is idempotent, so calling it twice is free.

To re-run it by hand::

    odoo shell -d <db> --no-http
    >>> env['hr.salary.rule'].sudo()._nihao_setup_loan_rule()
    >>> env.cr.commit()
"""

from . import models
from . import wizard


def post_init_hook(env):
    env['hr.salary.rule'].sudo()._nihao_setup_loan_rule()
