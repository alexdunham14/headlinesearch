-- Rebuild the database from the R2 archive on a fresh server. Run with
-- clickhouse-client after scripts/schema.sql, substituting the account id
-- and keys. Reads every extract; a few hours for the full corpus. The files
-- table is rebuilt from the object names so the ingest carries on from there.
INSERT INTO headlines (ts, domain, url, title)
SELECT ts, domain, url, title
FROM s3('https://ACCOUNT_ID.r2.cloudflarestorage.com/gdelt-gkg/headlines/**/*.tsv.gz',
        'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY', 'TSV',
        'ts DateTime(''UTC''), domain String, url String, title String')
SETTINGS max_insert_threads = 2, s3_skip_empty_files = 1;

INSERT INTO files (ts, status, rows, size)
SELECT parseDateTimeBestEffort(extract(_path, '(\d{14})\.tsv\.gz$')), 'ok', count(), 0
FROM s3('https://ACCOUNT_ID.r2.cloudflarestorage.com/gdelt-gkg/headlines/**/*.tsv.gz',
        'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY', 'TSV',
        'ts DateTime(''UTC''), domain String, url String, title String')
GROUP BY _path;
