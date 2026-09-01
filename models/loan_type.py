# -*- coding: utf-8 -*-
"""Loan products, configured rather than coded.

Modelled on ``hr.salary.rule``: a record per product, carrying how its interest
is worked out, with a method picker that ends in a Python escape hatch for
anything the built-in methods do not cover. HR adds a product from the back
office; nobody edits Python to launch one.

NihaoExpress lends interest-free, so every seeded product is ``none`` and the
arithmetic below never runs for them. It exists because a lender offering
construction, business or appliance loans at different rates should configure
them here instead of forking the addon.

**Three interest methods, deliberately not more.**

``flat``
    Interest on the original principal for the whole term, the standard
    Philippine "add-on" quote: ``P x r x years``. Simple, and what most
    employer and appliance loans actually charge even where the marketing
    implies otherwise.

``diminishing``
    Interest accrues on what is still owed, recomputed each repayment. Cheaper
    for the borrower than a flat rate quoted at the same number, which is
    exactly why the two must be distinguishable rather than folded into one
    "rate" field.

``code``
    A Python expression, evaluated the same way a salary rule's is. For tiered
    rates, caps, or products with a fee structure no field can express.
"""

import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)

# Weeks in a year, used to turn a term in weeks into a term in years. 365.25/7
# rather than 52, so a long loan does not quietly accrue a fortnight of extra
# interest over its life.
WEEKS_PER_YEAR = 52.178571428571431

DEFAULT_INTEREST_CODE = (
    "# Available variables:\n"
    "#  principal   : amount borrowed\n"
    "#  term_weeks  : repayments at the agreed weekly figure\n"
    "#  weekly      : the agreed weekly repayment\n"
    "#  loan        : the efs.loan record (may be empty on a preview)\n"
    "#  loan_type   : this product\n"
    "#\n"
    "# Set `result` to the TOTAL interest for the whole loan.\n"
    "result = 0.0\n"
)


