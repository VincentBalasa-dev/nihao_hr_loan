# -*- coding: utf-8 -*-
"""Repayment rules: salary-rule-style records deciding what a payslip deducts.

The repayment policy used to be one global formula in Settings. A record
per rule -- exactly the shape of ``hr.salary.rule`` -- lets a deployment
keep a catalogue ("one instalment per payslip", "2,000 at month-end",
"10% of the balance") and lets the application form pick one per loan, so
different clients, products and situations run different arithmetic side
by side.

Flat loans sharing the same rule also share its pot: the rule's figure is
computed once for the group and split equally across them, the same
one-instalment-per-employee behaviour the built-in rule has. Loans with
no rule keep the built-in behaviour and the Settings knobs.
"""

import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

DEFAULT_RULE_CODE = (
    "# One instalment per payslip; a closing balance under two\n"
    "# instalments is taken in full so the loan can finish.\n"
    "result = min(per, balance)\n"
    "if 0.005 < balance - result < per:\n"
    "    result = balance\n"
)


class LoanRepaymentRule(models.Model):
    _name = 'efs.loan.repayment.rule'
    _description = 'Loan Repayment Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    amount_python_compute = fields.Text(
        string='Python Code', required=True, default=DEFAULT_RULE_CODE,
        help='Assign the figure this payslip deducts to `result`. '
             'Available: `result` (pre-filled with the built-in figure), '
             '`per` (the instalment), `balance` (total outstanding across '
             'the loans on this rule), `paid`, `loans`, `employee`, '
             '`date_from`, `date_to`, `covers_15th`, `covers_month_end`, '
             'and the `date`, `timedelta`, `monthrange` tools. The figure '
             'is split equally across the loans on this rule, and the '
             'pay-availability cap and whole-instalment floor still apply '
             'after it.')
    note = fields.Text(
        string='Description',
        help='What this rule does, in the words a colleague needs.')

    def _evaluate(self, localdict, fallback):
        """Run the rule's code; a rule that raises must not break payroll."""
        self.ensure_one()
        from odoo.tools.safe_eval import safe_eval
        try:
            safe_eval(self.amount_python_compute or '',
                      localdict, mode='exec', nocopy=True)
            return float(localdict.get('result') or 0.0)
        except Exception:
            _logger.warning(
                'Repayment rule "%s" (id %s) raised; using the built-in '
                'figure of %.2f instead.', self.name, self.id, fallback,
                exc_info=True)
            return fallback
