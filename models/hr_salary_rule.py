# -*- coding: utf-8 -*-
"""The DED_LOAN salary rule: created, adopted, and kept in step.

Set up in Python rather than an XML data file for two reasons. The rule has to
attach to whichever "Deduction" category the deployment already uses -- a data
file would either hardcode an XML id this module does not own, or create a
second DED category alongside the existing one. And on a database where
somebody already typed a loan deduction in by hand, the right answer is to
adopt that rule rather than create a second one beside it: two deduction lines
on one payslip is a payroll error nobody spots until an employee does.

Lives on the model rather than inside ``post_init_hook`` because a hook runs
only at INSTALL. An upgrade never re-runs it, so a shipped change to the
formula would not reach the rule. As a model method it can be called from a
migration script, from ``odoo shell``, or by hand -- and it is idempotent, so
calling it again is free.
"""

import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# The salary rule this module owns. Also referenced from models/hr_payslip.py
# -- change it in both places or neither.
LOAN_RULE_CODE = 'DED_LOAN'

# Names an already-typed loan deduction plausibly goes by on a database that
# was keeping this by hand. Compared case-insensitively, and only against rules
# with no code at all, because a rule someone gave a code to is a rule someone
# may be referencing from another formula.
ADOPTABLE_NAMES = ('loans', 'loan', 'loan repayment', 'loan deduction')

# After the statutory deductions (SSS, PhilHealth and Pag-IBIG typically sit in
# the 100s), before NET.
LOAN_RULE_SEQUENCE = 150

# The formula. The period is passed because repayment is a per-period figure: what
# a payslip owes depends on how many days it covers, so a 31-day cutoff takes
# slightly more than a 30-day one. Negative, because deductions are stored as
# negative totals and summed into the DED category.
#
# `available` is the pay the slip still has when this rule runs: the running
# category totals at sequence 150 -- earnings (BASIC), allowances (ALW) and
# the statutory deductions already taken (DED, negative). The deduction is
# capped there so a thin or empty slip is never driven below a zero net BY
# THE LOAN. A rule can only see what ran before it, so a deduction somebody
# sequences after 150 can still push the net negative -- that is that rule's
# placement, not this one's. (categories reads 0.0 for a code a deployment
# does not use, so this is safe on any category setup.)
LOAN_RULE_FORMULA = (
    'result = -employee._loan_deduction_total('
    'payslip.date_from, payslip.date_to, '
    'available=categories.BASIC + categories.ALW + categories.DED)'
)

RULE_NOTE = (
    'Managed by the nihao_hr_loan module. Deducts each employee\'s '
    'authorised loan repayment for the days this payslip covers; confirming '
    'the payslip posts the repayments against the loans. Editing the formula '
    'here will put the payslip and the loan ledger out of step.'
)


