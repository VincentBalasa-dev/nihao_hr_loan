# nihao_hr_loan — Employee Salary Loans

Back-office salary loans for the NihaoExpress Odoo 18 server. **One folder,
one Install click.**

| | |
|---|---|
| Module | `nihao_hr_loan` |
| Depends on | `hr`, `hr_contract`, `mail`, `base_setup` (all Odoo core) and OCA `payroll` |
| Contains | loan products, the application and its two-level approval, the Labor Code authorization gate, the repayment ledger, the settings screen, the `DED_LOAN` salary rule and the payslip hook that posts repayments |

Every dependency except `payroll` ships inside Odoo itself and is installed
automatically when Loans is installed — verified on a database with nothing
but `base`. OCA `payroll` is the free Community payroll addon; the client has
to have it in their addons folder, which is the one thing that varies between
clients. Nothing in this module names a client.

The payroll half was briefly a separate `auto_install` module so the loan
half could install without a payroll app. Merged back by decision: one thing
to copy to the server outweighed installing without payroll. The cost is
real and worth knowing — **uninstalling `payroll` uninstalls this module and
its data with it**, and a client with no payroll app cannot install it.

There is **no REST layer** and **no `fastapi` dependency** — the live server does
not have the OCA `fastapi` addon. Model technical names are kept in the
`efs.loan*` namespace so an API can be added later without a data migration.

---

## The lifecycle

```
        employee applies
              │
           pending ──────── rejected      (level 1: HR endorses)
              │ Endorse
          endorsed ──────── rejected      (level 2: administrator approves)
              │ Approve  → start_date = today + 14 days
           active
              │ Record Authorization      ← nothing is deducted before this
              │
    payroll deducts each cutoff → efs.loan.payment posted on payslip confirm
              │
            paid
```

**An active loan deducts nothing until the Salary Deduction Authorization is
recorded.** That is Labor Code article 113 and handbook section 6, and it is
the one failure mode that looks completely healthy: the loan is approved, the
figures are right, and payroll takes zero. Three things surface it — a warning
banner on the loan form, an `Awaiting Authorization` count on the employee's
Loans page, and the **Active without Authorization** filter on the loan list.

Separation of duties is enforced **by identity**: `hr.group_hr_manager` implies
`hr.group_hr_user`, so the two levels cannot be told apart by group. Whoever
endorses a loan is refused its approval by name. An administrator may approve a
loan nobody endorsed — that is the documented escape hatch for HR being away,
and it is stamped `approval_override` rather than passed off as ordinary.

## Where things are in the UI

`nihao_hr_loan` is an **application**: it installs from Apps like any other app
and gets its own tile in the app switcher.

| | |
|---|---|
| Loans ▸ Loan Applications | every loan |
| Loans ▸ To Approve | the queue: pending + endorsed |
| Loans ▸ Repayments | the ledger |
| Loans ▸ Configuration ▸ Loan Products | products and their interest method (administrator only) |
| Employees ▸ *employee* ▸ Loans tab | eligibility, standing, that employee's loans |

The Loans tab on the employee form is the in-context entry point; the menus
above are the standalone one. Deliberately not duplicated under Employees as
well — two menus onto the same action is how people end up unsure which is
real.

## Generic by design

The module ships **no client's policy**. Three switches make it fit a client
without touching code, all under Loans ▸ Configuration ▸ Settings:

**Policy Enforcement** — what a broken rule *does*.

| Mode | A rule fails, so… |
|---|---|
| `Enforce` (default) | the application is refused with the reason |
| `Advise` | it is accepted and **flagged**: a banner on the form lists every rule it breaks, the list shows *Policy OK*, and a *Policy Issues* filter finds them. HR decides. Approving one is recorded as approved despite policy issues. |
| `Off` | the rules are not evaluated |

The rules stay exactly where they are; the mode only changes their consequence.

