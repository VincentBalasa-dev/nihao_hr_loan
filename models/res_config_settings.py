# -*- coding: utf-8 -*-
"""Lending policy, as a settings screen rather than technical parameters.

The figures were originally `ir.config_parameter` rows, which meant HR had to
be walked into Settings > Technical > System Parameters and told to edit a
key by hand. That is a developer's hiding place, not configuration.

Each field below is bound to the same parameter it always wrote, via
``config_parameter``, so nothing about how the rules read their values
changed -- only where a human sets them.

Every new rule defaults to **off**. A policy that switches itself on during an
upgrade would start refusing loan applications that were fine the day before,
and nobody would connect the two.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError

from . import loan_policy


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── What a broken rule does ─────────────────────────────────────────────
    loan_policy_enforcement = fields.Selection([
        ('enforce', 'Enforce - refuse the application'),
        ('advise', 'Advise - accept it, flag it for HR to decide'),
        ('off', 'Off - do not evaluate the rules'),
    ], string='Policy Enforcement',
        config_parameter=loan_policy.PARAM_ENFORCEMENT, default='enforce',
        help='The rules below stay where they are; this only changes their '
             'consequence. Advise mode records every rule an application '
             'breaks on the loan itself, so HR reviews rather than the '
             'system refusing.')

    # ── Eligibility ─────────────────────────────────────────────────────────
    loan_min_service_years = fields.Float(
        string='Minimum Service (years)',
        config_parameter=loan_policy.PARAM_MIN_SERVICE_YEARS,
        default=loan_policy.DEFAULT_MIN_SERVICE_YEARS,
        help='Continuous service required before an employee may borrow at '
             'all. Set to 0 to allow anyone to apply.')

    loan_max_active = fields.Integer(
        string='Maximum Concurrent Loans',
        config_parameter=loan_policy.PARAM_MAX_ACTIVE_LOANS,
        default=loan_policy.DEFAULT_MAX_ACTIVE_LOANS,
        help='How many loans one employee may have running at once. '
             '0 means no limit.')

    loan_require_settled = fields.Boolean(
        string='Settle Before Reborrowing',
        config_parameter=loan_policy.PARAM_REQUIRE_SETTLED,
        help='Refuse a new application while the employee still owes anything '
             'on an earlier loan.')

    loan_cooldown_days = fields.Integer(
        string='Cooling-off Period (days)',
        config_parameter=loan_policy.PARAM_COOLDOWN_DAYS,
        default=loan_policy.DEFAULT_COOLDOWN_DAYS,
        help='Days that must pass after settling a loan before the same '
             'employee may apply again. 0 means none.')

    # ── Amount ──────────────────────────────────────────────────────────────
    loan_ceiling_counts_debt = fields.Boolean(
        string='Ceiling Includes Existing Debt',
        config_parameter=loan_policy.PARAM_COUNT_EXISTING_DEBT,
        help='Measure the maximum loanable amount against what is still '
             'owed on unfinished loans plus the new request, rather than '
             'against the new request on its own. Repayments re-open room '
             'under the ceiling; pair with Minimum Room to Reborrow to '
             'decide when that room is enough to borrow against.')

    loan_reborrow_min_headroom = fields.Float(
        string='Minimum Room to Reborrow',
        config_parameter=loan_policy.PARAM_REBORROW_MIN_HEADROOM,
        default=loan_policy.DEFAULT_REBORROW_MIN_HEADROOM,
        help='An employee who still has unfinished loans may only file a '
             'new application once their open room under the ceiling '
             '(ceiling minus outstanding balances) reaches this figure. '
             'It limits when they may come back, never how much they must '
             'ask for. 0 disables it. Never applied to a first loan. '
             'Needs Ceiling Includes Existing Debt.')

    # ── Repayment ───────────────────────────────────────────────────────────
    # Deliberately absent. The repayment configuration -- instalment,
    # percent, start delay, repayment cap, and the deduction arithmetic
    # itself -- lives on the Repayment Rules catalogue (Loans >
    # Configuration > Repayment Rules), one record per deal, picked per
    # application. A single company-wide setting could not express two
    # clients on different deals.

    # ── External borrowers ──────────────────────────────────────────────────
    loan_allow_external = fields.Boolean(
        string='Allow External Borrowers',
        config_parameter=loan_policy.PARAM_ALLOW_EXTERNAL,
        help='Let a loan be made to a contact rather than an employee. The '
             'service, salary and coverage rules do not apply to them, and '
             'their repayments are recorded by hand.')
    loan_external_max_amount = fields.Float(
        string='Maximum for External Borrowers',
        config_parameter=loan_policy.PARAM_EXTERNAL_MAX,
        help='The only automatic limit on an external loan, since there is '
             'no salary to measure against. 0 means no limit - use Advise '
             'mode and review by hand.')

    # ── Coverage (handbook s.2) ─────────────────────────────────────────────
    loan_eligible_types = fields.Char(
        string='Eligible Employee Types',
        config_parameter=loan_policy.PARAM_ELIGIBLE_TYPES,
        help='Comma-separated employee types that may borrow: employee '
             '(regular), worker, student, trainee, contractor, freelance. '
             'Leave empty to allow every type. NihaoExpress: "employee".')

    loan_agency_discretion = fields.Boolean(
        string='Agency Staff by Management Discretion',
        config_parameter=loan_policy.PARAM_AGENCY_DISCRETION,
        help='Employees attached to a manpower agency may not apply '
             'themselves; only an HR administrator can file a loan for them. '
             'Needs the agency field from hr_api_odoo; ignored without it.')

    # ── Read-only summary, so the screen is not just a form of blanks ───────
    loan_tier_count = fields.Integer(
        string='Eligibility Bands', compute='_compute_loan_summary')
    loan_product_count = fields.Integer(
        string='Loan Products', compute='_compute_loan_summary')

    # ── Payroll integration ─────────────────────────────────────────────────
    loan_payroll_ready = fields.Boolean(
        compute='_compute_loan_payroll_status')
    loan_payroll_status = fields.Char(
        compute='_compute_loan_payroll_status')

    @api.depends('company_id')
    @api.depends_context('company')
    def _compute_loan_payroll_status(self):
        """Whether payslips can actually deduct loans, in one line.

        Ready means the DED_LOAN salary rule exists and sits on at least
        one salary structure; anything less and payroll-collection rules
        silently deduct nothing.
        """
        ready = self.env[
            'efs.loan.repayment.rule']._payroll_collection_ready()
        status = 'Not set up - payslips will not deduct loans.'
        if ready:
            rule = self.env['hr.salary.rule'].sudo().search(
                [('code', '=', 'DED_LOAN')], limit=1)
            structures = self.env['hr.payroll.structure'].sudo().search(
                [('rule_ids', 'in', rule.ids)])
            status = 'DED_LOAN is on: %s' % ', '.join(
                structures.mapped('name'))
        for rec in self:
            rec.loan_payroll_ready = ready
            rec.loan_payroll_status = status

    def action_setup_loan_payroll(self):
        """One press: create/adopt DED_LOAN and attach it to the live
        salary structures. Idempotent - pressing it again is free."""
        if 'hr.salary.rule' not in self.env:
            raise UserError(
                'The payroll app is not installed, so there is nothing to '
                'wire the loan deduction into. Scheduled collection works '
                'without it.')
        rule = self.env['hr.salary.rule'].sudo()._nihao_setup_loan_rule()
        structures = self.env['hr.payroll.structure'].sudo().search(
            [('rule_ids', 'in', rule.ids)])
        if structures:
            message = ('Payroll deduction is set up: DED_LOAN is on %s.'
                       % ', '.join(structures.mapped('name')))
            kind = 'success'
        else:
            message = ('DED_LOAN was created, but no salary structure '
                       'carries deductions yet - add the rule to the '
                       'structure your payslips use.')
            kind = 'warning'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Payroll Integration',
                'message': message,
                'type': kind,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    # Depends on a real field, not only on context. The settings form is a
    # brand-new transient record loaded through `onchange`, and onchange only
    # computes fields whose *field* dependencies it just set -- a compute with
    # no field dependency is never triggered and the form shows the default
    # 0, however many bands exist. `company_id` is in every settings
    # onchange, so hanging the counters off it makes them compute on load.
    @api.depends('company_id')
    @api.depends_context('company')
    def _compute_loan_summary(self):
        tiers = self.env['efs.loan.eligibility.tier'].sudo().search_count([])
        products = self.env['efs.loan.type'].sudo().search_count([])
        for rec in self:
            rec.loan_tier_count = tiers
            rec.loan_product_count = products

    def get_values(self):
        """Also hand the counters to the form's defaults.

        `_compute_loan_summary` covers the browser, which always sends
        `company_id`. This covers anything that loads the settings without
        it -- a client's custom view, an RPC caller, a future Odoo that
        drops the field from the base arch -- so the counts are never a
        misleading 0 whatever the caller asked for.
        """
        values = super().get_values()
        values.update({
            'loan_tier_count':
                self.env['efs.loan.eligibility.tier'].sudo().search_count([]),
            'loan_product_count':
                self.env['efs.loan.type'].sudo().search_count([]),
        })
        return values

    def action_open_loan_tiers(self):
        return self.env['ir.actions.act_window']._for_xml_id(
            'nihao_hr_loan.action_loan_eligibility_tier')

    def action_open_loan_products(self):
        return self.env['ir.actions.act_window']._for_xml_id(
            'nihao_hr_loan.action_efs_loan_type')
