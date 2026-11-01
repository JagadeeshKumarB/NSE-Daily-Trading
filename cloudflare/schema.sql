-- The board (one row a part of the Nifty 50 list, plus the index) and each symbol's record for the day.
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, t INTEGER NOT NULL, v TEXT NOT NULL);