**Repayment Rules** (Loans ▸ Configuration ▸ Repayment Rules) — the whole
repayment side, one record per deal, salary-rule style. There is deliberately
**no Repayment block in Settings**: a single company-wide figure could not
express two clients (or two tiers of one client) on different deals, so each
rule carries its own figures — Repayment per Period, Percent of Principal,
Repayment Starts After, Maximum Repayment (% of salary) — plus the Python
code deciding what each payslip deducts. An application picks a rule (the
first active rule by sequence is offered by default) and the rule's figures
land on the form, still editable per loan and **copied onto the loan at
filing** — editing a rule later never re-prices a running loan. The rule's
figures are available to its code as `amount`, `percent`,
`start_delay_days` and `max_repayment_percent`.

Loans still each carry a basis (`Fixed` / `Percent` of principal) and a
period; both default to Fixed + Per Payslip (flat) and a loan product may
override them. The prorated periods (`Weekly` / `Semi-monthly` / `Monthly`)
remain for products that want calendar arithmetic; payroll prorates those by
the days each payslip covers.

**External Borrowers** — off by default. Turned on, a loan may be made to a
contact (`res.partner`) instead of an employee. The service, salary-ceiling
and coverage rules do not apply to a contact; the approval chain, ledger,
interest and cancellation are identical. Repayments are cash / bonus / other
only — there is no payslip to deduct from, and the module refuses a payroll
method on an external loan. The only automatic limit is *Maximum for External
Borrowers* (0 = none; use Advise mode and review by hand). A contact with a
portal login sees their own loans under *My Loans*.

Defaults — Enforce, Fixed, Per Payslip (flat) on the Standard rule,
employees only. On upgrade every existing rule is stamped with the figures
the old Settings actually held, so a running deployment keeps its deal.

## Policy numbers are parameters, not code

**Loans ▸ Configuration ▸ Settings.** Changing one takes effect immediately;
no upgrade, no restart. Every rule beyond the first two is **off** until a
client turns it on — a policy that switched itself on during an upgrade would
start refusing applications that were fine the day before.

### The handbook, line by line

Checked against the *Vikkings Manpower Recruitment Agency — Company Employee
Loan Policy* on 28 Aug 2026. Every line is either a setting or a workflow;
none is a constant in code.

| Handbook | Says | Module |
|---|---|---|
| s.2 Coverage | regular employees; agency staff by management discretion | **Eligible Employee Types** = `employee`; **Agency Staff by Management Discretion** on — an HR administrator may still file for them |
| s.3 Minimum service | one year continuous | **Minimum Service** = `1.0` |
| s.4 Ceiling | 1–2 yrs 50%, 3–4 yrs 100%, 5+ yrs 200% of monthly basic salary | three **Eligibility Bands**, floors with no gaps — 2½ years stays on the 1-year band |
| s.4 Management may approve, reduce or deny | | endorse / approve / reject, plus the recorded administrator override |
| s.5.1 Repayment | **₱1,000 per week** unless management approves otherwise | the repayment rule's **Repayment per Period** = `1000`; editable per loan |
| s.5.2 Start | two weeks after proceeds are received | the repayment rule's **Repayment Starts After** = `14` days |
| s.5.3 Faster repayment on request | | figure editable per loan, subject to approval; or a faster rule (e.g. the VIP double-instalment example) |
| s.6 Written authorization | Labor Code art. 113 | the **Record Authorization** gate — nothing deducts without it |
| s.7 Additional loan requests | *(cut off in the copy I was given)* | the optional rules — concurrent limit, settle-before-reborrow, cooling-off — are built and off; turn on whichever s.7 requires |

The bands are records, not a setting, because how many bands a client runs
is itself a policy choice. Delete them all and nobody may borrow — a ceiling
that fails open is not a ceiling.

## How much comes out of a payslip

Each loan carries an instalment and a period (weekly, semi-monthly or monthly). A payslip is worth
the share of one period its days actually cover:

```
due = repayment_amount × (days in the payslip period ÷ days in one repayment period)
```

A 31-day cutoff takes ₱4,428.57 at ₱1,000/week and a 30-day one takes
₱4,285.71, so a year totals exactly 52.18 weeks. The amount is capped at the
outstanding balance, so a final instalment never overshoots.

