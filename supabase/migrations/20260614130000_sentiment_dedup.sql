-- sentiment: one score per headline per method, enabling real upserts
-- (replaces the SELECT-then-insert workaround in news_av.py and
-- digest/sentiment.py, which is not concurrency-safe).
ALTER TABLE sentiment
  DROP CONSTRAINT IF EXISTS sentiment_headline_method_key;
ALTER TABLE sentiment
  ADD CONSTRAINT sentiment_headline_method_key
  UNIQUE (headline_id, method);