class HrSalaryRule(models.Model):
    _inherit = 'hr.salary.rule'

    @api.model
    def _nihao_setup_loan_rule(self):
        """Create or adopt the DED_LOAN rule and put it on the live structures.

        Idempotent. Returns the rule.
        """
        Rule = self.env['hr.salary.rule'].sudo()
        category = self._nihao_deduction_category()
        values = {
            'name': 'Loan Repayment',
            'code': LOAN_RULE_CODE,
            'category_id': category.id,
            'sequence': LOAN_RULE_SEQUENCE,
            'condition_select': 'none',
            'amount_select': 'code',
            'amount_python_compute': LOAN_RULE_FORMULA,
            'appears_on_payslip': True,
            'note': RULE_NOTE,
        }

        rule = Rule.search([('code', '=', LOAN_RULE_CODE)], limit=1)
        if rule:
            rule.write(values)
            _logger.info('Loan bridge: updated the existing %s rule (id %s).',
                         LOAN_RULE_CODE, rule.id)
        else:
            rule = self._nihao_adoptable_rule(category)
            if rule:
                rule.write(values)
                _logger.info(
                    'Loan bridge: adopted the hand-made salary rule "%s" '
                    '(id %s) as %s rather than creating a second loan '
                    'deduction beside it. Its structures and any existing '
                    'payslip lines keep working; it now carries the module '
                    'formula.', rule.name, rule.id, LOAN_RULE_CODE)
            else:
                rule = Rule.create(values)
                _logger.info('Loan bridge: created the %s rule (id %s).',
                             LOAN_RULE_CODE, rule.id)

        self._nihao_attach_rule_to_structures(rule)
        return rule

    @api.model
    def _nihao_deduction_category(self):
        """The Deduction category this deployment already uses, or a new one.

        Matched on code first because that is what payroll formulas reference
        (``categories.DED``), then on name for a database whose categories were
        typed in without codes.
        """
        Category = self.env['hr.salary.rule.category'].sudo()
        category = Category.search([('code', '=', 'DED')], limit=1)
        if category:
            return category
        category = Category.search([('name', '=ilike', 'deduction')], limit=1)
        if category:
            _logger.info(
                'Loan bridge: using the existing "%s" category (id %s), which '
                'has no code. Give it the code DED if payroll formulas need to '
                'reference it.', category.name, category.id)
            return category
        return Category.create({'name': 'Deduction', 'code': 'DED'})

    @api.model
    def _nihao_adoptable_rule(self, category):
        """An existing hand-made loan deduction to take over, or nothing.

        Deliberately narrow. A rule is adopted only when it has **no code** (so
        no other formula references it) and its name is one of a short list. If
        more than one candidate matches, none is adopted: guessing which of two
        loan rules is the real one is worse than leaving both and letting
        someone look.
        """
        Rule = self.env['hr.salary.rule'].sudo()
        # Matched in Python rather than with a domain because `in` on a Char is
        # case-sensitive in Postgres, and a case-sensitive pass can find ONE
        # candidate where a case-insensitive one would find two -- which would
        # slip an ambiguous database past the guard below and adopt the wrong
        # rule. One search, one comparison, no way for the two to disagree.
        # This runs at install, so scanning the untyped rules costs nothing.
        candidates = Rule.with_context(active_test=False).search(
            [('code', 'in', (False, ''))]
        ).filtered(lambda r: (r.name or '').strip().lower() in ADOPTABLE_NAMES)

        if not candidates:
            return None
        if len(candidates) > 1:
            _logger.warning(
                'Loan bridge: %d untyped salary rules look like a loan '
                'deduction (%s). None was adopted - a new %s rule was created '
                'instead. Archive the ones you do not want, or a payslip may '
                'carry two loan lines.',
                len(candidates), ', '.join(candidates.mapped('name')),
                LOAN_RULE_CODE)
            return None

        rule = candidates
        if rule.category_id and category and rule.category_id != category:
            _logger.info(
                'Loan bridge: adopted rule "%s" sits in category "%s", not '
                '"%s". Keeping its own category so existing payslip totals do '
                'not move.', rule.name, rule.category_id.name, category.name)
        return rule

    @api.model
    def _nihao_attach_rule_to_structures(self, rule):
        """Put the rule on the salary structures that already carry deductions.

        An adopted rule is usually already attached, and this leaves it there.
        A freshly created one is attached to every structure that already has a
        rule in the same category, so an existing payroll setup picks it up on
        install instead of quietly deducting nothing until somebody notices.
        """
        Structure = self.env['hr.payroll.structure'].sudo()

        already = Structure.search([('rule_ids', 'in', rule.ids)])
        if already:
            _logger.info(
                'Loan bridge: %s is already on %d structure(s): %s.',
                rule.code, len(already), ', '.join(already.mapped('name')))
            return already

        if not rule.category_id:
            _logger.warning(
                'Loan bridge: %s has no category, so no structure could be '
                'chosen for it. Add it to a salary structure by hand.',
                rule.code)
            return Structure

        targets = Structure.search(
            [('rule_ids.category_id', '=', rule.category_id.id)])
        if not targets:
            _logger.warning(
                'Loan bridge: no salary structure carries a "%s" rule, so %s '
                'was not attached to any. Add it to the structure your '
                'payslips use, or loans will never be deducted.',
                rule.category_id.name, rule.code)
            return targets

        targets.write({'rule_ids': [(4, rule.id)]})
        _logger.info('Loan bridge: attached %s to %d structure(s): %s.',
                     rule.code, len(targets), ', '.join(targets.mapped('name')))
        return targets
