# -*- coding: utf-8 -*-
"""1.2.6: cap the loan deduction at what the payslip can actually give.

The DED_LOAN formula lives on the salary rule record, so an installed
database keeps the old uncapped formula until it is rewritten. The setup
method is idempotent and carries the current formula; re-running it is the
whole migration.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['hr.salary.rule']._nihao_setup_loan_rule()
