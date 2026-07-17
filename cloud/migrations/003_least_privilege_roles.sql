BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brainhub_sync_ingest') THEN
    CREATE ROLE brainhub_sync_ingest NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brainhub_projection_worker') THEN
    CREATE ROLE brainhub_projection_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brainhub_graph_reader') THEN
    CREATE ROLE brainhub_graph_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;

REVOKE ALL ON FUNCTION public.current_brainhub_tenant() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.jsonb_has_forbidden_brainhub_key(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.brainhub_evidence_array_is_safe(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.brainhub_graph_payload_is_safe(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.brainhub_canonical_jsonb(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.brainhub_graph_payload_sha256(jsonb) FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO
  brainhub_sync_ingest, brainhub_projection_worker, brainhub_graph_reader;

GRANT SELECT, UPDATE ON public.installations TO brainhub_sync_ingest;
GRANT SELECT, INSERT ON public.sync_batches TO brainhub_sync_ingest;
GRANT SELECT, INSERT ON public.graph_events TO brainhub_sync_ingest;
GRANT EXECUTE ON FUNCTION public.current_brainhub_tenant() TO
  brainhub_sync_ingest, brainhub_projection_worker, brainhub_graph_reader;
GRANT EXECUTE ON FUNCTION public.jsonb_has_forbidden_brainhub_key(jsonb) TO
  brainhub_sync_ingest, brainhub_projection_worker;
GRANT EXECUTE ON FUNCTION public.brainhub_evidence_array_is_safe(jsonb) TO
  brainhub_sync_ingest, brainhub_projection_worker;
GRANT EXECUTE ON FUNCTION public.brainhub_graph_payload_is_safe(jsonb) TO
  brainhub_sync_ingest, brainhub_projection_worker;
GRANT EXECUTE ON FUNCTION public.brainhub_canonical_jsonb(jsonb) TO
  brainhub_sync_ingest, brainhub_projection_worker;
GRANT EXECUTE ON FUNCTION public.brainhub_graph_payload_sha256(jsonb) TO
  brainhub_sync_ingest, brainhub_projection_worker;
GRANT EXECUTE ON FUNCTION public.ingest_brainhub_sync_batch(
  uuid, uuid, bigint, bigint, jsonb
) TO brainhub_sync_ingest;

GRANT SELECT ON public.graph_events TO brainhub_projection_worker;
GRANT SELECT, INSERT, UPDATE ON
  public.nodes, public.edges, public.evidence_refs,
  public.node_revisions, public.edge_revisions
TO brainhub_projection_worker;

GRANT SELECT ON
  public.nodes, public.edges, public.evidence_refs,
  public.node_revisions, public.edge_revisions
TO brainhub_graph_reader;

-- AGE is intentionally absent from all grants. Until tenant-scoped
-- security-definer query functions exist, no application role can run Cypher.

COMMIT;
