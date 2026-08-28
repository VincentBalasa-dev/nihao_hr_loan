# Deploying to the live server

Target: `odoo-demo.auditninjaz.com`, database `mydb`, **Odoo 18.0 Community**,
`/home/ubuntu/odoo/odoo-bin` with `/home/ubuntu/odoo/odoo.conf`.

Already installed there and relevant: `payroll 18.0.1.2.1` (OCA),
`hr_api_odoo 18.0.1.25.0`, `hr`, `hr_contract`.

**This module does not modify any module already running on the server.** It
adds its own models and *inherits* three existing ones — `hr.employee` (new fields
and a Loans tab), `hr.payslip` (a hook on confirm/draft/cancel) and
`hr.salary.rule` (the setup method). Inheriting is additive: no file in
`ninjaz_order_database`, `hr_api_odoo` or `payroll` is touched, and
uninstalling removes only what this module added.

It depends on OCA `payroll`, so **uninstalling payroll would uninstall this
module and drop its loan data** — take a dump before ever doing that.

The one existing *record* it changes is the hand-typed "loans" salary rule —
section 2.

Nothing here has been run against live. Everything below was verified on the
local `odoo:18` stack against the same payroll version.

---

## 1. What will change on the live database

| | |
|---|---|
| New models | `efs.loan`, `efs.loan.payment`, `efs.loan.type`, `efs.loan.reject.wizard` |
| New fields on `hr.employee` | `loan_ids`, `loan_service_date`, and eight computed loan-standing fields |
| New views | 1 inherit on `hr.view_employee_form` (adds a Loans tab and a smart button); everything else is its own view |
| New menus | a top-level **Loans** app (3 items + Configuration ▸ Loan Products) and its tile in the app switcher |
| New salary rule | `DED_LOAN` — **or the existing "loans" rule adopted**, see below |
| System parameters | three seeded `nihao_hr_loan.*` policy values; the optional rules and the payslip link field are written only when set in Loans ▸ Configuration ▸ Settings |
| Fields on `hr.employee.public` | the same loan-standing fields, read-only, so an employee can see their own eligibility and file a loan |
| Module version | `18.0.1.2.1`| `18.0.1.2.0`. A fresh install needs no migration; the `migrations/` folder only matters for a database that had 1.0.0 installed |
| Generic switches | Policy Enforcement (Enforce / Advise / Off), Repayment Basis × Period, External Borrowers — all default to today's behaviour: Enforce, Fixed, Weekly, employees only |

Nothing existing is renamed, deleted or archived.

## 2. The existing "loans" salary rule

Live has 19 salary rules typed in through the UI. One is named **"loans"**,
category Deduction, with an **empty code** and no register.

The install hook **adopts it**: it sets that rule's code to `DED_LOAN`, gives
it the module's formula, and leaves it on whatever structures already reference
it. No second loan deduction is created, so no payslip grows a duplicate line.

It only adopts a rule that has **no code at all** and whose name is one of
`loans`, `loan`, `loan repayment`, `loan deduction` (case-insensitive). If two
rules match, it adopts **neither**, creates a fresh `DED_LOAN`, and writes a
warning naming both — read the install log.

> The formula on the live "loans" rule could not be read before this work: the
> shared login (uid 758) is denied `hr.salary.rule`. **Whatever formula that
> rule currently carries will be replaced.** If it was computing something,
> screenshot the rule form before installing.

## 3. Install

One folder, next to `ninjaz_order_database`. **The folder name is the module
name — do not rename it.**

```
nihao_hr_loan/
```

### Option A — from the Odoo Apps screen (plug and play)

1. Copy the folder onto the server's addons path.
2. Restart Odoo so it picks up the new folder on disk.
3. Apps ▸ **Update Apps List**.
4. Search **Loans**. `NihaoExpress HR - Employee Loans` appears under the
   default *Apps* filter — it is flagged `application`, so you do not have to
   clear that filter. Hit **Install**.

That is the whole install. The salary rule and the payslip hook come with it.

**Back up before step 4** — the install rewrites the existing "loans" salary
rule in place (section 2 above):

```bash
pg_dump -Fc mydb > ~/mydb-before-loans-$(date +%F).dump
```

### Option B — from the command line

```bash
cd /home/ubuntu/odoo
pg_dump -Fc mydb > ~/mydb-before-loans-$(date +%F).dump
./odoo-bin -c odoo.conf -d mydb -i nihao_hr_loan --stop-after-init
```

Either way, read what the bridge did to the salary rule:

```bash
grep "Loan bridge" ~/.local/share/Odoo/odoo.log | tail -20
```

Expect one of:

```
Loan bridge: adopted the hand-made salary rule "loans" (id NN) as DED_LOAN ...
Loan bridge: DED_LOAN is already on N structure(s): ...
```

or, if it could not adopt:

