# -*- coding: utf-8 -*-
"""1.2.2: loan references become LOAN-00001, the house style.

data/loan_sequence.xml is `noupdate`, so an installed database keeps the old
LOAN/<year>/0001 sequence until this rewrites it. Only the sequence changes:
existing loans keep the reference they were issued with -- renaming a
reference people have already quoted is worse than a mixed list.
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    seq = env.ref('nihao_hr_loan.seq_efs_loan', raise_if_not_found=False)
    if seq and seq.prefix != 'LOAN-':
        seq.write({'name': 'Loan', 'prefix': 'LOAN-', 'padding': 5})
