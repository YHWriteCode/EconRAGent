from __future__ import annotations

from lightrag_fork.constants import DEFAULT_SUMMARY_LANGUAGE
from lightrag_fork.schema import (
    DomainSchema,
    EntityTypeDefinition,
    RelationTypeDefinition,
)


ECONOMY_DOMAIN_SCHEMA = DomainSchema(
    domain_name="economy",
    profile_name="economy",
    enabled=True,
    mode="domain",
    language=DEFAULT_SUMMARY_LANGUAGE,
    description="Economy and finance oriented schema profile for extracting companies, policies, metrics, industries, and market events.",
    entity_types=[
        EntityTypeDefinition(
            name="Company",
            display_name="公司",
            description="Listed companies, private firms, issuers, and named enterprises.",
            aliases=["企业", "上市公司", "公司主体"],
        ),
        EntityTypeDefinition(
            name="Industry",
            display_name="行业",
            description="Sectors, subsectors, industrial chains, and market segments.",
            aliases=["产业", "赛道", "板块"],
        ),
        EntityTypeDefinition(
            name="Metric",
            display_name="指标",
            description="Financial and operating indicators such as revenue, margin, cash flow, valuation, and output.",
            aliases=["财务指标", "经营指标", "数据指标"],
        ),
        EntityTypeDefinition(
            name="Policy",
            display_name="政策",
            description="Fiscal, monetary, industrial, regulatory, and subsidy policies.",
            aliases=["政策工具", "监管政策", "产业政策"],
        ),
        EntityTypeDefinition(
            name="Event",
            display_name="事件",
            description="Macro events, market events, announcements, and time-bounded developments.",
            aliases=["市场事件", "公告事件", "经营事件"],
        ),
        EntityTypeDefinition(
            name="Asset",
            display_name="资产",
            description="Commodities, securities, currencies, bonds, and other tradable or reference assets.",
            aliases=["商品", "证券", "金融资产"],
        ),
        EntityTypeDefinition(
            name="Institution",
            display_name="机构",
            description="Banks, regulators, exchanges, funds, agencies, and policy institutions.",
            aliases=["金融机构", "监管机构", "政府机构"],
        ),
        EntityTypeDefinition(
            name="Country",
            display_name="国家",
            description="Countries, sovereign regions, and national economies.",
            aliases=["地区", "经济体"],
        ),
    ],
    relation_types=[
        RelationTypeDefinition(
            name="policy_supports",
            display_name="政策支持",
            description="A policy or institution supports a company, industry, asset, or economic activity.",
            source_types=["Policy", "Institution"],
            target_types=["Company", "Industry", "Asset", "Metric"],
            aliases=["扶持", "刺激", "支持"],
        ),
        RelationTypeDefinition(
            name="affects_metric",
            display_name="影响指标",
            description="An event, policy, asset, or institution changes a financial or operational metric.",
            source_types=["Event", "Policy", "Asset", "Institution"],
            target_types=["Metric"],
            aliases=["影响", "驱动", "压制", "改善"],
        ),
        RelationTypeDefinition(
            name="belongs_to_industry",
            display_name="所属行业",
            description="A company or institution belongs to or primarily operates in an industry.",
            source_types=["Company", "Institution"],
            target_types=["Industry"],
            aliases=["属于", "布局于", "深耕"],
        ),
        RelationTypeDefinition(
            name="operates_in_country",
            display_name="所在国家",
            description="A company, institution, or industry activity operates in or is associated with a country.",
            source_types=["Company", "Institution", "Industry"],
            target_types=["Country"],
            aliases=["位于", "面向", "覆盖"],
        ),
    ],
    aliases={
        "公司": "Company",
        "行业": "Industry",
        "指标": "Metric",
        "政策": "Policy",
        "事件": "Event",
        "资产": "Asset",
        "机构": "Institution",
        "国家": "Country",
    },
    extraction_rules=[
        "Prefer economically meaningful entities over generic nouns.",
        "When the text does not provide a proper company name, allow a normalized descriptive placeholder such as 'A battery materials company'.",
        "Prefer explicit policy, institution, metric, and industry relations when they are directly supported by the text.",
    ],
    metadata={"builtin": True, "domain": "economy"},
)
