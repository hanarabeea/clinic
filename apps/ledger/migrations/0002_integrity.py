"""The second layer of protection for the books: rules PostgreSQL itself enforces, whatever code (or person with a
SQL prompt) writes to the tables.

1. A posted entry balances and has at least two lines. Checked at COMMIT by deferred constraint triggers, so an entry
   can be built line by line inside a transaction but can never be committed out of balance.
2. A posted entry and its lines are immutable: no UPDATE, no DELETE, no new lines. Corrections are reversal entries.
3. An entry can only become posted with a number, inside an open period of its own clinic that contains its date.
4. A line belongs to the same clinic as its entry, its account and its cost center; the account is an active leaf;
   a line on a revenue or expense account names a cost center.
5. An account that has lines cannot become a group or change its type.

Every violation raises SQLSTATE 23514 (check_violation), which Django reports as IntegrityError.
"""

from django.db import migrations

FORWARD = r"""
CREATE FUNCTION ledger_entry_must_balance() RETURNS trigger AS $$
DECLARE
  eid bigint;
  st text;
  d numeric;
  c numeric;
  n integer;
BEGIN
  IF TG_TABLE_NAME = 'ledger_journalline' THEN
    IF TG_OP = 'DELETE' THEN eid := OLD.entry_id; ELSE eid := NEW.entry_id; END IF;
  ELSE
    eid := NEW.id;
  END IF;
  SELECT status INTO st FROM ledger_journalentry WHERE id = eid;
  IF st IS NULL OR st <> 'posted' THEN
    RETURN NULL;
  END IF;
  SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0), COUNT(*) INTO d, c, n
    FROM ledger_journalline WHERE entry_id = eid;
  IF n < 2 THEN
    RAISE EXCEPTION 'ledger: posted entry % has fewer than two lines', eid USING ERRCODE = '23514';
  END IF;
  IF d <> c THEN
    RAISE EXCEPTION 'ledger: entry % does not balance (debit %, credit %)', eid, d, c USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER ledger_line_balance
  AFTER INSERT OR UPDATE OR DELETE ON ledger_journalline
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION ledger_entry_must_balance();

CREATE CONSTRAINT TRIGGER ledger_entry_balance
  AFTER INSERT OR UPDATE ON ledger_journalentry
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION ledger_entry_must_balance();


CREATE FUNCTION ledger_entry_rules() RETURNS trigger AS $$
DECLARE
  pst text;
  ps date;
  pe date;
  porg bigint;
  pyear bigint;
BEGIN
  IF TG_OP <> 'INSERT' AND OLD.status = 'posted' THEN
    RAISE EXCEPTION 'ledger: posted entry % cannot be changed or deleted; post a reversal instead', OLD.id
      USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  IF NEW.status = 'posted' THEN
    IF NEW.number IS NULL THEN
      RAISE EXCEPTION 'ledger: a posted entry needs a number' USING ERRCODE = '23514';
    END IF;
    SELECT status, start_date, end_date, organization_id, fiscal_year_id INTO pst, ps, pe, porg, pyear
      FROM ledger_fiscalperiod WHERE id = NEW.period_id;
    IF porg IS DISTINCT FROM NEW.organization_id OR pyear IS DISTINCT FROM NEW.fiscal_year_id THEN
      RAISE EXCEPTION 'ledger: entry and its period belong to different clinics or years' USING ERRCODE = '23514';
    END IF;
    IF pst <> 'open' THEN
      RAISE EXCEPTION 'ledger: the period of % is closed', NEW.date USING ERRCODE = '23514';
    END IF;
    IF NEW.date < ps OR NEW.date > pe THEN
      RAISE EXCEPTION 'ledger: date % is outside its period', NEW.date USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_entry_rules
  BEFORE INSERT OR UPDATE OR DELETE ON ledger_journalentry
  FOR EACH ROW EXECUTE FUNCTION ledger_entry_rules();


CREATE FUNCTION ledger_line_rules() RETURNS trigger AS $$
DECLARE
  st text;
  eorg bigint;
  aorg bigint;
  agroup boolean;
  aactive boolean;
  atype text;
  corg bigint;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    SELECT status INTO st FROM ledger_journalentry WHERE id = OLD.entry_id;
    IF st = 'posted' THEN
      RAISE EXCEPTION 'ledger: lines of posted entry % cannot be changed or deleted', OLD.entry_id
        USING ERRCODE = '23514';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  SELECT status, organization_id INTO st, eorg FROM ledger_journalentry WHERE id = NEW.entry_id;
  IF st = 'posted' THEN
    RAISE EXCEPTION 'ledger: no line can be added to posted entry %', NEW.entry_id USING ERRCODE = '23514';
  END IF;
  SELECT organization_id, is_group, is_active, type INTO aorg, agroup, aactive, atype
    FROM ledger_account WHERE id = NEW.account_id;
  IF eorg IS DISTINCT FROM NEW.organization_id OR aorg IS DISTINCT FROM NEW.organization_id THEN
    RAISE EXCEPTION 'ledger: a line, its entry and its account must belong to one clinic' USING ERRCODE = '23514';
  END IF;
  IF agroup THEN
    RAISE EXCEPTION 'ledger: account % is a group, only leaf accounts take postings', NEW.account_id
      USING ERRCODE = '23514';
  END IF;
  IF NOT aactive THEN
    RAISE EXCEPTION 'ledger: account % is switched off', NEW.account_id USING ERRCODE = '23514';
  END IF;
  IF NEW.cost_center_id IS NULL THEN
    IF atype IN ('revenue', 'expense') THEN
      RAISE EXCEPTION 'ledger: a line on revenue or expense account % needs a cost center', NEW.account_id
        USING ERRCODE = '23514';
    END IF;
  ELSE
    SELECT organization_id INTO corg FROM ledger_costcenter WHERE id = NEW.cost_center_id;
    IF corg IS DISTINCT FROM NEW.organization_id THEN
      RAISE EXCEPTION 'ledger: the cost center belongs to another clinic' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_line_rules
  BEFORE INSERT OR UPDATE OR DELETE ON ledger_journalline
  FOR EACH ROW EXECUTE FUNCTION ledger_line_rules();


CREATE FUNCTION ledger_account_rules() RETURNS trigger AS $$
BEGIN
  IF (NEW.is_group AND NOT OLD.is_group) OR NEW.type <> OLD.type OR NEW.organization_id <> OLD.organization_id THEN
    IF EXISTS (SELECT 1 FROM ledger_journalline WHERE account_id = OLD.id) THEN
      RAISE EXCEPTION 'ledger: account % has postings: it cannot become a group, change type or move clinic', OLD.id
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_account_rules
  BEFORE UPDATE ON ledger_account
  FOR EACH ROW EXECUTE FUNCTION ledger_account_rules();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS ledger_account_rules ON ledger_account;
DROP FUNCTION IF EXISTS ledger_account_rules();
DROP TRIGGER IF EXISTS ledger_line_rules ON ledger_journalline;
DROP FUNCTION IF EXISTS ledger_line_rules();
DROP TRIGGER IF EXISTS ledger_entry_rules ON ledger_journalentry;
DROP FUNCTION IF EXISTS ledger_entry_rules();
DROP TRIGGER IF EXISTS ledger_entry_balance ON ledger_journalentry;
DROP TRIGGER IF EXISTS ledger_line_balance ON ledger_journalline;
DROP FUNCTION IF EXISTS ledger_entry_must_balance();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
    ]