Three things stop a deduction: no authorization, the period ending before
`start_date`, or nothing left owing.

## Using loan data in a salary rule

`employee` inside a salary rule's **Python Code** box is a real
`hr.employee` record, so everything this module adds to it is available in any
rule you write — not just `DED_LOAN`. The same reference is on the salary rule
form itself, under the **Help** tab, so you do not have to leave the screen you
are writing in.

Every expression below was run against a real payslip. The figures are for an
employee with two active authorised loans (₱1,000/week and ₱500/week) over a
31-day period.

```python
# What DED_LOAN ships with. Counts the days the payslip actually covers.
result = -employee._loan_deduction_total(payslip.date_from, payslip.date_to)
#                                                              -> -6,642.86

# Fields on the employee
result = -employee.loan_balance             # total still owed  -> -29,000.00
result = -employee.loan_cutoff_deduction    # one-week figure   ->  -1,500.00

# Straight into the loan records
result = -sum(l.repayment_amount for l in employee.loan_ids
              if l.state == 'active' and l.deduction_authorized)
#                                                              ->  -1,500.00

result = -sum(employee.loan_ids.filtered(lambda l: l.state == 'active')
                                .mapped('monthly_amortization'))
#                                                              ->  -6,522.32

# Per-loan breakdown: (loan, amount due this period), oldest first, already
# filtered for authorization, start date and remaining balance.
rows = employee._loan_deductions(payslip.date_from, payslip.date_to)
result = -sum(a for _l, a in rows)                             -> -6,642.86

# Mixing with payroll's own values
due = employee._loan_deduction_total(payslip.date_from, payslip.date_to)
result = -min(due, categories.BASIC * 0.20)                    -> -6,000.00

# Renaming the payslip line
result_name = 'Loan Repayment (%d active)' % employee.loan_active_count
result = -employee._loan_deduction_total(payslip.date_from, payslip.date_to)
#                                        line reads "Loan Repayment (2 active)"

# Reaching through a relation
result = -sum(l.repayment_amount for l in employee.loan_ids
              if l.state == 'active' and l.loan_type_id.code == 'personal')
```

| On `hr.employee` | |
|---|---|
| `loan_ids` | the loan records |
| `loan_balance` | total still owed across active loans |
| `loan_cutoff_deduction` | the one-week figure, for display |
| `loan_active_count` | loans currently being repaid |
| `loan_unauthorized_count` | active loans with no signed authorization — these deduct nothing |
| `loan_max_amount` | the handbook ceiling at this length of service |
| `loan_service_years` | continuous service |
| `_loan_deduction_total(date_from, date_to)` | the number `DED_LOAN` uses |
| `_loan_deductions(date_from, date_to)` | `(loan, amount)` pairs behind that number |

On each `efs.loan`: `amount`, `balance`, `total_paid`, `total_payable`,
`repayment_amount`, `monthly_amortization`, `interest_amount`, `state`,
`deduction_authorized`, `start_date`, `loan_type_id`.

Generators, `sum`, `min`, `max`, `round`, `filtered`, `mapped`, lambdas and
`%` formatting all work under `safe_eval`.

### Three things to get right

* **Always pass both dates.** `_loan_deduction_total()` with no arguments
  assumes **one week** — that is the ₱1,500 vs ₱6,642.86 difference above. A
  monthly payslip would deduct roughly a quarter of what is due.
* **Keep the result negative.** Deductions are stored as negative totals and
  summed into the Deduction category; a positive result would *increase* net
  pay.
* **Never create records from a rule.** Odoo blocks `env[...].create(...)` and
  `import` outright, and it would be wrong regardless: `compute_sheet()` runs
  again every time anyone recomputes a draft payslip, so you would post a
  duplicate repayment on each recompute. That is why repayments are posted in
  `action_payslip_done` — once, on confirm, stamped with the payslip number so
  re-confirming cannot double-credit, and reversed if it returns to draft.

