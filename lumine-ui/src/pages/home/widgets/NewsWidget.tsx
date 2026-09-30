import { useEffect, useState } from "react";
import { WidgetCard, WidgetEmpty, WidgetPending, openExternal } from "./kit";
import { Icon } from "../components/Icon";

/**
 * A short list of things people are reading, from Hacker News' front page.
 *
 * Not because it is the best news, but because it is the one that answers from
 * inside the app. Google News — which is what the agent's `get_news` tool uses —
 * returns no `Access-Control-Allow-Origin` header at all, so a browser cannot
 * read its RSS even though the desktop shell has no reason to care; Reddit
 * answers 403 to anything that is not a browser it likes; every general-news API
 * wants a key, and a key cannot go in the bundle. Algolia echoes the Origin back
 * and asks for nothing, which is the whole of the selection.
 *
 * The card says whose front page it is rather than calling itself "News". A
 * source that names itself is a source you can discount.
 */
const FEED_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=6";
const FALLBACK = "https://news.ycombinator.com";

type Story = {
  objectID: string;
  title: string | null;
  url: string | null;
  points: number | null;
  num_comments: number | null;
};

export function NewsWidget() {
  const [stories, setStories] = useState<Story[] | null>(null);
  const [failed, setFailed] = useState(false);
  /* The retry, and it is a counter rather than a callback inside the effect
     because the effect owns an `AbortController` it tears down on cleanup.
     Bumping this value *is* the refetch: the run ends, the controller aborts,
     the effect re-enters with a fresh one. Telling a reader "try again by
     reloading" was asking them to restart an application to reissue one GET. */
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const response = await fetch(FEED_URL, { signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        const data = (await response.json()) as { hits?: Story[] };
        // A story with no title is a job posting or a deleted thread, and a row
        // that reads as blank is worse than one fewer row.
        setStories((data.hits ?? []).filter((story) => story.title).slice(0, 6));
      } catch (error) {
        if (controller.signal.aborted) return;
        setStories(null);
        setFailed(true);
      }
    })();
    return () => controller.abort();
  }, [attempt]);

  const retry = () => {
    setFailed(false);
    setStories(null);
    setAttempt((value) => value + 1);
  };

  return (
    <WidgetCard title="Front page" meta="Hacker News" tone="widget-news">
      {stories === null && !failed && <WidgetPending label="Fetching the front page…" />}

      {failed && (
        <WidgetEmpty
          label="Nothing came back"
          detail="The feed may be having a moment. Trying again does not reload anything else."
        >
          <div className="widget-row flex items-center justify-between gap-2">
            <button type="button" className="widget-button primary" onClick={retry}>
              <Icon name="reset" size={14} />
              Try again
            </button>
          </div>
        </WidgetEmpty>
      )}

      {stories && stories.length === 0 && <WidgetEmpty label="No headlines" />}

      {stories && stories.length > 0 && (
        <ol className="widget-headlines flex shrink-0 flex-col m-0 p-0 list-none">
          {stories.map((story) => (
            <li key={story.objectID}>
              <button
                type="button"
                onClick={() => openExternal(story.url ?? `${FALLBACK}/item?id=${story.objectID}`)}
                title={story.title ?? ""}
              >
                <span className="widget-headline-title">{story.title}</span>
                <span className="widget-headline-meta text-faint text-[10.5px] tabular-nums">
                  {story.points ?? 0} pts
                  <span aria-hidden="true"> · </span>
                  {story.num_comments ?? 0} comments
                </span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </WidgetCard>
  );
}
