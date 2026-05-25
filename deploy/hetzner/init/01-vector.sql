-- Enables pgvector for the media_engine cache database.
-- Mounted read-only into /docker-entrypoint-initdb.d/ of the
-- pgvector/pgvector:pg16 image. Postgres' official entrypoint runs
-- every *.sql / *.sh in that directory exactly once on first cluster
-- init; the IF NOT EXISTS guard makes a re-run a no-op.
CREATE EXTENSION IF NOT EXISTS vector;
