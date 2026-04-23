import { useEffect, useMemo, useRef } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";

import { listDiscoverEvents } from "../lib/api";
import { formatTime } from "../lib/format";
import { queryKeys } from "../lib/queryKeys";
import type { DiscoverEvent, DiscoverSourceEntry } from "../types";

const MARKET_CARDS = [
  {
    name: "S&P Futures",
    value: "5,713.25",
    change: "-0.52%",
    tone: "down",
    points: [18, 22, 16, 20, 17, 28, 23, 31, 34, 30, 32, 29, 25, 21, 18, 26],
  },
  {
    name: "NASDAQ Fut.",
    value: "20,498.75",
    change: "-1.23%",
    tone: "down",
    points: [28, 24, 27, 22, 30, 36, 32, 40, 38, 34, 31, 29, 25, 21, 18, 26],
  },
  {
    name: "Bitcoin",
    value: "67,421.58",
    change: "+0.58%",
    tone: "up",
    points: [18, 20, 21, 23, 24, 23, 27, 25, 30, 34, 31, 36, 32, 29, 25, 24],
  },
  {
    name: "VIX",
    value: "18.92",
    change: "-2.97%",
    tone: "up",
    points: [30, 32, 33, 35, 38, 36, 34, 32, 31, 30, 28, 26, 25, 23, 20, 18],
  },
  {
    name: "USD/JPY",
    value: "155.68",
    change: "+0.32%",
    tone: "down",
    points: [25, 24, 28, 26, 24, 22, 21, 24, 29, 31, 28, 25, 23, 21, 24, 22],
  },
  {
    name: "Gold",
    value: "2,389.16",
    change: "+0.74%",
    tone: "up",
    points: [20, 22, 24, 25, 27, 26, 29, 31, 33, 35, 34, 32, 30, 28, 27, 25],
  },
] as const;

function sourceInitial(source: DiscoverSourceEntry) {
  return (source.label || source.domain || source.url || "?").slice(0, 1).toUpperCase();
}

function sourceTitle(source: DiscoverSourceEntry) {
  return source.domain || source.label || source.url;
}

function NewsVisual({ event, index }: { event: DiscoverEvent; index: number }) {
  const category = (event.category || event.headline || "market")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-");

  return (
    <div
      className={`news-visual news-visual-${index % 6}`}
      aria-label={`${event.headline || "新闻"} 配图`}
      role="img"
    >
      <span>{event.category || "Market"}</span>
      <strong>{category.slice(0, 14) || "market"}</strong>
    </div>
  );
}

function SourceIconStack({ event }: { event: DiscoverEvent }) {
  const sources = event.sources.slice(0, 4);
  return (
    <div className="news-source-strip">
      <div className="source-icon-stack" aria-label="新闻来源">
        {sources.map((source) => (
          <a
            className="source-icon-link"
            href={source.url}
            key={`${event.event_id}-${source.url}`}
            rel="noreferrer"
            target="_blank"
            title={sourceTitle(source)}
            aria-label={`打开来源 ${sourceTitle(source)}`}
          >
            {source.favicon_url ? (
              <img alt="" src={source.favicon_url} />
            ) : (
              <span>{sourceInitial(source)}</span>
            )}
          </a>
        ))}
      </div>
      <span className="source-count">{event.source_count} 个来源</span>
    </div>
  );
}

function Sparkline({
  points,
  tone,
}: {
  points: readonly number[];
  tone: "up" | "down";
}) {
  const polyline = points
    .map((point, index) => {
      const x = (index / Math.max(points.length - 1, 1)) * 100;
      const y = 42 - point;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg className={`sparkline sparkline-${tone}`} viewBox="0 0 100 44" aria-hidden="true">
      <polyline points={polyline} />
    </svg>
  );
}

export function DiscoverPage() {
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const discoverQuery = useInfiniteQuery({
    queryKey: queryKeys.discover("global", ""),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      listDiscoverEvents({
        cursor: pageParam,
        limit: 12,
      }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });

  const events = useMemo(
    () => discoverQuery.data?.pages.flatMap((page) => page.items ?? []) ?? [],
    [discoverQuery.data?.pages],
  );
  const featuredEvent = events[0] ?? null;
  const secondaryEvents = events.slice(1);

  useEffect(() => {
    const target = sentinelRef.current;
    if (!target) {
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries[0]?.isIntersecting) {
        return;
      }
      if (!discoverQuery.hasNextPage || discoverQuery.isFetchingNextPage) {
        return;
      }
      void discoverQuery.fetchNextPage();
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [
    discoverQuery.fetchNextPage,
    discoverQuery.hasNextPage,
    discoverQuery.isFetchingNextPage,
  ]);

  return (
    <div className="discover-layout discover-news-layout">
      <section className="discover-feed discover-news-feed">
        {discoverQuery.error ? (
          <div className="error-state">
            {discoverQuery.error instanceof Error
              ? discoverQuery.error.message
              : "发现页加载失败"}
          </div>
        ) : null}

        {featuredEvent ? (
          <article className="news-card featured-news-card">
            <div className="featured-news-copy">
              <span className="news-kicker">焦点新闻</span>
              <h1>{featuredEvent.headline || "未命名事件"}</h1>
              <p>{featuredEvent.summary || "暂无摘要"}</p>
              <div className="featured-news-meta">
                <span>{formatTime(featuredEvent.updated_at || featuredEvent.published_at)}</span>
                <SourceIconStack event={featuredEvent} />
              </div>
            </div>
            <NewsVisual event={featuredEvent} index={0} />
          </article>
        ) : discoverQuery.isLoading ? (
          <div className="empty-state">正在加载新闻流...</div>
        ) : (
          <div className="empty-state">当前没有发现结果。</div>
        )}

        {secondaryEvents.length ? (
          <div className="news-grid">
            {secondaryEvents.map((event, index) => (
              <article className="news-card compact-news-card" key={event.event_id}>
                <NewsVisual event={event} index={index + 1} />
                <div className="compact-news-copy">
                  <h2>{event.headline || "未命名事件"}</h2>
                  <p>{event.summary || "暂无摘要"}</p>
                </div>
                <footer className="compact-news-footer">
                  <SourceIconStack event={event} />
                  <span className="news-action-icons" aria-hidden="true">
                    ♡ ···
                  </span>
                </footer>
              </article>
            ))}
          </div>
        ) : null}

        <div ref={sentinelRef} />
        {discoverQuery.hasNextPage ? (
          <div className="discover-loadmore">
            <button
              className="primary-button market-more-button"
              type="button"
              onClick={() => void discoverQuery.fetchNextPage()}
            >
              {discoverQuery.isFetchingNextPage ? "加载中..." : "加载更多"}
            </button>
          </div>
        ) : null}
      </section>

      <aside className="market-sidebar">
        <h2>市场行情</h2>
        <div className="market-card-list">
          {MARKET_CARDS.map((market) => (
            <article className="market-card" key={market.name}>
              <div className="market-card-header">
                <div>
                  <h3>{market.name}</h3>
                  <strong>{market.value}</strong>
                </div>
                <span className={`market-change market-change-${market.tone}`}>
                  {market.change}
                </span>
              </div>
              <Sparkline points={market.points} tone={market.tone} />
            </article>
          ))}
        </div>
        <button className="market-more-button" type="button">
          查看全部市场
          <span aria-hidden="true">›</span>
        </button>
      </aside>
    </div>
  );
}
