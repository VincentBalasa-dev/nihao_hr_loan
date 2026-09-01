# -*- coding: utf-8 -*-
"""Lending policy, read from configuration.

Every figure the loan rules turn on is policy, not a property of the software:
how long someone must have worked here, how much they may borrow, how many
loans they may run at once, how fast they repay. A policy number that only
exists in Python is a policy number nobody can change without a deploy.

All of it is set in **Loans > Configuration > Settings**, which writes
``ir.config_parameter`` underneath. The loanable ceiling is the exception: its
bands are records (``efs.loan.eligibility.tier``), because the *number* of
bands is itself a policy choice and does not fit in one field.

The constants below are fallbacks used only when a setting is missing or
unreadable. They are chosen to fail **closed** — a broken configuration
refuses a loan rather than granting an unlimited one.

Plain functions rather than model methods so the loan model, the employee
model and the payroll bridge can all reach them without importing each other.
"""

import logging
from calendar import monthrange
from datetime import date

_logger = logging.getLogger(__name__)

# ── Eligibility ──────────────────────────────────────────────────────────────

# Continuous service required before anyone may borrow at all.
PARAM_MIN_SERVICE_YEARS = 'nihao_hr_loan.min_service_years'
DEFAULT_MIN_SERVICE_YEARS = 1.0

# How many loans one employee may have running at once. 0 = no limit.
PARAM_MAX_ACTIVE_LOANS = 'nihao_hr_loan.max_active_loans'
DEFAULT_MAX_ACTIVE_LOANS = 0

# Refuse a new application while any earlier loan is still unpaid.
PARAM_REQUIRE_SETTLED = 'nihao_hr_loan.require_previous_settled'
DEFAULT_REQUIRE_SETTLED = False

# Days that must pass after settling a loan before reapplying. 0 = none.
PARAM_COOLDOWN_DAYS = 'nihao_hr_loan.cooldown_days'
DEFAULT_COOLDOWN_DAYS = 0

# ── Amount ───────────────────────────────────────────────────────────────────

# Measure the ceiling against what the employee already owes plus the new
# request, rather than against the new request alone.
PARAM_COUNT_EXISTING_DEBT = 'nihao_hr_loan.ceiling_counts_existing_debt'
DEFAULT_COUNT_EXISTING_DEBT = False

# ── Repayment ────────────────────────────────────────────────────────────────

# The standard weekly instalment offered on a new application.
PARAM_WEEKLY_REPAYMENT = 'nihao_hr_loan.weekly_repayment'
DEFAULT_WEEKLY_REPAYMENT = 1000.0

# Days after approval before the first deduction is taken.
PARAM_START_DELAY_DAYS = 'nihao_hr_loan.repayment_start_delay_days'
DEFAULT_START_DELAY_DAYS = 14

# Ceiling on total loan repayments as a percentage of monthly basic salary,
# across every active loan. 0 = no limit. Guards against someone being
# approved into a wage they cannot live on.
PARAM_MAX_REPAYMENT_PCT = 'nihao_hr_loan.max_repayment_percent'
DEFAULT_MAX_REPAYMENT_PCT = 0.0

# 365.25 / 7 / 12. Used only to express a weekly figure per month for display;
# the deduction itself counts real days, so rounding here never compounds.
WEEKS_PER_MONTH = 4.348214285714286


def _raw(env, key, default):
    value = env['ir.config_parameter'].sudo().get_param(key)
    return default if value in (None, False, '') else value


def _number(env, key, default, cast=float, minimum=0.0):
    """A numeric setting, or the default when it is missing or unreadable."""
    raw = _raw(env, key, default)
    try:
        value = cast(float(raw))
    except (TypeError, ValueError):
        _logger.warning(
            'Loan setting %s is %r, which is not a number. Falling back to '
            '%r.', key, raw, default)
        return default
    return max(value, minimum) if minimum is not None else value


