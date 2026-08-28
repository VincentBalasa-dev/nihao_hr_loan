# -*- coding: utf-8 -*-
"""How much someone may borrow, by how long they have worked here.

A record per band rather than a setting, because the number of bands is itself
a policy choice. NihaoExpress runs three; a lender with a flat ceiling runs
one; a lender with none deletes them all. None of those should need a code
change, and none of them fit in a single field.

Each band is a **floor**: the multiple that applies is the one belonging to the
highest band whose year threshold the employee has reached. Someone at two and
a half years therefore stays on the one-year band. Raising a limit is a
management decision, and defaulting upward would grant it silently.

Below the lowest band the multiple is zero, which is what makes an employee
short of the minimum service period ineligible for any amount rather than for
an unlimited one.
"""

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class LoanEligibilityTier(models.Model):
    _name = 'efs.loan.eligibility.tier'
    _description = 'Loan Eligibility Tier'
    _order = 'min_service_years desc, id desc'

    name = fields.Char(
        string='Band', compute='_compute_name', store=True,
        help='Generated from the threshold and the multiple.')
    min_service_years = fields.Float(
        string='From (years of service)', required=True, default=1.0,
        help='The band applies from this many years of continuous service '
             'upward, until a higher band takes over.')
    salary_multiple = fields.Float(
        string='Multiple of Monthly Salary', required=True, default=0.5,
        help='Maximum loan as a multiple of monthly basic salary. 1.0 means '
             'one month, 2.0 two months.')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company,
        help='Leave empty to apply to every company.')

    _sql_constraints = [
        ('years_not_negative', 'CHECK(min_service_years >= 0)',
         'A service threshold cannot be negative.'),
        ('multiple_not_negative', 'CHECK(salary_multiple >= 0)',
         'A salary multiple cannot be negative.'),
    ]

    @api.depends('min_service_years', 'salary_multiple')
    def _compute_name(self):
        for rec in self:
            rec.name = 'From %g year(s): %g x monthly salary' % (
                rec.min_service_years or 0.0, rec.salary_multiple or 0.0)

    @api.constrains('min_service_years', 'company_id')
    def _check_unique_threshold(self):
        """Two bands starting at the same year make the ceiling ambiguous."""
        for rec in self:
            clash = self.search([
                ('id', '!=', rec.id),
                ('min_service_years', '=', rec.min_service_years),
                ('company_id', 'in', (False, rec.company_id.id)),
            ], limit=1)
            if clash:
                raise ValidationError(
                    'There is already a band starting at %g year(s) of '
                    'service. Two bands with the same threshold would make '
                    'the ceiling ambiguous — edit the existing one instead.'
                    % rec.min_service_years
                )

    @api.model
    def _multiple_for(self, service_years, company=None):
        """The multiple that applies at this seniority, or 0.0 below them all.

        Ordered highest-first by `_order`, so the first band the employee has
        reached is the one that applies.
        """
        company = company or self.env.company
        bands = self.sudo().search([
            ('company_id', 'in', (False, company.id)),
        ])
        for band in bands:
            if (service_years or 0.0) >= band.min_service_years:
                return band.salary_multiple
        return 0.0
