# -*- coding: utf-8 -*-
"""1.2.11: recompute the stored amortization figures.

`monthly_amortization` (and the term fields) are stored computes. Flat
loans created while the monthly-equivalent maths assumed a weekly calendar
still carry the old figure (a flat 1,000 stored as 4,348.21/month instead
of 2,000), which overweights them in the repayment-capacity check.
Recomputing against the current cadence puts every stored figure right.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['efs.loan'].search([])._compute_amortization()
