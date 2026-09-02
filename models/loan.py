# -*- coding: utf-8 -*-
"""Loans and their repayments.

Balance is derived from posted repayments rather than decremented in place. A
stored running balance and a payment history are two sources of truth for one
number, and they drift the first time a payment is corrected.

The lifecycle is an application, not a ledger entry: it is filed, endorsed by
HR, approved by an administrator, and only then does it start deducting -- and
for an employee, only if they have signed the Salary Deduction Authorization
the Labor Code requires. See ``loan_approval_mixin.py`` for the approval
chain and ``hr_employee.py`` for what payroll actually takes each period.

**Who can borrow.** An employee, always. A contact (``res.partner``) too, if
the client switches external borrowers on. The employee-only rules -- length
of service, salary ceiling, coverage, payroll deduction -- simply do not apply
to a contact; the approval chain, ledger, interest and cancellation are
identical.

**What a broken rule does** is a setting: refuse the application (`enforce`),
accept it but flag it for HR (`advise`), or not evaluate the rules at all
(`off`). The rules themselves live in one place either way.
"""

import logging
import math
import operator as operators
from calendar import monthrange
from datetime import date as date_type, timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from . import loan_policy
from .loan_approval_mixin import (APPROVER_GROUP, ENDORSER_GROUP,
                                  STATE_WRITE_CTX)

_logger = logging.getLogger(__name__)

BASIS_SELECTION = [
    ('fixed', 'Fixed amount per period'),
    ('percent', 'Percent of principal per period'),
]
PERIOD_SELECTION = [
    ('week', 'Weekly'),
    ('semimonth', 'Semi-monthly'),
    ('month', 'Monthly'),
    ('payslip', 'Per Payslip (flat)'),
]

# The loan fields payroll reads when it computes a DED_LOAN line (see
# hr_employee._loan_deductions). A write that touches none of these cannot
# change any payslip, so open slips are only recomputed when one of them moves.
PAYSLIP_INPUT_FIELDS = frozenset((
    'state', 'deduction_authorized', 'start_date', 'employee_id',
    'repayment_amount', 'repayment_basis', 'repayment_percent',
    'repayment_period', 'amount',
))


