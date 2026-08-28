# -*- coding: utf-8 -*-
"""Close the loan loop: a confirmed payslip repays the employee's loans.

Without this, `efs.loan` is a ledger nobody writes to. A loan is approved, it
shows a repayment figure, and then nothing ever reduces the balance --
`_sync_paid_state` can never fire and no loan reaches "Fully Paid" except by
hand.

Both directions are covered. Confirming a payslip posts what it deducted;
setting it back to draft or cancelling it takes those repayments back off,
because a payslip that was never paid did not repay anything.

Each posted repayment links back to its payslip (`payslip_id`), and the
payslip carries a Loan Repayments button pointing the other way.
"""

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Salary rule that carries the loan deduction. Created or adopted by the
# module's post_init_hook, because the rule has to reference a category
# resolved at install time rather than a hardcoded XML id.
LOAN_RULE_CODE = 'DED_LOAN'

# Money below this is rounding noise, not a repayment.
EPSILON = 0.005


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    loan_payment_ids = fields.One2many(
        'efs.loan.payment', 'payslip_id', string='Loan Repayments',
        readonly=True)
    loan_payment_count = fields.Integer(
        compute='_compute_loan_payment_count', string='# Loan Repayments')

    @api.depends('loan_payment_ids')
    def _compute_loan_payment_count(self):
        for slip in self:
            slip.loan_payment_count = len(slip.loan_payment_ids)

    @api.depends('name', 'number')
    def _compute_display_name(self):
        """Show the number when a payslip has no name.

        OCA payroll names a payslip from an onchange in the form, so one
        created any other way -- an import, a script, an API -- can have an
        empty name and shows as "Unnamed" wherever it is linked, including the
        Payslip column of the loan ledger. The number is always there and is
        the identifier people quote, so it is the fallback. A payslip that has
        a name is left exactly as payroll shows it.
        """
        super()._compute_display_name()
        for slip in self:
            if not slip.name and slip.number:
                slip.display_name = slip.number

    def action_open_loan_payments(self):
        """The Loan Repayments stat button on the payslip form."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Loan Repayments',
            'res_model': 'efs.loan.payment',
            'view_mode': 'list,form',
            'domain': [('payslip_id', '=', self.id)],
            'context': {'default_payslip_id': self.id},
        }

    def action_payslip_done(self):
        """Confirm the payslip, then post the repayments it deducted."""
        result = super().action_payslip_done()
        self._post_loan_repayments()
        return result

    def action_payslip_draft(self):
        """Reopen the payslip, and take its repayments back off the loans.

        A draft payslip has not paid anyone, so crediting a loan for it would
        show a balance falling against money that never moved. Reversing here
        also means the repayments are re-posted correctly if the payslip is
        recomputed and confirmed again -- the figures may well have changed.
        """
        self._reverse_loan_repayments('set back to draft')
        return super().action_payslip_draft()

    def action_payslip_cancel(self):
        """Cancel the payslip, and take its repayments back off the loans."""
        result = super().action_payslip_cancel()
        self._reverse_loan_repayments('cancelled')
        return result

    # ── Internals ───────────────────────────────────────────────────────────

    def _loan_reference(self):
        """The text stamp that ties an efs.loan.payment back to this payslip.

        Kept alongside the real `payslip_id` link because it is durable: a
        payslip that is later deleted clears the link (`set null`) but leaves
        this readable on the ledger row. It is also what the idempotency
        guard keys on. `number` is assigned when the sheet is computed, so it
        is set by the time this runs; the id fallback covers a payslip
        confirmed before a sequence existed.
        """
        self.ensure_one()
        return self.number or ('payslip-%s' % self.id)

    def _loan_deducted_amount(self):
        """What this payslip actually took for loans, as a positive figure.

        There is one DED_LOAN line **per contract** on the payslip, not one per
        payslip: OCA payroll iterates the salary rules once for each of the
        employee's contracts in the period. Their totals are not additive --
        `_get_lines_dict` credits the category with the delta against the
        previous line of the same code, so the payslip nets them down to a
        single deduction. Summing the lines here would double-credit the loan
        for any employee with two contracts in one period.
        """
        self.ensure_one()
        lines = self.line_ids.filtered(lambda line: line.code == LOAN_RULE_CODE)
        if not lines:
            return 0.0
        # Deductions are stored negative; the amount repaid is its inverse.
        return round(max(-line.total for line in lines), 2)

    def _post_loan_repayments(self):
        """Turn this payslip's DED_LOAN line into efs.loan.payment rows.

        The deducted total is allocated across the employee's active loans in
        the same order `_loan_deductions()` summed them, so the money is
        credited to the loans it was taken for.

        Re-confirming a payslip must not double-credit, so each payment is
        stamped with the payslip reference and an existing stamp is skipped.
        """
        Payment = self.env['efs.loan.payment'].sudo()

        for slip in self:
            deducted = slip._loan_deducted_amount()
            if deducted <= EPSILON:
                continue

            reference = slip._loan_reference()
            if Payment.search_count(['|', ('payslip_id', '=', slip.id),
                                     ('reference', '=', reference)]):
                _logger.info(
                    'Loan repayments for payslip %s are already posted; '
                    'skipping.', reference)
                continue

            remaining = deducted
            # The SAME period the DED_LOAN rule used. Repayment is a per-period
            # figure, so what is due depends on how many days the payslip
            # covers; called without the period this defaults to one period and
            # would credit a single period against a whole month's deduction,
            # losing the difference.
            for loan, due in slip.employee_id._loan_deductions(
                    slip.date_from, slip.date_to):
                if remaining <= EPSILON:
                    break
                amount = min(due, remaining, loan.balance)
                if amount <= EPSILON:
                    continue
                Payment.create({
                    'loan_id': loan.id,
                    'date': slip.date_to or fields.Date.context_today(self),
                    'amount': round(amount, 2),
                    'reference': reference,
                    'payslip_id': slip.id,
                    'state': 'posted',
                    'payment_method': 'payroll',
                })
                remaining = round(remaining - amount, 2)

            if remaining > EPSILON:
                # The rule deducted more than the open loans could absorb --
                # worth a log line, because it means payroll and the loan
                # ledger disagree about what is outstanding. The commonest
                # cause is the authorization being withdrawn between the
                # sheet being computed and the payslip being confirmed.
                _logger.warning(
                    'Payslip %s deducted %.2f for loans but only %.2f could '
                    'be allocated; %.2f is unaccounted for. Check the '
                    "employee's loans for a withdrawn Salary Deduction "
                    'Authorization or a balance that changed since the sheet '
                    'was computed.',
                    reference, deducted, deducted - remaining, remaining)

    def _reverse_loan_repayments(self, why):
        """Remove the repayments this payslip posted.

        Deleted rather than left in draft: a draft repayment on a cancelled
        payslip is a row nobody can explain a year later, and the balance is
        computed from posted rows anyway, so a draft one would be invisible
        while still cluttering the ledger. The payslip can be recomputed and
        confirmed again, which re-posts whatever is correct then.

        Only rows carrying this payslip's own link or reference are touched.
        """
        Payment = self.env['efs.loan.payment'].sudo()
        for slip in self:
            reference = slip._loan_reference()
            posted = Payment.search(['|', ('payslip_id', '=', slip.id),
                                     ('reference', '=', reference)])
            if not posted:
                continue
            loans = posted.loan_id
            total = sum(posted.mapped('amount'))
            posted.unlink()
            # unlink() already re-syncs, but a loan that was flipped to `paid`
            # by these very rows has to be looked at again now they are gone.
            loans._sync_paid_state()
            _logger.info(
                'Payslip %s was %s; reversed %.2f of loan repayments across '
                '%d loan(s).', reference, why, total, len(loans))
