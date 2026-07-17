BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'brainhub') THEN
    PERFORM ag_catalog.create_graph('brainhub');
  END IF;
END
$$;

CREATE TABLE public.tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  disabled_at timestamptz
);

CREATE TABLE public.installations (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  public_label text NOT NULL,
  last_cursor bigint NOT NULL DEFAULT 0 CHECK (last_cursor >= 0),
  last_seen_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (tenant_id, id)
);

CREATE TABLE public.sync_batches (
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  installation_id uuid NOT NULL,
  batch_id uuid NOT NULL,
  first_sequence bigint NOT NULL CHECK (first_sequence > 0),
  last_sequence bigint NOT NULL CHECK (last_sequence >= first_sequence),
  canonical_sha256 bytea NOT NULL CHECK (octet_length(canonical_sha256) = 32),
  accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, installation_id, batch_id),
  FOREIGN KEY (tenant_id, installation_id)
    REFERENCES public.installations(tenant_id, id) ON DELETE CASCADE
);

-- Defense in depth for the graph-only sync boundary. The API must validate the
-- published JSON Schema first; this database check prevents accidental raw
-- capture fields from entering the durable cloud ledger even if that layer is
-- bypassed.
CREATE FUNCTION public.jsonb_has_forbidden_brainhub_key(value jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
AS $$
DECLARE
  member record;
  normalized_key text;
BEGIN
  IF jsonb_typeof(value) = 'object' THEN
    FOR member IN
      SELECT entry.object_key, entry.object_value
      FROM jsonb_each(value) AS entry(object_key, object_value)
    LOOP
      normalized_key := lower(
        replace(replace(member.object_key, '-', '_'), ' ', '_')
      );
      IF normalized_key = ANY (ARRAY[
        'prompt', 'prompts', 'transcript', 'transcripts', 'messages',
        'assistant_message', 'assistant_text', 'user_message', 'tool_input',
        'tool_output', 'tool_result', 'source_code', 'file_content', 'raw_content',
        'content', 'chain_of_thought', 'chainofthought', 'hidden_reasoning',
        'internal_reasoning', 'reasoning_trace', 'credentials', 'credential',
        'password', 'passwd', 'secret', 'api_key', 'apikey', 'access_token',
        'refresh_token', 'authorization', 'private_key'
      ]) THEN
        RETURN true;
      END IF;
      IF public.jsonb_has_forbidden_brainhub_key(member.object_value) THEN
        RETURN true;
      END IF;
    END LOOP;
  ELSIF jsonb_typeof(value) = 'array' THEN
    FOR member IN
      SELECT entry.array_value
      FROM jsonb_array_elements(value) AS entry(array_value)
    LOOP
      IF public.jsonb_has_forbidden_brainhub_key(member.array_value) THEN
        RETURN true;
      END IF;
    END LOOP;
  END IF;
  RETURN false;
END
$$;

CREATE FUNCTION public.brainhub_evidence_array_is_safe(value jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
AS $$
DECLARE
  member jsonb;
BEGIN
  IF jsonb_typeof(value) <> 'array' OR jsonb_array_length(value) > 100 THEN
    RETURN false;
  END IF;
  FOR member IN SELECT item FROM jsonb_array_elements(value) AS evidence(item)
  LOOP
    IF jsonb_typeof(member) <> 'object'
      OR NOT member ?& ARRAY[
        'evidence_id', 'source_event_id', 'opaque_uri', 'anchor',
        'content_hash', 'visibility'
      ]
      OR (member - ARRAY[
        'evidence_id', 'source_event_id', 'opaque_uri', 'anchor',
        'content_hash', 'visibility'
      ]) <> '{}'::jsonb
      OR member ->> 'visibility' <> 'SYNCABLE'
    THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN true;
END
$$;

CREATE FUNCTION public.brainhub_graph_payload_is_safe(payload jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
AS $$
DECLARE
  member jsonb;
BEGIN
  IF jsonb_typeof(payload) <> 'object'
    OR NOT payload ?& ARRAY['projector', 'source_sequence', 'nodes', 'edges']
    OR (payload - ARRAY['projector', 'source_sequence', 'nodes', 'edges']) <> '{}'::jsonb
    OR jsonb_typeof(payload -> 'nodes') <> 'array'
    OR jsonb_typeof(payload -> 'edges') <> 'array'
    OR pg_column_size(payload) > 50 * 1024 * 1024
    OR public.jsonb_has_forbidden_brainhub_key(payload)
  THEN
    RETURN false;
  END IF;
  IF jsonb_array_length(payload -> 'nodes') > 100000
    OR jsonb_array_length(payload -> 'edges') > 1000000
  THEN
    RETURN false;
  END IF;

  FOR member IN SELECT item FROM jsonb_array_elements(payload -> 'nodes') AS node(item)
  LOOP
    IF jsonb_typeof(member) <> 'object'
      OR NOT member ?& ARRAY[
        'id', 'type', 'title', 'summary', 'sensitivity', 'review_state',
        'valid_from', 'valid_to', 'recorded_from', 'recorded_to',
        'source_event_id', 'actor_id', 'extractor', 'extractor_version',
        'external_ids', 'content_hash', 'evidence'
      ]
      OR (member - ARRAY[
        'id', 'type', 'title', 'summary', 'sensitivity', 'review_state',
        'valid_from', 'valid_to', 'recorded_from', 'recorded_to',
        'source_event_id', 'actor_id', 'extractor', 'extractor_version',
        'external_ids', 'content_hash', 'evidence'
      ]) <> '{}'::jsonb
      OR jsonb_typeof(member -> 'external_ids') <> 'array'
      OR jsonb_array_length(member -> 'external_ids') > 100
      OR NOT public.brainhub_evidence_array_is_safe(member -> 'evidence')
    THEN
      RETURN false;
    END IF;
  END LOOP;

  FOR member IN SELECT item FROM jsonb_array_elements(payload -> 'edges') AS edge(item)
  LOOP
    IF jsonb_typeof(member) <> 'object'
      OR NOT member ?& ARRAY[
        'id', 'source_id', 'target_id', 'relation', 'explanation',
        'confidence_class', 'confidence_score', 'sensitivity', 'review_state',
        'valid_from', 'valid_to', 'recorded_from', 'recorded_to',
        'source_event_id', 'actor_id', 'extractor', 'extractor_version', 'evidence'
      ]
      OR (member - ARRAY[
        'id', 'source_id', 'target_id', 'relation', 'explanation',
        'confidence_class', 'confidence_score', 'sensitivity', 'review_state',
        'valid_from', 'valid_to', 'recorded_from', 'recorded_to',
        'source_event_id', 'actor_id', 'extractor', 'extractor_version', 'evidence'
      ]) <> '{}'::jsonb
      OR NOT public.brainhub_evidence_array_is_safe(member -> 'evidence')
    THEN
      RETURN false;
    END IF;
  END LOOP;
  RETURN true;
END
$$;

-- Canonical JSON used by the Python producer: sorted object keys, original array
-- order, UTF-8, and no insignificant whitespace. Recomputing this server-side
-- means a client cannot bind an arbitrary digest to different graph facts.
CREATE FUNCTION public.brainhub_canonical_jsonb(value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
AS $$
DECLARE
  rendered text;
BEGIN
  CASE jsonb_typeof(value)
    WHEN 'object' THEN
      SELECT COALESCE(
        '{' || string_agg(
          to_json(member.key)::text || ':' || public.brainhub_canonical_jsonb(member.value),
          ',' ORDER BY member.key
        ) || '}',
        '{}'
      ) INTO rendered
      FROM jsonb_each(value) AS member(key, value);
    WHEN 'array' THEN
      SELECT COALESCE(
        '[' || string_agg(
          public.brainhub_canonical_jsonb(member.value),
          ',' ORDER BY member.ordinal
        ) || ']',
        '[]'
      ) INTO rendered
      FROM jsonb_array_elements(value) WITH ORDINALITY AS member(value, ordinal);
    ELSE
      rendered := value::text;
  END CASE;
  RETURN rendered;
END
$$;

CREATE FUNCTION public.brainhub_graph_payload_sha256(payload jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN encode(
  digest(convert_to(public.brainhub_canonical_jsonb(payload), 'UTF8'), 'sha256'),
  'hex'
);

CREATE TABLE public.graph_events (
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  installation_id uuid NOT NULL,
  local_sequence bigint NOT NULL CHECK (local_sequence > 0),
  event_id text NOT NULL,
  event_type text NOT NULL,
  recorded_at timestamptz NOT NULL,
  canonical_sha256 bytea NOT NULL CHECK (octet_length(canonical_sha256) = 32),
  graph_payload jsonb NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, installation_id, local_sequence),
  UNIQUE (tenant_id, installation_id, event_id),
  FOREIGN KEY (tenant_id, installation_id)
    REFERENCES public.installations(tenant_id, id) ON DELETE CASCADE,
  CHECK (public.brainhub_graph_payload_is_safe(graph_payload))
);

CREATE TABLE public.nodes (
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  node_id text NOT NULL,
  node_type text NOT NULL CHECK (node_type IN (
    'WORKSTREAM', 'RUN', 'TOPIC', 'TASK', 'DECISION', 'ARTIFACT', 'CLAIM', 'ACTOR', 'WORKSPACE'
  )),
  title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
  summary text NOT NULL DEFAULT '' CHECK (char_length(summary) <= 4000),
  sensitivity text NOT NULL DEFAULT 'INTERNAL' CHECK (sensitivity IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
  review_state text NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_state IN ('UNREVIEWED', 'ACCEPTED', 'REJECTED', 'NEEDS_REVIEW')),
  valid_from timestamptz,
  valid_to timestamptz,
  recorded_from timestamptz NOT NULL,
  recorded_to timestamptz,
  source_installation_id uuid NOT NULL,
  source_event_id text NOT NULL,
  actor_id text,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  external_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  content_hash text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (tenant_id, node_id),
  FOREIGN KEY (tenant_id, source_installation_id, source_event_id)
    REFERENCES public.graph_events(tenant_id, installation_id, event_id) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(external_ids) = 'array'),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
  CHECK (recorded_to IS NULL OR recorded_to > recorded_from)
);

CREATE INDEX nodes_tenant_type_idx ON public.nodes (tenant_id, node_type);
CREATE INDEX nodes_tenant_valid_idx ON public.nodes (tenant_id, valid_from, valid_to);
CREATE INDEX nodes_external_ids_gin ON public.nodes USING gin (external_ids);

CREATE TABLE public.edges (
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  edge_id text NOT NULL,
  source_id text NOT NULL,
  target_id text NOT NULL,
  relation text NOT NULL CHECK (relation IN (
    'HAS_RUN', 'ABOUT', 'PRODUCED', 'USED', 'MODIFIES', 'DEPENDS_ON', 'BLOCKS',
    'DECIDED_IN', 'DERIVED_FROM', 'REFERENCES', 'VERIFIES', 'CONTRADICTS',
    'SUPERSEDES', 'CONTINUES', 'ASSERTED_BY', 'PARTICIPATES_IN'
  )),
  explanation text NOT NULL CHECK (char_length(explanation) BETWEEN 1 AND 320),
  confidence_class text NOT NULL CHECK (confidence_class IN ('EXTRACTED', 'INFERRED', 'AMBIGUOUS')),
  confidence_score double precision NOT NULL CHECK (confidence_score BETWEEN 0 AND 1),
  sensitivity text NOT NULL DEFAULT 'INTERNAL' CHECK (sensitivity IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
  review_state text NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_state IN ('UNREVIEWED', 'ACCEPTED', 'REJECTED', 'NEEDS_REVIEW')),
  valid_from timestamptz,
  valid_to timestamptz,
  recorded_from timestamptz NOT NULL,
  recorded_to timestamptz,
  source_installation_id uuid NOT NULL,
  source_event_id text NOT NULL,
  actor_id text,
  extractor text NOT NULL,
  extractor_version text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (tenant_id, edge_id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES public.nodes(tenant_id, node_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, target_id) REFERENCES public.nodes(tenant_id, node_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, source_installation_id, source_event_id)
    REFERENCES public.graph_events(tenant_id, installation_id, event_id) ON DELETE RESTRICT,
  CHECK (source_id <> target_id OR relation IN ('REFERENCES', 'SUPERSEDES')),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
  CHECK (recorded_to IS NULL OR recorded_to > recorded_from)
);

CREATE INDEX edges_tenant_source_idx ON public.edges (tenant_id, source_id, relation);
CREATE INDEX edges_tenant_target_idx ON public.edges (tenant_id, target_id, relation);
CREATE INDEX edges_tenant_valid_idx ON public.edges (tenant_id, valid_from, valid_to);

CREATE TABLE public.evidence_refs (
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  evidence_id text NOT NULL,
  edge_id text,
  node_id text,
  source_installation_id uuid NOT NULL,
  source_event_id text NOT NULL,
  opaque_uri text NOT NULL,
  anchor jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_hash text,
  visibility text NOT NULL DEFAULT 'SYNCABLE' CHECK (visibility = 'SYNCABLE'),
  PRIMARY KEY (tenant_id, evidence_id),
  FOREIGN KEY (tenant_id, edge_id) REFERENCES public.edges(tenant_id, edge_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, node_id) REFERENCES public.nodes(tenant_id, node_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, source_installation_id, source_event_id)
    REFERENCES public.graph_events(tenant_id, installation_id, event_id) ON DELETE RESTRICT,
  CHECK ((edge_id IS NOT NULL)::integer + (node_id IS NOT NULL)::integer = 1)
);

-- Current rows make ordinary graph reads cheap. These append-only revision rows
-- preserve bitemporal correction history without copying local raw content.
CREATE TABLE public.node_revisions (
  tenant_id uuid NOT NULL,
  node_id text NOT NULL,
  recorded_from timestamptz NOT NULL,
  recorded_to timestamptz,
  source_installation_id uuid NOT NULL,
  source_event_id text NOT NULL,
  canonical_sha256 bytea NOT NULL CHECK (octet_length(canonical_sha256) = 32),
  assertion jsonb NOT NULL CHECK (jsonb_typeof(assertion) = 'object'),
  PRIMARY KEY (tenant_id, node_id, recorded_from),
  FOREIGN KEY (tenant_id, node_id)
    REFERENCES public.nodes(tenant_id, node_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, source_installation_id, source_event_id)
    REFERENCES public.graph_events(tenant_id, installation_id, event_id) ON DELETE RESTRICT,
  CHECK (recorded_to IS NULL OR recorded_to > recorded_from)
);

CREATE TABLE public.edge_revisions (
  tenant_id uuid NOT NULL,
  edge_id text NOT NULL,
  recorded_from timestamptz NOT NULL,
  recorded_to timestamptz,
  source_installation_id uuid NOT NULL,
  source_event_id text NOT NULL,
  canonical_sha256 bytea NOT NULL CHECK (octet_length(canonical_sha256) = 32),
  assertion jsonb NOT NULL CHECK (jsonb_typeof(assertion) = 'object'),
  PRIMARY KEY (tenant_id, edge_id, recorded_from),
  FOREIGN KEY (tenant_id, edge_id)
    REFERENCES public.edges(tenant_id, edge_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, source_installation_id, source_event_id)
    REFERENCES public.graph_events(tenant_id, installation_id, event_id) ON DELETE RESTRICT,
  CHECK (recorded_to IS NULL OR recorded_to > recorded_from)
);

CREATE TABLE public.audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
  actor_subject text NOT NULL,
  action text NOT NULL,
  object_type text NOT NULL,
  object_id text,
  outcome text NOT NULL CHECK (outcome IN ('ALLOWED', 'DENIED', 'ERROR')),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX audit_tenant_time_idx ON public.audit_log (tenant_id, occurred_at DESC);

CREATE FUNCTION public.current_brainhub_tenant() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE
RETURN NULLIF(current_setting('brainhub.tenant_id', true), '')::uuid;

ALTER TABLE public.installations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.graph_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evidence_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.node_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.edge_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.installations FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sync_batches FORCE ROW LEVEL SECURITY;
ALTER TABLE public.graph_events FORCE ROW LEVEL SECURITY;
ALTER TABLE public.nodes FORCE ROW LEVEL SECURITY;
ALTER TABLE public.edges FORCE ROW LEVEL SECURITY;
ALTER TABLE public.evidence_refs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.node_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.edge_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.audit_log FORCE ROW LEVEL SECURITY;

CREATE POLICY installations_tenant ON public.installations
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY sync_batches_tenant ON public.sync_batches
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY graph_events_tenant ON public.graph_events
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY nodes_tenant ON public.nodes
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY edges_tenant ON public.edges
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY evidence_tenant ON public.evidence_refs
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY node_revisions_tenant ON public.node_revisions
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY edge_revisions_tenant ON public.edge_revisions
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());
CREATE POLICY audit_tenant ON public.audit_log
  USING (tenant_id = public.current_brainhub_tenant())
  WITH CHECK (tenant_id = public.current_brainhub_tenant());

COMMIT;