class Loan(models.Model):
    _name = 'efs.loan'
    _description = 'Loan'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'efs.loan.approval.mixin']
    _order = 'start_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(string='Reference', readonly=True, copy=False,
                       default='New', index=True)

    # ── Who is borrowing ────────────────────────────────────────────────────
    borrower_type = fields.Selection([
        ('employee', 'Employee'),
        ('partner', 'External Borrower'),
    ], string='Borrower', default='employee', required=True, index=True,
        help='An employee is measured against the handbook rules and repaid '
             'through payroll. An external borrower is a contact: no service '
             'or salary rules apply, and repayments are recorded by hand.')
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', ondelete='cascade', index=True,
        tracking=True)
    partner_id = fields.Many2one(
        'res.partner', string='Contact', ondelete='restrict', index=True,
        tracking=True)
    borrower_name = fields.Char(
        string='Borrower Name', compute='_compute_borrower', store=True,
        index=True)
    # Whether the External option is even offered. Read from Settings; a view
    # cannot read a config parameter directly, so it goes through a field.
    external_allowed = fields.Boolean(compute='_compute_external_allowed')
    company_id = fields.Many2one(
        'res.company', string='Company', compute='_compute_borrower',
        store=True, index=True)

    # The product this loan is lent under. A record rather than a Selection so
    # a deployment can add its own -- construction, business, appliance --
    # without editing Python. See models/loan_type.py.
    loan_type_id = fields.Many2one(
        'efs.loan.type', string='Loan Product', required=True, index=True,
        tracking=True, ondelete='restrict')
    # A stored mirror of the product's code. Clients speak in codes, and a
    # related field would make every list query join.
    loan_type = fields.Char(
        string='Product Code', compute='_compute_loan_type_code', store=True,
        index=True)

    amount = fields.Monetary(
        string='Principal', required=True, tracking=True,
        help='Amount borrowed, before any repayment.')

    # ── How it is repaid ────────────────────────────────────────────────────
    # The instalment is the input and the term falls out of it, which is the
    # opposite of an ordinary amortisation. Basis and period are copied onto
    # the loan at filing from the product or the company default, so a later
    # change of policy never silently re-prices a running loan.
    repayment_basis = fields.Selection(
        BASIS_SELECTION, string='Repayment Basis', required=True,
        default=lambda self: self._default_repayment_basis(), tracking=True,
        help='Fixed: a set figure each period. Percent: a share of the '
             'principal each period, so 10% repays in about ten periods.')
    repayment_period = fields.Selection(
        PERIOD_SELECTION, string='Repayment Period', required=True,
        default=lambda self: self._default_repayment_period(), tracking=True,
        help='How often an instalment falls due. Payroll prorates by the days '
             'each payslip actually covers, so this means the same thing on '
             'any payroll schedule.')
    repayment_percent = fields.Float(
        string='Percent per Period', digits=(5, 2),
        default=lambda self: self._default_repayment_percent(), tracking=True,
        help='Used when the basis is Percent. The instalment is this share '
             'of the principal.')
    repayment_amount = fields.Monetary(
        string='Repayment per Period', compute='_compute_repayment_amount',
        store=True, readonly=False, tracking=True,
        help='What is repaid each period. Editable on a fixed basis -- the '
             'handbook lets an employee ask for more; computed from the '
             'principal on a percent basis.')
    repayment_rule_id = fields.Many2one(
        'efs.loan.repayment.rule', string='Repayment Rule', index=True,
        tracking=True, ondelete='restrict',
        default=lambda self: self._default_repayment_rule(),
        help='The deal this loan runs under: the arithmetic deciding what '
             'each payslip deducts for a flat (per-payslip) loan, plus its '
             'figures (instalment, start delay, repayment cap) -- picked at '
             'application time, like a salary rule. Flat loans sharing a '
             'rule share its instalment. Empty runs the built-in figure.')

    term_periods = fields.Integer(
        string='Term (periods)', compute='_compute_amortization', store=True,
        help='How many instalments repayment runs for. Derived, because the '
             'instalment is what was agreed.')
    term_months = fields.Integer(
        string='Term (months)', compute='_compute_amortization', store=True,
        help='The periods above, expressed in months. Derived.')

    purpose = fields.Text(string='Purpose', required=True)
    start_date = fields.Date(
        string='Start Date', tracking=True,
        help='First payroll period the deduction applies to. Set when the '
             'loan is approved.')
    currency_id = fields.Many2one(
        'res.currency', string='Currency', required=True,
        default=lambda self: self.env.company.currency_id)

    state = fields.Selection([
        ('pending', 'Pending HR'),
        ('endorsed', 'Endorsed - Pending Approval'),
        # There is deliberately no 'approved' state: final approval activates
        # the loan in the same write (action_approve), because approval and the
        # start of repayment are one decision for a company loan.
        ('active', 'Active'),
        # A cancellation asked for but not yet decided. The loan is still
        # active in every way that matters -- deductions continue -- because a
        # request is not a decision. See action_request_cancellation.
        ('cancel_requested', 'Cancellation Requested'),
        ('paid', 'Fully Paid'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='pending', required=True, index=True,
        tracking=True)

    payment_ids = fields.One2many(
        'efs.loan.payment', 'loan_id', string='Repayments')
    payment_count = fields.Integer(
        string='# Repayments', compute='_compute_balance')

    monthly_amortization = fields.Monetary(
        string='Per Month', compute='_compute_amortization', store=True,
        help='The instalment expressed per calendar month, for display. What '
             'payroll actually deducts is computed from the days in the pay '
             'period.')
    interest_amount = fields.Monetary(
        string='Interest', compute='_compute_amortization', store=True,
        help='Total interest over the life of the loan, from the product. '
             'Zero for an interest-free product.')
    total_payable = fields.Monetary(
        string='Total Payable', compute='_compute_amortization', store=True,
        help='Principal plus interest. This, not the principal, is what the '
             'instalments have to cover.')

    total_paid = fields.Monetary(string='Total Paid', compute='_compute_balance')
    balance = fields.Monetary(
        string='Outstanding Balance', compute='_compute_balance',
        search='_search_balance')
    paid_percentage = fields.Float(
        string='Repaid %', compute='_compute_balance')

    # ── Policy result ───────────────────────────────────────────────────────
    # What the lending rules say about this application, kept on the record
    # so it can be shown and filtered. In `enforce` mode a failing loan never
    # gets saved, so these are only ever populated in `advise` mode -- where
    # they are the whole point: HR sees exactly which rule the application
    # breaks and decides anyway.
    policy_ok = fields.Boolean(
        string='Policy OK', compute='_compute_policy', store=True, default=True)
    policy_warnings = fields.Text(
        string='Policy Issues', compute='_compute_policy', store=True)

    # Handbook section 6 / Labor Code article 113: no wage deduction without
    # the employee's written authorization. Nothing is deducted until this is
    # recorded, and the deduction stops if it is ever withdrawn. Meaningless
    # for an external borrower, who has no wage to deduct from.
    deduction_authorized = fields.Boolean(
        string='Deduction Authorized', readonly=True, copy=False, tracking=True,
        help='The employee has executed a Salary Deduction Authorization for '
             'this loan. Until this is ticked the loan deducts nothing, '
             'however active it looks.')
    authorized_date = fields.Datetime(
        string='Authorized On', readonly=True, copy=False)
    authorized_by = fields.Many2one(
        'res.users', string='Authorization Recorded By', readonly=True,
        copy=False)

    approved_by = fields.Many2one(
        'res.users', string='Approved By', readonly=True, copy=False)
    approved_date = fields.Datetime(
        string='Approval Date', readonly=True, copy=False)
    rejection_reason = fields.Text(
        string='Rejection Reason', readonly=True, copy=False)

    # ── Cancelling a loan that is already running ───────────────────────────
    # An approved loan cannot simply be cancelled: money has moved and payroll
    # is deducting. It goes back through the same two levels that approved it.
    cancel_reason = fields.Text(
        string='Cancellation Reason', readonly=True, copy=False)
    cancel_requested_by = fields.Many2one(
        'res.users', string='Cancellation Requested By', readonly=True,
        copy=False)
    cancel_requested_date = fields.Datetime(
        string='Cancellation Requested On', readonly=True, copy=False)
    cancel_endorsed_by = fields.Many2one(
        'res.users', string='Cancellation Endorsed By', readonly=True,
        copy=False, help='HR, level one of the cancellation chain.')
    cancel_endorsed_date = fields.Datetime(
        string='Cancellation Endorsed On', readonly=True, copy=False)
    cancelled_balance = fields.Monetary(
        string='Balance at Cancellation', readonly=True, copy=False,
        help='What was still outstanding the moment the loan was cancelled. '
             'Kept because cancelling stops the payroll deduction; it does '
             'not by itself decide whether the debt is forgiven.')

    # Odoo 18 form. Odoo 19 replaces this list with one `models.Constraint`
    # attribute per rule; the tuple names below equal those attribute names
    # minus the underscore, so the database constraint names match on both.
    _sql_constraints = [
        ('principal_positive', 'CHECK(amount > 0)',
         'Loan principal must be greater than zero.'),
        ('repayment_positive', 'CHECK(repayment_amount > 0)',
         'The repayment per period must be greater than zero.'),
    ]

    # ── Defaults ────────────────────────────────────────────────────────────
    # Everything repayment-shaped defaults from the Repayment Rules
    # catalogue: the first active rule (by sequence) is the standing offer,
    # and its figures land on the application. There is no company-wide
    # repayment Setting any more -- a deployment that wants a different
    # standing offer reorders or edits the rules.

    @api.model
    def _default_repayment_basis(self):
        return 'fixed'

    @api.model
    def _default_repayment_period(self):
        # Flat per payslip is the house semantics; the rules catalogue is
        # its customisation layer. A loan product may still override.
        return 'payslip'

    @api.model
    def _default_repayment_rule(self):
        return self.env['efs.loan.repayment.rule'].search(
            [], order='sequence, id', limit=1)

    @api.model
    def _default_repayment_percent(self):
        rule = self._default_repayment_rule()
        return rule.percent or loan_policy.DEFAULT_REPAYMENT_PERCENT

    def _rule_for_defaults(self):
        """The rule whose figures govern this loan: its own, else the
        catalogue's standing offer."""
        rule = self[:1].repayment_rule_id if self else False
        return rule or self._default_repayment_rule()

    def _default_start_date(self, approved_on=None):
        """Handbook section 5.2: N days after the proceeds are received.

        The delay is the rule's ``start_delay_days`` -- part of the deal,
        like its instalment.
        """
        base = (fields.Date.to_date(approved_on)
                or fields.Date.context_today(self))
        rule = self._rule_for_defaults()
        days = (rule.start_delay_days if rule
                else loan_policy.DEFAULT_START_DELAY_DAYS)
        return base + timedelta(days=days)

    @api.onchange('loan_type_id')
    def _onchange_loan_type_id(self):
        """Take the product's repayment terms, where it sets any.

        A product may override the company basis, period, amount or percent.
        Only fields still at the company default are moved -- overwriting a
        figure someone typed would silently re-price the loan.
        """
        for rec in self:
            product = rec.loan_type_id
            if not product:
                continue
            rule = rec._rule_for_defaults()
            default_pct = (rule.percent if rule
                           else loan_policy.DEFAULT_REPAYMENT_PERCENT)
            default_amt = (rule.amount if rule
                           else loan_policy.DEFAULT_WEEKLY_REPAYMENT)
            if product.repayment_basis != 'default':
                rec.repayment_basis = product.repayment_basis
            if product.repayment_period != 'default':
                rec.repayment_period = product.repayment_period
            if product.default_repayment_percent and \
                    rec.repayment_percent == default_pct:
                rec.repayment_percent = product.default_repayment_percent
            if product.default_repayment_amount and rec.repayment_basis == 'fixed' \
                    and (not rec.repayment_amount
                         or rec.repayment_amount == default_amt):
                rec.repayment_amount = product.default_repayment_amount

    @api.onchange('repayment_rule_id')
    def _onchange_repayment_rule_id(self):
        """Picking a rule is picking the deal: its figures land on the form.

        Deliberately overwrites what is on the screen -- switching from the
        Standard rule to a VIP rule is supposed to re-price the draft. A
        figure typed *after* the rule is chosen stays, because nothing
        fires again -- but never below the rule's floor, which
        `_check_repayment_minimum` holds.
        """
        for rec in self:
            rule = rec.repayment_rule_id
            if not rule:
                continue
            if rule.percent:
                rec.repayment_percent = rule.percent
            if rule.amount and rec.repayment_basis == 'fixed':
                rec.repayment_amount = rule.amount

    @api.onchange('repayment_amount')
    def _onchange_repayment_amount_floor(self):
        """Say it while they type, not only at save.

        The save-time constraint below is the enforcement; this is the
        courtesy popup so nobody fills a whole application around a
        figure the save will refuse.
        """
        rule = self.repayment_rule_id
        if rule and rule.amount > 0 and self.repayment_basis == 'fixed' \
                and 0 < (self.repayment_amount or 0.0) < rule.amount - 0.005:
            return {'warning': {
                'title': 'Below the rule\'s minimum',
                'message': (
                    'The "%s" repayment rule sets a minimum repayment of '
                    '%.2f per period. %.2f will be refused when this '
                    'application is saved - raise the repayment, or pick '
                    'a rule with a lower minimum.'
                    % (rule.name, rule.amount, self.repayment_amount)),
            }}

    @api.constrains('repayment_amount', 'repayment_rule_id',
                    'repayment_basis')
    def _check_repayment_minimum(self):
        """The rule's figure is a floor, not only a default.

        Handbook s.5.3 allows faster repayment on request -- never slower:
        an employee may raise the instalment above the rule's minimum, but
        a loan on the rule cannot creep below the deal it was filed under.
        Percent-basis loans are exempt (their instalment is computed from
        the principal, and the percent has its own default).
        """
        for rec in self:
            rule = rec.repayment_rule_id
            if not rule or rule.amount <= 0 or rec.repayment_basis != 'fixed':
                continue
            if (rec.repayment_amount or 0.0) < rule.amount - 0.005:
                raise ValidationError(
                    'The repayment per period cannot be below %.2f - the '
                    'minimum of the "%s" repayment rule. Raise the '
                    'repayment, or pick a rule with a lower minimum.'
                    % (rule.amount, rule.name))

    # ── Computes ────────────────────────────────────────────────────────────

    @api.depends('borrower_type', 'employee_id', 'employee_id.name',
                 'employee_id.company_id', 'partner_id', 'partner_id.name',
                 'partner_id.company_id')
    def _compute_borrower(self):
        for rec in self:
            if rec.borrower_type == 'partner':
                rec.borrower_name = rec.partner_id.name or ''
                rec.company_id = (rec.partner_id.company_id
                                  or self.env.company)
            else:
                rec.borrower_name = rec.employee_id.name or ''
                rec.company_id = (rec.employee_id.company_id
                                  or self.env.company)

    def _compute_external_allowed(self):
        allowed = loan_policy.allow_external_borrowers(self.env)
        for rec in self:
            rec.external_allowed = allowed

    @api.depends('loan_type_id')
    def _compute_loan_type_code(self):
        for rec in self:
            rec.loan_type = rec.loan_type_id.code or ''

    @api.depends('amount', 'repayment_basis', 'repayment_percent')
    def _compute_repayment_amount(self):
        """The instalment.

        Percent basis: always the principal times the percent -- raising the
        principal raises the instalment. Fixed basis: whatever was typed, or
        the rule's figure when nothing has been.
        """
        for rec in self:
            if rec.repayment_basis == 'percent':
                pct = rec.repayment_percent or 0.0
                rec.repayment_amount = round((rec.amount or 0.0) * pct / 100.0, 2)
            elif not rec.repayment_amount:
                rule = rec._rule_for_defaults()
                rec.repayment_amount = ((rule.amount if rule else 0.0)
                                        or loan_policy.DEFAULT_WEEKLY_REPAYMENT)

    def _period_days(self):
        self.ensure_one()
        # A flat loan's period IS the payroll cutoff; its length comes from
        # the cadence setting, not from the loan.
        if self.repayment_period == 'payslip':
            return loan_policy.PERIOD_DAYS.get(
                loan_policy.payslip_cadence(self.env), 365.25 / 24)
        return loan_policy.PERIOD_DAYS.get(self.repayment_period, 7.0)

    def _periods_per_month(self):
        self.ensure_one()
        if self.repayment_period == 'payslip':
            return loan_policy.PERIODS_PER_MONTH.get(
                loan_policy.payslip_cadence(self.env), 2.0)
        return loan_policy.PERIODS_PER_MONTH.get(
            self.repayment_period, loan_policy.WEEKS_PER_MONTH)

    @api.depends('amount', 'repayment_amount', 'repayment_period',
                 'loan_type_id', 'loan_type_id.interest_method',
                 'loan_type_id.interest_rate',
                 'loan_type_id.interest_python_compute')
    def _compute_amortization(self):
        for rec in self:
            per = rec.repayment_amount or 0.0
            if per > 0 and rec.amount:
                # Interest is worked out on a first pass over the
                # interest-free term, then folded into what must be repaid.
                # Solving the two together would need an iteration for a figure
                # that moves by at most a repayment; a second pass is enough
                # and is explainable to whoever has to check the schedule.
                base_periods = int(math.ceil(rec.amount / per))
                # The product's interest maths thinks in weeks; convert the
                # term so a monthly loan is not charged as if it were weekly.
                base_weeks = int(math.ceil(base_periods * rec._period_days() / 7.0))
                weekly_equiv = per * 7.0 / rec._period_days()
                interest = 0.0
                if rec.loan_type_id:
                    interest = rec.loan_type_id.compute_interest(
                        rec.amount, base_weeks, weekly_equiv, rec)
                rec.interest_amount = round(max(interest, 0.0), 2)
                rec.total_payable = round(rec.amount + rec.interest_amount, 2)
                rec.term_periods = int(math.ceil(rec.total_payable / per))
            else:
                rec.interest_amount = 0.0
                rec.total_payable = round(rec.amount or 0.0, 2)
                rec.term_periods = 0
            ppm = rec._periods_per_month()
            rec.term_months = (
                int(math.ceil(rec.term_periods / ppm)) if rec.term_periods else 0)
            # For display only. The figure payroll uses comes from the actual
            # days in the pay period -- see hr_employee._loan_deductions.
            rec.monthly_amortization = round(per * ppm, 2)

    @api.depends('amount', 'total_payable', 'payment_ids',
                 'payment_ids.amount', 'payment_ids.state')
    def _compute_balance(self):
        for rec in self:
            posted = rec.payment_ids.filtered(lambda p: p.state == 'posted')
            paid = sum(posted.mapped('amount'))
            # Owed against the total payable, not the principal: on an
            # interest-bearing product those differ, and settling the principal
            # alone would close a loan that still owes interest.
            owed = rec.total_payable or rec.amount or 0.0
            rec.total_paid = round(paid, 2)
            rec.balance = round(max(0.0, owed - paid), 2)
            # Clamped for the same reason the balance is: the progress bar this
            # feeds cannot render past its end, and a loan that somehow took an
            # extra peso is 100% repaid, not 104%. `total_paid` still carries
            # the unclamped truth.
            rec.paid_percentage = (
                min(paid / owed * 100.0, 100.0) if owed else 0.0)
            rec.payment_count = len(rec.payment_ids)

    def _search_balance(self, operator, value):
        """Make the computed balance filterable in the back office.

        A non-stored compute has no column, so Postgres cannot filter on it and
        Odoo refuses the domain outright -- which is what makes a "Has Balance"
        filter fail to load.

        This resolves the domain in Python and hands back an id list. That
        means loading every loan to answer one filter, which is fine at the
        scale this module operates at (one row per loan, not per transaction)
        and would not be if loans were ever counted in the millions. Storing
        the balance instead would trade this scan for a stale-value problem the
        moment a repayment is corrected.
        """
        comparisons = {
            '=': operators.eq, '!=': operators.ne,
            '<': operators.lt, '<=': operators.le,
            '>': operators.gt, '>=': operators.ge,
        }
        compare = comparisons.get(operator)
        if compare is None:
            raise ValidationError(
                'Outstanding balance cannot be filtered with "%s".' % operator)
        # An empty domain here is safe: it carries no balance term, so this
        # method is not re-entered.
        matching = [
            loan.id for loan in self.search([])
            if compare(loan.balance, value)
        ]
        return [('id', 'in', matching)]

    # ── The lending rules ───────────────────────────────────────────────────

    @api.depends('amount', 'borrower_type', 'employee_id', 'partner_id',
                 'state', 'repayment_amount', 'repayment_basis',
                 'repayment_percent', 'repayment_period')
    def _compute_policy(self):
        """Record what the rules say, for the form and the list.

        Evaluated only while the application is still open; once decided the
        result is history and re-running it against changed settings would
        rewrite it. In `off` mode nothing is evaluated at all.
        """
        mode = loan_policy.policy_enforcement(self.env)
        for rec in self:
            if mode == 'off' or rec.state not in ('pending', 'endorsed'):
                rec.policy_ok = True
                rec.policy_warnings = False
                continue
            issues = rec._policy_violations()
            rec.policy_ok = not issues
            rec.policy_warnings = '\n'.join(issues) if issues else False

    @api.constrains('amount', 'borrower_type', 'employee_id', 'partner_id',
                    'state', 'repayment_amount', 'repayment_percent')
    def _check_eligibility(self):
        """Refuse an application that breaks a rule -- in `enforce` mode only.

        Checked on the record rather than at one entry point so it holds
        however the loan is created: back office, import, shell or a REST
        layer added later. A limit that only exists in one entry point is not
        a limit.

        In `advise` mode the same rules run but their result is recorded on
        the loan (see ``_compute_policy``) instead of refusing it; in `off`
        mode they do not run.

        Loans past the decision are exempt: management reserves the right to
        approve, reduce or deny, so an approved figure outranks the default
        ceiling, and retro-failing an active loan would make every unrelated
        edit impossible.
        """
        if loan_policy.policy_enforcement(self.env) != 'enforce':
            return
        for rec in self:
            if rec.state not in ('pending', 'endorsed'):
                continue
            issues = rec._policy_violations()
            if issues:
                raise ValidationError('\n'.join(issues))

    def _policy_violations(self):
        """Every rule this application fails, as a list of plain sentences.

        Pure: raises nothing, writes nothing. The employee-only rules are
        skipped for an external borrower; the flat external ceiling is skipped
        for an employee.
        """
        self.ensure_one()
        issues = []
        if self.borrower_type == 'partner':
            if not self.partner_id:
                return issues
            checks = (self._check_external_ceiling, self._check_concurrent_loans,
                      self._check_cooldown)
        else:
            if not self.employee_id:
                return issues
            # The employee's own standing is read elevated. Deciding whether
            # somebody may borrow needs their length of service and monthly
            # wage, and an ordinary employee cannot read hr.contract -- so
            # without this an employee filing their own application would die
            # on "You are not allowed to access 'Employee Contract'". Nothing
            # is disclosed: the ceiling is already on hr.employee.public, and
            # the wage itself is never returned, only compared against.
            checks = (self._check_coverage, self._check_service_requirement,
                      self._check_concurrent_loans, self._check_cooldown,
                      self._check_amount_ceiling, self._check_repayment_capacity)
        for check in checks:
            message = check()
            if message:
                issues.append(message)
        return issues

    def _other_loans(self, states=None):
        """The borrower's other loans, excluding this application."""
        self.ensure_one()
        if self.borrower_type == 'partner':
            domain = [('partner_id', '=', self.partner_id.id)]
        else:
            domain = [('employee_id', '=', self.employee_id.id)]
        if self._origin.id:
            domain.append(('id', '!=', self._origin.id))
        if states:
            domain.append(('state', 'in', list(states)))
        return self.env['efs.loan'].sudo().search(domain)

    def _policy_employee(self):
        """The employee record the policy checks read, elevated.

        Length of service and monthly wage are needed to decide eligibility,
        and an ordinary employee cannot read hr.contract. Elevating here keeps
        an employee able to file their own application without widening any
        access rule; nothing is returned to them that hr.employee.public does
        not already expose.
        """
        self.ensure_one()
        return self.employee_id.sudo()

    def _borrower_label(self):
        self.ensure_one()
        return self.borrower_name or (
            self.partner_id.name if self.borrower_type == 'partner'
            else self.employee_id.name) or 'this borrower'

    def _check_coverage(self):
        """Handbook s.2: who the loan policy covers at all.

        Two settings, both off unless a client turns them on. Restricting to
        certain employee types ("regular employees") refuses everyone else.
        Agency staff, when that rule is on, are refused unless an HR
        administrator is the one filing -- the handbook's "management
        discretion", expressed as the same escape hatch that lets an
        administrator approve an un-endorsed loan. Superuser passes, so
        imports and migrations are not blocked.
        """
        self.ensure_one()
        if self.env.su:
            return None
        employee = self._policy_employee()

        allowed = loan_policy.eligible_employee_types(self.env)
        if allowed and 'employee_type' in employee._fields:
            kind = employee.employee_type or ''
            if kind not in allowed:
                labels = dict(employee._fields['employee_type'].selection)
                return (
                    'Company loans are open to %s employees only (handbook, '
                    'section 2). %s is recorded as %s.'
                    % (' / '.join(labels.get(k, k) for k in sorted(allowed)),
                       employee.name, labels.get(kind, kind or 'unset')))

        if (loan_policy.agency_by_discretion(self.env)
                and 'agency_id' in employee._fields and employee.agency_id
                and not self.env.user.has_group(APPROVER_GROUP)):
            return (
                '%s is under a manpower agency (%s). Handbook section 2 '
                'leaves such loans to management discretion, so an HR '
                'administrator has to file this application.'
                % (employee.name, employee.agency_id.display_name))
        return None

    def _check_service_requirement(self):
        """Minimum continuous service before anyone may borrow at all."""
        self.ensure_one()
        employee = self._policy_employee()
        if employee.loan_eligible:
            return None
        return (
            'Company loans require at least %g year(s) of continuous service. '
            '%s has %.1f year(s) on record.'
            % (loan_policy.min_service_years(self.env), employee.name,
               employee.loan_service_years or 0.0))

    def _check_concurrent_loans(self):
        """How many loans may run at once, and whether the last must be settled.

        Both off by default. Turned on, they are what stops someone stacking
        loans until the deductions swallow the wage -- the ceiling alone does
        not, because it measures one application at a time.
        """
        self.ensure_one()
        running = self._other_loans(('active', 'cancel_requested'))
        who = self._borrower_label()

        limit = loan_policy.max_active_loans(self.env)
        if limit and len(running) >= limit:
            return (
                '%s already has %d loan(s) being repaid and the limit is %d. '
                'One has to be settled before applying again.'
                % (who, len(running), limit))

        if loan_policy.require_previous_settled(self.env):
            unsettled = running.filtered(lambda loan: loan.balance > 0.01)
            if unsettled:
                return (
                    '%s still owes %.2f on %s. An earlier loan has to be '
                    'settled before a new one can be applied for.'
                    % (who, sum(unsettled.mapped('balance')),
                       ', '.join(unsettled.mapped('name'))))
        return None

    def _check_cooldown(self):
        """A waiting period after settling before reapplying.

        Measured from the last posted repayment on a settled loan -- the day
        the debt actually cleared, not the day the loan was approved or its
        nominal end date.
        """
        self.ensure_one()
        days = loan_policy.cooldown_days(self.env)
        if not days:
            return None
        settled = self._other_loans(('paid',))
        last = max(
            (p.date for loan in settled for p in loan.payment_ids
             if p.state == 'posted' and p.date),
            default=None)
        if not last:
            return None
        available = last + timedelta(days=days)
        if fields.Date.context_today(self) < available:
            return (
                '%s settled a loan on %s and the cooling-off period is %d '
                'day(s), so the next application can be made from %s.'
                % (self._borrower_label(), last, days, available))
        return None

    def _check_amount_ceiling(self):
        """The loanable ceiling for this length of service.

        By default it measures the new application on its own. Switch on
        "Ceiling Includes Existing Debt" and it measures the request plus
        what is still owed on every unfinished loan: repayments re-open
        room under the ceiling.

        The "Minimum Room to Reborrow" threshold then stops the nibble:
        an employee who still has unfinished loans may only file again
        once their open room (ceiling minus outstanding balances) has
        reached that figure. The threshold gates the ROOM, never the
        request -- once the room is there, a 500-peso application is as
        welcome as a 5,000 one. It is deliberately not applied to someone
        with no loans, so a small band ceiling cannot lock anyone out of
        their first loan.
        """
        self.ensure_one()
        employee = self._policy_employee()
        ceiling = employee.loan_max_amount or 0.0

        # A zero ceiling means no band covers this employee -- either none are
        # configured at all, or none reaches their length of service. It does
        # NOT mean "unconfigured, so allow anything": treating it that way is
        # how deleting the bands would silently lift every limit. To lend
        # without a ceiling, configure a band with a large multiple.
        if ceiling <= 0.0:
            return (
                'No eligibility band applies to %s at %.1f year(s) of '
                'service, so no amount can be approved. Set one in '
                'Loans > Configuration > Eligibility Bands.'
                % (employee.name, employee.loan_service_years or 0.0))

        requested = self.amount or 0.0
        existing = 0.0
        if loan_policy.ceiling_counts_existing_debt(self.env):
            others = self._other_loans(('active', 'cancel_requested'))
            # Balance, not principal: repayments re-open room. The
            # threshold below decides when the re-opened room is enough
            # to borrow against.
            existing = sum(others.mapped('balance'))
            threshold = loan_policy.reborrow_min_headroom(self.env)
            available = ceiling - existing
            if others and threshold > 0 and available < threshold - 0.005:
                return (
                    '%s still owes %.2f on %d unfinished loan(s), leaving '
                    '%.2f of loanable room. A new loan needs at least %.2f '
                    'of room - repayments free it up, or wait for a loan '
                    'to finish.'
                    % (employee.name, existing, len(others),
                       max(available, 0.0), threshold))

        if requested + existing <= ceiling + 0.005:
            return None

        wage = employee._loan_monthly_wage()
        share = (ceiling / wage * 100.0) if wage else 0.0
        if existing:
            return (
                'The maximum loanable amount for %s is %.2f (%.0f%% of monthly '
                'basic salary at %.1f year(s) of service). They still owe '
                '%.2f, leaving %.2f. Requested: %.2f.'
                % (employee.name, ceiling, share,
                   employee.loan_service_years or 0.0, existing,
                   max(ceiling - existing, 0.0), requested))
        return (
            'The maximum loanable amount for %s is %.2f - %.0f%% of monthly '
            'basic salary at %.1f year(s) of service. Requested: %.2f.'
            % (employee.name, ceiling, share,
               employee.loan_service_years or 0.0, requested))

    def _check_repayment_capacity(self):
        """Total repayments must leave the employee something to live on.

        Off by default. Turned on, it sums the instalment of every active loan
        plus this application, expresses it per month, and refuses if that
        exceeds the allowed share of monthly basic salary. The share is the
        repayment rule's ``max_repayment_percent`` -- part of the deal being
        applied for, so different deals carry different protections.

        This is the rule a ceiling cannot express: a modest loan repaid very
        fast takes more out of a payslip than a large one repaid slowly.
        """
        self.ensure_one()
        rule = self._rule_for_defaults()
        percent = (rule.max_repayment_percent if rule
                   else loan_policy.DEFAULT_MAX_REPAYMENT_PCT)
        if not percent:
            return None
        employee = self._policy_employee()
        wage = employee._loan_monthly_wage()
        if not wage:
            return None

        monthly = round(
            (self.repayment_amount or 0.0) * self._periods_per_month(), 2)
        for other in self._other_loans(('active', 'cancel_requested')):
            monthly += other.monthly_amortization or 0.0
        allowed = wage * percent / 100.0
        if monthly > allowed + 0.005:
            return (
                'Repayments would take %.2f a month from %s, which is %.0f%% '
                'of their %.2f monthly salary. The limit is %g%% (%.2f). '
                'Lower the repayment or the amount.'
                % (monthly, employee.name, monthly / wage * 100.0,
                   wage, percent, allowed))
        return None

    def _check_external_ceiling(self):
        """The only automatic limit on an external borrower: a flat maximum.

        There is no salary to measure against, so a client either sets this
        figure or runs external loans in `advise` mode and reviews by hand.
        Zero means no limit.
        """
        self.ensure_one()
        cap = loan_policy.external_max_amount(self.env)
        if cap and (self.amount or 0.0) > cap + 0.005:
            return (
                'The maximum amount for an external borrower is %.2f. '
                'Requested: %.2f.' % (cap, self.amount or 0.0))
        return None

    # ── Structural constraints ──────────────────────────────────────────────

    @api.constrains('borrower_type', 'employee_id', 'partner_id')
    def _check_borrower(self):
        """Exactly one borrower, of the declared kind, and externals only
        where the client has switched them on."""
        for rec in self:
            if rec.borrower_type == 'partner':
                if not rec.partner_id:
                    raise ValidationError(
                        'An external loan needs a contact as its borrower.')
                if rec.employee_id:
                    raise ValidationError(
                        'A loan is to an employee or to a contact, not both.')
                if not rec.env.su and not loan_policy.allow_external_borrowers(rec.env):
                    raise ValidationError(
                        'Loans to external borrowers are switched off. Turn '
                        'them on in Loans > Configuration > Settings.')
            else:
                if not rec.employee_id:
                    raise ValidationError(
                        'An employee loan needs an employee as its borrower.')
                if rec.partner_id:
                    raise ValidationError(
                        'A loan is to an employee or to a contact, not both.')

    @api.constrains('payment_ids', 'amount', 'total_payable')
    def _check_not_overpaid(self):
        """Repayments must not exceed what is payable.

        Cross-record arithmetic, so it cannot be a CHECK -- the sum lives in a
        second table.
        """
        for rec in self:
            paid = sum(
                p.amount for p in rec.payment_ids if p.state == 'posted')
            owed = rec.total_payable or rec.amount or 0.0
            if owed and paid - owed > 0.01:
                raise ValidationError(
                    'Repayments on %s total %.2f, which is more than the '
                    '%.2f payable.' % (rec.name, paid, owed)
                )

    # ── Create ──────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'efs.loan') or 'New'
        return super().create(vals_list)

    # ── Workflow ────────────────────────────────────────────────────────────

    def write(self, vals):
        """Route a direct `state` write through the workflow that owns it.

        The status bar in the form is clickable, which is a genuine
        convenience -- but a click writes ``state`` and nothing else. Left
        alone that would step straight past everything the buttons do: the
        HR/administrator group checks, the rule that nobody approves what they
        endorsed, stamping ``endorsed_by`` / ``approved_by``, and computing the
        repayment start date. A loan could reach Active with no approver on
        record and no start date, which is the whole two-level approval
        quietly turned off by a mouse click.

        So the click is treated as a request to run the corresponding action,
        not as permission to set the field. Writes made *by* those actions
        carry ``STATE_WRITE_CTX`` and pass through untouched, which is what
        stops this from recursing.
        """
        if 'repayment_amount' in vals and not self.env.su \
                and not self.env.user.has_group(ENDORSER_GROUP) \
                and not self.env.user.has_group(APPROVER_GROUP):
            running = self.filtered(
                lambda loan: loan.state in ('active', 'cancel_requested'))
            if running:
                # Handbook s.5.3: faster repayment ON REQUEST -- the request
                # goes to HR, who makes the change. A borrower must not be
                # able to re-price their own running loan, in either
                # direction. Applications still in pending/endorsed stay
                # editable by their filer as before.
                raise AccessError(
                    'Changing the repayment of a running loan is an HR '
                    'action. Ask HR to raise the instalment (%s).'
                    % ', '.join(running.mapped('name')))
        if 'state' not in vals or self.env.context.get(STATE_WRITE_CTX):
            result = super().write(vals)
            if PAYSLIP_INPUT_FIELDS & vals.keys():
                self._refresh_open_payslips()
            return result

        target = vals['state']
        remainder = {k: v for k, v in vals.items() if k != 'state'}
        if remainder:
            super().write(remainder)
        for rec in self:
            rec._transition_to(target)
        # The transition's own _state_write already refreshed open payslips
        # for the state change; the remainder only needs one if it carried
        # payroll inputs of its own.
        if PAYSLIP_INPUT_FIELDS & remainder.keys():
            self._refresh_open_payslips()
        return True

    def unlink(self):
        """Delete takes the whole loan with it: the record and its ledger.

        A business decision, deliberately the opposite of cancellation.
        Cancel ends a real loan and keeps every record; delete is the wipe --
        `efs.loan.payment.loan_id` is `ondelete='cascade'`, so the repayment
        rows go with the loan. Open payslips are recomputed so the deduction
        vanishes from them too. What does NOT change is any confirmed
        payslip: its DED_LOAN line describes money that was actually paid out
        at a lower net, and rewriting a paid slip would falsify payroll. To
        undo a confirmed deduction, draft or cancel that payslip first --
        which reverses its repayments -- then delete the loan.

        The employees are captured before super() -- afterwards the records
        are gone and there is nothing left to map for the payslip refresh.
        """
        employees = self.mapped('employee_id')
        result = super().unlink()
        self._refresh_open_payslips(employees)
        return result

    def _refresh_open_payslips(self, employees=None):
        """Recompute draft/waiting payslips after a loan changes under them.

        A payslip's lines are a snapshot taken by Compute Sheet; nothing on
        the loan reaches into them by itself. So a loan that is approved,
        cancelled, deleted, re-dated or (de)authorized after a sheet was
        computed leaves that sheet showing a DED_LOAN figure that is no
        longer true. OCA payroll does recompute at confirmation -- unless
        `payroll.prevent_compute_on_confirm` is set -- but the clerk reading
        the open slip is looking at the stale figure either way.

        Only draft and waiting slips are touched: a done slip is history and
        has already posted its repayments. Elevated because the person acting
        on a loan (HR) is not necessarily allowed to write payslips; the
        recompute is a mechanical consequence of their lawful action, the
        same reasoning as `_post_loan_repayments`.
        """
        employees = self.mapped('employee_id') if employees is None else employees
        if not employees:
            return
        slips = self.env['hr.payslip'].sudo().search([
            ('employee_id', 'in', employees.ids),
            ('state', 'in', ('draft', 'verify')),
        ])
        if slips:
            slips.compute_sheet()

    def _transition_to(self, target):
        """Run the action a status-bar click is asking for."""
        self.ensure_one()
        if target == self.state:
            return
        if target == 'endorsed':
            return self.action_endorse()
        if target == 'active':
            return self.action_approve()
        if target == 'cancelled':
            return self.action_cancel()
        if target == 'pending':
            return self.action_reset_to_pending()
        if target == 'rejected':
            raise UserError(
                'Use the Reject button rather than the status bar: a '
                'rejection has to carry a reason, and that is what the '
                'borrower is told.'
            )
        if target == 'paid':
            raise UserError(
                '"Fully Paid" is not something to set by hand -- it follows '
                'from the repayments. %s still has %.2f outstanding. Record '
                'the remaining repayment and it will settle itself.'
                % (self.name, self.balance)
            )
        raise UserError('%s is not a status this loan can be moved to.' % target)

    def _reporting_endorser_user(self):
        """The user who must endorse this application, when the org chart
        names one: the applicant's DIRECT MANAGER (``parent_id`` on the
        employee), provided that manager has an active login.

        ``None`` means the org chart names nobody -- no manager set, a
        manager without a user account, or an external borrower -- and the
        HR endorser group handles it, exactly as before the reporting-line
        rule existed. An empty field must never strand an application.
        """
        self.ensure_one()
        if self.borrower_type != 'employee':
            return None
        manager = self.employee_id.sudo().parent_id
        user = manager.user_id if manager else None
        return user if user and user.active else None

    def action_endorse(self):
        """Level 1: the applicant's manager endorses; HR is the fallback.

        When the employee's Manager field names someone with a login, that
        person -- and only that person -- endorses the application, however
        many HR groups anyone else holds. The endorsement runs as sudo
        after the identity check, because a line manager holds no HR ACLs;
        ``endorsed_by`` still records the real person. With no manager on
        record, the HR endorser group acts as it always did. The
        administrator's approve-from-pending override remains the escape
        hatch for an absent manager.

        The chatter notes throughout this model use ``_message_log``, not
        ``message_post``. ``message_post`` resolves an author and raises
        ``UserError: Unable to send message, please configure the sender's
        email address`` when the acting user has no email -- which would make
        endorsing, approving or rejecting a loan impossible for such a user,
        and (via ``_sync_paid_state``) would crash a payslip confirmation for
        a payroll clerk without one. These are audit notes, not
        notifications, so the lighter primitive is also the correct one.
        """
        for rec in self:
            manager_user = rec._reporting_endorser_user()
            if manager_user and not self.env.su:
                if self.env.uid != manager_user.id:
                    raise AccessError(
                        'This application is endorsed by the employee\'s '
                        'manager, %s. Ask them to decide it - or an '
                        'administrator may approve it directly, which is '
                        'recorded as an override.' % manager_user.name)
                rec.sudo()._approval_endorse()
            else:
                rec._approval_endorse()
            rec._message_log(body='Endorsed to the administrator by %s.'
                                  % self.env.user.display_name)

    def action_approve(self):
        """Level 2: final approval, which also activates the loan.

        Accepts a pending loan as well as an endorsed one -- an administrator
        may act when HR is unavailable -- but records that as an override.
        Approving a loan that carries policy warnings is allowed; that is what
        `advise` mode is for, and the warnings stay on the record.
        """
        self._approval_assert_group(APPROVER_GROUP, 'approve')
        self._approval_assert_state(('pending', 'endorsed'), 'approved')
        self._approval_check_separation()
        # Section 5.2: repayment commences some days after the employee
        # receives the proceeds, not on the day of approval.
        for rec in self:
            note = ''
            if rec.policy_warnings:
                note = ' Approved despite policy issues: %s' % rec.policy_warnings
            rec._state_write(dict(
                rec._approval_values(),
                state='active',
                start_date=rec.start_date or rec._default_start_date(),
            ))
            rec._message_log(
                body='Approved by %s. Repayment starts %s.%s'
                     % (self.env.user.display_name, rec.start_date, note))
        return self._notify_authorization_outstanding()

    def action_open_reject_wizard(self):
        """Ask for a reason, then reject.

        The reason is not optional in practice -- it is what the borrower is
        told -- so rejection goes through a wizard rather than a bare button
        that would always leave the field empty.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Reject Loan Application',
            'res_model': 'efs.loan.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_loan_id': self.id},
        }

    def action_reject(self, reason=''):
        """Refuse at either level. Both end the application.

        Gated at the HR level, not the administrator one: refusing is
        something either level does, and a level 1 that could only advance a
        request and never stop one would be a rubber stamp. A pending
        application whose employee has a manager on record may equally be
        rejected by that manager -- deciding means both directions, or
        endorsement would be a rubber stamp of its own.
        """
        self._approval_assert_state(('pending', 'endorsed'), 'rejected')
        for rec in self:
            manager_user = rec._reporting_endorser_user()
            target = rec
            if (rec.state == 'pending' and manager_user
                    and self.env.uid == manager_user.id
                    and not self.env.su):
                target = rec.sudo()   # identity beats group for the manager
            else:
                rec._approval_assert_group(ENDORSER_GROUP, 'reject')
            target._state_write(target._approval_reject_values(reason))
            rec._message_log(
                body='Rejected by %s. Reason: %s'
                     % (self.env.user.display_name, reason or 'not given'))

    def _is_own_loan(self):
        """True when the acting user is the borrower.

        An employee is matched through their user; an external borrower
        through the contact's portal users, if any.
        """
        self.ensure_one()
        uid = self.env.uid
        if self.borrower_type == 'partner':
            return uid in self.partner_id.sudo().user_ids.ids
        user = self.employee_id.sudo().user_id
        return bool(user and user.id == uid)

    def _assert_may_act_on_own(self, verb):
        """Let the borrower act on their own loan; otherwise require HR.

        An employee has read and create on ``efs.loan`` but not write --
        granting write would let them re-price an approved loan. So the two
        things they are entitled to do to their own record are done here,
        elevated after an ownership check, rather than by widening the ACL.
        """
        self.ensure_one()
        if self.env.su or self.env.user.has_group(ENDORSER_GROUP):
            return
        if not self._is_own_loan():
            raise AccessError(
                'You may only %s your own loan application.' % verb)

    def action_cancel(self):
        """Withdraw a loan application that has not been approved yet.

        Instant, because nothing has happened: no money has moved and payroll
        is not deducting. Once a loan is running this refuses, and
        ``action_request_cancellation`` is the way through instead.
        """
        for rec in self:
            if rec.state not in ('pending', 'endorsed'):
                raise ValidationError(
                    'This loan is already running, so it cannot simply be '
                    'cancelled. Request a cancellation instead - HR and the '
                    'administrator both have to agree, the same as they did '
                    'to approve it.'
                )
            rec._assert_may_act_on_own('cancel')
            rec.sudo()._state_write({'state': 'cancelled'})
            rec.sudo()._message_log(
                body='Withdrawn by %s before approval.'
                     % self.env.user.display_name)

    # ── Cancelling a loan that is already running ───────────────────────────

    def action_open_cancel_wizard(self):
        """Ask why, then request cancellation. A reason is not optional."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Request Loan Cancellation',
            'res_model': 'efs.loan.cancel.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_loan_id': self.id},
        }

    def action_request_cancellation(self, reason=''):
        """Ask for a running loan to be closed. The step before the two levels.

        Deliberately does NOT stop the deduction. A request is not a decision,
        and halting payroll the moment somebody asks would let anyone stop
        their own repayments unilaterally -- ``_loan_deductions`` therefore
        treats this state exactly like `active`.
        """
        for rec in self:
            if rec.state != 'active':
                raise ValidationError(
                    'Only a loan that is currently being repaid can have its '
                    'cancellation requested.')
            rec._assert_may_act_on_own('request cancellation of')
            rec.sudo()._state_write({
                'state': 'cancel_requested',
                'cancel_reason': reason,
                'cancel_requested_by': self.env.uid,
                'cancel_requested_date': fields.Datetime.now(),
            })
            rec.sudo()._message_log(
                body='Cancellation requested by %s. Reason: %s. Deductions '
                     'continue until the request is approved.'
                     % (self.env.user.display_name, reason or 'not given'))

    def action_endorse_cancellation(self):
        """Level 1: HR endorses the cancellation."""
        self._approval_assert_group(
            ENDORSER_GROUP, 'endorse the cancellation of')
        for rec in self:
            if rec.state != 'cancel_requested':
                raise ValidationError(
                    'There is no cancellation request on this loan to endorse.')
            rec.write({
                'cancel_endorsed_by': self.env.uid,
                'cancel_endorsed_date': fields.Datetime.now(),
            })
            rec._message_log(body='Cancellation endorsed by %s.'
                                  % self.env.user.display_name)

    def action_approve_cancellation(self):
        """Level 2: the administrator closes the loan.

        Requires HR to have endorsed, and refuses whoever endorsed it -- the
        same separation of duties that governs approving a loan in the first
        place. Cancelling stops the payroll deduction and records what was
        still outstanding; it does not decide whether that balance is forgiven,
        which is a commercial decision made outside this module.
        """
        self._approval_assert_group(
            APPROVER_GROUP, 'approve the cancellation of')
        for rec in self:
            if rec.state != 'cancel_requested':
                raise ValidationError(
                    'There is no cancellation request on this loan to approve.')
            if not rec.cancel_endorsed_by:
                raise ValidationError(
                    'HR has not endorsed this cancellation yet. It needs both '
                    'levels, the same as the approval did.')
            if rec.cancel_endorsed_by.id == self.env.uid:
                raise ValidationError(
                    'You endorsed this cancellation, so it needs someone else '
                    'to approve it.')
            outstanding = rec.balance
            rec._state_write({
                'state': 'cancelled',
                'cancelled_balance': outstanding,
                'deduction_authorized': False,
                'authorized_date': False,
                'authorized_by': False,
            })
            rec._message_log(
                body='Cancellation approved by %s. Payroll will deduct nothing '
                     'further. Outstanding at cancellation: %.2f - repayments '
                     'already made are unchanged.'
                     % (self.env.user.display_name, outstanding))

    def action_refuse_cancellation(self):
        """Turn the request down and put the loan back to active."""
        self._approval_assert_group(
            ENDORSER_GROUP, 'refuse the cancellation of')
        for rec in self:
            if rec.state != 'cancel_requested':
                raise ValidationError(
                    'There is no cancellation request on this loan to refuse.')
            rec._state_write({
                'state': 'active',
                'cancel_endorsed_by': False,
                'cancel_endorsed_date': False,
            })
            rec._message_log(
                body='Cancellation request refused by %s. The loan continues.'
                     % self.env.user.display_name)

    def action_reset_to_pending(self):
        """Send a rejected or cancelled application back for another look.

        Only from an ended state, and only where nothing was ever repaid --
        reopening a loan that has repayments would put the ledger behind the
        lifecycle. Administrator only: undoing a refusal is overruling
        whoever made it.
        """
        self._approval_assert_group(APPROVER_GROUP, 'reopen')
        for rec in self:
            if rec.state not in ('rejected', 'cancelled'):
                raise ValidationError(
                    'Only a rejected or cancelled application can be reset.')
            if rec.payment_ids:
                raise ValidationError(
                    '%s already has repayments recorded against it, so it '
                    'cannot be reset.' % rec.name)
        self._state_write({
            'state': 'pending',
            'endorsed_by': False,
            'endorsed_date': False,
            'approved_by': False,
            'approved_date': False,
            'approval_override': False,
            'rejected_by': False,
            'rejected_date': False,
            'rejected_stage': False,
            'rejection_reason': False,
            'start_date': False,
        })

    # ── Salary deduction authorization ──────────────────────────────────────

    def action_record_authorization(self):
        """Record that the employee has signed the deduction authorization.

        Handbook section 6, and Labor Code article 113: no wage deduction may
        be made without the employee's written authorization. Until this is
        recorded the loan deducts nothing, however active it looks.

        This flags that the signed document exists; it is not the document.
        Whoever ticks it is recorded, so there is someone to ask.
        """
        self._approval_assert_group(ENDORSER_GROUP, 'record authorization for')
        for rec in self:
            if rec.deduction_authorized:
                continue
            if rec.borrower_type != 'employee':
                raise ValidationError(
                    'A Salary Deduction Authorization only applies to an '
                    'employee. %s is an external borrower and repays by hand.'
                    % rec._borrower_label())
            rec.write({
                'deduction_authorized': True,
                'authorized_date': fields.Datetime.now(),
                'authorized_by': self.env.uid,
            })
            rec._message_log(
                body='Salary Deduction Authorization recorded by %s. Payroll '
                     'will deduct from the next run.'
                     % self.env.user.display_name)

    def action_withdraw_authorization(self):
        """Withdraw the authorization; deductions stop from the next run.

        Administrator only, deliberately asymmetric with recording it: HR can
        start a lawful deduction, but stopping one already running changes what
        an employee is paid, which is the administrator's call.
        """
        self._approval_assert_group(APPROVER_GROUP, 'withdraw authorization on')
        for rec in self:
            if not rec.deduction_authorized:
                continue
            rec.write({
                'deduction_authorized': False,
                'authorized_date': False,
                'authorized_by': False,
            })
            rec._message_log(
                body='Salary Deduction Authorization withdrawn by %s. Nothing '
                     'further will be deducted for this loan.'
                     % self.env.user.display_name)

    def _notify_authorization_outstanding(self):
        """Warn on screen when an approved employee loan still cannot deduct.

        An active loan with no authorization is the one failure mode nobody
        notices: everything looks right and payroll quietly takes nothing.
        """
        blocked = self.filtered(
            lambda loan: loan.state == 'active'
            and loan.borrower_type == 'employee'
            and not loan.deduction_authorized)
        if not blocked:
            return True
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Deduction authorization outstanding',
                'message': (
                    '%s is active but has no Salary Deduction Authorization '
                    'on file, so payroll will deduct nothing. Record it from '
                    'the loan form.' % ', '.join(blocked.mapped('name'))),
                'type': 'warning',
                'sticky': True,
            },
        }

    # ── Repayment state ─────────────────────────────────────────────────────

    def _sync_paid_state(self):
        """Flip an active loan to `paid` once nothing is outstanding.

        Called after a repayment posts rather than run as a compute: `state` is
        user-editable and a compute would fight the approval buttons.
        """
        for rec in self:
            if rec.state in ('active', 'cancel_requested') and rec.balance <= 0.01:
                rec._state_write({'state': 'paid'})
                rec._message_log(body='Fully repaid.')
            elif rec.state == 'paid' and rec.balance > 0.01:
                rec._state_write({'state': 'active'})

    def action_open_payments(self):
        """The repayments stat button on the loan form."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Repayments',
            'res_model': 'efs.loan.payment',
            'view_mode': 'list,form',
            'domain': [('loan_id', '=', self.id)],
            'context': {'default_loan_id': self.id},
        }


class LoanScheduledCollection(models.Model):
    """Scheduled (no-payroll) collection, in its own inherit for legibility.

    The lending-business mode: loans whose repayment rule says
    ``collection = 'scheduled'`` are never touched by payroll. Instead a
    daily cron materialises each instalment that has fallen due as an
    UNPAID repayment dated its due date -- the reminder row in
    Loans > Repayments -- and a cashier marks it Paid when the money
    actually arrives. Only Paid rows reduce the balance, so a generated
    reminder is a to-collect note, never money.
    """
    _inherit = 'efs.loan'

    SCHED_REF = 'SCHED/%s/%s'   # loan reference, due date

    def _scheduled_due_dates(self, until):
        """Due dates from the start date through ``until``, per the
        rule's cadence. Bounded, so a mis-set start date cannot spin."""
        self.ensure_one()
        start = self.start_date
        if not start:
            return []
        cadence = self.repayment_rule_id.cadence or 'semimonth'
        dates = []

        def add_months(d, n):
            year = d.year + (d.month - 1 + n) // 12
            month = (d.month - 1 + n) % 12 + 1
            return date_type(year, month,
                             min(d.day, monthrange(year, month)[1]))

        if cadence == 'week':
            d = start
            while d <= until and len(dates) < 600:
                dates.append(d)
                d += timedelta(days=7)
        elif cadence == 'month':
            i = 0
            d = start
            while d <= until and len(dates) < 600:
                dates.append(d)
                i += 1
                d = add_months(start, i)
        else:   # semimonth: the 15th and the month-end, from the start on
            cursor = date_type(start.year, start.month, 1)
            while cursor <= until and len(dates) < 600:
                last = monthrange(cursor.year, cursor.month)[1]
                for day in (15, last):
                    d = date_type(cursor.year, cursor.month, day)
                    if start <= d <= until:
                        dates.append(d)
                cursor = add_months(cursor, 1)
        return dates

    @api.model
    def _cron_generate_scheduled_repayments(self):
        """Daily. Create the Unpaid reminder for every instalment that has
        fallen due, and prune reminders whose loan stopped collecting.

        Idempotent by reference (SCHED/<loan>/<date>): a due date is
        materialised once, whether the row is still Unpaid or was already
        marked Paid. Reminders stop when what is already on the calendar
        (Unpaid rows) covers the balance, and the final instalment
        snap-closes exactly like the payroll figure does.
        """
        today = fields.Date.context_today(self)
        Payment = self.env['efs.loan.payment'].sudo()

        # Reminders of loans that no longer collect are noise: the loan
        # was cancelled, deleted (cascade covers that), or paid off by
        # cash. Only generated rows are pruned - a row a human typed is
        # not ours to delete.
        stale = Payment.search([
            ('state', '=', 'draft'),
            ('reference', '=like', 'SCHED/%'),
        ]).filtered(lambda p: p.loan_id.state not in
                    ('active', 'cancel_requested')
                    or (p.loan_id.balance or 0.0) <= 0.005)
        if stale:
            stale.unlink()

        loans = self.search([
            ('state', 'in', ('active', 'cancel_requested')),
            ('repayment_rule_id.collection', '=', 'scheduled'),
            ('start_date', '!=', False),
        ])
        created = 0
        for loan in loans:
            per = loan.repayment_amount or 0.0
            balance = loan.balance or 0.0
            if per <= 0.005 or balance <= 0.005:
                continue
            mine = Payment.search([('loan_id', '=', loan.id),
                                   ('reference', '=like', 'SCHED/%')])
            seen = set(mine.mapped('reference'))
            # Unpaid rows already on the calendar still expect money, so
            # only the uncovered remainder generates new reminders.
            remaining = round(balance - sum(
                p.amount for p in mine if p.state == 'draft'), 2)
            for due in loan._scheduled_due_dates(today):
                if remaining <= 0.005:
                    break
                ref = self.SCHED_REF % (loan.name, due)
                if ref in seen:
                    continue
                amount = min(per, remaining)
                if 0.005 < remaining - amount < per - 0.005:
                    amount = remaining   # snap-close the tail
                Payment.create({
                    'loan_id': loan.id,
                    'date': due,
                    'amount': round(amount, 2),
                    'payment_method': 'scheduled',
                    'reference': ref,
                    'state': 'draft',
                })
                remaining = round(remaining - amount, 2)
                created += 1
        if created or stale:
            _logger.info(
                'Scheduled loan collection: %d reminder(s) created, %d '
                'stale pruned.', created, len(stale))


