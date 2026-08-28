# -*- coding: utf-8 -*-
"""Ask why, before asking for a running loan to be cancelled.

Cancelling an approved loan stops a payroll deduction and leaves a balance
outstanding, so the reason is the whole point: it is what HR and the
administrator decide on, and what the employee is answered with.
"""

from odoo import fields, models
from odoo.exceptions import ValidationError


class LoanCancelWizard(models.TransientModel):
    _name = 'efs.loan.cancel.wizard'
    _description = 'Request Cancellation of a Running Loan'

    loan_id = fields.Many2one(
        'efs.loan', string='Loan', required=True, ondelete='cascade')
    borrower_name = fields.Char(
        related='loan_id.borrower_name', string='Borrower', readonly=True)
    balance = fields.Monetary(
        related='loan_id.balance', string='Outstanding', readonly=True)
    currency_id = fields.Many2one(related='loan_id.currency_id', readonly=True)
    reason = fields.Text(
        string='Reason', required=True,
        help='Why the loan should be closed early. HR and the administrator '
             'both decide on this.')

    def action_confirm(self):
        self.ensure_one()
        if not (self.reason or '').strip():
            raise ValidationError('Give a reason for the cancellation.')
        self.loan_id.action_request_cancellation(self.reason.strip())
        return {'type': 'ir.actions.act_window_close'}