If you change the `DED_LOAN` formula so it no longer agrees with
`_loan_deductions()`, the allocator logs *"N unaccounted for"* rather than
mis-crediting a loan. Check the log after a payroll run.

## How a repayment says where it came from

Every repayment carries two generic fields, and nothing client-specific:

| Field | Set by | Holds |
|---|---|---|
| `payment_method` | payroll → `Payroll Deduction`; a person → `Cash`, `Bonus / Incentive`, `Other` | the category |
| `payslip_id` | payroll | the payslip itself, as a clickable link |
| `reference` | payroll → the payslip number; a person → a receipt number, or nothing | free text, optional |

That is deliberately all the module knows. Not every repayment comes from a
payslip — a cash advance repaid at the cashier, a bonus applied to the balance
— and not every client wants a payslip link at all.

### The payslip link

A payroll repayment carries a real **Payslip** link (`payslip_id`) — clickable,
searchable, filled automatically when the payslip is confirmed. The payslip
carries a **Loan Repayments** button pointing the other way. Cash, bonus and
other repayments have no payslip and the field stays empty.

`reference` stays beside it as the durable text stamp: deleting a payslip
clears the link but never the ledger row, and it is what stops a re-confirmed
payslip crediting a loan twice.

## Where the balance comes from

`balance` is **derived** from posted repayments, never decremented in place. A
stored running balance and a payment history are two sources of truth for one
number and they drift the first time a payment is corrected.

The consequence worth knowing: `balance` is a non-stored compute, so the
"Has Balance" filter resolves in Python (`_search_balance`) and loads every
loan to answer. That is fine at one row per employee loan and would not be at
millions.

## Loan products

Shaped like `hr.salary.rule`: a record per product with a method picker ending
in a Python escape hatch. All six seeded products are interest-free, because
the handbook sets a repayment and says nothing about interest — charging any
would be inventing policy.

- **No interest** — the default.
- **Flat rate** — `P × r × years` on the original principal, the standard
  Philippine "add-on" quote. Repaying early still pays the full interest.
- **Diminishing balance** — accrues on what is still owed. Cheaper than a flat
  rate quoted at the same number, which is why the two must stay distinct.
- **Python code** — evaluated the way a salary rule's is. Set `result` to the
  total interest for the whole loan.

Interest is folded into `total_payable`, and repayments are measured against
that, not the principal — settling the principal alone would close a loan that
still owes interest.

## Access

| | `efs.loan` | `efs.loan.payment` | `efs.loan.type` |
|---|---|---|---|
| Employee (`base.group_user`) | read/create **own only** | read own | read |
| HR Officer (`hr.group_hr_user`) | read/write/create, **all** | read/write/create | read |
| HR Administrator (`hr.group_hr_manager`) | full | full | full |

Row-level rules restrict an employee to loans where
`employee_id.user_id = user.id`. A global company rule ANDs on top.

## Installing

Drop the folder on the addons path, restart Odoo, then Apps ▸ **Update Apps
List** ▸ search **Loans** ▸ **Install**. It appears under the default *Apps*
filter, so there is nothing to un-filter.

From the command line instead:

```bash
./odoo-bin -c odoo.conf -d mydb -i nihao_hr_loan --stop-after-init
```

After a code change:

```bash
./odoo-bin -c odoo.conf -d mydb -u nihao_hr_loan --stop-after-init
```

**A `post_init_hook` runs at install and never again** — an upgrade does not
re-run it. If a shipped change has to reach an already-installed database, put
it in `migrations/<version>/post-migrate.py` calling
`env['hr.salary.rule']._nihao_setup_loan_rule()`, which is idempotent and
exists for exactly that reason.

## Odoo version

Written for **Odoo 18**, which is what the live server runs. Everything that
differs on 19 is commented at the site: `_sql_constraints` lists rather than
`models.Constraint`, `safe_eval(..., nocopy=True)` because 18 still copies the
context, and `contract_id.wage` rather than the delegated `hr.version` field.
