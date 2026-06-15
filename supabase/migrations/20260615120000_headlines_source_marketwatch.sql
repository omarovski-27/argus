-- headlines.source: add 'marketwatch' (Source 3 wire layer).
-- Reuters discontinued its public RSS (feeds.reuters.com is NXDOMAIN); the wire
-- layer now pulls MarketWatch top-stories RSS, written as source='marketwatch'
-- (specific-source convention, matching av/reuters/reddit).
-- Append-only: 'reuters' is kept so any historical reuters rows stay valid.
-- A CHECK cannot be altered in place, so drop and re-add with the widened list.
ALTER TABLE headlines
  DROP CONSTRAINT IF EXISTS headlines_source_chk;
ALTER TABLE headlines
  ADD CONSTRAINT headlines_source_chk
  CHECK (source in ('av', 'reuters', 'reddit', 'marketwatch'));
