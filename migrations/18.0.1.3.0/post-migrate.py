# -*- coding: utf-8 -*-
"""1.3.0: the repayment figures move from Settings onto the rules catalogue.

The Repayment block of Settings is gone; each `efs.loan.repayment.rule` now
carries its own instalment, percent, start delay and repayment cap. New
columns land with the field defaults (1,000 / 10% / 14 days / no cap), so
this copies whatever the deployment had actually configured in the old
parameters onto EVERY existing rule -- the deal HR was running keeps its
figures instead of silently reverting to the shipped ones.

The dead parameters are then dropped. The two that were seeded with XML ids
(weekly_repayment, repayment_start_delay_days) are also removed from
data/loan_parameters.xml, so the loader would delete them anyway; the ones
Settings wrote without an XML id (repayment_amount, repayment_percent,
repayment_basis, repayment_period, max_repayment_percent,
default_repayment_rule_id) would otherwise linger as zombie config.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

DEAD_PARAMS = (
    'nihao_hr_loan.repayment_basis',
    'nihao_hr_loan.repayment_period',
    'nihao_hr_loan.repayment_amount',
    'nihao_hr_loan.repayment_percent',
    'nihao_hr_loan.weekly_repayment',
    'nihao_hr_loan.repayment_start_delay_days',
    'nihao_hr_loan.max_repayment_percent',
    'nihao_hr_loan.default_repayment_rule_id',
)


def _number(icp, key, default, cast=float):
    raw = icp.get_param(key)
    try:
        value = cast(float(raw))
    except (TypeError, ValueError):
        return default
    return value


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    icp = env['ir.config_parameter'].sudo()

    amount = _number(icp, 'nihao_hr_loan.repayment_amount', 0.0)
    if amount <= 0:
        amount = _number(icp, 'nihao_hr_loan.weekly_repayment', 1000.0)
    if amount <= 0:
        amount = 1000.0
    percent = _number(icp, 'nihao_hr_loan.repayment_percent', 10.0)
    if percent <= 0:
        percent = 10.0
    start_delay = _number(
        icp, 'nihao_hr_loan.repayment_start_delay_days', 14, int)
    max_pct = _number(icp, 'nihao_hr_loan.max_repayment_percent', 0.0)

    rules = env['efs.loan.repayment.rule'].with_context(
        active_test=False).search([])
    if rules:
        rules.write({
            'amount': amount,
            'percent': percent,
            'start_delay_days': start_delay,
            'max_repayment_percent': max_pct,
        })
        _logger.info(
            'nihao_hr_loan 1.3.0: stamped %d repayment rule(s) with the '
            'old settings: amount %.2f, percent %.2f, start delay %d, '
            'max repayment %.2f%%.',
            len(rules), amount, percent, start_delay, max_pct)

    stale = icp.search([('key', 'in', list(DEAD_PARAMS))])
    if stale:
        stale.unlink()
