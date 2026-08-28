# -*- coding: utf-8 -*-
"""1.1.1: let a contact's portal user see their own external loan.

The record rules in security/loan_security.xml are `noupdate`, which is right
for data a client may edit -- but it also means `-u` never re-reads them. The
1.1.0 domains, which add the partner branch, therefore have to be written
here for databases installed before it. A fresh install reads the XML.
"""
from odoo import api, SUPERUSER_ID

OWN_LOAN = ("['|', ('employee_id.user_id', '=', user.id), "
            "('partner_id.user_ids', 'in', [user.id])]")
OWN_PAYMENT = ("['|', ('loan_id.employee_id.user_id', '=', user.id), "
               "('loan_id.partner_id.user_ids', 'in', [user.id])]")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid, domain in (('nihao_hr_loan.rule_efs_loan_own', OWN_LOAN),
                          ('nihao_hr_loan.rule_efs_loan_payment_own', OWN_PAYMENT)):
        rule = env.ref(xmlid, raise_if_not_found=False)
        if rule:
            rule.write({'domain_force': domain})
