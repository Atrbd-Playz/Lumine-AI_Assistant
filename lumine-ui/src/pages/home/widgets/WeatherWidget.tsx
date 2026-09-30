import { useEffect, useState } from "react";
import { Icon } from "../components/Icon";
import type { IconName } from "../types";
import { WidgetCard, WidgetPending, openExternal, usePersistentState } from "./kit";

/**
 * Weather, from Open-Meteo.
 *
 * Both endpoints are keyless and both answer `Access-Control-Allow-Origin: *`,
 * which is the *entire* reason this card exists as a card rather than as a
 * request the worker makes. A widget that needs a credential cannot ship one to
 * the browser bundle, and a widget that needs the agent needs a conversation —
 * neither of which is a thing you glance at.
 *
 * The place is remembered and the forecast is not. A temperature from last week
 * is worse than no temperature, so every mount refetches; the city, which is a
 * preference rather than an observation, is what gets written down.
 */
const GEO_URL = "https://geocoding-api.open-meteo.com/v1/search";
const FORECAST_URL = "https://api.open-meteo.com/v1/forecast";

type Place = { name: string; lat: number; lon: number };
type Forecast = {
  temp: number;
  feels: number;
  wind: number;
  high: number;
  low: number;
  code: number;
};
type GeoResult = Place & { country?: string; admin1?: string };
type Phase = "idle" | "loading" | "ready" | "error";

/** WMO 4677 codes, which is what the API returns, in one table. */
const SKY: Array<{ label: string; icon: IconName; match: (code: number) => boolean }> = [
  { label: "Clear", icon: "sun", match: (c) => c === 0 },
  { label: "Mostly clear", icon: "sun", match: (c) => c === 1 },
  { label: "Partly cloudy", icon: "cloud", match: (c) => c === 2 },
  { label: "Overcast", icon: "cloud", match: (c) => c === 3 },
  { label: "Fog", icon: "cloud", match: (c) => c === 45 || c === 48 },
  { label: "Drizzle", icon: "rain", match: (c) => c >= 51 && c <= 57 },
  { label: "Rain", icon: "rain", match: (c) => c >= 61 && c <= 67 },
  { label: "Snow", icon: "snow", match: (c) => (c >= 71 && c <= 77) || c === 85 || c === 86 },
  { label: "Showers", icon: "rain", match: (c) => c >= 80 && c <= 82 },
  { label: "Thunderstorm", icon: "storm", match: (c) => c >= 95 },
];

function describe(code: number): { label: string; icon: IconName } {
  const found = SKY.find((entry) => entry.match(code));
  // Unknown code: "Cloudy" is never wrong enough to be worth being right about.
  return found ? { label: found.label, icon: found.icon } : { label: "Cloudy", icon: "cloud" };
}

/**
 * The city the machine already believes it is in, from its own zone id.
 *
 * It returns nothing when the zone does not name a place. `UTC`, `GMT` and
 * `Etc/GMT+5` all split to something, and the last segment of each was being
 * saved as the user's city — a card headed "UTC" above a forecast, which is
 * the one name on screen that is certainly not where anyone lives. The search
 * box is the documented path for exactly this reason, so declining to guess
 * costs nothing but the lucky case.
 */
function cityFromTimezone(): string {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone ?? "";
  const segments = zone.split("/");
  if (segments.length < 2) return "";
  const city = (segments[segments.length - 1] ?? "").replace(/_/g, " ");
  // Two segments, but no city in the second: `Etc/GMT+5`.
  if (!/^[A-Za-z]/.test(city) || /[0-9+-]/.test(city)) return "";
  return city;
}

/** A number the API actually sent, rounded — or nothing at all. */
function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.round(value) : null;
}

