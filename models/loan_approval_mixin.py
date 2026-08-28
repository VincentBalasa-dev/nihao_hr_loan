# -*- coding: utf-8 -*-
"""Two-level approval for a loan application.

    employee applies
          |
       pending  ----- rejected   (level 1, HR endorses)
          | endorse
      endorsed  ----- rejected   (level 2, administrator approves)
          | approve
       active

Level 1 is HR, level 2 is the administrator. That ordering is the company's,
and it is the reverse of Odoo's own ``hr.leave`` two-step, which validates
manager-then-HR -- worth knowing before anyone reads the Odoo docs and
concludes this is wired backwards.

**Separation of duties is enforced by identity, not by role.** Odoo's
``hr.group_hr_manager`` implies ``hr.group_hr_user``, so every approver is also
an endorser and the two levels cannot be told apart by group membership.
Whoever endorses a record is therefore refused its approval by name.

An administrator may approve something nobody endorsed. That is a deliberate
escape hatch for the case where HR is unavailable, and it is recorded as an
override rather than passed off as an ordinary approval -- an audit trail that
quietly loses the distinction is worse than no audit trail.

Rejection at either level ends the application. A level 1 that could be
overruled upward would be advice, not approval.

Kept as a mixin, and named for loans, so that adding overtime or leave
approvals later does not have to inherit a model that carries loan semantics.
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

# Level 1 is HR, level 2 is the administrator. Named here rather than repeated
# at each call site, so a deployment that uses different HR groups changes one
# line and a second model using this mixin cannot drift from the first.
ENDORSER_GROUP = 'hr.group_hr_user'
APPROVER_GROUP = 'hr.group_hr_manager'

# Marks a `state` write as coming from the workflow itself. A write WITHOUT it
# is one that came from somewhere else -- in practice a click on the form's
# status bar, which writes the field directly and would otherwise walk straight
# past the group checks and the audit stamps. The model's write() turns such a
# write into the matching action instead; see Loan.write(). Every workflow
# method therefore has to go through `_state_write`, or it re-enters that
# interception and recurses.
STATE_WRITE_CTX = 'nihao_loan_state_write'


class LoanApprovalMixin(models.AbstractModel):
    _name = 'efs.loan.approval.mixin'
    _description = 'Two-level approval trail'

    endorsed_by = fields.Many2one(
        'res.users', string='Endorsed By', readonly=True, copy=False,
        help='HR, level one of the approval chain.')
    endorsed_date = fields.Datetime(
        string='Endorsed On', readonly=True, copy=False)

    approval_override = fields.Boolean(
        string='Approved Without Endorsement', readonly=True, copy=False,
        help='Approved by an administrator while still pending, bypassing HR. '
             'Recorded so the exception is visible rather than '
             'indistinguishable from an ordinary approval.')

    rejected_by = fields.Many2one(
        'res.users', string='Rejected By', readonly=True, copy=False)
    rejected_date = fields.Datetime(
        string='Rejected On', readonly=True, copy=False)
    rejected_stage = fields.Selection([
        ('endorsement', 'HR'),
        ('approval', 'Administrator'),
    ], string='Rejected At', readonly=True, copy=False,
        help='Which level refused it. Both end the application.')

    def _approval_assert_state(self, expected, verb):
        """Refuse a transition the record is not in the right state for."""
        for rec in self:
            if rec.state not in expected:
                raise ValidationError(
                    'This application is %s, so it can no longer be %s.'
                    % (dict(rec._fields['state'].selection).get(
                        rec.state, rec.state), verb)
                )

    def _approval_assert_group(self, group, level):
        """Refuse a level to whoever is not entitled to act at it.

        A ``groups=`` attribute on a header button only HIDES it. The method
        stays callable over RPC, from ``odoo shell``, from an automated
        action, and from any REST layer added later -- so without this check
        the two levels are a UI convention, not a control.

        Verified before it existed: a plain HR officer could approve a loan a
        *different* officer had endorsed, and could approve an un-endorsed one
        outright, skipping the HR level altogether. Only the identity check
        below was biting.

        Superuser passes through, so migrations, the payroll bridge and the
        shell are not locked out of their own data.
        """
        if self.env.su:
            return
        if not self.env.user.has_group(group):
            record = self.env.ref(group, raise_if_not_found=False)
            raise AccessError(
                'Only a member of "%s" may %s this. Ask whoever holds that '
                'role.' % (record.full_name if record else group, level)
            )

    def _state_write(self, vals):
        """Write `state` for real, bypassing the status-bar interception.

        Only the workflow methods may call this -- it is what tells the
        model's ``write()`` that the transition has already been through its
        checks.
        """
        return self.with_context(**{STATE_WRITE_CTX: True}).write(vals)

    def _approval_endorse(self):
        """Level 1. HR moves a pending application to endorsed."""
        self._approval_assert_group(ENDORSER_GROUP, 'endorse')
        self._approval_assert_state(('pending',), 'endorsed')
        self._state_write({
            'state': 'endorsed',
            'endorsed_by': self.env.uid,
            'endorsed_date': fields.Datetime.now(),
        })

    def _approval_check_separation(self):
        """One person may not endorse and then approve the same record."""
        for rec in self:
            if rec.endorsed_by and rec.endorsed_by.id == self.env.uid:
                raise ValidationError(
                    'You endorsed this application, so it needs someone else '
                    'to approve it.'
                )

    def _approval_values(self):
        """Fields to write when a record is finally approved.

        ``pending`` here means nobody endorsed it, so the approval is an
        override and says so.
        """
        self.ensure_one()
        return {
            'approved_by': self.env.uid,
            'approved_date': fields.Datetime.now(),
            'approval_override': self.state == 'pending',
        }

    def _approval_reject_values(self, reason=''):
        """Fields to write when a record is refused, at either level."""
        self.ensure_one()
        return {
            'state': 'rejected',
            'rejection_reason': reason,
            'rejected_by': self.env.uid,
            'rejected_date': fields.Datetime.now(),
            'rejected_stage': (
                'approval' if self.state == 'endorsed' else 'endorsement'),
        }

    @api.model
    def _approval_open_states(self):
        """States an application is still waiting on someone in."""
        return ('pending', 'endorsed')
