\set ON_ERROR_STOP on

BEGIN;

INSERT INTO public.tenants(id, slug)
VALUES ('10000000-0000-0000-0000-000000000001', 'sync-test');
SELECT set_config('brainhub.tenant_id', '10000000-0000-0000-0000-000000000001', true);
INSERT INTO public.installations(id, tenant_id, public_label)
VALUES (
  '20000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  'integration fixture'
);

CREATE TEMP TABLE sync_fixture(events jsonb) ON COMMIT DROP;
WITH payload AS (
  SELECT jsonb_build_object(
    'projector', '1.0.0',
    'source_sequence', 1,
    'nodes', jsonb_build_array(),
    'edges', jsonb_build_array()
  ) AS graph_payload
)
INSERT INTO sync_fixture(events)
SELECT jsonb_build_array(jsonb_build_object(
  'sequence', 1,
  'event_id', 'fixture-event-0001',
  'event_type', 'com.brainhub.graph.imported.v1',
  'recorded_at', '2026-07-17T09:40:00Z',
  'canonical_sha256', public.brainhub_graph_payload_sha256(graph_payload),
  'graph_payload', graph_payload
))
FROM payload;

SELECT public.ingest_brainhub_sync_batch(
  '20000000-0000-0000-0000-000000000001',
  '30000000-0000-0000-0000-000000000001',
  1,
  1,
  (SELECT events FROM sync_fixture)
);

DO $$
DECLARE
  cursor_value bigint;
  event_total bigint;
BEGIN
  SELECT last_cursor INTO cursor_value
    FROM public.installations
   WHERE id = '20000000-0000-0000-0000-000000000001';
  SELECT count(*) INTO event_total
    FROM public.graph_events
   WHERE installation_id = '20000000-0000-0000-0000-000000000001';
  IF cursor_value <> 1 OR event_total <> 1 THEN
    RAISE EXCEPTION 'atomic ingest did not advance cursor and event together';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM public.graph_events
     WHERE installation_id = '20000000-0000-0000-0000-000000000001'
       AND encode(canonical_sha256, 'hex') =
         public.brainhub_graph_payload_sha256(graph_payload)
  ) THEN
    RAISE EXCEPTION 'server did not recompute and store the graph payload digest';
  END IF;
END
$$;

-- Exact replay is idempotent and returns the acknowledged batch boundary.
SELECT public.ingest_brainhub_sync_batch(
  '20000000-0000-0000-0000-000000000001',
  '30000000-0000-0000-0000-000000000001',
  1,
  1,
  (SELECT events FROM sync_fixture)
);

DO $$
BEGIN
  BEGIN
    PERFORM public.ingest_brainhub_sync_batch(
      '20000000-0000-0000-0000-000000000001',
      '30000000-0000-0000-0000-000000000001',
      1,
      1,
      '[{"sequence":1,"event_id":"changed-event","event_type":"com.brainhub.graph.imported.v1","recorded_at":"2026-07-17T09:40:00Z","canonical_sha256":"0000000000000000000000000000000000000000000000000000000000000000","graph_payload":{"projector":"1.0.0","source_sequence":1,"nodes":[],"edges":[]}}]'::jsonb
    );
    RAISE EXCEPTION 'expected conflicting replay to fail';
  EXCEPTION WHEN unique_violation THEN
    NULL;
  END;

  BEGIN
    PERFORM public.ingest_brainhub_sync_batch(
      '20000000-0000-0000-0000-000000000001',
      '30000000-0000-0000-0000-000000000001',
      1,
      2,
      (SELECT events FROM sync_fixture)
    );
    RAISE EXCEPTION 'expected conflicting replay bounds to fail';
  EXCEPTION WHEN unique_violation THEN
    NULL;
  END;

  BEGIN
    PERFORM public.ingest_brainhub_sync_batch(
      '20000000-0000-0000-0000-000000000001',
      '30000000-0000-0000-0000-000000000004',
      2,
      2,
      '[{"sequence":2,"event_id":"fixture-event-0002","event_type":"com.brainhub.graph.imported.v1","recorded_at":"2026-07-17T09:41:00Z","canonical_sha256":"0000000000000000000000000000000000000000000000000000000000000000","graph_payload":{"projector":"1.0.0","source_sequence":2,"nodes":[],"edges":[]}}]'::jsonb
    );
    RAISE EXCEPTION 'expected a forged event digest to fail';
  EXCEPTION WHEN invalid_parameter_value THEN
    NULL;
  END;

  BEGIN
    PERFORM public.ingest_brainhub_sync_batch(
      '20000000-0000-0000-0000-000000000001',
      '30000000-0000-0000-0000-000000000002',
      3,
      3,
      '[{"sequence":3,"event_id":"fixture-event-0003","event_type":"com.brainhub.graph.imported.v1","recorded_at":"2026-07-17T09:42:00Z","canonical_sha256":"0000000000000000000000000000000000000000000000000000000000000000","graph_payload":{"projector":"1.0.0","source_sequence":3,"nodes":[],"edges":[]}}]'::jsonb
    );
    RAISE EXCEPTION 'expected a cursor gap to fail';
  EXCEPTION WHEN invalid_parameter_value THEN
    NULL;
  END;

  IF public.brainhub_graph_payload_is_safe(
    '{"projector":"1.0.0","source_sequence":2,"nodes":[],"edges":[],"prompt":"private"}'::jsonb
  ) THEN
    RAISE EXCEPTION 'raw graph payload field passed policy check';
  END IF;
END
$$;

ROLLBACK;
