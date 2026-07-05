"""Argus analyst — the Phase 5 fundamental-analysis module (argus-analyst-module.md).

Data-pack layer (P1): ticker→CIK resolution (``cik``), peer discovery (``peers``),
EDGAR filings text extraction (``filings``), Stage-8 estimates context
(``estimates``), and the frozen-pack assembler (``data_pack``). The dossier engine
(valuation + synthesis + Law-1 lint) arrives in later packages and reads ONLY the
frozen pack (Law 2).
"""
