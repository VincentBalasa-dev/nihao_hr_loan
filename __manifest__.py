# -*- coding: utf-8 -*-
{
    'name': 'NihaoExpress HR - Employee Loans',
    'summary': 'Salary loans: products, two-level approval, and the payroll deduction they drive',
    'description': """
Employee salary loans for the NihaoExpress Odoo.

Carries the whole loan lifecycle in the back office: an application, HR
endorsement, administrator approval, the signed Salary Deduction
Authorization the Labor Code requires, and a repayment ledger whose balance is
derived from posted repayments rather than decremented in place.

Loan products are records, not a Selection, so a deployment adds an offering --
construction, appliance, business -- from Configuration instead of editing
Python. Each product decides how its interest is worked out: none, flat,
diminishing balance, or a Python formula evaluated the way a salary rule's is.

Policy numbers from the employee handbook (the weekly repayment, the minimum
service period, the loanable ceiling by length of service, the two-week delay
before repayment starts) are system parameters, not constants -- see
data/loan_parameters.xml.

**Payroll.** The deduction arithmetic lives on `hr.employee` -- "what does
this person give up for a period running from A to B" -- as a pure function of
an employee and a date range; repayments can also be recorded by hand.

The `DED_LOAN` salary rule and the payslip hook that posts repayments when a
payslip is confirmed are part of this module; OCA `payroll` is a dependency.
Every payroll repayment links to its payslip, and every payslip carries a Loan
Repayments button.

**No REST layer.** This module is back office only. The models deliberately
keep the `efs.loan*` technical names the NihaoExpress Staff Portal's API
contract was written against, so an API can be added later without a data
migration.
""",
    # Odoo 18 is what the live company server runs (18.0 Community). Every
    # construct here is 18-form: `_sql_constraints` lists rather than
    # `models.Constraint`, `groups_id` rather than `group_ids`, and
    # `safe_eval(..., nocopy=True)` because 18 still copies the context.
    'version': '18.0.1.2.16',
    'category': 'Human Resources',
    'author': 'NihaoExpress',
    'website': 'https://odoo-demo.auditninjaz.com',
    # hr, hr_contract, mail, base_setup ship inside every Odoo and are
    # installed automatically with this module (verified on a database with
    # nothing but `base`). hr_contract is where the monthly basic salary the
    # loan ceiling is a multiple of lives in Odoo 18.
    #
    # payroll is OCA's free Community payroll addon, for the DED_LOAN salary
    # rule and the payslip hook that posts repayments. It is a hard dependency
    # by decision: one folder to copy to the server outweighed installing
    # without a payroll app. The cost, stated in README and DEPLOY: a server
    # without OCA payroll cannot install this, and uninstalling payroll
    # uninstalls this module and its data with it.
    'depends': ['hr', 'hr_contract', 'mail', 'base_setup', 'payroll'],
    'data': [
        'security/loan_security.xml',
        'security/ir.model.access.csv',
        'data/loan_sequence.xml',
        'data/loan_parameters.xml',
        'data/loan_types.xml',
        'data/loan_tiers.xml',
        'data/loan_repayment_rules.xml',
        'wizard/loan_reject_wizard_views.xml',
        'wizard/loan_cancel_wizard_views.xml',
        'views/loan_tier_views.xml',
        'views/res_config_settings_views.xml',
        'views/loan_type_views.xml',
        'views/loan_views.xml',
        'views/hr_employee_views.xml',
        'views/loan_menus.xml',
        'views/loan_repayment_rule_views.xml',
        'views/hr_salary_rule_views.xml',
        'views/hr_payslip_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    # An application, not a technical add-on. Odoo's Apps view filters on this
    # flag: with `application: False` the module only appears once somebody
    # clears the "Apps" filter, which is not a module anyone finds by browsing.
    # It also earns its own root menu and tile -- see views/loan_menus.xml.
    'application': True,
    'images': ['static/description/icon.png'],
    'license': 'LGPL-3',
}
