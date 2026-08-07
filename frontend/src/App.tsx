// WaterEvents viewer — TWO pages only, both list views: Events + Media. Stripped from the old ir-data-viewer to the
// simplest possible shell (no login gate, no telemetry, no company rail). Reuses the glass sidebar + table styling
// from styles.css. {USER 2026-07-23 "clear it to only keep two pages, one is the event one is the media, both list
// view, simplest as possible ... use this [existing design] ... follow our current json and table design"}.
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import TodayView from "./views/TodayView";
import EventsView from "./views/EventsView";
// Today · Media replaces the old flat MediaView in the nav: it is strictly more useful (same runs, plus the stage-2
// status strip and the artifact buttons). MediaView itself stays importable but is no longer routed.
// {USER 2026-08-05 "let's keep two pages one is today events and one is today media"}
// [CONFIDENCE: CONFIRMED 100% — direct user directive.]
import MediaTodayView from "./views/MediaTodayView";
import IrUrlsView from "./views/IrUrlsView";
import ApiDocsView from "./views/ApiDocsView";
// Playground is a TEST surface, not a data view: it drives the render VM and the LLM by hand so a human can see
// exactly what each one returns for a given input. It sits last in the nav because it is a tool, not a report.
// {USER 2026-08-07 "create a new sidebar for testing purpose ... so i can test myself with prompts"}
import PlaygroundView from "./views/PlaygroundView";

// Minimal stroke icons (currentColor) matching the old glass nav.
const svg = (inner: ReactNode): ReactNode => (
  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
       strokeWidth="1.35" strokeLinecap="round" strokeLinejoin="round">{inner}</svg>
);
const ICONS = {
  today: svg(<><circle cx="8" cy="8" r="3.1" /><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3.2 3.2l1.1 1.1M11.7 11.7l1.1 1.1M12.8 3.2l-1.1 1.1M4.3 11.7l-1.1 1.1" /></>),
  events: svg(<><rect x="2" y="3" width="12" height="11" rx="1.5" /><path d="M2 6.5h12M5 1.5v2.5M11 1.5v2.5" /></>),
  media: svg(<><rect x="2" y="3" width="12" height="10" rx="1.5" /><path d="M6.5 6l4 2.5-4 2.5V6z" /></>),
  // link/chain glyph — this page is about the ENTRY URLS we crawl per company, so a link is the honest icon.
  irurls: svg(<><path d="M6.5 9.5a2.8 2.8 0 004 0l2.2-2.2a2.8 2.8 0 00-4-4l-.9.9" /><path d="M9.5 6.5a2.8 2.8 0 00-4 0L3.3 8.7a2.8 2.8 0 004 4l.9-.9" /></>),
  // angle-brackets + slash — the page documents an HTTP surface for other programs, not another data view.
  apidocs: svg(<><path d="M5.2 4.5L2 8l3.2 3.5M10.8 4.5L14 8l-3.2 3.5M9.3 3l-2.6 10" /></>),
  // beaker — the page is where you try something and watch what comes out, which is what the other five are not.
  playground: svg(<><path d="M6.2 1.8v4.1L2.6 12a1.4 1.4 0 001.2 2.2h8.4A1.4 1.4 0 0013.4 12L9.8 5.9V1.8" /><path d="M5.2 1.8h5.6M4.6 9.6h6.8" /></>),
};

// Real routes so each page is deep-linkable + back/forward works (a minimal history-API router, no react-router).
type View = "today" | "events" | "media" | "irurls" | "apidocs" | "playground";
const ROUTES: Record<View, string> = { today: "/today", events: "/events", media: "/media", irurls: "/ir-urls", apidocs: "/api-docs", playground: "/playground" };
function pathToView(p: string): View {
  const s = p.replace(/\/+$/, "");
  if (s === "/events") return "events";
  if (s === "/media") return "media";
  if (s === "/ir-urls") return "irurls";
  if (s === "/api-docs") return "apidocs";
  if (s === "/playground") return "playground";
  return "today";                                  // "/" and "/today" → Today is the default landing page
}

function NavItem({ label, icon, active, href, onClick }:
  { label: string; icon: ReactNode; active: boolean; href: string; onClick: () => void }) {
  return (
    <a className={`nav-item${active ? " active" : ""}`} href={href}
       onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return; e.preventDefault(); onClick(); }}>
      <span className="nav-ico">{icon}</span>
      <span className="nav-label">{label}</span>
    </a>
  );
}

export default function App() {
  const [view, setViewState] = useState<View>(() => pathToView(window.location.pathname));
  const setView = (v: View) => {
    setViewState(v);
    if (window.location.pathname !== ROUTES[v]) window.history.pushState({ view: v }, "", ROUTES[v]);
  };
  useEffect(() => {
    const onPop = () => setViewState(pathToView(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="os-chip">
          <span className="os-dot" />
          <span className="os-name">waterevents</span>
          <span className="os-ver">live</span>
        </div>
        <nav className="nav">
          <NavItem label="Today · Events" icon={ICONS.today} active={view === "today"} href={ROUTES.today} onClick={() => setView("today")} />
          <NavItem label="Events" icon={ICONS.events} active={view === "events"} href={ROUTES.events} onClick={() => setView("events")} />
          <NavItem label="Today · Media" icon={ICONS.media} active={view === "media"} href={ROUTES.media} onClick={() => setView("media")} />
          <NavItem label="IR_URLS" icon={ICONS.irurls} active={view === "irurls"} href={ROUTES.irurls} onClick={() => setView("irurls")} />
          <NavItem label="API" icon={ICONS.apidocs} active={view === "apidocs"} href={ROUTES.apidocs} onClick={() => setView("apidocs")} />
          <NavItem label="Playground" icon={ICONS.playground} active={view === "playground"} href={ROUTES.playground} onClick={() => setView("playground")} />
        </nav>
      </aside>
      <main className="stage">
        {view === "today" ? <TodayView />
          : view === "media" ? <MediaTodayView />
          : view === "irurls" ? <IrUrlsView />
          : view === "apidocs" ? <ApiDocsView />
          : view === "playground" ? <PlaygroundView />
          : <EventsView />}
      </main>
    </div>
  );
}
