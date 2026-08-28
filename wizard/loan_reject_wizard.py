# -*- coding: utf-8 -*-
"""Ask why, before refusing a loan application.

``efs.loan.action_reject`` takes a reason, but a plain header button cannot
pass one, so a button-only rejection always stored an empty reason -- and the
reason is the whole point: it is what the employee is told and what an appeal
is argued against.
"""

from odoo import fields, models
from odoo.exceptions import ValidationError


class LoanRejectWizard(models.TransientModel):
    _name = 'efs.loan.reject.wizard'
    _description = 'Reject a Loan Application'

    loan_id = fields.Many2one(
        'efs.loan', string='Loan', required=True, ondelete='cascade')
    borrower_name = fields.Char(
        related='loan_id.borrower_name', string='Borrower', readonly=True)
    amount = fields.Monetary(
        related='loan_id.amount', string='Principal', readonly=True)
    currency_id = fields.Many2one(
        related='loan_id.currency_id', readonly=True)
    reason = fields.Text(
        string='Reason', required=True,
        help='Recorded on the loan and posted to its chatter. This is what '
             'the employee is told.')

    def action_confirm(self):
        self.ensure_one()
        if not (self.reason or '').strip():
            raise ValidationError('Give a reason for the rejection.')
        self.loan_id.action_reject(self.reason.strip())
        return {'type': 'ir.actions.act_window_close'}