export function WeatherWidget() {
  const [place, setPlace] = usePersistentState<Place | null>("lumine.widget.place", null);
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GeoResult[]>([]);
  const [searching, setSearching] = useState(false);
  /**
   * What was last searched, and whether asking failed.
   *
   * Without these two the search box looked broken rather than unsuccessful: a
   * 500 fell into the same `catch` as a typo and both rendered an unchanged
   * empty card, and a genuine zero-hit search rendered nothing either. The card
   * next door already distinguishes "nothing found" from "could not ask", so
   * this is the same contract rather than a new one.
   */
  const [searchedFor, setSearchedFor] = useState<string | null>(null);
  const [searchFailed, setSearchFailed] = useState(false);
  const [geoTried, setGeoTried] = useState(false);
  /**
   * Bumped by the retry button to re-run the fetch.
   *
   * Open-Meteo answers 503 under its own load, and a card whose error text says
   * "try again in a moment" with no control to do so is a card that is asking
   * the reader to close and reopen the app. The attempt is part of the effect's
   * identity rather than a flag the effect reads, so bumping it always starts a
   * clean request instead of depending on `place` having changed underneath.
   */
  const [attempt, setAttempt] = useState(0);

  // Best effort, and only once. `getCurrentPosition` needs a permission grant
  // the desktop shell never asks for, so this is expected to fail there — the
  // search box below is the real path, and this is the lucky one.
  useEffect(() => {
    if (place !== null || geoTried || !("geolocation" in navigator)) return;
    setGeoTried(true);
    let cancelled = false;
    navigator.geolocation.getCurrentPosition(
      (position) => {
        if (cancelled) return;
        const name = cityFromTimezone();
        if (!name) return;
        setPlace({ name, lat: position.coords.latitude, lon: position.coords.longitude });
      },
      () => undefined,
      { timeout: 8000, maximumAge: 600_000 },
    );
    return () => {
      cancelled = true;
    };
  }, [place, geoTried, setPlace]);

  useEffect(() => {
    if (place === null) {
      setForecast(null);
      setPhase("idle");
      return;
    }
    const controller = new AbortController();
    setPhase("loading");
    setForecast(null);

    (async () => {
      try {
        const url =
          `${FORECAST_URL}?latitude=${place.lat}&longitude=${place.lon}` +
          "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m" +
          "&daily=temperature_2m_max,temperature_2m_min&forecast_days=1&timezone=auto";
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json();
        /* Every figure is checked before it is stored, and a gap anywhere is
           treated as a failed request rather than as a missing row.
           `Math.round(undefined)` is `NaN`, and `NaN` never throws: it renders.
           A truncated payload used to draw "NaN°C" above "Feels NaN°" on a card
           whose phase said `ready`, which is worse than an outage because it
           looks like the service answering nonsense. Throwing lands in the
           catch, and the catch already has "Try again" and "Change place". */
        const temp = readNumber(data?.current?.temperature_2m);
        const feels = readNumber(data?.current?.apparent_temperature);
        const wind = readNumber(data?.current?.wind_speed_10m);
        const high = readNumber(data?.daily?.temperature_2m_max?.[0]);
        const low = readNumber(data?.daily?.temperature_2m_min?.[0]);
        const code = data?.current?.weather_code;
        if (
          temp === null ||
          feels === null ||
          wind === null ||
          high === null ||
          low === null ||
          typeof code !== "number" ||
          !Number.isFinite(code)
        ) {
          throw new Error("malformed forecast");
        }
        setForecast({ temp, feels, wind, high, low, code });
        setPhase("ready");
      } catch (error) {
        // An aborted fetch is this component changing its mind, not the network
        // failing, and reporting it as an outage would be a lie told loudly.
        if (controller.signal.aborted) return;
        setPhase("error");
      }
    })();

    return () => controller.abort();
  }, [place, attempt]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const term = query.trim();
    if (term.length < 2) return;
    setSearching(true);
    setSearchFailed(false);
    setSearchedFor(term);
    try {
      const response = await fetch(`${GEO_URL}?name=${encodeURIComponent(term)}&count=5&language=en&format=json`);
      // Checked here exactly as the forecast fetch checks it. Without this a
      // gateway error page reached `response.json()`, threw, and was reported
      // as "no matches" — the service being down and the city not existing
      // became the same sentence.
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      const found: GeoResult[] = (data.results ?? []).map(
        (row: { name: string; latitude: number; longitude: number; country?: string; admin1?: string }) => ({
          name: row.name,
          lat: row.latitude,
          lon: row.longitude,
          country: row.country,
          admin1: row.admin1,
        }),
      );
      setResults(found);
    } catch {
      setResults([]);
      setSearchFailed(true);
    } finally {
      setSearching(false);
    }
  };

  const sky = forecast ? describe(forecast.code) : null;
  const header = place ? `${place.name}` : undefined;

  if (place === null) {
    return (
      <WidgetCard title="Weather" tone="widget-weather">
        <div className="widget-empty flex flex-col gap-1">
          <p>Where are you?</p>
          <small>A city is enough. Saved here; sent to Open-Meteo only.</small>
        </div>
        <form className="widget-place flex gap-1.5" onSubmit={submit}>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search a city"
            aria-label="Search a city"
            spellCheck={false}
          />
          <button type="submit" className="widget-button primary" disabled={searching || query.trim().length < 2}>
            {searching ? "…" : "Find"}
          </button>
        </form>
        {results.length > 0 && (
          <ul className="widget-places flex flex-col gap-1 m-0 p-0 list-none">
            {results.map((result) => (
              <li key={`${result.name}-${result.lat}-${result.lon}`}>
                <button
                  type="button"
                  onClick={() => {
                    setPlace({ name: result.name, lat: result.lat, lon: result.lon });
                    setResults([]);
                    setQuery("");
                    setSearchedFor(null);
                    setSearchFailed(false);
                  }}
                >
                  <span>{result.name}</span>
                  <small>{[result.admin1, result.country].filter(Boolean).join(", ")}</small>
                </button>
              </li>
            ))}
          </ul>
        )}
        {/* Asked and got nothing back. The two cases say different things
            because they are different: one is a spelling, the other is the
            network, and "no matches" for the second sends the user off to
            retype a city they spelled correctly. */}
        {!searching && searchedFor !== null && results.length === 0 && (
          <p className="widget-empty-line m-0 text-foreground text-[14px] font-semibold tracking-[-0.02em]">
            {searchFailed ? "The place search did not answer. Try again." : `No city matches ${searchedFor}.`}
          </p>
        )}
      </WidgetCard>
    );
  }

  return (
    <WidgetCard
      title="Weather"
      tone="widget-weather"
      meta={header}
      glyph={sky ? <Icon name={sky.icon} size={16} weight="fill" /> : undefined}
    >
      {phase === "loading" && <WidgetPending label="Reading the sky…" />}

      {phase === "error" && (
        <div className="widget-empty flex flex-col gap-1">
          <p>Could not reach Open-Meteo</p>
          <small>
            The place is remembered. This is usually the service having a moment rather than your
            connection — try again, or pick another city.
          </small>
          <div className="widget-row flex items-center justify-between gap-2">
            <button
              type="button"
              className="widget-button primary"
              onClick={() => setAttempt((value) => value + 1)}
            >
              <Icon name="reset" size={14} />
              Try again
            </button>
            <button type="button" className="widget-button" onClick={() => setPlace(null)}>
              Change place
            </button>
          </div>
        </div>
      )}

      {phase === "ready" && forecast && sky && (
        <>
          <p className="widget-figure m-0 text-foreground text-[34px] font-semibold tabular-nums tracking-[-0.03em] leading-[1]">
            {forecast.temp}
            <span className="widget-unit">°C</span>
          </p>
          <p className="widget-caption">{sky.label}</p>
          <dl className="widget-stats grid grid-cols-[repeat(4,_minmax(0,_1fr))] gap-1.5 m-0">
            <div>
              <dt>Feels</dt>
              <dd>{forecast.feels}°</dd>
            </div>
            <div>
              <dt>High</dt>
              <dd>{forecast.high}°</dd>
            </div>
            <div>
              <dt>Low</dt>
              <dd>{forecast.low}°</dd>
            </div>
            <div>
              <dt>Wind</dt>
              <dd>{forecast.wind}</dd>
            </div>
          </dl>
          <div className="widget-row flex items-center justify-between gap-2">
            <button type="button" className="widget-button" onClick={() => setPlace(null)}>
              Change place
            </button>
            <a
              className="widget-link"
              href={`https://open-meteo.com/en/docs#latitude=${place.lat}&longitude=${place.lon}`}
              onClick={(event) => {
                event.preventDefault();
                openExternal(`https://open-meteo.com/en/docs#latitude=${place.lat}&longitude=${place.lon}`);
              }}
            >
              Forecast
            </a>
          </div>
        </>
      )}
    </WidgetCard>
  );
}