```
Loan bridge: created the DED_LOAN rule (id NN).
Loan bridge: attached DED_LOAN to N structure(s): ...
```

A `no salary structure carries a "Deduction" rule` warning means the rule is on
**no** structure and loans will never deduct — attach it by hand in
Payroll ▸ Configuration ▸ Salary Structures.

## 4. After installing

1. **Check the salary structure.** Payroll ▸ Configuration ▸ Salary Rules ▸
   `DED_LOAN` — confirm it is on the structure your payslips actually use, and
   that it sits after the statutory deductions (sequence 150) and before NET.
2. **Set the policy parameters** if the handbook figures differ from the
   defaults in `README.md`.
3. **Fill in service dates.** Eligibility needs a hire date. If
   `loan_service_date` is empty the module falls back to the employee's first
   contract start date, and failing that the record's creation date — which
   reads as zero years of service and denies the loan. Set
   `loan_service_date` on the employee form for anyone whose contract history
   does not reflect their real start.
4. **Run one payslip in draft** for an employee with an authorised loan and
   confirm the `DED_LOAN` line shows the expected figure before confirming.
5. **The payslip link needs nothing from you.** Every payroll repayment
   links to its payslip, and every payslip shows a *Loan Repayments* button.
6. **Switch on handbook section 2.** Loans ▸ Configuration ▸ Settings ▸
   Coverage: set *Eligible Employee Types* to `employee` and tick *Agency
   Staff by Management Discretion*. Both ship **off** so other clients do not
   inherit NihaoExpress policy; on live they need turning on by hand. With
   them on, a contractor is refused and an agency employee can only be filed
   for by an HR administrator.
7. **Leave the generic switches alone for NihaoExpress.** Policy Enforcement
   = Enforce, Repayment Basis = Fixed, Period = Weekly, External Borrowers off
   — that is the handbook. They exist for the next client: a lender who wants
   HR to review by hand sets Advise; one who repays 10% of the principal a
   month sets Percent / Monthly; one who lends to non-staff switches External
   Borrowers on and sets a maximum.

## 5. Rollback

```bash
# Uninstall from the UI (Apps ▸ NihaoExpress HR - Employee Loans ▸ Uninstall),
# or:
./odoo-bin shell -c odoo.conf -d mydb --no-http <<'PY'
env['ir.module.module'].search([
    ('name', '=', 'nihao_hr_loan')
]).button_immediate_uninstall()
env.cr.commit()
PY
```

Uninstalling `nihao_hr_loan` drops the loan tables and their data. **The adopted salary rule is
not restored to its old name and empty code** — it was an existing record, so
uninstall leaves it as `DED_LOAN` with the module formula and a dangling
`employee._loan_deduction_total` call that will raise on the next payslip
compute. If you uninstall, either delete that rule or clear its formula.
Restoring the dump is the clean path.

## 6. Verified locally, not on live

Run against `odoo:18` + OCA payroll `18.0.1.2.1` (commit `56b1142`), the same
version live runs:

- 65 functional checks: eligibility bands, ceiling enforcement, the two-level
  approval chain and its separation-of-duties refusal, the authorization gate,
  day-accurate deduction across 7/30/31-day periods, payslip deduction,
  repayment posting, re-confirm not double-crediting, draft/cancel reversing
  repayments, overpayment refusal, the final instalment cap, flat and
  diminishing interest, and every policy parameter actually steering the rules.
- Adoption of a hand-typed untyped "loans" rule, reproducing the live shape.
- Ambiguity guard: two candidate rules ⇒ adopt neither.
- Record rules: an employee sees only their own loans; an HR officer sees all.
- Every view renders for a real `hr.group_hr_user`.
- The **two levels are enforced in Python**, not just hidden: a plain employee
  cannot endorse or approve, an HR officer cannot approve at all (endorsed by
  someone else or not), and nobody can approve what they themselves endorsed.
- **Dependencies auto-install**: on a database with nothing but `base`, one
  click on Loans pulls in `hr`, `hr_contract` and `mail` by itself. Only OCA
  `payroll` must already be present.
- **Employee self-service**: an ordinary employee files their own loan,
  sees their own eligibility, withdraws it while unapproved, and cannot touch
  anyone else's. The Loans app shows them only *My Loans*.
- **Cancelling a running loan needs both levels**: the request leaves
  deductions running, HR must endorse before the administrator can approve,
  the endorser cannot approve their own endorsement, refusing puts the loan
  back, and repayments already made survive.
- **Payslip link both ways**: a payroll repayment links to its payslip and the
  payslip counts its loan repayments; reversing the payslip removes both.
- A fresh install from the **Apps screen** on a clean database with payroll
  present: the module listed, visible under the default filter, and one
  Install brings the app tile, the `DED_LOAN` rule, the six loan products, the
  four policy parameters and the payslip hook.

**Not verified:** anything against the live database, and the content of the
live "loans" rule's current formula.
