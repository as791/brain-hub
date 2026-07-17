BEGIN;

-- Ingest one already-schema-validated sync batch. The caller must set
-- `SET LOCAL brainhub.tenant_id` after authenticating the installation. A row
-- lock makes cursor advancement, idempotency, event writes, and the batch audit
-- record one atomic operation.
CREATE FUNCTION public.ingest_brainhub_sync_batch(
  p_installation_id uuid,
  p_batch_id uuid,
  p_first_sequence bigint,
  p_last_sequence bigint,
  p_events jsonb
) RETURNS bigint
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  tenant uuid := public.current_brainhub_tenant();
  current_cursor bigint;
  existing_hash bytea;
  existing_first_sequence bigint;
  existing_last_sequence bigint;
  batch_hash bytea;
  event_count bigint;
BEGIN
  IF tenant IS NULL THEN
    RAISE EXCEPTION 'brainhub.tenant_id must be set for this transaction'
      USING ERRCODE = '42501';
  END IF;
  IF jsonb_typeof(p_events) <> 'array' THEN
    RAISE EXCEPTION 'events must be a JSON array' USING ERRCODE = '22023';
  END IF;
  IF p_first_sequence < 1 OR p_last_sequence < p_first_sequence THEN
    RAISE EXCEPTION 'invalid sync sequence bounds' USING ERRCODE = '22023';
  END IF;

  SELECT last_cursor
    INTO current_cursor
    FROM public.installations
   WHERE tenant_id = tenant AND id = p_installation_id
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'installation is not registered for this tenant'
      USING ERRCODE = '42501';
  END IF;

  batch_hash := digest(convert_to(p_events::text, 'UTF8'), 'sha256');
  SELECT canonical_sha256, first_sequence, last_sequence
    INTO existing_hash, existing_first_sequence, existing_last_sequence
    FROM public.sync_batches
   WHERE tenant_id = tenant
     AND installation_id = p_installation_id
     AND batch_id = p_batch_id;
  IF FOUND THEN
    IF existing_hash <> batch_hash
      OR existing_first_sequence <> p_first_sequence
      OR existing_last_sequence <> p_last_sequence
    THEN
      RAISE EXCEPTION 'batch id was reused with different content or sequence bounds'
        USING ERRCODE = '23505';
    END IF;
    RETURN p_last_sequence;
  END IF;

  IF p_first_sequence <> current_cursor + 1 THEN
    RAISE EXCEPTION 'sync batch must begin at cursor + 1 (expected %, got %)',
      current_cursor + 1, p_first_sequence USING ERRCODE = '22023';
  END IF;
  event_count := jsonb_array_length(p_events);
  IF event_count <> p_last_sequence - p_first_sequence + 1 OR event_count > 500 THEN
    RAISE EXCEPTION 'sync event count does not match bounds or exceeds 500'
      USING ERRCODE = '22023';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM jsonb_array_elements(p_events) WITH ORDINALITY AS item(event, ordinal)
     WHERE (item.event ->> 'sequence')::bigint <> p_first_sequence + item.ordinal - 1
        OR NOT public.brainhub_graph_payload_is_safe(item.event -> 'graph_payload')
        OR COALESCE((item.event ->> 'canonical_sha256') !~ '^[a-f0-9]{64}$', true)
        OR (item.event ->> 'canonical_sha256') IS DISTINCT FROM
          public.brainhub_graph_payload_sha256(item.event -> 'graph_payload')
  ) THEN
    RAISE EXCEPTION 'sync events are non-contiguous or violate the graph-only policy'
      USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.sync_batches(
    tenant_id, installation_id, batch_id, first_sequence, last_sequence,
    canonical_sha256
  ) VALUES (
    tenant, p_installation_id, p_batch_id, p_first_sequence, p_last_sequence,
    batch_hash
  );

  INSERT INTO public.graph_events(
    tenant_id, installation_id, local_sequence, event_id, event_type,
    recorded_at, canonical_sha256, graph_payload
  )
  SELECT
    tenant,
    p_installation_id,
    (item.event ->> 'sequence')::bigint,
    item.event ->> 'event_id',
    item.event ->> 'event_type',
    (item.event ->> 'recorded_at')::timestamptz,
    decode(public.brainhub_graph_payload_sha256(item.event -> 'graph_payload'), 'hex'),
    item.event -> 'graph_payload'
  FROM jsonb_array_elements(p_events) AS item(event);

  UPDATE public.installations
     SET last_cursor = p_last_sequence,
         last_seen_at = clock_timestamp()
   WHERE tenant_id = tenant AND id = p_installation_id;

  RETURN p_last_sequence;
END
$$;

REVOKE ALL ON FUNCTION public.ingest_brainhub_sync_batch(
  uuid, uuid, bigint, bigint, jsonb
) FROM PUBLIC;

COMMIT;
