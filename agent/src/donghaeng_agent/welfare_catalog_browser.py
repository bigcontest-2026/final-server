"""One read-only SQL envelope for a browser page and its source-backed details.

The prefix and filter clauses come only from the catalog's trusted query builder.
User values remain bound parameters in the prefix/filters, followed by limit/offset.
"""


def browser_page_query(catalog_sql, corpus, filters, *, explicit=False):
    if corpus not in ('corpus', 'ranked_corpus'):
        raise ValueError('invalid catalog relation')
    order = ('c.region_match_rank,' if explicit else '') + 'c.title,c.program_version_id'
    return catalog_sql + f''',
    filtered AS NOT MATERIALIZED (SELECT c.* FROM {corpus} c {filters}),
    page_rows AS MATERIALIZED (
        SELECT * FROM filtered c ORDER BY {order} LIMIT %s OFFSET %s
    )
    SELECT
      (SELECT count(*) FROM corpus) AS total,
      (SELECT count(*) FROM filtered) AS matched,
      (SELECT COALESCE(jsonb_agg(to_jsonb(c) ORDER BY {order}), '[]'::jsonb)
        FROM page_rows c) AS rows,
      (SELECT COALESCE(jsonb_agg(to_jsonb(child)
          ORDER BY child.category_name,child.category_code), '[]'::jsonb)
        FROM (
          SELECT t.program_version_id,t.category_code,k.category_name,k.description,t.assignment_method,
            t.review_status,t.rule_version,t.evidence_id,e.source_quote
          FROM program_tags t JOIN categories k USING(category_code)
          LEFT JOIN evidence e ON e.evidence_id=t.evidence_id
            AND e.program_version_id=t.program_version_id AND e.evidence_kind='category_basis'
          WHERE t.program_version_id IN (SELECT program_version_id FROM page_rows)
            AND t.review_status<>'rejected'
        ) child) AS tags,
      (SELECT COALESCE(jsonb_agg(to_jsonb(child)-'_order_id'
          ORDER BY child.scope_type,child.region_code,child._order_id), '[]'::jsonb)
        FROM (
          SELECT p.program_version_id,p.scope_type,p.region_id,p.scope_purpose,p.mapping_status,
            p.original_region_text,r.region_code,r.region_name,r.code_system,
            p.program_region_id AS _order_id
          FROM program_regions p LEFT JOIN administrative_regions r USING(region_id)
          WHERE p.program_version_id IN (SELECT program_version_id FROM page_rows)
        ) child) AS scopes,
      (SELECT COALESCE(jsonb_agg(to_jsonb(child)-'_order_id'
          ORDER BY child.condition_type,child._order_id), '[]'::jsonb)
        FROM (
          SELECT r.program_version_id,r.condition_type,r.operator,r.condition_value,r.logic_group,
            r.review_status,r.evidence_id,e.source_quote,r.rule_id AS _order_id
          FROM eligibility_rules r
          LEFT JOIN evidence e ON e.evidence_id=r.evidence_id
            AND e.program_version_id=r.program_version_id AND e.evidence_kind='eligibility_basis'
          WHERE r.program_version_id IN (SELECT program_version_id FROM page_rows)
        ) child) AS rules,
      (SELECT COALESCE(jsonb_agg(to_jsonb(child) ORDER BY child.evidence_id), '[]'::jsonb)
        FROM (
          SELECT program_version_id,evidence_id,source_quote FROM evidence
          WHERE program_version_id IN (SELECT program_version_id FROM page_rows)
            AND evidence_kind='official_link'
        ) child) AS links,
      (SELECT COALESCE(jsonb_agg(to_jsonb(child) ORDER BY child.program_version_id), '[]'::jsonb)
        FROM (
          SELECT vd.program_version_id,max(d.retrieved_at) AS source_retrieved_at
          FROM program_version_documents vd
          JOIN program_versions v ON v.program_version_id=vd.program_version_id
          JOIN programs p ON p.program_id=v.program_id
          JOIN source_documents d ON d.document_id=vd.document_id AND d.source_id=p.source_id
          WHERE vd.program_version_id IN (SELECT program_version_id FROM page_rows)
          GROUP BY vd.program_version_id
        ) child) AS documents,
      (SELECT COALESCE(jsonb_agg(to_jsonb(child) ORDER BY child.category_id), '[]'::jsonb)
        FROM (
          SELECT DISTINCT t.category_code AS category_id,k.category_name AS label
          FROM corpus c JOIN program_tags t USING(program_version_id)
          JOIN categories k USING(category_code) WHERE t.review_status<>'rejected'
        ) child) AS categories'''