class LoanPayment(models.Model):
    _name = 'efs.loan.payment'
    _description = 'Loan Repayment'
    _order = 'date desc, id desc'

    loan_id = fields.Many2one(
        'efs.loan', string='Loan', required=True, ondelete='cascade',
        index=True)
    borrower_name = fields.Char(
        related='loan_id.borrower_name', string='Borrower', store=True,
        index=True)
    employee_id = fields.Many2one(
        related='loan_id.employee_id', string='Employee', store=True,
        index=True)
    partner_id = fields.Many2one(
        related='loan_id.partner_id', string='Contact', store=True,
        index=True)
    company_id = fields.Many2one(
        related='loan_id.company_id', string='Company', store=True, index=True)
    date = fields.Date(
        string='Payment Date', required=True, default=fields.Date.context_today,
        index=True)
    amount = fields.Monetary(string='Amount', required=True)
    currency_id = fields.Many2one(
        related='loan_id.currency_id', string='Currency', store=True,
        readonly=True)
    # How the money arrived. Generic on purpose: every lender needs to tell a
    # payroll deduction from a cash advance, and none of these name a foreign
    # model. The payroll bridge stamps `payroll`; a human picks the rest.
    #
    # Anything more specific -- WHICH payslip, WHICH receipt -- is a per-client
    # detail and does not live here. A client that wants a clickable payslip
    # adds an `x_` Many2one through the UI and names it in Settings; the
    # bridge fills it without this module ever learning it exists.
    payment_method = fields.Selection([
        ('payroll', 'Payroll Deduction'),
        ('scheduled', 'Scheduled'),
        ('cash', 'Cash'),
        ('bonus', 'Bonus / Incentive'),
        ('other', 'Other'),
    ], string='Method', default='cash', required=True, index=True,
        help='Payroll Deduction is set automatically when a payslip is '
             'confirmed; Scheduled is stamped by the daily job that '
             'materialises due instalments for no-payroll loans. Choose '
             'Cash, Bonus or Other when recording a repayment by hand.')
    reference = fields.Char(
        string='Reference', index=True,
        help='Free text. Payroll fills in the payslip number; for a manual '
             'repayment put a receipt number, or leave it empty. It is also '
             'what stops a re-confirmed payslip crediting the loan twice.')
    # The payslip this deduction came from, as a real link. Empty for a cash,
    # bonus or other repayment -- there is nothing to link. `reference` stays
    # beside it as the durable text stamp: `set null` here means deleting a
    # payslip clears the link but never the ledger row, and the idempotency
    # guard keys on the text, not the link.
    payslip_id = fields.Many2one(
        'hr.payslip', string='Payslip', index=True, ondelete='set null',
        readonly=True, copy=False,
        help='Filled by payroll when the payslip is confirmed. Click to open '
             'the payslip this repayment was deducted on.')
    state = fields.Selection([
        ('draft', 'Unpaid'),
        ('posted', 'Paid'),
    ], string='Status', default='posted', required=True, index=True,
        help='Only Paid repayments reduce the outstanding balance. An '
             'Unpaid row is a due instalment waiting to be collected - '
             'the scheduled reminders are born Unpaid.')

    def action_mark_paid(self):
        """The cashier's button: the money arrived, count it."""
        self.filtered(lambda p: p.state == 'draft').write(
            {'state': 'posted'})

    @api.depends('loan_id.name', 'date', 'amount', 'currency_id')
    def _compute_display_name(self):
        """"LOAN-00085 · 2026-05-30 · 6,428.57" rather than the raw id."""
        for rec in self:
            amount = rec.currency_id and rec.currency_id.format(rec.amount) \
                or '%.2f' % (rec.amount or 0.0)
            rec.display_name = ' · '.join(
                p for p in (rec.loan_id.name, str(rec.date or ''), amount) if p)

    # The loan's standing, shown on the repayment form so a row makes sense
    # on its own: what this instalment is against, and what is left after it.
    loan_amount = fields.Monetary(
        related='loan_id.amount', string='Principal', readonly=True)
    loan_total_paid = fields.Monetary(
        related='loan_id.total_paid', string='Total Repaid', readonly=True)
    loan_balance = fields.Monetary(
        related='loan_id.balance', string='Outstanding', readonly=True)
    loan_state = fields.Selection(
        related='loan_id.state', string='Loan Status', readonly=True)

    # Odoo 18 form. Odoo 19 replaces this list with one `models.Constraint`
    # attribute per rule; the tuple names below equal those attribute names
    # minus the underscore, so the database constraint names match on both.
    _sql_constraints = [
        ('amount_positive', 'CHECK(amount > 0)',
         'A repayment amount must be greater than zero.'),
    ]

    def action_open_loan(self):
        """The stat button on the repayment form."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'efs.loan',
            'res_id': self.loan_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.constrains('payment_method', 'loan_id')
    def _check_method_fits_borrower(self):
        """An external borrower has no payslip to be deducted from."""
        for rec in self:
            if rec.payment_method == 'payroll' \
                    and rec.loan_id.borrower_type == 'partner':
                raise ValidationError(
                    '%s is an external borrower; a repayment cannot be a '
                    'payroll deduction. Record it as Cash, Bonus or Other.'
                    % rec.loan_id._borrower_label())

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # `efs.loan._check_not_overpaid` is declared on `payment_ids`, but Odoo
        # only re-runs a constraint on the record whose field was written --
        # and creating a repayment writes `loan_id` on the CHILD, not
        # `payment_ids` on the parent. Without this call the overpayment guard
        # never fires for the path every repayment actually takes, which is
        # exactly how a loan ends up 104% repaid.
        records.loan_id._check_not_overpaid()
        records.loan_id._sync_paid_state()
        return records

    def write(self, vals):
        loans_before = self.loan_id
        result = super().write(vals)
        if {'amount', 'state', 'loan_id'} & set(vals):
            # Both sides: a repayment moved off a loan has to leave it
            # consistent, and the loan it moved onto has to be re-checked.
            affected = loans_before | self.loan_id
            affected._check_not_overpaid()
            affected._sync_paid_state()
        return result

    def unlink(self):
        loans = self.loan_id
        result = super().unlink()
        loans._sync_paid_state()
        return result
