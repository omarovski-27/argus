"""Argus Signal Lab — a pre-registered EXPERIMENTAL timing signal (Law 1, Amendment #2).

Law 1 has always forbidden timing instructions. Amendment #2 (Omar, 2026-07-13) permits
ONE narrow, disciplined exception: a signal rendered as a labelled HYPOTHESIS UNDER TEST,
never an instruction. The discipline that makes it lawful is enforced in code here:

  * the render carries a mandatory ``🧪 experiment`` label and states only its CONDITION
    and its RECORD — never buy/sell/enter/exit/should/good-day (a hard render test);
  * the signal is scored in SHADOW only — simulated P&L, never a real order;
  * the rule and its verdict GATES are pre-registered in an immutable config blob before
    the track record begins, exactly like the journal's kill criteria (Law 6); and
  * PROMOTION into the real strategy requires a PASS verdict AND an explicit recorded
    decision by Omar — the code renders evidence, it never executes or recommends.

The subsystem is deterministic and boring (Law 8): a pure rule evaluator (``rule``), a
pure shadow scorer (``shadow``), a pure ledger-stats + monotonic gate engine
(``ledger``), a pure render (``render``), and thin DB wrappers (``engine`` / ``job``).
"""
