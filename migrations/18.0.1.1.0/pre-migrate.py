# -*- coding: utf-8 -*-
"""1.1.0: the repayment is no longer assumed to be weekly.

`weekly_amortization` becomes `repayment_amount` and `term_weeks` becomes
`term_periods`, alongside a new basis (fixed / percent of principal) and
period (week / semi-month / month) on each loan. Columns are renamed rather
than added-and-copied so existing loans keep their agreed figure untouched.
"""


def _rename(cr, table, old, new):
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND column_name IN (%s, %s)
    """, (table, old, new))
    present = {row[0] for row in cr.fetchall()}
    if old in present and new not in present:
        cr.execute('ALTER TABLE %s RENAME COLUMN %s TO %s' % (table, old, new))


def migrate(cr, version):
    _rename(cr, 'efs_loan', 'weekly_amortization', 'repayment_amount')
    _rename(cr, 'efs_loan', 'term_weeks', 'term_periods')
    _rename(cr, 'efs_loan_type', 'default_weekly_repayment',
            'default_repayment_amount')
    # The old CHECK still references the renamed column and would sit beside
    # the new one for ever; the model now declares `repayment_positive`.
    cr.execute('ALTER TABLE efs_loan DROP CONSTRAINT IF EXISTS '
               'efs_loan_weekly_positive')
