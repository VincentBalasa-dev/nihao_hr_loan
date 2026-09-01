# -*- coding: utf-8 -*-
"""Loan standing on the employee record, and the deduction payroll asks for.

Everything a manager needs to answer "can this person borrow, and what do they
already owe" without loading every loan row -- plus ``_loan_deductions``, the
one method that decides what comes out of a payslip.

That method lives here rather than in the payroll bridge on purpose: it is loan
logic, not payroll logic, and it has to work whether or not a payroll app is
installed. The bridge only turns its answer into a payslip line.
"""

import logging

from odoo import api, fields, models

from . import loan_policy

_logger = logging.getLogger(__name__)


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    loan_ids = fields.One2many('efs.loan', 'employee_id', string='Loans')
    loan_count = fields.Integer(
        compute='_compute_loan_standing', string='# Loans')

    loan_active_count = fields.Integer(
        compute='_compute_loan_standing', string='Ongoing Loans',
        help='Loans currently being repaid. Excludes pending applications and '
             'settled loans.')
    loan_balance = fields.Monetary(
        compute='_compute_loan_standing', string='Current Debt',
        currency_field='loan_currency_id',
        help='Total still owed across every ongoing loan.')
    loan_cutoff_deduction = fields.Monetary(
        compute='_compute_loan_standing', string='Deduction per Cutoff',
        currency_field='loan_currency_id',
        help='What payroll takes from a one-week period across all ongoing '
             'loans. Capped at the outstanding balance, so a final instalment '
             'never overshoots. A real payslip is measured over its own dates.')
    loan_unauthorized_count = fields.Integer(
        compute='_compute_loan_standing', string='Awaiting Authorization',
        help='Active loans with no Salary Deduction Authorization on file. '
             'Payroll deducts nothing for these.')

    loan_eligible = fields.Boolean(
        compute='_compute_loan_standing', string='Eligible for a Loan',
        help='Handbook section 3: the minimum period of continuous service.')
    loan_max_amount = fields.Monetary(
        compute='_compute_loan_standing', string='Maximum Loanable',
        currency_field='loan_currency_id',
        help='Handbook section 4, as a multiple of monthly basic salary by '
             'length of service. Management may still reduce or deny.')
    loan_service_years = fields.Float(
        compute='_compute_loan_standing', string='Years of Service')

    loan_currency_id = fields.Many2one(
        'res.currency', compute='_compute_loan_standing')

    # A hire date the loan rules can rely on. Optional: left empty, the
    # fallbacks below are used instead. It exists because `first_contract_date`
    # is a compute over hr.contract that is wrong for anyone rehired, and
    # because a company that tracks continuous service differently needs
    # somewhere to say so.
    loan_service_date = fields.Date(
        string='Loan Service Start',
        help='Start of continuous service for loan eligibility. Leave empty '
             'to use the first contract start date, and failing that the date '
             'the employee record was created.')

    # ── Service and salary ──────────────────────────────────────────────────

    def _loan_service_start(self):
        """The date continuous service is measured from.

        Three sources, in order of how much they are trusted:

        1. ``loan_service_date``, if somebody set it. An explicit answer beats
           a derived one.
        2. ``first_contract_date``, from ``hr_contract``. Looked up through
           ``_fields`` rather than read directly so a version that renames or
           drops it degrades to the next fallback instead of raising.
        3. ``create_date``. A poor proxy, but it is a real date and it fails
           safe: a record created today reads as zero years of service, which
           denies a loan rather than granting one.
        """
        self.ensure_one()
        if self.loan_service_date:
            return self.loan_service_date
        # sudo(): first_contract_date is a manager-only compute over hr.contract,
        # which an HR officer without the Contracts group cannot read.
        if 'first_contract_date' in self._fields and self.sudo().first_contract_date:
            return self.sudo().first_contract_date
        if self.create_date:
            return fields.Date.to_date(self.create_date)
        return False

    def _loan_monthly_wage(self):
        """Monthly basic salary, wherever this Odoo version keeps it.

        Odoo 18: on the running contract, ``employee.contract_id.wage`` (module
        ``hr_contract``, hence the manifest dependency).
        Odoo 19: contracts became ``hr.version`` and ``wage`` is delegated onto
        ``hr.employee`` directly. The getattr keeps this one method correct on
        both, so the loan ceiling never silently computes against zero after a
        version move -- which would waive the handbook's section 4 limit.
        """
        self.ensure_one()
        # sudo(): hr.contract is readable only by the Contracts groups; an HR
        # officer without them must still see the ceiling on the employee form.
        contract = getattr(self.sudo(), 'contract_id', None)
        if contract and contract.wage:
            return float(contract.wage)
        return float(getattr(self, 'wage', 0.0) or 0.0)

    @api.depends('loan_ids.state', 'loan_ids.balance',
                 'loan_ids.repayment_amount', 'loan_ids.repayment_period',
                 'loan_ids.deduction_authorized',
                 'loan_service_date', 'contract_id.wage')
    def _compute_loan_standing(self):
        """Loan standing for one employee, per the employee handbook.

        Section 3 sets a minimum service requirement; section 4 caps the
        principal at a multiple of monthly basic salary that rises with length
        of service. Both are computed here rather than only checked at
        application time so a manager can see the limit on the employee form
        *before* anyone applies.

        This is what an employee *may* apply for. Section 4 also reserves the
        right to approve, reduce or deny on financial capacity and employee
        record, so nothing here is an entitlement.
        """
        today = fields.Date.context_today(self)
        minimum = loan_policy.min_service_years(self.env)
        for rec in self:
            # `cancel_requested` counts as ongoing: the loan is still being
            # deducted until somebody approves the cancellation.
            ongoing = ('active', 'cancel_requested')
            active = rec.loan_ids.filtered(lambda loan: loan.state in ongoing)
            rec.loan_count = len(rec.loan_ids)
            rec.loan_active_count = len(active)
            rec.loan_balance = round(sum(active.mapped('balance')), 2)
            rec.loan_unauthorized_count = len(
                active.filtered(lambda loan: not loan.deduction_authorized))
            rec.loan_currency_id = (
                rec.company_id.currency_id or self.env.company.currency_id)
            # A one-week figure, for display. A payslip is measured over its
            # own dates -- see _loan_deductions.
            rec.loan_cutoff_deduction = rec._loan_deduction_total()

            start = rec._loan_service_start()
            years = ((today - start).days / 365.25) if start else 0.0
            rec.loan_service_years = round(years, 2)
            rec.loan_eligible = years >= minimum

            multiple = loan_policy.ceiling_multiple(self.env, years)
            rec.loan_max_amount = round(rec._loan_monthly_wage() * multiple, 2)

    # ── What payroll deducts ────────────────────────────────────────────────

    def _loan_deductions(self, date_from=None, date_to=None):
        """Active loans and what each gives up for one pay period.

        Returns a list of ``(loan, amount)``, oldest first, so payroll deducts
        and the repayment allocator credits in the same order.

        The handbook (section 5.1) sets repayment as a flat weekly figure, so
        the amount owed for a period is that figure times the weeks the period
        actually spans. Counting real days rather than assuming a fixed month
        keeps a 31-day cutoff slightly larger than a 30-day one and makes a
        year total exactly 52.18 weeks -- which is what "PHP 1,000 per week"
        means. Given no period, one week is assumed.

        Three things stop a deduction entirely:

        * **No written authorization** (section 6, Labor Code article 113).
          A wage deduction without it is unlawful, so an unauthorised loan
          deducts nothing however active it looks. Deliberately silent --
          payroll should still run, the loan simply does not participate. The
          `loan_unauthorized_count` field above is what makes this visible.
        * **Before the start date** (section 5.2). Repayment commences some
          days after the proceeds are received.
        * **Nothing left owing.** The amount is capped at the balance, because
          a final instalment usually exceeds what remains and deducting it in
          full would push the balance negative.
        """
        self.ensure_one()
        # `self.id` is a NewId on an unsaved form, which Postgres cannot be
        # asked about. An employee nobody has saved yet has no loans, so the
        # honest answer is an empty list rather than a traceback in the middle
        # of typing a new employee.
        employee_id = self._origin.id
        if not employee_id:
            return []
        loans = self.env['efs.loan'].sudo().search(
            [('employee_id', '=', employee_id),
             #  deducts exactly like : a request is
             # not a decision, and stopping payroll the moment somebody asks
             # would let anyone halt their own repayments unilaterally.
             ('state', 'in', ('active', 'cancel_requested'))],
            order='start_date asc, id asc',
        )

        period_from = fields.Date.to_date(date_from) if date_from else None
        period_to = fields.Date.to_date(date_to) if date_to else None
        days = None
        if period_from and period_to:
            days = max((period_to - period_from).days + 1, 0)

        result = []
        flat_loans = []
        for loan in loans:
            if not loan.deduction_authorized:
                continue
            if loan.start_date and period_to \
                    and loan.start_date > period_to:
                continue
            if loan.repayment_period == 'payslip':
                # Flat loans share ONE instalment per cutoff between them --
                # gathered here, priced together below.
                flat_loans.append(loan)
                continue
            else:
                # A loan starting mid-period is charged for its own days
                # only. Without this clamp a monthly slip qualifying by a
                # single day charged the whole month: a loan starting the
                # 15th deducted 30/7 weeks on a Sep 1-30 slip instead of
                # 16/7. The days that count run from the later of the
                # period start and the loan start.
                loan_days = days
                if days is not None and loan.start_date \
                        and loan.start_date > period_from:
                    loan_days = max((period_to - loan.start_date).days + 1, 0)
                # Each loan carries its own period (weekly, semi-monthly,
                # monthly). The instalment is scaled by the share of one
                # period the payslip covers, so a monthly figure on a
                # semi-monthly payroll comes out as roughly half per slip
                # and exactly the whole over a month. With no dates, one
                # full period is assumed.
                share = (1.0 if loan_days is None
                         else loan_days / loan._period_days())
            due = min((loan.repayment_amount or 0.0) * share,
                      loan.balance or 0.0)
            if due > 0.005:
                result.append((loan, round(due, 2)))
        if flat_loans:
            result = self._loan_flat_shares(flat_loans, period_to) + result
        return result

    def _loan_flat_shares(self, flat_loans, period_to):
        """Flat loans share one instalment per cutoff, split equally.

        The instalment belongs to the EMPLOYEE, not to each loan: holding
        two flat loans does not double what a payslip gives up. One
        instalment (the largest among the flat loans, normally they are all
        the same figure) falls due per week from the earliest start date;
        what the schedule says should have been paid, minus what has been,
        is the pot this slip owes. The pot is then split equally across the
        flat loans -- 1,000 over two loans credits 500 to each balance --
        with a loan that cannot absorb its share (almost repaid) spilling
        the difference to the others, so a closed loan hands its half back
        and the survivor starts receiving the full instalment.

        Schedule-driven, so a cutoff that deducted nothing rolls forward to
        the next slip rather than onto the end of the loans, and a borrower
        who paid ahead by hand owes nothing until the schedule catches up.
        """
        per = max((loan.repayment_amount or 0.0) for loan in flat_loans)
        starts = [loan.start_date for loan in flat_loans if loan.start_date]
        if period_to and starts:
            earliest = min(starts)
            elapsed = max((period_to - earliest).days // 7 + 1, 0)
            # What has been paid against the shared schedule: every posted
            # payment on ANY of the employee's flat loans since the window
            # opened -- not just the loans still running. A loan that
            # settles mid-window leaves the pot, but the instalments it
            # absorbed were the employee's weekly payments and still count,
            # or the survivor would be billed a phantom catch-up the slip
            # after a sibling closes. Bounded by the window's start so a
            # flat loan settled years ago cannot pause a brand-new one.
            paid = sum(self.env['efs.loan.payment'].sudo().search([
                ('loan_id.employee_id', '=', self._origin.id),
                ('loan_id.repayment_period', '=', 'payslip'),
                ('state', '=', 'posted'),
                ('date', '>=', earliest),
            ]).mapped('amount'))
            pot = max(per * elapsed - paid, 0.0)
        else:
            pot = per
        pot = min(pot, sum((loan.balance or 0.0) for loan in flat_loans))
        if pot <= 0.005:
            return []
        return self._loan_split_equally(
            [(loan, loan.balance or 0.0) for loan in flat_loans], pot)

    @api.model
    def _loan_split_equally(self, caps, amount):
        """Split ``amount`` equally across ``caps`` [(loan, cap), ...].

        Each loan gets an even share, capped; whatever a capped loan cannot
        take is re-split among the rest. Rounding drift (a third of 1,000 is
        333.33 three times, a centavo short) lands on the first loan so the
        shares always sum back to the amount actually taken. Returns
        [(loan, share)] in the order given, zero shares dropped.
        """
        shares = {}
        open_items = [(loan, cap) for loan, cap in caps if cap > 0.005]
        remaining = amount
        while remaining > 0.005 and open_items:
            even = remaining / len(open_items)
            progressed = False
            still_open = []
            for loan, cap in open_items:
                got = shares.get(loan.id, 0.0)
                take = min(even, cap - got)
                if take > 0.005:
                    shares[loan.id] = got + take
                    remaining -= take
                    progressed = True
                if cap - shares.get(loan.id, 0.0) > 0.005:
                    still_open.append((loan, cap))
            open_items = still_open
            if not progressed:
                break
        out = []
        for loan, cap in caps:
            share = round(shares.get(loan.id, 0.0), 2)
            if share > 0.005:
                out.append((loan, share))
        allocated = round(amount - remaining, 2)
        drift = round(allocated - sum(share for _loan, share in out), 2)
        if out and abs(drift) >= 0.01:
            loan0, share0 = out[0]
            cap0 = dict((loan.id, cap) for loan, cap in caps)[loan0.id]
            out[0] = (loan0, round(min(share0 + drift, cap0), 2))
        return out

    def _loan_deduction_total(self, date_from=None, date_to=None,
                              available=None):
        """Total to deduct for one pay period. What the DED_LOAN rule calls.

        ``available`` is what the payslip has left to give when the rule
        runs -- the running BASIC + allowance + deduction totals at the
        DED_LOAN sequence. The deduction never exceeds it and never drives
        the net below zero: a wage deduction larger than the wage is not a
        repayment, it is a payroll error (and, for an employee, a Labor
        Code problem). A slip with no earnings therefore deducts nothing.

        A flat (per-payslip) loan is additionally taken in WHOLE instalments
        only: owed 2,000 against 1,300 of pay deducts 1,000 and the employee
        keeps the 300 -- payroll does not nibble a partial instalment out of
        someone's change. The exception is the scheduled remainder itself: a
        final 300 that is all the loan still asks for is taken in full. A
        prorated loan (week/semimonth/month) still caps to the peso, since
        its instalment is already a day-count fraction.

        The shortfall needs no bookkeeping of its own: the balance is
        derived from posted repayments, and a flat loan's per-slip due is
        schedule-minus-paid (see ``_loan_deductions``), so whatever a slip
        could not give is asked for again on the next one.

        ``None`` means no cap, which keeps the employee-form smart button
        (a rate, not a slip) and any caller that has no payslip context
        working exactly as before.
        """
        self.ensure_one()
        pairs = self._loan_deductions(date_from, date_to)
        if available is None:
            return round(sum(amount for _loan, amount in pairs), 2)
        remaining = round(max(available, 0.0), 2)
        total = 0.0
        # The flat loans are one pot (see _loan_flat_shares), so the
        # whole-instalment floor applies to the pot, not to each equal
        # share -- two loans splitting 1,000 must not each be floored to 0.
        # Flat first, prorated after, mirrored by _post_loan_repayments.
        flat_pairs = [(loan, due) for loan, due in pairs
                      if loan.repayment_period == 'payslip']
        pot_due = round(sum(due for _loan, due in flat_pairs), 2)
        if pot_due > 0.005:
            per = max((loan.repayment_amount or 0.0)
                      for loan, _due in flat_pairs)
            take = min(pot_due, remaining)
            if take < pot_due - 0.005 and per > 0:
                take = int(take / per) * per
            take = round(take, 2)
            total += take
            remaining = round(remaining - take, 2)
        for loan, due in pairs:
            if loan.repayment_period == 'payslip':
                continue
            take = round(min(due, remaining), 2)
            if take <= 0.005:
                continue
            total += take
            remaining = round(remaining - take, 2)
        return round(total, 2)

    def action_open_loans(self):
        """The Loans smart button on the employee form."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Loans',
            'res_model': 'efs.loan',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {'default_employee_id': self.id},
        }
