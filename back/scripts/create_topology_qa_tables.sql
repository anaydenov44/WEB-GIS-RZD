CREATE EXTENSION IF NOT EXISTS postgis;

CREATE INDEX IF NOT EXISTS idx_rail_graph_dangling_nodes_geom
ON rail_graph_dangling_nodes USING GIST (geom);

CREATE TABLE IF NOT EXISTS rail_graph_gap_candidates (
    id BIGSERIAL PRIMARY KEY,
    audit_run_id UUID NOT NULL,
    scope_key TEXT NOT NULL,

    source_node_hash TEXT NOT NULL,
    target_node_hash TEXT NOT NULL,
    source_component_id BIGINT NOT NULL,
    target_component_id BIGINT NOT NULL,

    distance_m DOUBLE PRECISION NOT NULL,
    connector_type TEXT NOT NULL DEFAULT 'endpoint_snap',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'candidate',

    geom geometry(LineString, 4326) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (distance_m >= 0),
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (status IN ('candidate', 'accepted', 'rejected', 'created'))
);

CREATE INDEX IF NOT EXISTS idx_rail_graph_gap_candidates_scope
ON rail_graph_gap_candidates (scope_key);

CREATE INDEX IF NOT EXISTS idx_rail_graph_gap_candidates_audit
ON rail_graph_gap_candidates (audit_run_id);

CREATE INDEX IF NOT EXISTS idx_rail_graph_gap_candidates_status
ON rail_graph_gap_candidates (status);

CREATE INDEX IF NOT EXISTS idx_rail_graph_gap_candidates_distance
ON rail_graph_gap_candidates (distance_m);

CREATE INDEX IF NOT EXISTS idx_rail_graph_gap_candidates_geom
ON rail_graph_gap_candidates USING GIST (geom);

CREATE TABLE IF NOT EXISTS rail_graph_connectors (
    id BIGSERIAL PRIMARY KEY,
    source_gap_candidate_id BIGINT REFERENCES rail_graph_gap_candidates(id) ON DELETE SET NULL,
    scope_key TEXT NOT NULL,

    source_node_hash TEXT NOT NULL,
    target_node_hash TEXT NOT NULL,

    length_km DOUBLE PRECISION NOT NULL,
    cost_multiplier DOUBLE PRECISION NOT NULL DEFAULT 8.0,
    connector_type TEXT NOT NULL DEFAULT 'endpoint_snap',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,

    geom geometry(LineString, 4326) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (length_km >= 0),
    CHECK (cost_multiplier >= 1),
    CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX IF NOT EXISTS idx_rail_graph_connectors_scope
ON rail_graph_connectors (scope_key);

CREATE INDEX IF NOT EXISTS idx_rail_graph_connectors_nodes
ON rail_graph_connectors (source_node_hash, target_node_hash);

CREATE INDEX IF NOT EXISTS idx_rail_graph_connectors_enabled
ON rail_graph_connectors (enabled);

CREATE INDEX IF NOT EXISTS idx_rail_graph_connectors_geom
ON rail_graph_connectors USING GIST (geom);