class LoanType(models.Model):
    _name = 'efs.loan.type'
    _description = 'Loan Product'
    _order = 'sequence, name'

    name = fields.Char(string='Name', required=True, translate=True)
    code = fields.Char(
        string='Code', required=True,
        help='Stable key any API or client speaks in. Changing it on a '
             'product that already has loans will orphan them.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(
        default=True,
        help='Archived products stay on existing loans but are no longer '
             'offered.')
    note = fields.Text(
        string='Description',
        help='A place to record the terms this product was agreed on.')

    interest_method = fields.Selection([
        ('none', 'No interest'),
        ('flat', 'Flat rate on principal'),
        ('diminishing', 'Diminishing balance'),
        ('code', 'Python code'),
    ], string='Interest', required=True, default='none',
        help='How interest is worked out. "No interest" is the default '
             'because a product that silently charges is worse than one that '
             'refuses to be configured.')

    interest_rate = fields.Float(
        string='Rate (% per year)', digits=(5, 3),
        help='Annual rate. Applied to the principal for a flat product, and '
             'to the outstanding balance for a diminishing one.')

    interest_python_compute = fields.Text(
        string='Python Code', default=DEFAULT_INTEREST_CODE)

    # ── Repayment terms ─────────────────────────────────────────────────────
    # Each product may override the company defaults from Settings. "Use
    # company setting" is the default for both pickers, so a product carries
    # no policy of its own until somebody gives it one. The figures are
    # copied onto each loan at filing, so changing a product later never
    # re-prices a running loan.
    repayment_basis = fields.Selection([
        ('default', 'Use company setting'),
        ('fixed', 'Fixed amount per period'),
        ('percent', 'Percent of principal per period'),
    ], string='Repayment Basis', required=True, default='default')
    repayment_period = fields.Selection([
        ('default', 'Use company setting'),
        ('week', 'Weekly'),
        ('semimonth', 'Semi-monthly'),
        ('month', 'Monthly'),
        ('payslip', 'Per Payslip (flat)'),
    ], string='Repayment Period', required=True, default='default')
    default_repayment_amount = fields.Monetary(
        string='Default Repayment per Period', currency_field='currency_id',
        help='Suggested instalment on a fixed basis. Leave at zero to use '
             'the company-wide figure.')
    default_repayment_percent = fields.Float(
        string='Default Percent per Period', digits=(5, 2),
        help='Suggested share of the principal per period on a percent '
             'basis. Leave at zero to use the company-wide figure.')
    currency_id = fields.Many2one(
        'res.currency', default=lambda self: self.env.company.currency_id)

    loan_count = fields.Integer(compute='_compute_loan_count', string='Loans')

    # Odoo 18 form. Odoo 19 replaces this list with one `models.Constraint`
    # attribute per rule; the tuple names below equal those attribute names
    # minus the underscore, so the database constraint names match on both.
    _sql_constraints = [
        ('unique_code', 'UNIQUE(code)',
         'Another loan product already uses this code.'),
        ('rate_not_negative', 'CHECK(interest_rate >= 0)',
         'An interest rate cannot be negative.'),
    ]

    def _compute_loan_count(self):
        counts = {}
        if self.ids:
            data = self.env['efs.loan'].sudo()._read_group(
                [('loan_type_id', 'in', self.ids)], ['loan_type_id'],
                ['__count'])
            counts = {loan_type.id: count for loan_type, count in data}
        for rec in self:
            rec.loan_count = counts.get(rec.id, 0)

    @api.constrains('interest_method', 'interest_rate')
    def _check_rate_present(self):
        """A rate-based product with no rate charges nothing, silently."""
        for rec in self:
            if rec.interest_method in ('flat', 'diminishing') \
                    and not rec.interest_rate:
                raise ValidationError(
                    '"%s" is set to charge interest but its rate is zero. Set '
                    'a rate, or choose "No interest".' % rec.name
                )

    def action_open_loans(self):
        """The stat button on the product form."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Loans',
            'res_model': 'efs.loan',
            'view_mode': 'list,form',
            'domain': [('loan_type_id', '=', self.id)],
            'context': {'default_loan_type_id': self.id},
        }

    def compute_interest(self, principal, term_weeks, weekly, loan=None):
        """Total interest for one loan over its whole term.

        Returns a positive amount to be added to the principal, or 0.0.
        Rounding is left to the caller so a two-step calculation does not round
        twice.
        """
        self.ensure_one()
        principal = float(principal or 0.0)
        term_weeks = int(term_weeks or 0)
        if principal <= 0 or term_weeks <= 0:
            return 0.0

        method = self.interest_method
        if method == 'none':
            return 0.0

        years = term_weeks / WEEKS_PER_YEAR

        if method == 'flat':
            return principal * (self.interest_rate / 100.0) * years

        if method == 'diminishing':
            # Interest accrues on what is still owed, one repayment at a time.
            # Iterated rather than solved in closed form because the weekly
            # figure is fixed by the handbook and the term follows from it --
            # the usual annuity formula answers the opposite question.
            weekly_rate = (self.interest_rate / 100.0) / WEEKS_PER_YEAR
            balance = principal
            interest = 0.0
            weekly = float(weekly or 0.0)
            if weekly <= 0:
                return 0.0
            # Bounded so a rate high enough to outrun the repayment cannot spin
            # forever; at that point the product is misconfigured, not slow.
            for _unused in range(term_weeks * 2 + 520):
                if balance <= 0.005:
                    break
                accrued = balance * weekly_rate
                interest += accrued
                balance += accrued - weekly
            return max(interest, 0.0)

        if method == 'code':
            ctx = {
                'principal': principal,
                'term_weeks': term_weeks,
                'weekly': float(weekly or 0.0),
                'loan': loan,
                'loan_type': self,
                'result': 0.0,
            }
            try:
                # Odoo 18: safe_eval COPIES the context unless nocopy=True, and
                # without it `result` is written to the copy and lost -- the
                # formula silently returns 0. Odoo 19 removed the keyword and
                # always mutates in place; drop `nocopy=True` when moving up.
                safe_eval(self.interest_python_compute or '', ctx, mode='exec',
                          nocopy=True)
            except Exception as exc:
                raise ValidationError(
                    'The interest formula on "%s" failed: %s' % (self.name, exc)
                )
            return float(ctx.get('result') or 0.0)

        return 0.0

    @api.model
    def _link_loans_by_code(self):
        """Attach loans carrying only a product code to the product record.

        Kept for databases where loans were imported with `loan_type` filled in
        and `loan_type_id` empty -- a spreadsheet import, or a migration from a
        build where the product was a Selection. Idempotent, and a no-op on a
        fresh install.

        Loans whose code matches no product are reported rather than silently
        dropped onto a default one.
        """
        Loan = self.env['efs.loan'].sudo()
        rows = Loan.search([('loan_type_id', '=', False)])
        if not rows:
            return 0

        by_code = {
            product.code: product
            for product in self.sudo().with_context(
                active_test=False).search([])
        }
        moved, orphans = 0, []
        for loan in rows:
            product = by_code.get(loan.loan_type)
            if product:
                loan.loan_type_id = product.id
                moved += 1
            else:
                orphans.append((loan.name, loan.loan_type))

        if orphans:
            _logger.warning(
                'efs.loan: %d loan(s) have no matching product and were left '
                'unlinked: %s', len(orphans), orphans)
        return moved
