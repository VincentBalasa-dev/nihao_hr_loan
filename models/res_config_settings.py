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
        help='Measure the maximum loanable amount against what the employee '
             'already owes plus the new request, rather than against the new '
             'request on its own.')

    # ── Repayment ───────────────────────────────────────────────────────────
    loan_repayment_basis = fields.Selection([
        ('fixed', 'Fixed amount per period'),
        ('percent', 'Percent of principal per period'),
    ], string='Repayment Basis',
        config_parameter=loan_policy.PARAM_REPAYMENT_BASIS, default='fixed',
        help='Fixed: a set figure each period. Percent: a share of the '
             'principal each period, so 10% repays in about ten periods. A '
             'loan product may override this.')
    loan_repayment_period = fields.Selection([
        ('week', 'Weekly'),
        ('semimonth', 'Semi-monthly'),
        ('month', 'Monthly'),
        ('payslip', 'Per Payslip (flat)'),
    ], string='Repayment Period',
        config_parameter=loan_policy.PARAM_REPAYMENT_PERIOD, default='week',
        help='How often an instalment falls due. Weekly / semi-monthly / '
             'monthly are prorated by the days each payslip covers, so they '
             'mean the same thing on any payroll schedule. Per Payslip is '
             'flat: the whole instalment comes out of every payslip, '
             'whatever its length. A loan product may override this.')
    loan_payslip_cadence = fields.Selection([
        ('semimonth', 'Semi-monthly (15th and month-end)'),
        ('week', 'Weekly'),
        ('month', 'Monthly (month-end)'),
    ], string='Cutoff Cadence',
        config_parameter=loan_policy.PARAM_PAYSLIP_CADENCE,
        default=loan_policy.DEFAULT_PAYSLIP_CADENCE,
        help='How many payslips a month holds, for the monthly-equivalent '
             'maths on Per Payslip (flat) loans: the capacity check and the '
             'term/interest figures. The deduction itself is always one '
             'instalment per payslip, whatever the cadence.')
    loan_flat_deduct_on = fields.Selection([
        ('always', 'Every payslip'),
        ('fifteenth', 'Only payslips covering the 15th'),
        ('month_end', 'Only payslips covering month-end'),
        ('either', 'Payslips covering the 15th or month-end'),
    ], string='Deduct On',
        config_parameter=loan_policy.PARAM_FLAT_DEDUCT_ON, default='always',
        help='When a flat instalment is taken. "Every payslip" deducts on '
             'each computed slip; the others deduct only on a slip whose '
             'period covers that day of the month - for clients whose loans '
             'are paid once a month or on one specific cutoff. The formula '
             'below can still override the figure either way.')
    # Char, not Text: res.config.settings only round-trips boolean / number /
    # char / selection / many2one fields through config_parameter, and a
    # Text here crashes the whole Settings page at default_get. Char holds
    # a multi-line formula fine; the ace widget edits it.
    loan_flat_formula = fields.Char(
        string='Flat Deduction Formula',
        config_parameter=loan_policy.PARAM_FLAT_FORMULA,
        help='Optional salary-rule-style Python overriding what a payslip '
             'deducts for flat loans. Assign to `result`; available: '
             '`result` (the built-in figure), `per` (the instalment), '
             '`balance` (total outstanding across flat loans), `loans`, '
             '`employee`, `date_from`, `date_to`. Example:\n'
             'result = min(per * 2, balance)\n'
             'Leave empty for the standard rule (one instalment per '
             'payslip; a closing balance under two instalments is taken in '
             'full). The pay-availability cap and whole-instalment floor '
             'still apply after the formula. A formula that raises is '
             'logged and ignored so payroll never breaks on a typo.')
    loan_repayment_amount = fields.Float(
        string='Default Repayment per Period',
        config_parameter=loan_policy.PARAM_REPAYMENT_AMOUNT,
        default=loan_policy.DEFAULT_WEEKLY_REPAYMENT,
        help='On a fixed basis: offered on a new application, and editable '
             'per loan.')
    loan_repayment_percent = fields.Float(
        string='Default Percent per Period',
        config_parameter=loan_policy.PARAM_REPAYMENT_PERCENT,
        default=loan_policy.DEFAULT_REPAYMENT_PERCENT,
        help='On a percent basis: the share of the principal repaid each '
             'period. Editable per loan.')

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

    loan_start_delay_days = fields.Integer(
        string='Repayment Starts After (days)',
        config_parameter=loan_policy.PARAM_START_DELAY_DAYS,
        default=loan_policy.DEFAULT_START_DELAY_DAYS,
        help='Days between approval and the first deduction, so the employee '
             'has the proceeds before anything is taken back.')

    loan_max_repayment_percent = fields.Float(
        string='Maximum Repayment (% of salary)',
        config_parameter=loan_policy.PARAM_MAX_REPAYMENT_PCT,
        default=loan_policy.DEFAULT_MAX_REPAYMENT_PCT,
        help='Cap on total loan repayments across every active loan, as a '
             'percentage of monthly basic salary. 0 means no limit. Guards '
             'against approving someone into a wage they cannot live on.')

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