def _flag(env, key, default):
    raw = _raw(env, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ('1', 'true', 'yes', 't')


# ── Accessors ────────────────────────────────────────────────────────────────

def min_service_years(env):
    return _number(env, PARAM_MIN_SERVICE_YEARS, DEFAULT_MIN_SERVICE_YEARS)


def max_active_loans(env):
    return _number(env, PARAM_MAX_ACTIVE_LOANS, DEFAULT_MAX_ACTIVE_LOANS, int)


def require_previous_settled(env):
    return _flag(env, PARAM_REQUIRE_SETTLED, DEFAULT_REQUIRE_SETTLED)


def cooldown_days(env):
    return _number(env, PARAM_COOLDOWN_DAYS, DEFAULT_COOLDOWN_DAYS, int)


def ceiling_counts_existing_debt(env):
    return _flag(env, PARAM_COUNT_EXISTING_DEBT, DEFAULT_COUNT_EXISTING_DEBT)


def weekly_repayment(env):
    value = _number(env, PARAM_WEEKLY_REPAYMENT, DEFAULT_WEEKLY_REPAYMENT)
    # A zero weekly repayment would mean a loan that never repays, and the
    # field is required to be positive anyway.
    return value if value > 0 else DEFAULT_WEEKLY_REPAYMENT


def start_delay_days(env):
    return _number(env, PARAM_START_DELAY_DAYS, DEFAULT_START_DELAY_DAYS, int)


def max_repayment_percent(env):
    return _number(env, PARAM_MAX_REPAYMENT_PCT, DEFAULT_MAX_REPAYMENT_PCT)


def ceiling_multiple(env, service_years, company=None):
    """The multiple of monthly salary allowed at this seniority.

    Zero below the lowest band, which is what makes someone short of the
    minimum service period ineligible for any amount rather than for an
    unlimited one. With no bands configured at all the answer is also zero --
    a ceiling that fails open is not a ceiling.
    """
    return env['efs.loan.eligibility.tier']._multiple_for(
        service_years, company=company)




# ── Coverage ─────────────────────────────────────────────────────────────────

# Handbook s.2: "all regular employees ... Employees under manpower agency may
# be accommodated by management discretion." Two settings, both OFF by default
# so no other client inherits this policy by accident.

# Comma-separated hr.employee.employee_type keys that may borrow. Empty = all.
# Odoo 18's keys: employee (regular), worker, student, trainee, contractor,
# freelance. NihaoExpress sets "employee".
PARAM_ELIGIBLE_TYPES = 'nihao_hr_loan.eligible_employee_types'

# When on, an employee attached to a manpower agency is refused unless an HR
# administrator files the application -- that is what "management discretion"
# means in practice. Read through `_fields` because `agency_id` comes from
# hr_api_odoo, which this module must not depend on.
PARAM_AGENCY_DISCRETION = 'nihao_hr_loan.agency_by_discretion'


def eligible_employee_types(env):
    """Allowed employee_type keys, or an empty set meaning no restriction."""
    raw = _raw(env, PARAM_ELIGIBLE_TYPES, '') or ''
    return {t.strip() for t in str(raw).split(',') if t.strip()}


def agency_by_discretion(env):
    return _flag(env, PARAM_AGENCY_DISCRETION, False)


# ── Enforcement ──────────────────────────────────────────────────────────────

# What a broken rule DOES. `enforce` refuses the application (the default and
# the only behaviour before this setting existed). `advise` accepts it but
# records every rule it fails on the loan, so HR sees them and decides. `off`
# does not evaluate the rules at all. The rules themselves live in the same
# Settings screen either way; this only changes their consequence.
PARAM_ENFORCEMENT = 'nihao_hr_loan.policy_enforcement'
ENFORCEMENT_MODES = ('enforce', 'advise', 'off')


def policy_enforcement(env):
    raw = str(_raw(env, PARAM_ENFORCEMENT, 'enforce') or '').strip().lower()
    return raw if raw in ENFORCEMENT_MODES else 'enforce'


# ── Repayment basis and period ───────────────────────────────────────────────

# Basis: `fixed` -- a peso figure per period (NihaoExpress: P1,000).
#        `percent` -- a share of the PRINCIPAL per period (10% -> ~10 periods).
# Period: `week` / `semimonth` / `month`. The deduction is prorated by the
# days a payslip actually covers, so a setting means the same thing on any
# payroll schedule. Both are company defaults; a loan product may override
# them; the figure itself is stored on each loan.
PARAM_REPAYMENT_BASIS = 'nihao_hr_loan.repayment_basis'
PARAM_REPAYMENT_PERIOD = 'nihao_hr_loan.repayment_period'
PARAM_REPAYMENT_AMOUNT = 'nihao_hr_loan.repayment_amount'
PARAM_REPAYMENT_PERCENT = 'nihao_hr_loan.repayment_percent'
DEFAULT_REPAYMENT_PERCENT = 10.0

BASES = ('fixed', 'percent')
# 'payslip' is the flat option: the instalment comes out of every payslip
# whole, whatever span of days the slip covers -- no pro-rating. Its
# schedule (how many instalments have fallen due by a given date) and its
# calendar-equivalent maths (term, interest, the monthly capacity check)
# follow the CUTOFF CADENCE setting below, because a flat per-payslip
# figure has no calendar of its own -- the payroll's cutoff calendar is
# its calendar.
PERIODS = ('week', 'semimonth', 'month', 'payslip')
# 'payslip' has no row here: its day-length and per-month figures follow the
# cutoff cadence setting -- see Loan._period_days / _periods_per_month.
PERIOD_DAYS = {'week': 7.0, 'semimonth': 365.25 / 24, 'month': 365.25 / 12}
PERIODS_PER_MONTH = {'week': WEEKS_PER_MONTH, 'semimonth': 2.0, 'month': 1.0}
PERIOD_LABELS = {'week': 'Weekly', 'semimonth': 'Semi-monthly',
                 'month': 'Monthly', 'payslip': 'Per Payslip (flat)'}


def repayment_basis(env):
    raw = str(_raw(env, PARAM_REPAYMENT_BASIS, 'fixed') or '').strip().lower()
    return raw if raw in BASES else 'fixed'


def repayment_period(env):
    raw = str(_raw(env, PARAM_REPAYMENT_PERIOD, 'week') or '').strip().lower()
    return raw if raw in PERIODS else 'week'


# ── The payroll's cutoff calendar, for flat (per-payslip) loans ──────────────
# One flat instalment falls due per CUTOFF. What a cutoff is -- a half-month
# (paydays on the 15th and month-end), a week, a month -- is the company's
# payroll calendar, not something a loan can know on its own.
PARAM_PAYSLIP_CADENCE = 'nihao_hr_loan.payslip_cadence'
CADENCES = ('week', 'semimonth', 'month')
DEFAULT_PAYSLIP_CADENCE = 'semimonth'


def payslip_cadence(env):
    raw = str(_raw(env, PARAM_PAYSLIP_CADENCE,
                   DEFAULT_PAYSLIP_CADENCE) or '').strip().lower()
    return raw if raw in CADENCES else DEFAULT_PAYSLIP_CADENCE


def cutoff_count(start, end, cadence):
    """How many cutoffs END inside [start, end], inclusive.

    This is the flat loan's schedule: one instalment falls due each time a
    cutoff closes. Semi-monthly cutoffs close on the 15th and the last day
    of each month, monthly ones on the last day; weekly ones are counted
    arithmetically (a closing day per 7 days from the start). A slip
    covering Sep 1-30 against a loan started Sep 15 therefore owes two
    semi-monthly instalments -- the 9/15 and 9/30 closes -- not the three
    week-marks the old weekly-only clock counted.
    """
    if end < start:
        return 0
    if cadence == 'week':
        return (end - start).days // 7 + 1
    count = 0
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        last_day = date(cursor.year, cursor.month,
                        monthrange(cursor.year, cursor.month)[1])
        closes = [last_day] if cadence == 'month' else [
            date(cursor.year, cursor.month, 15), last_day]
        for close in closes:
            if start <= close <= end:
                count += 1
        cursor = date(cursor.year + (cursor.month == 12),
                      cursor.month % 12 + 1, 1)
    return count


def repayment_amount(env):
    """The default fixed instalment. Falls back to the pre-1.1 key."""
    value = _number(env, PARAM_REPAYMENT_AMOUNT, 0.0)
    if value <= 0:
        value = _number(env, PARAM_WEEKLY_REPAYMENT, DEFAULT_WEEKLY_REPAYMENT)
    return value if value > 0 else DEFAULT_WEEKLY_REPAYMENT


def repayment_percent(env):
    value = _number(env, PARAM_REPAYMENT_PERCENT, DEFAULT_REPAYMENT_PERCENT)
    return value if value > 0 else DEFAULT_REPAYMENT_PERCENT


# ── External borrowers ───────────────────────────────────────────────────────

# Off by default: a lender to its own staff has no business accepting an
# application from a contact. Turned on, a loan may point at a res.partner
# instead of an employee. Employee-only rules (service, salary ceiling,
# coverage, payroll deduction) do not apply; the only automatic limit is the
# flat maximum below, and 0 means none -- use `advise` mode for manual review.
PARAM_ALLOW_EXTERNAL = 'nihao_hr_loan.allow_external_borrowers'
PARAM_EXTERNAL_MAX = 'nihao_hr_loan.external_max_amount'


def allow_external_borrowers(env):
    return _flag(env, PARAM_ALLOW_EXTERNAL, False)


def external_max_amount(env):
    return _number(env, PARAM_EXTERNAL_MAX, 0.0)
