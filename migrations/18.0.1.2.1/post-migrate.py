# -*- coding: utf-8 -*-
"""1.2.1: link existing payroll repayments to their payslip.

`payslip_id` is new. Rows posted by payroll before it existed carry the
payslip number in `reference`; where that number still matches exactly one
payslip, the link is filled in. Anything else is left alone.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE efs_loan_payment p
           SET payslip_id = s.id
          FROM hr_payslip s
         WHERE p.payslip_id IS NULL
           AND p.payment_method = 'payroll'
           AND p.reference IS NOT NULL
           AND s.number = p.reference
           AND (SELECT COUNT(*) FROM hr_payslip s2 WHERE s2.number = p.reference) = 1
    """)
