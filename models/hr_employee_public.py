# -*- coding: utf-8 -*-
"""Loan standing on the public employee record.

Odoo 18 splits the employee in two. HR staff read ``hr.employee``; everyone
else is transparently routed to ``hr.employee.public``, a cut-down model that
exposes only non-sensitive fields. A field added to the first and not the
second simply does not exist for an ordinary employee, and any read of it
fails::

    AccessError: The fields "loan_ids", which you are trying to read,
    are not available for employee public profiles.

That is what stopped an employee filing their own loan: creating one reads the
employee record to check eligibility, and the read died here rather than in
anything to do with loans.

Only what somebody may see about *themselves* is mirrored. The monthly wage is
deliberately not among these fields -- ``loan_max_amount`` is derived from it
and is safe to show, but the salary itself stays on ``hr.employee`` where the
HR access rules govern it.
"""

from odoo import fields, models

from . import loan_policy


class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'

    loan_ids = fields.One2many('efs.loan', 'employee_id', string='Loans',
                               readonly=True)
    loan_count = fields.Integer(
        compute='_compute_loan_standing', string='# Loans')
    loan_active_count = fields.Integer(
        compute='_compute_loan_standing', string='Ongoing Loans')
    loan_balance = fields.Monetary(
        compute='_compute_loan_standing', string='Current Debt',
        currency_field='loan_currency_id')
    loan_cutoff_deduction = fields.Monetary(
        compute='_compute_loan_standing', string='Deduction per Cutoff',
        currency_field='loan_currency_id')
    loan_unauthorized_count = fields.Integer(
        compute='_compute_loan_standing', string='Awaiting Authorization')
    loan_eligible = fields.Boolean(
        compute='_compute_loan_standing', string='Eligible for a Loan')
    loan_max_amount = fields.Monetary(
        compute='_compute_loan_standing', string='Maximum Loanable',
        currency_field='loan_currency_id')
    loan_service_years = fields.Float(
        compute='_compute_loan_standing', string='Years of Service')
    loan_currency_id = fields.Many2one(
        'res.currency', compute='_compute_loan_standing')
    loan_service_date = fields.Date(string='Loan Service Start', readonly=True)

    def _compute_loan_standing(self):
        """Read the private employee's answers, rather than recompute them.

        The arithmetic lives once, on ``hr.employee``. Duplicating it here
        would be a second implementation of the handbook that drifts from the
        first. ``sudo()`` is what makes the private record readable; the record
        rules on ``efs.loan`` still decide which loans the user may see.
        """
        private = self.env['hr.employee'].sudo()
        for rec in self:
            source = private.browse(rec.id).exists()
            rec.loan_count = source.loan_count if source else 0
            rec.loan_active_count = source.loan_active_count if source else 0
            rec.loan_balance = source.loan_balance if source else 0.0
            rec.loan_cutoff_deduction = (
                source.loan_cutoff_deduction if source else 0.0)
            rec.loan_unauthorized_count = (
                source.loan_unauthorized_count if source else 0)
            rec.loan_eligible = source.loan_eligible if source else False
            rec.loan_max_amount = source.loan_max_amount if source else 0.0
            rec.loan_service_years = source.loan_service_years if source else 0.0
            rec.loan_currency_id = (
                source.loan_currency_id if source else self.env.company.currency_id)
