# -*- coding: utf-8 -*-
"""1.1.0: carry the old weekly setting into the new key.

New NOT NULL columns (`repayment_basis`, `repayment_period`, `borrower_type`)
are filled with their field defaults by the ORM during the upgrade -- which
are exactly the pre-1.1 behaviour (fixed, weekly, employee). Only the config
parameter needs copying by hand.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    icp = env['ir.config_parameter'].sudo()
    old = icp.get_param('nihao_hr_loan.weekly_repayment')
    if old and not icp.get_param('nihao_hr_loan.repayment_amount'):
        icp.set_param('nihao_hr_loan.repayment_amount', old)
